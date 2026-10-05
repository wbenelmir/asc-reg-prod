"""Audit contract shared by in-memory tests and the persistent Prompt 3 recorder."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class AuditRecord:
    """One audit entry, shaped after Schema §15.1 `AuditEvent`.

    `before_summary`/`after_summary` are for REDACTED, bounded field
    summaries only -- never OTP values, passwords, tokens, private keys,
    document bytes, or full sensitive identifiers (Schema §15.1).

    Fields added in Prompt 3 (`event_edition_id`, `actor_user_id`, ...) are
    all optional so the Prompt 2 minimal-field call sites keep working
    unchanged.
    """

    actor_type: str
    action_code: str
    target_type: str
    result: str
    reason_code: str | None = None
    before_summary: dict[str, Any] | None = None
    after_summary: dict[str, Any] | None = None
    actor_user_id: uuid.UUID | None = None
    actor_person_id: uuid.UUID | None = None
    target_uuid: uuid.UUID | None = None
    event_edition_id: uuid.UUID | None = None
    correlation_id: str = ""
    network_fingerprint: str = ""
    occurred_at: datetime | None = None


class AuditRecorder(Protocol):
    """Append-only audit recording seam.

    Callers MUST invoke `record()` from inside the same database
    transaction as the action it documents (Schema §16.5). The Prompt 3
    persistent implementation additionally installs a database trigger
    rejecting ordinary `UPDATE`/`DELETE` on `audit.AuditEvent`
    (accepted plan §5.6) -- application services never expose an update or
    delete path in the first place; corrections create new, linked events
    (`DATA-005`).
    """

    def record(self, entry: AuditRecord) -> None: ...


class InMemoryAuditRecorder:
    """In-memory `AuditRecorder` -- Prompt 2 foundation unit tests only."""

    def __init__(self) -> None:
        self._entries: list[AuditRecord] = []

    def record(self, entry: AuditRecord) -> None:
        self._entries.append(entry)

    @property
    def entries(self) -> list[AuditRecord]:
        return list(self._entries)
