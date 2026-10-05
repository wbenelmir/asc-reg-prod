"""Operations Back Office review domain (Phase 2 Prompt 3).

New app (TRD §13.1 names `reviews` as a distinct later-phase application,
ADR-0001 defers its creation until the phase that needs it). This module
never duplicates `Registration`'s own public/internal status columns or
`RegistrationSubmission`'s snapshot evidence -- it references them.

Prompt 3/Prompt 4 boundary (an explicit work-package instruction):
no Participant Role, Badge Type, Access Profile, access-rule, credential,
pass, badge-stock, entry, or offline model exists here. `RegistrationDecision`
records only the APPROVED/NOT_APPROVED outcome fact; the service layer
refuses to actually commit an APPROVED outcome until Prompt 4's assignment
models exist (see `apps.reviews.services`).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.fields import EncryptedTextField
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel

# ---------------------------------------------------------------------------
# ReviewCase
# ---------------------------------------------------------------------------


class ReviewCaseType(models.TextChoices):
    STANDARD = "STANDARD", _("Standard")
    IDENTITY = "IDENTITY", _("Identity")
    DUPLICATE = "DUPLICATE", _("Duplicate")
    RESTRICTED = "RESTRICTED", _("Restricted")
    INFORMATION_RESPONSE = "INFORMATION_RESPONSE", _("Information response")


class ReviewQueueCode(models.TextChoices):
    """A controlled, bounded queue catalogue (Phase 2 Prompt 3 §5.1
    "controlled queue code") -- never an arbitrary caller-supplied string.
    """

    GENERAL = "GENERAL", _("General")
    IDENTITY = "IDENTITY", _("Identity")
    DUPLICATE = "DUPLICATE", _("Duplicate")
    RESTRICTED = "RESTRICTED", _("Restricted")
    INFORMATION_RESPONSE = "INFORMATION_RESPONSE", _("Information response")
    SPECIALIST = "SPECIALIST", _("Specialist")


class ReviewCaseStatus(models.TextChoices):
    QUEUED = "QUEUED", _("Queued")
    ASSIGNED = "ASSIGNED", _("Assigned")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    WAITING = "WAITING", _("Waiting")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")

    @classmethod
    def open_statuses(cls) -> tuple[str, ...]:
        return (cls.QUEUED, cls.ASSIGNED, cls.IN_PROGRESS, cls.WAITING)


MIN_REVIEW_PRIORITY = 1
MAX_REVIEW_PRIORITY = 100
DEFAULT_REVIEW_PRIORITY = 50


class ReviewCase(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """One Registration Context under operations review (Phase 2 Prompt 3 §5.1).

    `event_edition`/`organization` are copied from the Registration Context
    at creation time (never re-derived on every read) so every scope-
    filtering selector can index and filter on THIS table directly, exactly
    like `apps.invitations.models.DelegationBatch` copies its own scope
    columns rather than joining through to the target on every query.
    `organization` is nullable: an `OPEN`-source Registration has no source
    organization at all.
    """

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="review_cases"
    )
    case_type = models.CharField(max_length=24, choices=ReviewCaseType.choices)
    queue_code = models.CharField(max_length=24, choices=ReviewQueueCode.choices)
    priority = models.PositiveSmallIntegerField(default=DEFAULT_REVIEW_PRIORITY)
    status = models.CharField(
        max_length=16, choices=ReviewCaseStatus.choices, default=ReviewCaseStatus.QUEUED
    )
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "reviews_review_case"
        # Distinct, explicitly-named atomic permissions (Phase 2 Prompt 3
        # §11) -- deny by default, granted only through an explicit
        # `ScopedGroupMembership`. `cancel_registration` lives here (not on
        # `Registration` itself, which this app never owns) because Django
        # custom permissions must be declared on some model.
        permissions = [
            ("assign_reviewcase", "Can assign or reassign a review case"),
            ("change_reviewcase_status", "Can start or change a review case's status"),
            ("view_duplicatecandidates", "Can view duplicate candidates for a review case"),
            ("reopen_reviewcase", "Can reopen a closed registration for review"),
            ("cancel_registration", "Can operationally cancel a registration"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(priority__gte=MIN_REVIEW_PRIORITY)
                & models.Q(priority__lte=MAX_REVIEW_PRIORITY),
                name="rev_case_priority_bounded",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "status", "queue_code"], name="rev_case_event_status_idx"
            ),
            models.Index(fields=["organization", "status"], name="rev_case_org_status_idx"),
            models.Index(fields=["registration", "status"], name="rev_case_reg_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"case:{self.pk}:{self.case_type}:{self.status}"


class ReviewAssignment(UUIDPrimaryKeyModel, TimestampedModel):
    """Append-only assignment history for one `ReviewCase` (Phase 2 Prompt 3 §5.2).

    Reassignment NEVER overwrites or deletes a prior row -- it closes the
    previous assignment (`ended_at`, `is_current=False`) and creates a new
    one, mirroring `apps.invitations.models.InvitationLink` rotation.
    Exactly one CURRENT assignment per case is enforced by the partial
    unique constraint below. Assignment narrows WHO IS RESPONSIBLE, never
    WHO MAY SEE the case -- an independently scoped reviewer keeps
    visibility through `apps.accounts.selectors.scope_filtered_queryset`
    regardless of who the case happens to be assigned to.
    """

    review_case = models.ForeignKey(
        ReviewCase, on_delete=models.CASCADE, related_name="assignments"
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    assigned_group = models.ForeignKey(
        "auth.Group", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=300, blank=True, default="")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        db_table = "reviews_review_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["review_case"],
                condition=models.Q(is_current=True),
                name="rev_assignment_one_current_uq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(assigned_user__isnull=False, assigned_group__isnull=True)
                    | models.Q(assigned_user__isnull=True, assigned_group__isnull=False)
                ),
                name="rev_assignment_exactly_one_target",
            ),
        ]
        indexes = [models.Index(fields=["review_case", "is_current"], name="rev_assign_case_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"assignment:{self.review_case_id}:{'current' if self.is_current else 'closed'}"


# ---------------------------------------------------------------------------
# Versioned checklist definitions and immutable results
# ---------------------------------------------------------------------------


class ChecklistDefinition(UUIDPrimaryKeyModel, TimestampedModel):
    """One versioned checklist for a given Event Edition and case type
    (Phase 2 Prompt 3 §5.3). `items` is a bounded, controlled list of
    `{"code": str, "required": bool}` dicts -- validated at creation time
    against `apps.reviews.services.ALLOWED_CHECKLIST_ITEM_CODES`, never an
    arbitrary caller-supplied shape.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    case_type = models.CharField(max_length=24, choices=ReviewCaseType.choices)
    version_label = models.CharField(max_length=32)
    items = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "reviews_checklist_definition"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "case_type", "version_label"],
                name="rev_checklistdef_event_type_version_uq",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"checklist:{self.case_type}:{self.version_label}"


class ChecklistResultValue(models.TextChoices):
    PASS = "PASS", _("Pass")
    FAIL = "FAIL", _("Fail")
    NOT_APPLICABLE = "NOT_APPLICABLE", _("Not applicable")
    NEEDS_FOLLOW_UP = "NEEDS_FOLLOW_UP", _("Needs follow-up")


class ChecklistResult(UUIDPrimaryKeyModel):
    """Append-only checklist evidence (Phase 2 Prompt 3 §5.3). Re-recording
    the SAME item creates a NEW row -- prior evidence is immutable and
    remains queryable, never updated or deleted. No `updated_at`."""

    review_case = models.ForeignKey(
        ReviewCase, on_delete=models.CASCADE, related_name="checklist_results"
    )
    checklist_definition = models.ForeignKey(
        ChecklistDefinition, on_delete=models.PROTECT, related_name="+"
    )
    item_code = models.CharField(max_length=64)
    result = models.CharField(max_length=20, choices=ChecklistResultValue.choices)
    notes = models.CharField(max_length=1000, blank=True, default="")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField()

    class Meta:
        db_table = "reviews_checklist_result"
        indexes = [
            models.Index(
                fields=["review_case", "item_code", "recorded_at"], name="rev_checkresult_case_idx"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"checklist-result:{self.review_case_id}:{self.item_code}:{self.result}"


class InternalReviewNote(UUIDPrimaryKeyModel, TimestampedModel):
    """Never participant-visible internal note (Phase 2 Prompt 3 §5.4).
    Always encrypted at rest, regardless of content -- the service layer
    never copies this into any participant-facing text."""

    review_case = models.ForeignKey(
        ReviewCase, on_delete=models.CASCADE, related_name="internal_notes"
    )
    note_encrypted = EncryptedTextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "reviews_internal_review_note"
        indexes = [models.Index(fields=["review_case", "created_at"], name="rev_note_case_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the note content
        return f"internal-note:{self.review_case_id}"


# ---------------------------------------------------------------------------
# Additional Information Requests
# ---------------------------------------------------------------------------


class InformationRequestStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SENT = "SENT", _("Sent")
    RESPONSE_IN_PROGRESS = "RESPONSE_IN_PROGRESS", _("Response in progress")
    SUBMITTED = "SUBMITTED", _("Submitted")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")
    OVERDUE = "OVERDUE", _("Overdue")

    @classmethod
    def active_statuses(cls) -> tuple[str, ...]:
        # SUBMITTED remains active until an operational reviewer resolves
        # and closes it.  Otherwise a second round could be opened while
        # the first response is still awaiting review.
        return (cls.DRAFT, cls.SENT, cls.RESPONSE_IN_PROGRESS, cls.SUBMITTED, cls.OVERDUE)

    @classmethod
    def participant_action_required_statuses(cls) -> tuple[str, ...]:
        return (cls.SENT, cls.RESPONSE_IN_PROGRESS, cls.OVERDUE)


class InformationRequestPurpose(models.TextChoices):
    IDENTITY_CORRECTION = "IDENTITY_CORRECTION", _("Identity correction")
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD", _("Document upload")
    CLARIFICATION = "CLARIFICATION", _("Clarification")
    OTHER = "OTHER", _("Other")


class InformationRequest(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """One Additional Information Request for a Registration Context
    (Phase 2 Prompt 3 §7.1). `message_en`/`message_fr`/`message_ar` mirror
    `InterestTopic.label`/`label_fr`/`label_ar` -- short, reviewer-authored,
    participant-safe text, displayed with an English fallback
    (`localized_message`)."""

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="information_requests"
    )
    review_case = models.ForeignKey(
        ReviewCase,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="information_requests",
    )
    sequence = models.PositiveIntegerField()
    status = models.CharField(
        max_length=24,
        choices=InformationRequestStatus.choices,
        default=InformationRequestStatus.DRAFT,
    )
    purpose = models.CharField(max_length=24, choices=InformationRequestPurpose.choices)
    message_en = models.CharField(max_length=2000)
    message_fr = models.CharField(max_length=2000, blank=True, default="")
    message_ar = models.CharField(max_length=2000, blank=True, default="")
    deadline_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=300, blank=True, default="")
    # A participant-authored, NOT-YET-final response draft (Phase 2 Prompt 3
    # §7.3 "supports saving a response draft") -- cleared once
    # `submit_information_response` creates the immutable `InformationResponse`
    # snapshot. Bounded to the same shape as that snapshot; never itself
    # treated as submission evidence.
    draft_snapshot_json = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "reviews_information_request"
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "sequence"], name="rev_inforeq_reg_sequence_uq"
            ),
            # One ACTIVE request per Registration (Phase 2 Prompt 3 §7.1) --
            # a partial unique index over the non-terminal statuses only;
            # CLOSED/CANCELLED requests never block a fresh one.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status__in=list(InformationRequestStatus.active_statuses())),
                name="rev_inforeq_one_active_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "status"], name="rev_inforeq_reg_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"info-request:{self.registration_id}:{self.sequence}"

    @property
    def localized_message(self) -> str:
        from django.utils.translation import get_language

        language = (get_language() or "en").split("-", 1)[0].lower()
        if language == "fr" and self.message_fr:
            return self.message_fr
        if language == "ar" and self.message_ar:
            return self.message_ar
        return self.message_en


class RequestItemKind(models.TextChoices):
    FIELD_CORRECTION = "FIELD_CORRECTION", _("Field correction")
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD", _("Document upload")
    CLARIFICATION = "CLARIFICATION", _("Clarification")


class RequestItem(UUIDPrimaryKeyModel, TimestampedModel):
    """One requested item on an `InformationRequest` (Phase 2 Prompt 3 §7.2).

    `field_code` is validated at the SERVICE layer against
    `apps.reviews.services.ALLOWED_REQUEST_FIELD_CODES` -- never an
    arbitrary model path, Python attribute, SQL fragment, template
    expression, or HTML. `document_type` is validated against
    `apps.documents.models.DocumentType` when `kind=DOCUMENT_UPLOAD`.
    """

    information_request = models.ForeignKey(
        InformationRequest, on_delete=models.CASCADE, related_name="items"
    )
    kind = models.CharField(max_length=24, choices=RequestItemKind.choices)
    field_code = models.CharField(max_length=64, blank=True, default="")
    document_type = models.CharField(max_length=32, blank=True, default="")
    is_required = models.BooleanField(default=True)
    instructions_en = models.CharField(max_length=500, blank=True, default="")
    instructions_fr = models.CharField(max_length=500, blank=True, default="")
    instructions_ar = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "reviews_request_item"
        indexes = [models.Index(fields=["information_request"], name="rev_reqitem_request_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"request-item:{self.information_request_id}:{self.kind}"

    @property
    def localized_instructions(self) -> str:
        from django.utils.translation import get_language

        language = (get_language() or "en").split("-", 1)[0].lower()
        if language == "fr" and self.instructions_fr:
            return self.instructions_fr
        if language == "ar" and self.instructions_ar:
            return self.instructions_ar
        return self.instructions_en


class InformationResponse(UUIDPrimaryKeyModel):
    """Immutable participant response snapshot (Phase 2 Prompt 3 §7.3). No
    `updated_at`/`version` -- append-only evidence, never mutated after
    creation. `idempotency_key` makes submission safe to retry."""

    information_request = models.OneToOneField(
        InformationRequest, on_delete=models.PROTECT, related_name="response"
    )
    submitted_by_person = models.ForeignKey(
        "people.Person", on_delete=models.PROTECT, related_name="+"
    )
    snapshot_json = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    submitted_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=100, unique=True)

    class Meta:
        db_table = "reviews_information_response"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"response:{self.information_request_id}"


class ResponseItem(UUIDPrimaryKeyModel):
    """One answered `RequestItem` within an `InformationResponse` (Phase 2
    Prompt 3 §7.3) -- links to the EXACT requested item, never a loosely
    matched field name. `value_encrypted` holds a corrected field value or
    clarification text; `document` holds an uploaded evidence document,
    reusing the existing protected-document boundary."""

    response = models.ForeignKey(
        InformationResponse, on_delete=models.CASCADE, related_name="items"
    )
    request_item = models.ForeignKey(RequestItem, on_delete=models.PROTECT, related_name="+")
    value_encrypted = EncryptedTextField(blank=True, default="")
    document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "reviews_response_item"
        constraints = [
            models.UniqueConstraint(
                fields=["response", "request_item"], name="rev_respitem_response_item_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial, never the value
        return f"response-item:{self.response_id}:{self.request_item_id}"


# ---------------------------------------------------------------------------
# Duplicate-candidate review
# ---------------------------------------------------------------------------


class DuplicateOutcome(models.TextChoices):
    SAME_PERSON_ANOTHER_CONTEXT = "SAME_PERSON_ANOTHER_CONTEXT", _("Same person, another context")
    ACCIDENTAL_DUPLICATE = "ACCIDENTAL_DUPLICATE", _("Accidental duplicate")
    DIFFERENT_PERSON = "DIFFERENT_PERSON", _("Different person")
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE", _("Insufficient evidence")
    ESCALATED_RESTRICTED = "ESCALATED_RESTRICTED", _("Escalated to restricted specialist")


class DuplicateCandidateResolution(UUIDPrimaryKeyModel, TimestampedModel):
    """One reviewer outcome for one candidate on a DUPLICATE `ReviewCase`
    (Phase 2 Prompt 3 §8). Never merges, deletes, or reassigns identity
    values -- purely an evidentiary record of the reviewer's decision.
    An outcome for one candidate never mutates any OTHER Registration or
    Person row."""

    review_case = models.ForeignKey(
        ReviewCase, on_delete=models.CASCADE, related_name="duplicate_resolutions"
    )
    candidate_person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    candidate_identity_identifier = models.ForeignKey(
        "people.IdentityIdentifier",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    outcome = models.CharField(max_length=32, choices=DuplicateOutcome.choices)
    reason = models.CharField(max_length=500, blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reviewed_at = models.DateTimeField()

    class Meta:
        db_table = "reviews_duplicate_candidate_resolution"
        indexes = [models.Index(fields=["review_case"], name="rev_dupres_case_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"duplicate-resolution:{self.review_case_id}:{self.outcome}"


# ---------------------------------------------------------------------------
# Registration decision history
# ---------------------------------------------------------------------------


class RegistrationDecisionOutcome(models.TextChoices):
    APPROVED = "APPROVED", _("Approved")
    NOT_APPROVED = "NOT_APPROVED", _("Not approved")


class RegistrationDecision(UUIDPrimaryKeyModel, TimestampedModel):
    """Immutable, append-only decision history for a Registration (Phase 2
    Prompt 3 §9). Exactly one CURRENT decision per Registration is enforced
    by a partial unique constraint; reopening supersedes the current
    decision (`is_current=False`) without deleting it -- `supersedes`
    points BACKWARD to the decision this one replaces.

    `internal_note_encrypted`/`internal_reason_code` are NEVER shown to a
    participant; `participant_reason_code` is a separate, deliberately
    coarser, approved reason-code catalogue safe for participant display.
    """

    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="decisions"
    )
    sequence = models.PositiveIntegerField()
    outcome = models.CharField(max_length=16, choices=RegistrationDecisionOutcome.choices)
    internal_reason_code = models.CharField(max_length=64)
    internal_note_encrypted = EncryptedTextField(blank=True, default="")
    participant_reason_code = models.CharField(max_length=64, blank=True, default="")
    is_current = models.BooleanField(default=True)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField()
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "reviews_registration_decision"
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "sequence"], name="rev_decision_reg_sequence_uq"
            ),
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(is_current=True),
                name="rev_decision_one_current_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["registration", "is_current"], name="rev_decision_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"decision:{self.registration_id}:{self.sequence}:{self.outcome}"
