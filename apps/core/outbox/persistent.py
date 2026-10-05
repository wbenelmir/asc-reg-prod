"""Persistent, transaction-scoped `OutboxPublisher` (Schema §16.5, Prompt 3).

Satisfies the same `OutboxPublisher` protocol as the Prompt 2
`InMemoryOutboxPublisher` -- callers do not change. `enqueue()` inserts a
`core.OutboxEvent` row using the CURRENT database connection, so it MUST be
called from inside the same transaction as the domain change it describes:
a rollback removes both together. Actual publication/dispatch is a
separate, later concern (a `transaction.on_commit` hook or worker pass),
deliberately not implemented by this class -- Phase 1 has no real
Redis/Celery broker integration (ADR-0009).
"""

from __future__ import annotations

from apps.core.models import OutboxEvent
from apps.core.redaction import sanitize_persistent_payload

from .contracts import OutboxMessage


class PersistentOutboxPublisher:
    """Inserts one `core.OutboxEvent` row per `enqueue()` call, in the caller's transaction."""

    def enqueue(self, message: OutboxMessage) -> None:
        OutboxEvent.objects.create(
            id=message.event_uuid,
            event_type=message.event_type,
            aggregate_type=message.aggregate_type,
            aggregate_id=message.aggregate_id,
            payload=sanitize_persistent_payload(message.payload),
            occurred_at=message.occurred_at,
        )
