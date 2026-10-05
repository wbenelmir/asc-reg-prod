"""Prompt 3 migration round trip and refusal to erase durable sync evidence."""

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.entry.models import SyncOperation
from apps.entry.services import offline_sync
from apps.entry.tests.test_offline_migration import (
    BUILDS,
    CURRENT,
    _constraint_exists,
    _table_exists,
    _trigger_exists,
)
from apps.entry.tests.test_offline_sync import _admit

pytestmark = pytest.mark.django_db(transaction=True)


def test_0005_forward_reverse_forward_without_sync_evidence():
    assert _table_exists("entry_sync_operation")
    assert _trigger_exists("entry_sync_operation_guard")
    try:
        call_command("migrate", "entry", BUILDS, verbosity=0)
        assert not _table_exists("entry_sync_operation")
        assert _constraint_exists("entry_event_online_only")
    finally:
        call_command("migrate", "entry", CURRENT, verbosity=0)
    assert _constraint_exists("entry_event_offline_coherent")
    assert not _constraint_exists("entry_event_online_only")
    assert _trigger_exists("entry_reconciliation_link_prevent_update_delete")


def test_0005_refuses_reversal_even_for_pending_evidence(offline_world):
    op = _admit(offline_world)
    intake = offline_sync._intake(
        offline_world.device,
        offline_world.store.store_id,
        offline_world.store.envelope(op),
        quarantined=False,
        now=timezone.now(),
    )
    assert SyncOperation.objects.get(pk=intake.row_id).status == "PENDING"
    try:
        with pytest.raises(RuntimeError, match="cannot be reversed"):
            call_command("migrate", "entry", BUILDS, verbosity=0)
        assert SyncOperation.objects.get(pk=intake.row_id).payload_hash
        assert _trigger_exists("entry_sync_operation_guard")
    finally:
        call_command("migrate", "entry", CURRENT, verbosity=0)
