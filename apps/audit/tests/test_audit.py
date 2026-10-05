"""Append-only `AuditEvent` tests: PostgreSQL trigger (ADR-0008) and persistent recorder."""

from __future__ import annotations

import uuid

import pytest
from django.db import connection, transaction
from django.db.utils import ProgrammingError
from django.utils import timezone

from apps.audit.contracts import AuditRecord
from apps.audit.models import AuditEvent
from apps.audit.services import PersistentAuditRecorder

pytestmark = pytest.mark.django_db


def _make_event(**overrides) -> AuditEvent:
    defaults = dict(
        occurred_at=timezone.now(),
        actor_type="SYSTEM",
        action_code="TEST_ACTION",
        result="SUCCESS",
        correlation_id="corr-1",
    )
    defaults.update(overrides)
    return AuditEvent.objects.create(**defaults)


def test_trigger_blocks_update() -> None:
    event = _make_event()
    with pytest.raises(ProgrammingError, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(reason_code="tampered")


def test_trigger_blocks_delete() -> None:
    event = _make_event()
    with pytest.raises(ProgrammingError, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()
    # Row must still exist -- the DELETE was rejected, not silently no-op'd.
    assert AuditEvent.objects.filter(pk=event.pk).exists()


def test_trigger_is_present_and_enabled_in_the_catalog() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgenabled FROM pg_trigger WHERE tgrelid = 'audit_event'::regclass "
            "AND tgname = 'audit_event_prevent_update_delete' AND NOT tgisinternal"
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] != "D"  # not disabled


def test_persistent_recorder_creates_a_row() -> None:
    recorder = PersistentAuditRecorder()
    recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            action_code="SIGN_IN_SUCCESS",
            target_type="OperationalUser",
            result="SUCCESS",
            correlation_id="corr-2",
        )
    )
    assert AuditEvent.objects.filter(action_code="SIGN_IN_SUCCESS").exists()


def test_persistent_recorder_redacts_sensitive_summary_values() -> None:
    recorder = PersistentAuditRecorder()
    recorder.record(
        AuditRecord(
            actor_type="SYSTEM",
            action_code="OTP_ISSUED",
            target_type="AuthenticationChallenge",
            result="SUCCESS",
            before_summary=None,
            after_summary={
                "channel": "EMAIL",
                "otp_value": str(246810),
                "nested": {"passport_number": "P" + str(1234567)},
            },
        )
    )
    event = AuditEvent.objects.get(action_code="OTP_ISSUED")
    assert event.after_summary == {
        "channel": "EMAIL",
        "otp_value": "***REDACTED***",
        "nested": {"passport_number": "***REDACTED***"},
    }


def test_audit_event_rollback_leaves_no_row() -> None:
    marker = str(uuid.uuid4())
    try:
        with transaction.atomic():
            _make_event(correlation_id=marker)
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    assert not AuditEvent.objects.filter(correlation_id=marker).exists()
