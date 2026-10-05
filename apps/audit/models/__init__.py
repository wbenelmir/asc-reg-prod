"""Append-only `AuditEvent` (Schema §15.1, ADR-0002 bigint exception, ADR-0008).

`device_id` (Schema §15.1) is omitted: `EntryDevice` (Schema §11.1) is
Phase 2+ scope (the Phase 1 scope excluded the `entry` app).

Application services expose only an append operation -- no service,
selector, or admin path updates or deletes a row here; corrections create
new, linked events. `audit.0001` additionally installs a `BEFORE UPDATE OR
DELETE` PostgreSQL trigger as defence in depth (ADR-0008); the owning local
role can still disable/drop it, which is why real database-role
isolation is verified separately and post-migration
(`scripts/check.py audit-isolation`), never claimed here.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class AuditActorType(models.TextChoices):
    PARTICIPANT = "PARTICIPANT", "Participant"
    OPERATIONAL_USER = "OPERATIONAL_USER", "Operational user"
    SYSTEM = "SYSTEM", "System"
    DEVICE = "DEVICE", "Device"


class AuditResult(models.TextChoices):
    SUCCESS = "SUCCESS", "Success"
    FAILURE = "FAILURE", "Failure"
    DENIED = "DENIED", "Denied"


class AuditEvent(models.Model):
    """Append-only audit trail entry (Schema §15.1).

    Uses the Schema-sanctioned exception to the UUID-primary-key rule
    (ADR-0002): a `bigint` physical primary key for partition-friendly
    index locality, plus a separate global `event_uuid`.

    Content here MUST NOT include OTP values, passwords, tokens, private
    keys, document bytes, or full sensitive identifiers -- only redacted,
    bounded summaries (Schema §15.1).
    """

    id = models.BigAutoField(primary_key=True)
    event_uuid = models.UUIDField(default=uuid.uuid7, editable=False, unique=True)
    occurred_at = models.DateTimeField()
    actor_type = models.CharField(max_length=20, choices=AuditActorType.choices)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    actor_person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    action_code = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100, blank=True, default="")
    target_uuid = models.UUIDField(null=True, blank=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    result = models.CharField(max_length=16, choices=AuditResult.choices)
    reason_code = models.CharField(max_length=100, blank=True, default="")
    before_summary = models.JSONField(null=True, blank=True)
    after_summary = models.JSONField(null=True, blank=True)
    correlation_id = models.CharField(max_length=64)
    network_fingerprint = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "audit_event"
        indexes = [
            models.Index(fields=["event_edition", "occurred_at"], name="aud_event_edition_idx"),
            models.Index(
                fields=["target_type", "target_uuid", "occurred_at"], name="aud_event_target_idx"
            ),
            models.Index(fields=["actor_user", "occurred_at"], name="aud_event_actor_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.action_code}:{self.event_uuid}"
