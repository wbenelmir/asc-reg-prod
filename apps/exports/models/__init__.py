"""Purpose-bound controlled exports (Phase 2 Prompt 5 §4.5).

One `ExportRequest` row per generated export: the exact scope, purpose,
reason, and minimized field schema used to build it, its private storage
key, its expiry, and every retrieval. There is no update path that widens
scope or fields after creation -- an export whose parameters must change is
a NEW `ExportRequest`, never a mutated one (append-only evidence, mirroring
`apps.accreditation.BulkAssignmentOperation`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel


class ExportDatasetKind(models.TextChoices):
    REGISTRATIONS_SUMMARY = "REGISTRATIONS_SUMMARY", "Registrations summary"


class ExportStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    READY = "READY", "Ready"
    EXPIRED = "EXPIRED", "Expired"


# Controlled purpose vocabulary (never an arbitrary caller-supplied string,
# mirroring `apps.reviews`'s `ALLOWED_REQUEST_FIELD_CODES` pattern). Adding a
# new purpose is a one-line change here, reviewed like any other allowlist
# change -- never inferred from free text.
ALLOWED_EXPORT_PURPOSE_CODES: frozenset[str] = frozenset(
    {
        "OPERATIONAL_REPORTING",
        "EVENT_LOGISTICS_PLANNING",
        "ORGANIZATION_DELEGATION_OVERSIGHT",
    }
)

# The MINIMIZED, stable field schema actually written to the generated file
# for each dataset kind (Phase 2 Prompt 5 §4.5 "minimized fields", "stable
# export schema"). Deliberately excludes every identity document, encrypted
# identity field, contact destination, and internal review note -- adding a
# field here is a reviewed, explicit decision, never an accidental default.
EXPORT_DATASET_FIELD_SCHEMAS: dict[str, tuple[str, ...]] = {
    ExportDatasetKind.REGISTRATIONS_SUMMARY: (
        "public_reference",
        "public_status",
        "event_name",
        "organization_name",
        "preferred_language",
        "submitted_at",
    ),
}

EXPORT_SCHEMA_VERSION = "v1"


class ExportRequest(UUIDPrimaryKeyModel, TimestampedModel):
    dataset_kind = models.CharField(max_length=32, choices=ExportDatasetKind.choices)
    schema_version = models.CharField(max_length=16, default=EXPORT_SCHEMA_VERSION)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="export_requests"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="export_requests",
    )
    purpose_code = models.CharField(max_length=64)
    reason = models.CharField(max_length=500)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="export_requests"
    )
    status = models.CharField(
        max_length=16, choices=ExportStatus.choices, default=ExportStatus.PENDING
    )
    idempotency_key = models.CharField(max_length=200, unique=True)
    target_registration_ids = models.JSONField(default=list, blank=True)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    storage_key = models.CharField(max_length=200, blank=True, default="")
    content_sha256 = models.CharField(max_length=64, blank=True, default="")
    expires_at = models.DateTimeField()
    ready_at = models.DateTimeField(null=True, blank=True)
    retrieved_count = models.PositiveIntegerField(default=0)
    last_retrieved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "exports_export_request"
        indexes = [
            models.Index(fields=["event_edition", "status"], name="exp_request_event_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"export:{self.pk}:{self.dataset_kind}:{self.status}"
