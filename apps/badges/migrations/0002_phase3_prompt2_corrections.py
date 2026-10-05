"""Phase 3 Prompt 2 correction pass: immutable signed material and command binding.

Three groups of change:

1. `DigitalEntryPass` gains the two remaining signed claims as immutable
   snapshot columns (`bai`, `pid`) plus the persisted ES256 signature. Before
   this, display re-signed the credential on every page render and read those
   two claims back from mutable rows, so a later assignment change silently
   altered what an "issued" credential produced.
2. `PassLifecycleOperation` gains a command fingerprint and target identity,
   so an `operation_id` binds to one exact command instead of to whatever
   command claimed it first.
3. `VerificationKey` gains canonical DER, the basis for fingerprinting and
   key comparison.

The data step backfills what is genuinely derivable. It deliberately does NOT
fabricate a signature: any credential issued before this migration has none,
and re-signing would require the private key inside a migration. Those rows
are left with an empty `signature_hex`, and `reconstruct_pass_token` fails
closed for them -- they must be replaced through the normal replacement
command rather than silently resurrected.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_snapshot_and_der(apps_registry, schema_editor):
    """Fill the derivable columns for rows that predate this migration."""
    import base64

    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )

    DigitalEntryPass = apps_registry.get_model("badges", "DigitalEntryPass")
    ParticipantEventPseudonym = apps_registry.get_model("badges", "ParticipantEventPseudonym")
    VerificationKey = apps_registry.get_model("badges", "VerificationKey")

    for credential in DigitalEntryPass.objects.select_related(
        "badge_assignment", "registration"
    ).iterator():
        updates = []
        if not credential.badge_assignment_public_reference:
            reference = getattr(credential.badge_assignment, "public_reference", "")
            if reference:
                credential.badge_assignment_public_reference = reference
                updates.append("badge_assignment_public_reference")
        if not credential.participant_event_pseudonym:
            pseudonym = ParticipantEventPseudonym.objects.filter(
                person_id=credential.registration.person_id,
                event_edition_id=credential.registration.event_edition_id,
            ).first()
            if pseudonym is not None:
                credential.participant_event_pseudonym = pseudonym.pseudonym
                updates.append("participant_event_pseudonym")
        if updates:
            credential.save(update_fields=updates)

    for key in VerificationKey.objects.filter(public_key_der_b64="").iterator():
        try:
            public_key = load_pem_public_key(key.public_key_pem.encode("utf-8"))
            der = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        except Exception:  # noqa: BLE001, S112 - a malformed legacy key is left as-is,
            # deliberately: this migration must not fail the whole deployment over one
            # unparseable historical row, and the key service refuses such a key at
            # every point where it would actually be used.
            continue
        key.public_key_der_b64 = base64.b64encode(der).decode("ascii")
        key.save(update_fields=["public_key_der_b64"])


def clear_backfilled_columns(apps_registry, schema_editor):
    """Reverse: blank the derived columns so the fields can be dropped."""
    DigitalEntryPass = apps_registry.get_model("badges", "DigitalEntryPass")
    VerificationKey = apps_registry.get_model("badges", "VerificationKey")
    DigitalEntryPass.objects.update(
        badge_assignment_public_reference="", participant_event_pseudonym=""
    )
    VerificationKey.objects.update(public_key_der_b64="")


class Migration(migrations.Migration):
    dependencies = [
        ("badges", "0001_phase3_prompt2"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="digitalentrypass",
            name="badge_assignment_public_reference",
            field=models.CharField(blank=True, default="", max_length=22),
        ),
        migrations.AddField(
            model_name="digitalentrypass",
            name="participant_event_pseudonym",
            field=models.CharField(blank=True, default="", max_length=22),
        ),
        migrations.AddField(
            model_name="digitalentrypass",
            name="signature_hex",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="passlifecycleoperation",
            name="command_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="passlifecycleoperation",
            name="target_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="passlifecycleoperation",
            name="target_type",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="passlifecycleoperation",
            name="verification_key",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="badges.verificationkey",
            ),
        ),
        migrations.AddField(
            model_name="verificationkey",
            name="public_key_der_b64",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="passlifecycleoperation",
            name="operation_type",
            field=models.CharField(
                choices=[
                    ("GENERATE", "Generate"),
                    ("ACTIVATE", "Activate"),
                    ("SUSPEND", "Suspend"),
                    ("RESUME", "Resume"),
                    ("REVOKE", "Revoke"),
                    ("REPLACE", "Replace"),
                    ("EXPIRE", "Expire"),
                    ("KEY_PUBLISH", "Publish verification key"),
                    ("KEY_PROMOTE", "Promote verification key"),
                    ("KEY_RETIRE", "Retire verification key"),
                    ("KEY_REVOKE", "Revoke verification key"),
                ],
                max_length=24,
            ),
        ),
        migrations.AddIndex(
            model_name="passlifecycleoperation",
            index=models.Index(fields=["target_type", "target_id"], name="bdg_pass_op_target_idx"),
        ),
        migrations.RunPython(backfill_snapshot_and_der, clear_backfilled_columns),
    ]
