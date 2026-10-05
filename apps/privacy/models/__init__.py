"""Legal documents, required acceptance, and optional consent (Schema §14).

`AcceptanceRecord` (required Terms acceptance / Privacy Notice
acknowledgement) and `ConsentRecord` (optional, purpose-specific,
append-only) are deliberately separate tables -- withdrawing or changing
optional consent must never rewrite historical required acceptance
evidence (Schema §14.2/§14.3). Neither exposes an update path at the
service layer; both are append-only by construction (no `updated_at`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel


class LegalDocumentType(models.TextChoices):
    PRIVACY_NOTICE = "PRIVACY_NOTICE", "Privacy notice"
    TERMS = "TERMS", "Terms"
    OTHER = "OTHER", "Other"


class LegalDocument(UUIDPrimaryKeyModel, TimestampedModel):
    code = models.CharField(max_length=64, unique=True)
    document_type = models.CharField(max_length=24, choices=LegalDocumentType.choices)

    class Meta:
        db_table = "privacy_legal_document"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class LegalDocumentVersionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    RETIRED = "RETIRED", "Retired"


class LegalDocumentVersion(UUIDPrimaryKeyModel, TimestampedModel):
    """One version of a `LegalDocument` (Schema §14.1). Immutable once PUBLISHED
    (enforced at the service layer -- no update path once published)."""

    legal_document = models.ForeignKey(
        LegalDocument, on_delete=models.CASCADE, related_name="versions"
    )
    language = models.CharField(max_length=8)
    version_label = models.CharField(max_length=32)
    content = models.TextField(blank=True, default="")
    published_url = models.CharField(max_length=500, blank=True, default="")
    content_hash = models.CharField(max_length=64)
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=LegalDocumentVersionStatus.choices,
        default=LegalDocumentVersionStatus.DRAFT,
    )

    class Meta:
        db_table = "privacy_legal_document_version"
        constraints = [
            models.UniqueConstraint(
                fields=["legal_document", "language", "version_label"],
                name="prv_docversion_doc_lang_label_uq",
            ),
        ]


class AcceptanceAction(models.TextChoices):
    ACCEPTED = "ACCEPTED", "Accepted"
    ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"


class AcceptanceSource(models.TextChoices):
    PUBLIC_WEB = "PUBLIC_WEB", "Public web"
    ASSISTED_REGISTRATION = "ASSISTED_REGISTRATION", "Assisted registration"
    MIGRATION = "MIGRATION", "Migration"


class AcceptanceRecord(UUIDPrimaryKeyModel):
    """Required Terms acceptance / Privacy Notice acknowledgement (Schema §14.2).

    Append-only evidence: no `updated_at`, no exposed update path.
    """

    person = models.ForeignKey(
        "people.Person", on_delete=models.PROTECT, related_name="acceptances"
    )
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="acceptances"
    )
    legal_document_version = models.ForeignKey(
        LegalDocumentVersion, on_delete=models.PROTECT, related_name="acceptances"
    )
    action = models.CharField(max_length=16, choices=AcceptanceAction.choices)
    accepted_at = models.DateTimeField()
    language = models.CharField(max_length=8)
    source = models.CharField(max_length=24, choices=AcceptanceSource.choices)
    session_reference = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "privacy_acceptance_record"
        indexes = [
            models.Index(
                fields=["person", "legal_document_version"], name="prv_acceptance_person_idx"
            ),
            models.Index(fields=["registration"], name="prv_acceptance_reg_idx"),
        ]


class ConsentPurpose(UUIDPrimaryKeyModel, TimestampedModel):
    """Stable optional-consent purpose (Schema §14.3)."""

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "privacy_consent_purpose"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class ConsentAction(models.TextChoices):
    GRANTED = "GRANTED", "Granted"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class ConsentRecord(UUIDPrimaryKeyModel):
    """Append-only optional-consent record (Schema §14.3).

    Current consent is DERIVED by a selector reading the latest row per
    `(person, purpose)` -- never a separately stored "current" flag that
    could drift from the append-only history.
    """

    person = models.ForeignKey("people.Person", on_delete=models.PROTECT, related_name="consents")
    purpose = models.ForeignKey(ConsentPurpose, on_delete=models.PROTECT, related_name="records")
    action = models.CharField(max_length=16, choices=ConsentAction.choices)
    source = models.CharField(max_length=24, choices=AcceptanceSource.choices)
    occurred_at = models.DateTimeField()
    legal_document_version = models.ForeignKey(
        LegalDocumentVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "privacy_consent_record"
        indexes = [
            models.Index(
                fields=["person", "purpose", "occurred_at"], name="prv_consent_person_purpose_idx"
            ),
        ]


# ---------------------------------------------------------------------------
# Phase 2 Prompt 5: privacy-request lifecycle, retention categories, and
# Legal Hold foundations (§4.6). No destructive retention job exists;
# `RetentionCategory.retention_period_days` may remain unset (externally
# governed / pending policy approval) -- this module never invents a final
# retention duration.
# ---------------------------------------------------------------------------


class PrivacyRequestType(models.TextChoices):
    ACCESS = "ACCESS", "Access"
    RECTIFICATION = "RECTIFICATION", "Rectification"
    DELETION = "DELETION", "Deletion"
    OBJECTION = "OBJECTION", "Objection"


class PrivacyRequestStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETED = "COMPLETED", "Completed"
    REJECTED = "REJECTED", "Rejected"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class PrivacyRequest(UUIDPrimaryKeyModel, TimestampedModel):
    """One privacy request lifecycle record (§4.6). Status transitions are
    enforced by `apps.privacy.services.transition_privacy_request_status`,
    never by a direct `.save()` -- see its allowed-transition table."""

    person = models.ForeignKey(
        "people.Person", on_delete=models.PROTECT, related_name="privacy_requests"
    )
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="privacy_requests",
    )
    request_type = models.CharField(max_length=16, choices=PrivacyRequestType.choices)
    status = models.CharField(
        max_length=16, choices=PrivacyRequestStatus.choices, default=PrivacyRequestStatus.RECEIVED
    )
    scope_description = models.CharField(max_length=1000)
    reason = models.CharField(max_length=1000, blank=True, default="")
    received_at = models.DateTimeField()
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolution_note = models.CharField(max_length=1000, blank=True, default="")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "privacy_request"
        indexes = [
            models.Index(fields=["person", "status"], name="prv_request_person_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"privacy-request:{self.pk}:{self.request_type}:{self.status}"


class RetentionCategory(UUIDPrimaryKeyModel, TimestampedModel):
    """A named data-retention category (§4.6). `retention_period_days` may
    remain `NULL` -- "externally governed" or "pending policy approval" --
    never a fabricated default duration."""

    code = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=500)
    retention_period_days = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "privacy_retention_category"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


class LegalHold(UUIDPrimaryKeyModel, TimestampedModel):
    """A Legal Hold on one Registration (§4.6). While active
    (`released_at IS NULL`), the Registration must never be treated as
    eligible for routine deletion by any future retention job -- enforced
    by `apps.privacy.selectors.is_under_legal_hold`, which any such job
    MUST consult first."""

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="legal_holds"
    )
    reason = models.CharField(max_length=1000)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    placed_at = models.DateTimeField()
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "privacy_legal_hold"
        constraints = [
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(released_at__isnull=True),
                name="prv_legal_hold_one_active_per_registration_uq",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"legal-hold:{self.pk}:{self.registration_id}"
