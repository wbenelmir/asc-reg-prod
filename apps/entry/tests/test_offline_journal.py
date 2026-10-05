"""The durable invalidation journal behind critical deltas (Phase 4 Prompt 2,
independent re-review correction C). PostgreSQL.

These tests mutate authoritative tables WITHOUT the application services --
`QuerySet.update()`, `bulk_create()`, backdated timestamps, raw TRUNCATE --
and prove the next delta still carries the change or fails closed. The
single-transaction cases rely on the building transaction's own journal
rows being ordered by id; commit ORDER across real concurrent transactions
is proven in `tests/concurrency/test_offline_concurrency.py`.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.entry.models import (
    OfflineChangeJournal,
    OfflinePackage,
    RestrictionCategory,
    RestrictionSeverity,
    SecurityRestriction,
)
from apps.entry.offline_contract import REBUILD_BLOCKING_REASONS, TYP_DELTA_MANIFEST
from apps.entry.services.offline_journal import (
    TRACKED_TABLES,
    journal_changes,
    prune_journal,
    tables_by_trigger,
)
from apps.entry.services.offline_packages import current_package, issue_delta
from apps.entry.tests import offline_factories

pytestmark = pytest.mark.django_db

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _delta_body(offline_device, package):
    body, nonce, signature = offline_device.signed("delta", {"package_id": package.public_id})
    _row, raw = issue_delta(
        device=offline_device.device,
        package_public_id=package.public_id,
        nonce=nonce,
        signature=signature,
        body=body,
    )
    return offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")[1]


def test_every_tracked_table_carries_both_journal_triggers():
    migration = importlib.import_module(
        "apps.entry.migrations.0004_offline_build_lifecycle_and_journal"
    )
    assert migration._TRACKED_TABLES == TRACKED_TABLES
    installed = tables_by_trigger()
    for table in TRACKED_TABLES:
        assert installed.get(table) == {
            "entry_offline_journal_row",
            "entry_offline_journal_truncate",
        }, table


def test_a_package_records_its_journal_watermark(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    assert package.data_snapshot and ":" in package.data_snapshot
    assert package.journal_xmin is not None and package.build_txid is not None
    assert package.journal_high_id is not None
    assert journal_changes(package).row_count == 0  # nothing happened since


def test_a_queryset_update_without_updated_at_reaches_the_delta(
    offline_device, active_pass, registration
):
    """`QuerySet.update()` bypasses `save()` and `updated_at`."""
    from apps.accreditation.models import AccessProfileAssignment

    package, _ = offline_factories.download(offline_device)
    stamp = AccessProfileAssignment.objects.get(registration=registration).updated_at
    AccessProfileAssignment.objects.filter(registration=registration).update(
        effective_until=timezone.now() + timedelta(minutes=1)
    )
    assert AccessProfileAssignment.objects.get(registration=registration).updated_at == stamp
    body = _delta_body(offline_device, package)
    assert {"jti": active_pass.jti, "reason": "ASSIGNMENT_CHANGED"} in body["access_withdrawn"]


def test_a_backdated_bulk_insert_reaches_the_delta(
    offline_device, active_pass, registration, event, staff
):
    """A row whose timestamps pre-date the package cutoff: an `updated_at`
    filter would miss it; the journal does not."""
    package, _ = offline_factories.download(offline_device)
    past = package.data_cutoff_at - timedelta(hours=1)
    (restriction,) = SecurityRestriction.objects.bulk_create(
        [
            SecurityRestriction(
                person=registration.person,
                event_edition=event,
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                starts_at=past,
                reason_encrypted="synthetic bulk restriction",
                created_by=staff,
            )
        ]
    )
    SecurityRestriction.objects.filter(pk=restriction.pk).update(created_at=past, updated_at=past)
    body = _delta_body(offline_device, package)
    assert body["restrictions"][0]["jti"] == active_pass.jti
    assert body["restrictions"][0]["restrictions"][0]["severity"] == "DENY_ENTRY"


def test_a_raw_sql_pass_change_withdraws_the_pass(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE badges_digital_entry_pass SET valid_until = valid_until - interval '1 minute' "
            "WHERE id = %s",
            [active_pass.pk],
        )
    body = _delta_body(offline_device, package)
    assert {"jti": active_pass.jti, "reason": "PASS_CHANGED"} in body["access_withdrawn"]


def test_a_truncate_fails_closed_with_a_blocking_rebuild(offline_device, active_pass):
    package, _ = offline_factories.download(offline_device)
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE entry_override_reason CASCADE")
    changes = journal_changes(package)
    assert "UNTRACKED_CHANGE" in changes.rebuild_reasons
    body = _delta_body(offline_device, package)
    assert body["rebuild_required"] is True
    assert set(body["rebuild_reasons"]) & REBUILD_BLOCKING_REASONS


def test_more_changes_than_one_delta_may_carry_fail_closed(offline_device, active_pass, settings):
    from apps.badges.models import DigitalEntryPass

    package, _ = offline_factories.download(offline_device)
    settings.ENTRY_OFFLINE_DELTA_MAX_CHANGES = 3
    for _ in range(4):
        DigitalEntryPass.objects.filter(pk=active_pass.pk).update(status="ACTIVE")
    body = _delta_body(offline_device, package)
    assert body["rebuild_reasons"] == ["BULK_CHANGE"]
    assert "BULK_CHANGE" in REBUILD_BLOCKING_REASONS


def test_a_non_revocation_key_change_requires_a_rebuild(offline_device, active_pass, key):
    from apps.badges.models import VerificationKey

    package, _ = offline_factories.download(offline_device)
    VerificationKey.objects.filter(pk=key.pk).update(not_after=timezone.now() + timedelta(days=1))
    body = _delta_body(offline_device, package)
    assert "KEY_SET_CHANGED" in body["rebuild_reasons"]


def test_a_package_without_a_watermark_is_never_current_and_fails_closed(
    offline_device, active_pass
):
    package, _ = offline_factories.download(offline_device)
    legacy = OfflinePackage(
        **{
            field.attname: getattr(package, field.attname)
            for field in OfflinePackage._meta.concrete_fields
        }
    )
    legacy.data_snapshot = ""
    assert journal_changes(legacy).rebuild_reasons == {"SNAPSHOT_MISSING"}
    assert "SNAPSHOT_MISSING" in REBUILD_BLOCKING_REASONS
    assert current_package(offline_device.device).pk == package.pk  # watermarked only


def test_journal_rows_hold_internal_ids_only(offline_device, active_pass, registration, staff):
    from apps.entry.services.restrictions import create_restriction

    offline_factories.download(offline_device)
    create_restriction(
        actor=factories_security(registration),
        person=registration.person,
        event_edition=registration.event_edition,
        severity=RestrictionSeverity.MANUAL_REVIEW,
        category=RestrictionCategory.SECURITY_CONCERN,
        reason="SENTINEL-JOURNAL-REASON",
    )
    rows = list(OfflineChangeJournal.objects.all())
    assert rows
    text = json.dumps([row.refs for row in rows])
    assert "SENTINEL-JOURNAL-REASON" not in text
    assert registration.person.display_name not in text
    for row in rows:
        for values in row.refs.values():
            for value in values:
                assert _UUID.match(value), value


def factories_security(registration):
    from apps.entry.tests import factories

    return factories.make_user(
        "journal.security@example.test",
        group_name="Security Restriction Managers",
        event=registration.event_edition,
    )


def test_pruning_keeps_what_a_live_package_still_needs(offline_device, active_pass, settings):
    from apps.badges.models import DigitalEntryPass

    package, _ = offline_factories.download(offline_device)
    DigitalEntryPass.objects.filter(pk=active_pass.pk).update(status="ACTIVE")
    uncovered_before = journal_changes(package).row_count
    assert uncovered_before >= 1
    far_future = timezone.now() + timedelta(
        seconds=settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS + 60
    )
    prune_journal(now=far_future)
    assert journal_changes(package).row_count == uncovered_before
