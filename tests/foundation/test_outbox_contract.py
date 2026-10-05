"""Non-persistent outbox contract (Prompt 2; accepted plan §6.2)."""

from __future__ import annotations

from apps.core.outbox import InMemoryOutboxPublisher, OutboxMessage


def test_enqueue_records_the_message() -> None:
    publisher = InMemoryOutboxPublisher()
    message = OutboxMessage(
        event_type="registration.submitted",
        aggregate_type="Registration",
        aggregate_id="00000000-0000-0000-0000-000000000000",
        payload={"note": "synthetic"},
    )

    publisher.enqueue(message)

    assert publisher.messages == [message]


def test_each_message_gets_a_distinct_event_uuid_by_default() -> None:
    first = OutboxMessage(event_type="a", aggregate_type="X", aggregate_id="1", payload={})
    second = OutboxMessage(event_type="a", aggregate_type="X", aggregate_id="1", payload={})
    assert first.event_uuid != second.event_uuid
    assert first.event_uuid.version == 7
    assert second.event_uuid.version == 7


def test_clear_empties_the_publisher() -> None:
    publisher = InMemoryOutboxPublisher()
    publisher.enqueue(
        OutboxMessage(event_type="a", aggregate_type="X", aggregate_id="1", payload={})
    )
    publisher.clear()
    assert publisher.messages == []
