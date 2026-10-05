"""Phase 4 Prompt 2, independent re-review corrections B and C (ADR-0023).

Additive only:

* `entry_offline_package` gains the change-journal watermark columns
  (`data_snapshot`, `journal_xmin`, `build_txid`, `journal_high_id`); they
  join the immutable content columns of `entry_offline_package_guard()`;
* `entry_offline_package_build`: the idempotent, concurrency-safe build
  lifecycle (one open build per device, reserved version, lease); never
  deleted, terminal statuses final (`entry_offline_build_guard()`);
* `entry_offline_change_journal`: the durable invalidation journal, written
  ONLY by the `entry_offline_journal_row()` / `entry_offline_journal_truncate()`
  triggers installed below on every table that can change a package or a
  delta. Row triggers see every INSERT/UPDATE/DELETE, including
  `QuerySet.update()`, bulk operations and raw SQL; the statement trigger
  records a TRUNCATE (which bypasses row triggers) so the next delta fails
  closed with a full rebuild.

Reversal: safe while no build exists and no package carries a watermark.
Once they exist, reversing is deliberately unavailable (the reverse would
drop build evidence and the journal a live package depends on). Journal
rows alone do not block reversal: they are derived data.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models

# Keep in step with `apps.entry.services.offline_journal.TRACKED_TABLES`
# (a test pins that both lists and the installed triggers agree).
_TRACKED_TABLES = {
    "badges_digital_entry_pass": ("registration_id",),
    "badges_verification_key": (),
    "entry_security_restriction": ("registration_id", "person_id"),
    "accreditation_badge_type_assignment": ("registration_id",),
    "accreditation_access_profile_assignment": ("registration_id",),
    "accreditation_access_rule_assignment": ("registration_id", "access_rule_id"),
    "accreditation_access_rule": ("access_profile_id",),
    "accreditation_access_profile": (),
    "registrations_registration": ("person_id",),
    "events_venue": (),
    "events_gate": ("venue_id",),
    "events_zone": ("venue_id",),
    "entry_override_reason": (),
}

_JOURNAL_FUNCTIONS = """
CREATE FUNCTION entry_offline_journal_row()
RETURNS trigger AS $$
DECLARE
    old_row jsonb;
    new_row jsonb;
    cur jsonb;
    refs jsonb := '{}'::jsonb;
    col text;
    event_id uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        old_row := to_jsonb(OLD);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        new_row := to_jsonb(NEW);
    END IF;
    cur := COALESCE(new_row, old_row);
    IF TG_NARGS > 0 THEN
        FOREACH col IN ARRAY TG_ARGV LOOP
            refs := refs || jsonb_build_object(col, (
                SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb)
                FROM (VALUES (old_row ->> col), (new_row ->> col)) AS t(v)
                WHERE v IS NOT NULL
            ));
        END LOOP;
    END IF;
    IF old_row IS NOT NULL AND new_row IS NOT NULL
       AND (old_row ->> 'event_edition_id') IS DISTINCT FROM (new_row ->> 'event_edition_id')
    THEN
        event_id := NULL;  -- moved between events: applies to every event
    ELSE
        event_id := NULLIF(cur ->> 'event_edition_id', '')::uuid;
    END IF;
    INSERT INTO entry_offline_change_journal
        (txid, table_name, operation, row_id, event_edition_id, refs, recorded_at)
    VALUES (
        pg_current_xact_id()::text::bigint, TG_TABLE_NAME, left(TG_OP, 1),
        COALESCE(cur ->> 'id', ''), event_id, refs, clock_timestamp()
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION entry_offline_journal_truncate()
RETURNS trigger AS $$
BEGIN
    INSERT INTO entry_offline_change_journal
        (txid, table_name, operation, row_id, event_edition_id, refs, recorded_at)
    VALUES (
        pg_current_xact_id()::text::bigint, TG_TABLE_NAME, 'T',
        '', NULL, '{}'::jsonb, clock_timestamp()
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def _journal_triggers_sql() -> str:
    statements = []
    for table, columns in _TRACKED_TABLES.items():
        args = ", ".join(f"'{column}'" for column in columns)
        statements.append(
            f"CREATE TRIGGER entry_offline_journal_row AFTER INSERT OR UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION entry_offline_journal_row({args});"
        )
        statements.append(
            f"CREATE TRIGGER entry_offline_journal_truncate AFTER TRUNCATE "
            f"ON {table} FOR EACH STATEMENT EXECUTE FUNCTION entry_offline_journal_truncate();"
        )
    return "\n".join(statements)


def _drop_journal_triggers_sql() -> str:
    statements = []
    for table in _TRACKED_TABLES:
        statements.append(f"DROP TRIGGER IF EXISTS entry_offline_journal_row ON {table};")
        statements.append(f"DROP TRIGGER IF EXISTS entry_offline_journal_truncate ON {table};")
    statements.append("DROP FUNCTION IF EXISTS entry_offline_journal_row();")
    statements.append("DROP FUNCTION IF EXISTS entry_offline_journal_truncate();")
    return "\n".join(statements)


_PACKAGE_GUARD_COMMON = """
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'entry_offline_package rows are never deleted';
    END IF;
    IF NEW.public_id IS DISTINCT FROM OLD.public_id
       OR NEW.device_id IS DISTINCT FROM OLD.device_id
       OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
       OR NEW.event_edition_id IS DISTINCT FROM OLD.event_edition_id
       OR NEW.package_version IS DISTINCT FROM OLD.package_version
       OR NEW.scope_version IS DISTINCT FROM OLD.scope_version
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.sensitivity IS DISTINCT FROM OLD.sensitivity
       OR NEW.signing_key_id IS DISTINCT FROM OLD.signing_key_id
       OR NEW.recipient_key_id IS DISTINCT FROM OLD.recipient_key_id
       OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
       OR NEW.data_cutoff_at IS DISTINCT FROM OLD.data_cutoff_at
       OR NEW.aging_at IS DISTINCT FROM OLD.aging_at
       OR NEW.stale_at IS DISTINCT FROM OLD.stale_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
       OR NEW.ciphertext_sha256 IS DISTINCT FROM OLD.ciphertext_sha256
       OR NEW.response_sha256 IS DISTINCT FROM OLD.response_sha256
       OR NEW.response_bytes IS DISTINCT FROM OLD.response_bytes
       OR NEW.entry_count IS DISTINCT FROM OLD.entry_count
       OR NEW.stored_object_key IS DISTINCT FROM OLD.stored_object_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       {extra}
       OR (OLD.stored_object_deleted_at IS NOT NULL
           AND NEW.stored_object_deleted_at IS DISTINCT FROM
               OLD.stored_object_deleted_at)
       OR (OLD.status <> 'READY' AND NEW.status IS DISTINCT FROM OLD.status)
    THEN
        RAISE EXCEPTION 'entry_offline_package content is immutable';
    END IF;
    RETURN NEW;
"""

_WATERMARK_COLUMNS = """
       OR NEW.data_snapshot IS DISTINCT FROM OLD.data_snapshot
       OR NEW.journal_xmin IS DISTINCT FROM OLD.journal_xmin
       OR NEW.build_txid IS DISTINCT FROM OLD.build_txid
       OR NEW.journal_high_id IS DISTINCT FROM OLD.journal_high_id"""


def _package_guard_sql(extra: str) -> str:
    return (
        "CREATE OR REPLACE FUNCTION entry_offline_package_guard()\n"
        "RETURNS trigger AS $$\nBEGIN"
        + _PACKAGE_GUARD_COMMON.replace("{extra}", extra)
        + "END;\n$$ LANGUAGE plpgsql;"
    )


_BUILD_GUARD = """
CREATE FUNCTION entry_offline_build_guard()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'entry_offline_package_build rows are never deleted';
    END IF;
    IF NEW.device_id IS DISTINCT FROM OLD.device_id
       OR NEW.event_edition_id IS DISTINCT FROM OLD.event_edition_id
       OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
       OR NEW.recipient_key_id IS DISTINCT FROM OLD.recipient_key_id
       OR NEW.package_version IS DISTINCT FROM OLD.package_version
       OR NEW.request_reason IS DISTINCT FROM OLD.request_reason
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
       OR (OLD.status IN ('READY', 'FAILED', 'CANCELLED')
           AND (NEW.status IS DISTINCT FROM OLD.status
                OR NEW.package_id IS DISTINCT FROM OLD.package_id))
    THEN
        RAISE EXCEPTION 'entry_offline_package_build identity and terminal status are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entry_offline_build_guard
BEFORE UPDATE OR DELETE ON entry_offline_package_build
FOR EACH ROW EXECUTE FUNCTION entry_offline_build_guard();
"""


def _refuse_reversal_once_builds_exist(apps, schema_editor):
    """Reverse guard (runs FIRST when this migration is unapplied)."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM entry_offline_package_build) "
            "OR EXISTS (SELECT 1 FROM entry_offline_package WHERE data_snapshot <> '')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError(
                "entry.0004 cannot be reversed: package builds or journal-watermarked "
                "packages exist (reversal is deliberately unavailable once offline "
                "records exist)."
            )


class Migration(migrations.Migration):
    dependencies = [
        ("entry", "0003_offline_preparation"),
        ("events", "0004_venue_zone_gate"),
        ("badges", "0006_badgeissuance_registration_required"),
        ("accreditation", "0005_access_checkpoint_rules"),
        ("registrations", "0007_registration_reg_public_ref_upper_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="offlinepackage",
            name="build_txid",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="offlinepackage",
            name="data_snapshot",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="offlinepackage",
            name="journal_high_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="offlinepackage",
            name="journal_xmin",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="OfflineChangeJournal",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("txid", models.BigIntegerField()),
                ("table_name", models.CharField(max_length=63)),
                ("operation", models.CharField(max_length=1)),
                ("row_id", models.CharField(blank=True, default="", max_length=64)),
                ("event_edition_id", models.UUIDField(blank=True, null=True)),
                ("refs", models.JSONField(default=dict)),
                ("recorded_at", models.DateTimeField()),
            ],
            options={
                "db_table": "entry_offline_change_journal",
                "indexes": [
                    models.Index(fields=["txid"], name="entry_ojournal_txid_idx"),
                    models.Index(fields=["recorded_at"], name="entry_ojournal_recorded_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="OfflinePackageBuild",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid7, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("package_version", models.BigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("BUILDING", "Building"),
                            ("READY", "Ready"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("request_reason", models.CharField(max_length=32)),
                ("status_reason_code", models.CharField(blank=True, default="", max_length=32)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("pending_object_key", models.CharField(blank=True, default="", max_length=64)),
                ("requested_at", models.DateTimeField()),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="entry.entrydevice",
                    ),
                ),
                (
                    "event_edition",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="events.eventedition",
                    ),
                ),
                (
                    "package",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="build",
                        to="entry.offlinepackage",
                    ),
                ),
                (
                    "recipient_key",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="entry.entrydevicekey",
                    ),
                ),
                (
                    "scope",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="entry.devicescope",
                    ),
                ),
            ],
            options={
                "db_table": "entry_offline_package_build",
                "indexes": [
                    models.Index(fields=["status", "lease_until"], name="entry_obuild_status_idx")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status__in", ("PENDING", "BUILDING"))),
                        fields=("device",),
                        name="entry_obuild_one_open_per_device",
                    ),
                    models.UniqueConstraint(
                        fields=("device", "package_version"),
                        name="entry_obuild_device_version_uq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("package_version__gte", 1)),
                        name="entry_obuild_version_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("status", "READY"), _negated=True),
                            ("package__isnull", False),
                            _connector="OR",
                        ),
                        name="entry_obuild_ready_has_package",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            sql=_JOURNAL_FUNCTIONS + _journal_triggers_sql(),
            reverse_sql=_drop_journal_triggers_sql(),
        ),
        migrations.RunSQL(
            sql=_package_guard_sql(_WATERMARK_COLUMNS),
            reverse_sql=_package_guard_sql(""),
        ),
        migrations.RunSQL(
            sql=_BUILD_GUARD,
            reverse_sql="""
                DROP TRIGGER IF EXISTS entry_offline_build_guard ON entry_offline_package_build;
                DROP FUNCTION IF EXISTS entry_offline_build_guard();
            """,
        ),
        # Last operation, so its reverse runs first on unapply.
        migrations.RunPython(
            migrations.RunPython.noop, reverse_code=_refuse_reversal_once_builds_exist
        ),
    ]
