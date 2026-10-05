"""Non-persistent audit contract (Prompt 2; accepted plan §5.6)."""

from __future__ import annotations

from apps.audit.contracts import AuditRecord, InMemoryAuditRecorder


def test_record_is_appended() -> None:
    recorder = InMemoryAuditRecorder()
    entry = AuditRecord(
        actor_type="operational_user",
        action_code="SIGN_IN_SUCCESS",
        target_type="OperationalUser",
        result="success",
    )

    recorder.record(entry)

    assert recorder.entries == [entry]


def test_entries_is_a_defensive_copy() -> None:
    recorder = InMemoryAuditRecorder()
    recorder.record(AuditRecord(actor_type="a", action_code="b", target_type="c", result="success"))
    snapshot = recorder.entries
    snapshot.clear()
    assert len(recorder.entries) == 1
