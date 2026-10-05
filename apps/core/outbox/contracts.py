"""Outbox protocol -- transaction-scoped enqueue seam.

TRD `BE-003`: complex side effects MUST use an outbox or
`transaction.on_commit` pattern so messages are not sent for rolled-back
data. Schema §16.5: `OutboxEvent` is inserted with the domain transaction
and consumed asynchronously.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class OutboxMessage:
    """One immutable outbox entry.

    `event_uuid` is the global idempotency key a Prompt 3+ persistent
    implementation and its Celery consumer both key off; a caller MUST
    generate a fresh UUID per logical event, not per delivery attempt, so
    retries remain idempotent (`BE-006`).
    """

    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any]
    event_uuid: uuid.UUID = field(default_factory=uuid.uuid7)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class OutboxPublisher(Protocol):
    """Transaction-scoped enqueue seam.

    Callers MUST invoke `enqueue()` from inside the same database
    transaction that performs the state change the message describes.
    The Prompt 3 persistent implementation inserts a `core.OutboxEvent` row
    in that same transaction, so a rollback removes both the domain change
    and the outbox row together; a separate `transaction.on_commit` hook
    then schedules the actual Celery dispatch, which never fires for
    rolled-back data.
    """

    def enqueue(self, message: OutboxMessage) -> None: ...
