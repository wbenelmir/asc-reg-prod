"""Migrations `entry.0003_offline_preparation` and
`entry.0004_offline_build_lifecycle_and_journal`: forward, reverse and
forward again on PostgreSQL while no offline record exists, and a
deliberately refused reversal once one does (Phase 4 Prompt 2 and its independent
re-review corrections B and C).

Transactional: these tests really unapply and re-apply the migration on the
test database, and always leave it fully migrated.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

PREVIOUS = "0002_verificationsample"
PREPARATION = "0003_offline_preparation"
BUILDS = "0004_offline_build_lifecycle_and_journal"
CURRENT = "0005_offline_sync_and_reconciliation"


def _table_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{name}"])
        return cursor.fetchone()[0]


def _constraint_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = %s)", [name])
        return cursor.fetchone()[0]


def _trigger_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = %s)", [name])
        return cursor.fetchone()[0]


def test_forward_reverse_forward_without_offline_records():
    assert _table_exists("entry_offline_package")
    assert _trigger_exists("entry_offline_package_guard")
    assert not _constraint_exists("entry_scope_online_only")
    try:
        call_command("migrate", "entry", PREVIOUS, verbosity=0)
        assert not _table_exists("entry_offline_package")
        assert not _table_exists("entry_device_key")
        assert not _trigger_exists("entry_offline_package_guard")
        assert _constraint_exists("entry_scope_online_only")  # Phase 3 boundary restored
        assert _constraint_exists("entry_event_online_only")
        assert _constraint_exists("entry_device_enrolled_has_credential")
        call_command("migrate", "entry", BUILDS, verbosity=0)
        # Prompt 2 retains its online-only boundary before Prompt 3 applies.
        assert _constraint_exists("entry_event_online_only")
    finally:
        call_command("migrate", "entry", CURRENT, verbosity=0)
    assert _table_exists("entry_offline_package")
    assert _trigger_exists("entry_device_key_guard")
    assert _constraint_exists("entry_event_offline_coherent")


def test_reversal_is_refused_once_an_offline_record_exists(django_user_model):
    from django.utils import timezone

    from apps.entry.models import OfflineEventSetting
    from apps.entry.tests import factories

    event = factories.make_event("MIGGUARD")
    actor = factories.make_user("migration.actor@example.test")
    OfflineEventSetting.objects.create(
        event_edition=event, enabled=False, changed_by=actor, changed_at=timezone.now()
    )
    try:
        with pytest.raises(RuntimeError, match="deliberately unavailable"):
            call_command("migrate", "entry", PREVIOUS, verbosity=0)
        assert _table_exists("entry_offline_event_setting")  # nothing was dropped
    finally:
        call_command("migrate", "entry", CURRENT, verbosity=0)


def _function_source(name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT prosrc FROM pg_proc WHERE proname = %s", [name])
        row = cursor.fetchone()
    return row[0] if row else ""


def test_0004_forward_reverse_forward_without_builds():
    from apps.entry.services.offline_journal import TRACKED_TABLES, tables_by_trigger

    assert _table_exists("entry_offline_package_build")
    assert _table_exists("entry_offline_change_journal")
    assert "data_snapshot" in _function_source("entry_offline_package_guard")
    assert set(tables_by_trigger()) >= set(TRACKED_TABLES)
    try:
        call_command("migrate", "entry", PREPARATION, verbosity=0)
        assert not _table_exists("entry_offline_package_build")
        assert not _table_exists("entry_offline_change_journal")
        assert not _trigger_exists("entry_offline_build_guard")
        assert tables_by_trigger() == {}
        # The 0003 guard body is restored, without the watermark columns.
        guard = _function_source("entry_offline_package_guard")
        assert guard and "data_snapshot" not in guard
    finally:
        call_command("migrate", "entry", CURRENT, verbosity=0)
    assert _table_exists("entry_offline_package_build")
    assert _trigger_exists("entry_offline_build_guard")
    assert set(tables_by_trigger()) >= set(TRACKED_TABLES)


def test_0004_reversal_is_refused_once_a_build_exists(settings, tmp_path, monkeypatch):
    from apps.core.crypto.package_signing import (
        InMemoryPackageSigningKeyProvider,
        set_package_signing_key_provider_for_testing,
    )
    from apps.entry.services import offline_packages
    from apps.entry.tests import factories, offline_factories

    settings.ENTRY_OFFLINE_ENABLED = True
    settings.PRIVATE_STORAGE_ROOT = tmp_path
    set_package_signing_key_provider_for_testing(
        InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")
    )
    monkeypatch.setattr(offline_packages, "_dispatch_build", lambda build_id: None)
    try:
        factories.seed_reference_values()
        event = factories.make_event("MIGBUILD")
        layout = factories.VenueLayout(event)
        admin = factories.make_user(
            "migration.builds@example.test", group_name="Entry Device Administrators", event=event
        )
        device, _secret = factories.enroll_device(event=event, layout=layout, admin=admin)
        offline_factories.enable_event(event, admin)
        offline_factories.make_offline_capable(device, admin=admin, layout=layout)
        offline_factories.provision(offline_factories.SyntheticDevice(device=device), admin=admin)
        offline_packages.request_package_build(device, reason="MIGRATION_TEST")
        try:
            with pytest.raises(RuntimeError, match="deliberately unavailable"):
                call_command("migrate", "entry", PREPARATION, verbosity=0)
            assert _table_exists("entry_offline_package_build")  # nothing was dropped
        finally:
            call_command("migrate", "entry", CURRENT, verbosity=0)
    finally:
        set_package_signing_key_provider_for_testing(None)
