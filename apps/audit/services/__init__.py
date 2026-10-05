"""Persistent, append-only `AuditRecorder` (Schema §15, ADR-0008).

Satisfies the same `AuditRecorder` protocol as the Prompt 2
`InMemoryAuditRecorder`. `record()` MUST be called from inside the same
database transaction as the action it documents (Schema §16.5). No method
here (or anywhere else in this app) ever updates or deletes an
`AuditEvent` row -- corrections are new, linked events. The PostgreSQL
`BEFORE UPDATE OR DELETE` trigger installed by `audit.0001` is defence in
depth against an application mistake, not this class's only line of
defence.
"""

from __future__ import annotations

from django.utils import timezone

from apps.audit.contracts import AuditRecord
from apps.audit.models import AuditEvent
from apps.core.redaction import sanitize_persistent_payload


class PersistentAuditRecorder:
    """Inserts one `audit.AuditEvent` row per `record()` call, in the caller's transaction."""

    def record(self, entry: AuditRecord) -> None:
        AuditEvent.objects.create(
            occurred_at=entry.occurred_at or timezone.now(),
            actor_type=entry.actor_type,
            actor_user_id=entry.actor_user_id,
            actor_person_id=entry.actor_person_id,
            action_code=entry.action_code,
            target_type=entry.target_type,
            target_uuid=entry.target_uuid,
            event_edition_id=entry.event_edition_id,
            result=entry.result,
            reason_code=entry.reason_code or "",
            before_summary=(
                sanitize_persistent_payload(entry.before_summary)
                if entry.before_summary is not None
                else None
            ),
            after_summary=(
                sanitize_persistent_payload(entry.after_summary)
                if entry.after_summary is not None
                else None
            ),
            correlation_id=entry.correlation_id,
            network_fingerprint=entry.network_fingerprint,
        )
