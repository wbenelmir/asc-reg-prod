"""Invitation campaigns, reusable links, delegation import, and on-behalf claims.

New app introduced by Phase 2 Prompt 2 (TRD §13.1 names `invitations` as a
distinct later-phase application; ADR-0001 defers its creation until the
phase that actually needs it).

Campaign/link separation (Phase 2 Prompt 2 approved domain correction):
`InvitationCampaign` is the STABLE identity that organizations, statistics,
and every created `Registration` attach to; `InvitationLink` is a rotatable
credential belonging to exactly one campaign. Rotating a link creates a new
`InvitationLink` row for the SAME campaign and disables the old one -- it
never creates a new campaign, and every `Registration`/`InvitationUse`
created through any of a campaign's links (past or present) stays attached
to that one stable campaign. See docs/architecture/ADR-0016 for the
independent token-hashing design.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.fields import BlindIndexField, EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel


class InvitationCampaignStatus(models.TextChoices):
    """Labels are translated (matches the Phase 1 pattern for every other
    stored-enum choices class); the stored enum VALUE is never translated."""

    DRAFT = "DRAFT", _("Draft")
    ACTIVE = "ACTIVE", _("Active")
    SUSPENDED = "SUSPENDED", _("Suspended")
    EXPIRED = "EXPIRED", _("Expired")
    CLOSED = "CLOSED", _("Closed")


class InvitationFallbackMode(models.TextChoices):
    DENY = "DENY", _("Deny")
    OFFER_OPEN_REGISTRATION = "OFFER_OPEN_REGISTRATION", _("Offer open registration")


class InvitationCampaign(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """Stable campaign identity (Schema §6.3). Never stores the active
    reusable token directly -- see `InvitationLink` (approved domain
    correction, Phase 2 Prompt 2)."""

    public_reference = models.CharField(max_length=32, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="invitation_campaigns"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="invitation_campaigns",
    )
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16,
        choices=InvitationCampaignStatus.choices,
        default=InvitationCampaignStatus.DRAFT,
    )
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # NULL means unlimited capacity -- never a magic sentinel integer
    # (Schema §6.3 "optional submission capacity").
    capacity = models.PositiveIntegerField(null=True, blank=True)
    language = models.CharField(max_length=8, default="en")
    fallback_mode = models.CharField(
        max_length=32,
        choices=InvitationFallbackMode.choices,
        default=InvitationFallbackMode.OFFER_OPEN_REGISTRATION,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "invitations_campaign"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(capacity__isnull=True) | models.Q(capacity__gte=0),
                name="inv_campaign_capacity_nonneg",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "organization"], name="inv_campaign_event_org_idx"
            ),
            models.Index(fields=["status"], name="inv_campaign_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.public_reference


class InvitationLinkStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    ROTATED = "ROTATED", _("Rotated")
    REVOKED = "REVOKED", _("Revoked")
    EXPIRED = "EXPIRED", _("Expired")


class InvitationLink(UUIDPrimaryKeyModel, TimestampedModel):
    """Rotatable credential belonging to exactly one campaign (approved domain
    correction, Phase 2 Prompt 2). Only the SHA-256 hash of a high-entropy
    random token is ever persisted -- see ADR-0016. The raw token is
    returned to the caller only at creation/rotation time and is never
    stored anywhere, in any form, at rest.
    """

    campaign = models.ForeignKey(InvitationCampaign, on_delete=models.CASCADE, related_name="links")
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16, choices=InvitationLinkStatus.choices, default=InvitationLinkStatus.ACTIVE
    )
    rotated_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rotated_to"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "invitations_link"
        constraints = [
            # One current ACTIVE link per campaign (Schema §6.3 "reusable
            # controlled invitation link"). Rotation transactionally flips
            # the old row to ROTATED and creates a new ACTIVE row -- it
            # never leaves two ACTIVE rows for the same campaign even
            # transiently outside that one transaction.
            models.UniqueConstraint(
                fields=["campaign"],
                condition=models.Q(status=InvitationLinkStatus.ACTIVE),
                name="inv_link_one_active_per_campaign_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="inv_link_campaign_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw token
        return f"link:{self.pk}:{self.status}"


class InvitationUseKind(models.TextChoices):
    DRAFT_CREATED = "DRAFT_CREATED", _("Draft created")
    SUBMITTED = "SUBMITTED", _("Submitted")


class InvitationUse(UUIDPrimaryKeyModel):
    """Records meaningful invitation use without tracking browsing data
    (Schema §6.4). Append-only -- no `updated_at`, no exposed update path.

    Preserves provenance on the exact `Registration` and the exact
    `InvitationLink` used, without ever storing the raw token (Phase 2
    Prompt 2 requirement).
    """

    campaign = models.ForeignKey(InvitationCampaign, on_delete=models.PROTECT, related_name="uses")
    link = models.ForeignKey(InvitationLink, on_delete=models.PROTECT, related_name="uses")
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="invitation_uses"
    )
    use_kind = models.CharField(max_length=16, choices=InvitationUseKind.choices)
    occurred_at = models.DateTimeField()
    # Idempotency key (Schema §6.4 `operation_id`) -- a caller retrying the
    # same logical operation (e.g. the same submission attempt) supplies
    # the same value, and the unique constraint below makes a second
    # attempt a safe no-op rather than a duplicate row / double capacity
    # consumption.
    operation_id = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "invitations_use"
        constraints = [
            # At most one row per (registration, use_kind): a SUBMITTED use
            # can be created at most once per Registration, which is what
            # makes campaign-capacity counting safe against retries
            # (Phase 2 Prompt 2 capacity requirement).
            models.UniqueConstraint(
                fields=["registration", "use_kind"], name="inv_use_reg_kind_uq"
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "occurred_at"], name="inv_use_campaign_idx"),
            models.Index(fields=["campaign", "use_kind"], name="inv_use_campaign_kind_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.use_kind}:{self.registration_id}"


class OnBehalfClaimStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    CLAIMED = "CLAIMED", _("Claimed")
    EXPIRED = "EXPIRED", _("Expired")
    REVOKED = "REVOKED", _("Revoked")


class OnBehalfClaim(UUIDPrimaryKeyModel, TimestampedModel):
    """Secure single-use claim for an authorized on-behalf `Registration`
    (Phase 2 Prompt 2, AF-ORG-03/04).

    Only the SHA-256 hash of a high-entropy random claim token is
    persisted -- mirroring `InvitationLink.token_hash` (ADR-0016). The
    intended participant email is never stored as plain text: it is held
    only as an `EncryptedTextField` (for the eventual notification) plus a
    versioned blind index (for deterministic claim matching), the same
    ADR-0006 pattern `people.ContactPoint` uses.
    """

    registration = models.OneToOneField(
        "registrations.Registration", on_delete=models.CASCADE, related_name="on_behalf_claim"
    )
    claim_token_hash = models.CharField(max_length=64, unique=True)
    intended_email_encrypted = EncryptedTextField()
    intended_email_hash = BlindIndexField()
    intended_email_hash_key_version = models.SmallIntegerField()
    status = models.CharField(
        max_length=16, choices=OnBehalfClaimStatus.choices, default=OnBehalfClaimStatus.PENDING
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    expires_at = models.DateTimeField()
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "invitations_on_behalf_claim"
        indexes = [
            models.Index(fields=["intended_email_hash"], name="inv_claim_email_hash_idx"),
            models.Index(fields=["status", "expires_at"], name="inv_claim_status_expiry_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw token/email
        return f"claim:{self.registration_id}:{self.status}"


class DelegationBatchStatus(models.TextChoices):
    UPLOADED = "UPLOADED", _("Uploaded")
    VALIDATED = "VALIDATED", _("Validated")
    APPLYING = "APPLYING", _("Applying")
    APPLIED = "APPLIED", _("Applied")
    FAILED = "FAILED", _("Failed")


class DelegationBatch(UUIDPrimaryKeyModel, TimestampedModel):
    """One uploaded delegation CSV import (Schema §6.5 `DelegationBatch`)."""

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="delegation_batches"
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="delegation_batches"
    )
    stored_object = models.ForeignKey(
        "documents.StoredObject", on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=16, choices=DelegationBatchStatus.choices, default=DelegationBatchStatus.UPLOADED
    )
    # Caller-supplied idempotency key for the whole batch (Phase 2 Prompt 2
    # "do not rely only on (batch, row_number)") -- re-uploading/re-applying
    # under the same key is a safe no-op, never a second batch.
    idempotency_key = models.CharField(max_length=100, unique=True)
    row_count = models.PositiveIntegerField(default=0)
    valid_row_count = models.PositiveIntegerField(default=0)
    applied_row_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "invitations_delegation_batch"
        indexes = [models.Index(fields=["organization", "created_at"], name="inv_delbatch_org_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"batch:{self.pk}:{self.status}"


class DelegationRowStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    VALID = "VALID", _("Valid")
    INVALID = "INVALID", _("Invalid")
    APPLIED = "APPLIED", _("Applied")


class DelegationClaimDeliveryStatus(models.TextChoices):
    """Durable delivery outcome for a delegated row's claim email (Phase 2
    Prompt 2 V2 correction pass "make delegated-claim delivery durable and
    recoverable"). Blank (`""`) means no delivery has been attempted yet.
    """

    QUEUED = "QUEUED", _("Queued")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")


class DelegationRow(UUIDPrimaryKeyModel, TimestampedModel):
    """One validated/applied row of a `DelegationBatch` (Schema §6.5
    `DelegationRow`).

    Data minimization (Phase 2 Prompt 2): the candidate email is the only
    personal value genuinely needed later (to send the individual
    completion link) and is stored ONLY as an `EncryptedTextField` plus a
    versioned blind index for deterministic duplicate-row detection --
    never as unprotected staging JSON. `error_codes` holds only bounded,
    non-sensitive validation codes, never a personal value.
    """

    batch = models.ForeignKey(DelegationBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    candidate_email_encrypted = EncryptedTextField(blank=True, default="")
    candidate_email_hash = BlindIndexField(blank=True, default="")
    candidate_email_hash_key_version = models.SmallIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=DelegationRowStatus.choices, default=DelegationRowStatus.PENDING
    )
    error_codes = models.JSONField(default=list, blank=True)
    registration = models.ForeignKey(
        "registrations.Registration",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Durable, retry-safe claim-email delivery evidence (Phase 2 Prompt 2 V2
    # correction pass "make delegated-claim delivery durable and
    # recoverable"): `claim_delivery_url_encrypted` holds the absolute claim
    # URL (which embeds the raw, single-use claim token) ENCRYPTED at rest,
    # never in plaintext -- it exists only so a FAILED delivery can be
    # retried without re-issuing a second claim, and is cleared (rendered
    # unrecoverable) once delivery succeeds or the underlying claim expires.
    claim_delivery_status = models.CharField(
        max_length=16, choices=DelegationClaimDeliveryStatus.choices, blank=True, default=""
    )
    claim_delivery_url_encrypted = EncryptedTextField(blank=True, default="")
    claim_delivery_message = models.ForeignKey(
        "communications.CommunicationMessage",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "invitations_delegation_row"
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"], name="inv_delrow_batch_rownum_uq"
            ),
            # Deterministic row identity WITHOUT exposing PII (Phase 2
            # Prompt 2 "deterministic row identity derived safely"): the
            # blind index of the normalized candidate email, scoped to the
            # batch, so the same batch cannot apply the same person twice --
            # while the SAME email in a DIFFERENT batch/campaign/
            # organization remains a separate, valid candidate.
            models.UniqueConstraint(
                fields=["batch", "candidate_email_hash"],
                condition=~models.Q(candidate_email_hash=""),
                name="inv_delrow_batch_identity_uq",
            ),
        ]
        indexes = [models.Index(fields=["batch", "status"], name="inv_delrow_batch_status_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the raw email
        return f"row:{self.batch_id}:{self.row_number}"
