"""Persistent Outbox transaction-boundary tests (Schema §16.5).

These are LOCAL Django transaction tests only -- no Redis, no Celery
broker, no worker process is exercised (ADR-0009). Labeled accurately:
this proves the outbox row commits/rolls back with its domain transaction,
not that a message was ever actually dispatched anywhere.
"""

from __future__ import annotations

import pytest
from django.db import transaction

from apps.core.models import OutboxEvent, OutboxEventStatus
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher

pytestmark = pytest.mark.django_db


def test_enqueue_persists_a_pending_row() -> None:
    publisher = PersistentOutboxPublisher()
    message = OutboxMessage(
        event_type="test.event",
        aggregate_type="TestAggregate",
        aggregate_id="agg-1",
        payload={"key": "value"},
    )
    publisher.enqueue(message)
    row = OutboxEvent.objects.get(pk=message.event_uuid)
    assert row.status == OutboxEventStatus.PENDING
    assert row.payload == {"key": "value"}
    assert row.id.version == 7


def test_enqueue_redacts_sensitive_payload_values() -> None:
    publisher = PersistentOutboxPublisher()
    message = OutboxMessage(
        event_type="test.sensitive",
        aggregate_type="TestAggregate",
        aggregate_id="agg-sensitive",
        payload={
            "status": "accepted",
            "otp": str(246810),
            "nested": {"nin": str(123456789012345)},
        },
    )
    publisher.enqueue(message)
    row = OutboxEvent.objects.get(pk=message.event_uuid)
    assert row.payload == {
        "status": "accepted",
        "otp": "***REDACTED***",
        "nested": {"nin": "***REDACTED***"},
    }


def test_rollback_removes_both_domain_change_and_outbox_row() -> None:
    from django.utils import timezone

    from apps.events.models import EventEdition

    publisher = PersistentOutboxPublisher()
    message = OutboxMessage(
        event_type="test.rollback",
        aggregate_type="EventEdition",
        aggregate_id="will-not-exist",
        payload={},
    )
    try:
        with transaction.atomic():
            EventEdition.objects.create(
                code="ROLLBACK-TEST",
                name="Rollback Test",
                timezone="UTC",
                starts_at=timezone.now(),
                ends_at=timezone.now(),
            )
            publisher.enqueue(message)
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    assert not EventEdition.objects.filter(code="ROLLBACK-TEST").exists()
    assert not OutboxEvent.objects.filter(pk=message.event_uuid).exists()


def test_commit_persists_both_domain_change_and_outbox_row() -> None:
    from django.utils import timezone

    from apps.events.models import EventEdition

    publisher = PersistentOutboxPublisher()
    message = OutboxMessage(
        event_type="test.commit",
        aggregate_type="EventEdition",
        aggregate_id="will-exist",
        payload={},
    )
    with transaction.atomic():
        EventEdition.objects.create(
            code="COMMIT-TEST",
            name="Commit Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
        )
        publisher.enqueue(message)

    assert EventEdition.objects.filter(code="COMMIT-TEST").exists()
    assert OutboxEvent.objects.filter(pk=message.event_uuid).exists()


def test_two_enqueues_for_the_same_logical_event_have_different_uuids_by_default() -> None:
    publisher = PersistentOutboxPublisher()
    first = OutboxMessage(event_type="e", aggregate_type="A", aggregate_id="1", payload={})
    second = OutboxMessage(event_type="e", aggregate_type="A", aggregate_id="1", payload={})
    publisher.enqueue(first)
    publisher.enqueue(second)
    assert first.event_uuid != second.event_uuid
    assert OutboxEvent.objects.filter(aggregate_id="1").count() == 2
