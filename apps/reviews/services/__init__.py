"""Review commands: case lifecycle, assignment, checklist, information
requests, duplicate resolution, decisions, reopen/withdraw/cancel (Phase 2
Prompt 3).

Cross-app rule (mirrors ADR-0001 for `apps.invitations`): this module
mutates `Registration.public_status`/`internal_status` directly (there is
no separate `apps.registrations` service boundary for these transitions
yet) but NEVER writes participant profile fields, and never creates a
Prompt 4 assignment record of any kind.
"""

from __future__ import annotations

import hashlib

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.communications.purposes import CommunicationPurpose
from apps.communications.services import queue_communication
from apps.core.crypto import compute_blind_index, get_key_provider
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher
from apps.people.models import ContactPointStatus, ContactPointType
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
)
from apps.reviews.models import (
    ChecklistDefinition,
    ChecklistResult,
    ChecklistResultValue,
    DuplicateCandidateResolution,
    DuplicateOutcome,
    InformationRequest,
    InformationRequestPurpose,
    InformationRequestStatus,
    InformationResponse,
    InternalReviewNote,
    RegistrationDecision,
    RegistrationDecisionOutcome,
    RequestItem,
    RequestItemKind,
    ResponseItem,
    ReviewAssignment,
    ReviewCase,
    ReviewCaseStatus,
    ReviewCaseType,
    ReviewQueueCode,
)


def _queue_registration_communication(
    *, registration: Registration, purpose_code: str, context: dict, idempotency_key: str
):
    """Best-effort: queue a Phase 2 Prompt 5 communication for `registration`'s
    person. Silently returns `None` (never raises) when the person has no
    active, verified, login-enabled email contact -- a missing contact must
    never turn an otherwise-successful review action into a failed one,
    mirroring `apps.registrations.confirmation.send_registration_confirmation`.
    """
    if registration.person_id is None:
        return None
    contact = registration.person.contact_points.filter(
        type=ContactPointType.EMAIL,
        login_enabled=True,
        is_verified=True,
        status=ContactPointStatus.ACTIVE,
    ).first()
    if contact is None:
        return None
    return queue_communication(
        purpose_code=purpose_code,
        event_edition=registration.event_edition,
        person=registration.person,
        registration=registration,
        language=registration.preferred_language or "en",
        destination=contact.value_encrypted,
        context=context,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Optimistic concurrency (Phase 2 Prompt 3 §12)
# ---------------------------------------------------------------------------


class StaleVersionError(Exception):
    """Raised when a caller's `expected_version` no longer matches the
    locked row's current version -- the view layer renders this as a
    visible conflict response (HTTP 409 for an HTMX/HTTP action), never a
    silent last-write-wins overwrite."""


def _lock_versioned(model_cls, *, pk, expected_version: int):
    locked = model_cls.objects.select_for_update().get(pk=pk)
    if locked.version != expected_version:
        name = model_cls.__name__
        raise StaleVersionError(
            f"{name} {pk} expected version {expected_version}, found {locked.version}."
        )
    return locked


def _lock_registration_of(model_cls, *, pk) -> Registration:
    """Lock the Registration a review case or information request belongs to,
    BEFORE that row (IDV-Q-C1, lock order).

    Every command that may change a Registration takes its row first, then
    its own review case or information request. The commands that end review
    work -- a decision, a withdrawal, an operational cancellation and the final
    identity rejection -- already lock the Registration and then the open
    cases and requests, so the two orders can no longer deadlock. The
    Registration of a case or request never changes, so reading it without a
    lock is safe; the caller's version check on its own row follows.
    """
    registration_id = (
        model_cls.objects.filter(pk=pk).values_list("registration_id", flat=True).first()
    )
    if registration_id is None:
        raise model_cls.DoesNotExist(f"{model_cls.__name__} {pk} does not exist.")
    return Registration.objects.select_for_update().get(pk=registration_id)


#: A Registration in one of these public statuses is closed: no review
#: command may reopen its review work or change its status (only the
#: authorized `reopen_registration` may).
CLOSED_PUBLIC_STATUSES = (
    RegistrationPublicStatus.NOT_APPROVED,
    RegistrationPublicStatus.WITHDRAWN,
)


def _require_registration_open(registration: Registration) -> None:
    if registration.public_status in CLOSED_PUBLIC_STATUSES:
        raise InvalidStateTransitionError(
            f"The registration is {registration.public_status}; its review work is closed."
        )


def _bump_version(instance, *, extra_fields: list[str]) -> None:
    instance.version = instance.version + 1
    instance.save(update_fields=[*extra_fields, "version", "updated_at"])


def _set_registration_status(
    registration_id,
    *,
    public_status: str | None = None,
    internal_status: str | None = None,
    only_if_internal_status: str | None = None,
) -> Registration:
    """Lock and version a Registration status change inside the caller's
    transaction. Review commands must never mutate shared Registration
    state through an unversioned QuerySet.update()."""
    registration = Registration.objects.select_for_update().get(pk=registration_id)
    if (
        only_if_internal_status is not None
        and registration.internal_status != only_if_internal_status
    ):
        return registration
    fields: list[str] = []
    if public_status is not None and registration.public_status != public_status:
        registration.public_status = public_status
        fields.append("public_status")
    if internal_status is not None and registration.internal_status != internal_status:
        registration.internal_status = internal_status
        fields.append("internal_status")
    if fields:
        _bump_version(registration, extra_fields=fields)
    return registration


def _terminate_open_review_work(
    registration: Registration, *, case_status: str, request_reason: str
) -> None:
    """End open cases, assignments, and information requests when the
    Registration itself reaches a terminal outcome."""
    now = timezone.now()
    open_cases = ReviewCase.objects.select_for_update().filter(
        registration=registration, status__in=ReviewCaseStatus.open_statuses()
    )
    case_ids = list(open_cases.values_list("pk", flat=True))
    if case_ids:
        ReviewAssignment.objects.filter(review_case_id__in=case_ids, is_current=True).update(
            is_current=False, ended_at=now
        )
        open_cases.update(
            status=case_status,
            closed_at=now,
            version=F("version") + 1,
            updated_at=now,
        )
    InformationRequest.objects.select_for_update().filter(
        registration=registration,
        status__in=InformationRequestStatus.active_statuses(),
    ).update(
        status=InformationRequestStatus.CANCELLED,
        cancelled_at=now,
        cancellation_reason=request_reason[:300],
        version=F("version") + 1,
        updated_at=now,
    )


def _close_identity_work(registration: Registration, **kwargs) -> None:
    """IDV-C1 (R-IDV-06): end the identity work of the Registration this
    transaction just closed -- discard pending checks and close an open identity
    correction -- after its row lock (the identity lock order). Never changes
    the Registration."""
    from apps.people.services.identity_verification import (
        close_identity_work_for_registration,
    )

    close_identity_work_for_registration(registration, **kwargs)


# ---------------------------------------------------------------------------
# Controlled catalogues (never an arbitrary caller-supplied code/path)
# ---------------------------------------------------------------------------

ALLOWED_CHECKLIST_ITEM_CODES: dict[str, tuple[str, ...]] = {
    ReviewCaseType.STANDARD: (
        "IDENTITY_PLAUSIBLE",
        "CONTACT_PLAUSIBLE",
        "PROFESSIONAL_INFO_COMPLETE",
        "INTERESTS_COMPLETE",
    ),
    ReviewCaseType.IDENTITY: (
        "IDENTITY_DOCUMENT_LEGIBLE",
        "IDENTITY_NAME_MATCHES",
        "IDENTITY_DOB_MATCHES",
        "IDENTITY_PHOTO_MATCHES",
    ),
    ReviewCaseType.DUPLICATE: ("DUPLICATE_EVIDENCE_REVIEWED",),
    ReviewCaseType.RESTRICTED: (
        "RESTRICTED_EVIDENCE_REVIEWED",
        "RESTRICTED_ESCALATION_JUSTIFIED",
    ),
    ReviewCaseType.INFORMATION_RESPONSE: ("RESPONSE_COMPLETE", "RESPONSE_CONSISTENT"),
}

ALLOWED_REQUEST_FIELD_CODES: frozenset[str] = frozenset(
    {
        "given_names",
        "family_name",
        "date_of_birth",
        "nationality_code",
        "country_of_residence",
        "mobile_number",
        "organization_name",
        "job_title",
        "department",
        "sector",
        "professional_profile_url",
    }
)

# Correcting any of these fields concerns the participant's declared
# identity -- resubmitting one returns verification to PENDING (Phase 2
# Prompt 3 §7.3 "sensitive identity changes must return the relevant
# verification state to pending"), never left at whatever state an earlier
# review round left it in.
IDENTITY_SENSITIVE_FIELD_CODES: frozenset[str] = frozenset(
    {"given_names", "family_name", "date_of_birth", "nationality_code"}
)

ALLOWED_REQUEST_DOCUMENT_TYPES: frozenset[str] = frozenset({"REQUESTED_EVIDENCE"})

INTERNAL_NOTE_MAX_LENGTH = 4000


class InvalidChecklistItemError(Exception):
    """Raised for an item code not present in the checklist definition."""


class InvalidRequestItemError(Exception):
    """Raised for a `RequestItem` whose `field_code`/`document_type` is not
    on the controlled allowlist -- never an arbitrary model path, Python
    attribute, SQL fragment, template expression, or HTML."""


class ActiveInformationRequestExistsError(Exception):
    """Raised when a Registration already has a non-terminal Information
    Request (Phase 2 Prompt 3 §7.1's partial unique constraint)."""


class ApprovalRequiresAssignmentsError(Exception):
    """Raised by `record_approved_decision` -- Prompt 4's Participant Role/
    Badge Type/Access Profile assignment models do not exist yet, and no
    approval bypass is authorized for this prompt (Prompt 3/4
    boundary). Fails BEFORE any database write; never a placeholder or
    default assignment."""


class ApprovalRequiresVerifiedIdentityError(Exception):
    """Raised by `record_approved_decision` when the registration has no
    current, valid, verified identity (owner decision IDV-Q1, 2026-10-02).
    Fails BEFORE any write. `code` is an
    `apps.people.selectors.clearance.ClearanceCode` (never identity data);
    a registration submitted before identity verification existed has
    `NO_IDENTITY_CASE`."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RegistrationOwnershipError(Exception):
    """Raised when a participant attempts to act on a Registration Context
    they do not own."""


class DocumentPurposeMismatchError(Exception):
    """Raised when a response item's uploaded document does not belong to
    the exact `RequestItem` it claims to answer."""


class InvalidInformationResponseError(Exception):
    """Raised when a response omits required data or references an item
    outside the exact Information Request being answered."""


class InvalidStateTransitionError(Exception):
    """Raised when a controlled workflow transition is not permitted."""


# ---------------------------------------------------------------------------
# Checklist definitions
# ---------------------------------------------------------------------------


def create_checklist_definition(
    *, event_edition, case_type: str, version_label: str, item_codes: list[str] | None = None
) -> ChecklistDefinition:
    """Create one versioned checklist definition, validated against the
    controlled `ALLOWED_CHECKLIST_ITEM_CODES` catalogue for `case_type`
    (Phase 2 Prompt 3 §5.3) -- never an arbitrary caller-supplied item
    shape. Defaults to every allowed item for the case type, all required.
    """
    if case_type not in ReviewCaseType.values:
        raise InvalidChecklistItemError(f"Unknown review case type: {case_type!r}.")
    allowed = ALLOWED_CHECKLIST_ITEM_CODES.get(case_type, ())
    codes = item_codes if item_codes is not None else list(allowed)
    unknown = set(codes) - set(allowed)
    if unknown:
        raise InvalidChecklistItemError(
            f"Unknown checklist item code(s) for {case_type}: {unknown}"
        )
    return ChecklistDefinition.objects.create(
        event_edition=event_edition,
        case_type=case_type,
        version_label=version_label,
        items=[{"code": code, "required": True} for code in codes],
    )


# ---------------------------------------------------------------------------
# ReviewCase lifecycle
# ---------------------------------------------------------------------------


def open_review_case(
    *,
    registration: Registration,
    case_type: str,
    queue_code: str,
    priority: int | None = None,
    actor=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ReviewCase:
    from apps.reviews.models import (
        DEFAULT_REVIEW_PRIORITY,
        MAX_REVIEW_PRIORITY,
        MIN_REVIEW_PRIORITY,
    )

    if case_type not in ReviewCaseType.values:
        raise ValueError("Unknown review case type.")
    if queue_code not in ReviewQueueCode.values:
        raise ValueError("Unknown review queue code.")
    effective_priority = priority if priority is not None else DEFAULT_REVIEW_PRIORITY
    if not MIN_REVIEW_PRIORITY <= effective_priority <= MAX_REVIEW_PRIORITY:
        raise ValueError("Review priority is outside the permitted range.")

    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        case = ReviewCase.objects.create(
            registration=registration,
            case_type=case_type,
            queue_code=queue_code,
            priority=effective_priority,
            status=ReviewCaseStatus.QUEUED,
            event_edition_id=registration.event_edition_id,
            organization_id=registration.source_organization_id,
            opened_at=timezone.now(),
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER" if actor else "SYSTEM",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_CASE_OPENED,
                target_type="ReviewCase",
                target_uuid=case.pk,
                event_edition_id=registration.event_edition_id,
                result="SUCCESS",
                after_summary={"case_type": case_type, "queue_code": queue_code},
                correlation_id=correlation_id,
            )
        )
    return case


def assign_review_case(
    *,
    review_case: ReviewCase,
    expected_version: int,
    assigned_by,
    assigned_user=None,
    assigned_group=None,
    reason: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ReviewAssignment:
    """Close the current assignment (if any) and create a new one -- never
    overwrites or deletes prior assignment history (Phase 2 Prompt 3 §5.2).
    """
    if (assigned_user is None) == (assigned_group is None):
        raise ValueError("assign_review_case requires exactly one user or group target.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration, then the case.
        registration = _lock_registration_of(ReviewCase, pk=review_case.pk)
        case = _lock_versioned(ReviewCase, pk=review_case.pk, expected_version=expected_version)
        if case.status not in ReviewCaseStatus.open_statuses():
            # A completed or cancelled case is never given a current assignee.
            raise InvalidStateTransitionError(f"A {case.status} review case cannot be assigned.")
        _require_registration_open(registration)
        ReviewAssignment.objects.filter(review_case=case, is_current=True).update(
            is_current=False, ended_at=now
        )
        assignment = ReviewAssignment.objects.create(
            review_case=case,
            assigned_user=assigned_user,
            assigned_group=assigned_group,
            assigned_by=assigned_by,
            reason=reason,
            started_at=now,
            is_current=True,
        )
        if case.status == ReviewCaseStatus.QUEUED:
            case.status = ReviewCaseStatus.ASSIGNED
        _bump_version(case, extra_fields=["status"])
        _set_registration_status(
            case.registration_id,
            internal_status=RegistrationInternalStatus.ASSIGNED,
            only_if_internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(assigned_by, "pk", None),
                action_code=action_codes.REVIEW_ASSIGNMENT_RECORDED,
                target_type="ReviewCase",
                target_uuid=case.pk,
                event_edition_id=case.event_edition_id,
                result="SUCCESS",
                after_summary={
                    "assigned_user_id": str(getattr(assigned_user, "pk", "")) or None,
                    "assigned_group_id": getattr(assigned_group, "pk", None),
                },
                correlation_id=correlation_id,
            )
        )
    return assignment


def change_review_case_status(
    *,
    review_case: ReviewCase,
    new_status: str,
    expected_version: int,
    actor,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ReviewCase:
    allowed_transitions = {
        ReviewCaseStatus.QUEUED: {ReviewCaseStatus.IN_PROGRESS, ReviewCaseStatus.CANCELLED},
        ReviewCaseStatus.ASSIGNED: {ReviewCaseStatus.IN_PROGRESS, ReviewCaseStatus.CANCELLED},
        ReviewCaseStatus.IN_PROGRESS: {
            ReviewCaseStatus.WAITING,
            ReviewCaseStatus.COMPLETED,
            ReviewCaseStatus.CANCELLED,
        },
        ReviewCaseStatus.WAITING: {
            ReviewCaseStatus.IN_PROGRESS,
            ReviewCaseStatus.COMPLETED,
            ReviewCaseStatus.CANCELLED,
        },
        ReviewCaseStatus.COMPLETED: set(),
        ReviewCaseStatus.CANCELLED: set(),
    }
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration, then the case.
        registration = _lock_registration_of(ReviewCase, pk=review_case.pk)
        case = _lock_versioned(ReviewCase, pk=review_case.pk, expected_version=expected_version)
        _require_registration_open(registration)
        old_status = case.status
        if new_status not in allowed_transitions.get(old_status, set()):
            raise InvalidStateTransitionError(
                f"ReviewCase cannot transition from {old_status} to {new_status}."
            )
        case.status = new_status
        if new_status in (ReviewCaseStatus.COMPLETED, ReviewCaseStatus.CANCELLED):
            case.closed_at = timezone.now()
            _bump_version(case, extra_fields=["status", "closed_at"])
            ReviewAssignment.objects.filter(review_case=case, is_current=True).update(
                is_current=False, ended_at=case.closed_at
            )
        else:
            _bump_version(case, extra_fields=["status"])
        action_code = (
            action_codes.REVIEW_CASE_CANCELLED
            if new_status == ReviewCaseStatus.CANCELLED
            else action_codes.REVIEW_CASE_STARTED
            if new_status == ReviewCaseStatus.IN_PROGRESS
            else action_codes.REVIEW_CASE_COMPLETED
            if new_status == ReviewCaseStatus.COMPLETED
            else action_codes.REVIEW_CASE_STATUS_CHANGED
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_code,
                target_type="ReviewCase",
                target_uuid=case.pk,
                event_edition_id=case.event_edition_id,
                result="SUCCESS",
                before_summary={"status": old_status},
                after_summary={"status": new_status},
                correlation_id=correlation_id,
            )
        )
        if new_status == ReviewCaseStatus.IN_PROGRESS:
            _set_registration_status(
                case.registration_id,
                public_status=RegistrationPublicStatus.UNDER_REVIEW,
                internal_status=RegistrationInternalStatus.REVIEW_IN_PROGRESS,
            )
    return case


# ---------------------------------------------------------------------------
# Checklist evidence
# ---------------------------------------------------------------------------


def record_checklist_result(
    *,
    review_case: ReviewCase,
    checklist_definition: ChecklistDefinition,
    item_code: str,
    result: str,
    actor,
    notes: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ChecklistResult:
    if (
        checklist_definition.event_edition_id != review_case.event_edition_id
        or checklist_definition.case_type != review_case.case_type
    ):
        raise InvalidChecklistItemError(
            "The checklist definition does not belong to this case's event and type."
        )
    allowed_codes = {item.get("code") for item in checklist_definition.items}
    if item_code not in allowed_codes:
        raise InvalidChecklistItemError(f"{item_code!r} is not part of this checklist definition.")
    if result not in ChecklistResultValue.values:
        raise InvalidChecklistItemError(f"Unknown checklist result: {result!r}.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        entry = ChecklistResult.objects.create(
            review_case=review_case,
            checklist_definition=checklist_definition,
            item_code=item_code,
            result=result,
            notes=notes[:1000],
            actor=actor,
            recorded_at=timezone.now(),
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_CHECKLIST_RESULT_RECORDED,
                target_type="ReviewCase",
                target_uuid=review_case.pk,
                event_edition_id=review_case.event_edition_id,
                result="SUCCESS",
                after_summary={"item_code": item_code, "result": result},
                correlation_id=correlation_id,
            )
        )
    return entry


def add_internal_note(
    *,
    review_case: ReviewCase,
    note_text: str,
    created_by,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> InternalReviewNote:
    bounded_text = note_text[:INTERNAL_NOTE_MAX_LENGTH]
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        note = InternalReviewNote.objects.create(
            review_case=review_case, note_encrypted=bounded_text, created_by=created_by
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(created_by, "pk", None),
                action_code=action_codes.REVIEW_INTERNAL_NOTE_ADDED,
                target_type="ReviewCase",
                target_uuid=review_case.pk,
                event_edition_id=review_case.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return note


# ---------------------------------------------------------------------------
# Duplicate-candidate review
# ---------------------------------------------------------------------------


def resolve_duplicate_candidate(
    *,
    review_case: ReviewCase,
    outcome: str,
    reviewed_by,
    candidate_person=None,
    candidate_identity_identifier=None,
    reason: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> DuplicateCandidateResolution:
    if review_case.case_type != ReviewCaseType.DUPLICATE:
        raise ValueError("resolve_duplicate_candidate requires a DUPLICATE review case.")
    if outcome not in DuplicateOutcome.values:
        raise ValueError("Unknown duplicate-resolution outcome.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration first, before any row that
        # refers to the case is written.
        registration = _lock_registration_of(ReviewCase, pk=review_case.pk)
        _require_registration_open(registration)
        resolution = DuplicateCandidateResolution.objects.create(
            review_case=review_case,
            candidate_person=candidate_person,
            candidate_identity_identifier=candidate_identity_identifier,
            outcome=outcome,
            reason=reason[:500],
            reviewed_by=reviewed_by,
            reviewed_at=timezone.now(),
        )
        if outcome == DuplicateOutcome.ESCALATED_RESTRICTED:
            open_review_case(
                registration=review_case.registration,
                case_type=ReviewCaseType.RESTRICTED,
                queue_code="RESTRICTED",
                actor=reviewed_by,
                audit_recorder=audit_recorder,
                correlation_id=correlation_id,
            )
        else:
            _set_registration_status(
                review_case.registration_id,
                internal_status=RegistrationInternalStatus.REVIEW_IN_PROGRESS,
                only_if_internal_status=RegistrationInternalStatus.DUPLICATE_REVIEW,
            )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(reviewed_by, "pk", None),
                action_code=action_codes.REVIEW_DUPLICATE_RESOLVED,
                target_type="ReviewCase",
                target_uuid=review_case.pk,
                event_edition_id=review_case.event_edition_id,
                result="SUCCESS",
                after_summary={"outcome": outcome},
                correlation_id=correlation_id,
            )
        )
    return resolution


# ---------------------------------------------------------------------------
# Additional Information Requests
# ---------------------------------------------------------------------------


def create_information_request(
    *,
    registration: Registration,
    purpose: str,
    message_en: str,
    items: list[dict],
    created_by,
    review_case: ReviewCase | None = None,
    message_fr: str = "",
    message_ar: str = "",
    deadline_at=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> InformationRequest:
    """Create a DRAFT Information Request with its requested items.

    Locks the Registration row for the whole operation: this both serializes
    the monotonic `sequence` allocation AND lets the partial unique
    "one active request" constraint be checked deterministically, rather
    than racing two concurrent creators (Phase 2 Prompt 3 §12).
    """
    from apps.documents.models import DocumentType

    if purpose not in InformationRequestPurpose.values:
        raise InvalidRequestItemError("Unknown Information Request purpose.")
    if not items:
        raise InvalidRequestItemError("An Information Request requires at least one item.")

    for item in items:
        if item.get("kind") not in RequestItemKind.values:
            raise InvalidRequestItemError("Unknown request item kind.")
        if item["kind"] in (RequestItemKind.FIELD_CORRECTION, RequestItemKind.CLARIFICATION):
            if item.get("field_code") not in ALLOWED_REQUEST_FIELD_CODES:
                raise InvalidRequestItemError(
                    f"{item.get('field_code')!r} is not an allowed field code."
                )
        elif item["kind"] == RequestItemKind.DOCUMENT_UPLOAD:
            if item.get("document_type") not in ALLOWED_REQUEST_DOCUMENT_TYPES:
                raise InvalidRequestItemError(
                    f"{item.get('document_type')!r} is not an allowed document type."
                )
            if item["document_type"] not in DocumentType.values:
                raise InvalidRequestItemError("Unknown document type.")

    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)
        next_sequence = (
            InformationRequest.objects.filter(registration=locked_registration).count() + 1
        )
        try:
            with transaction.atomic():
                information_request = InformationRequest.objects.create(
                    registration=locked_registration,
                    review_case=review_case,
                    sequence=next_sequence,
                    purpose=purpose,
                    message_en=message_en[:2000],
                    message_fr=message_fr[:2000],
                    message_ar=message_ar[:2000],
                    deadline_at=deadline_at,
                    created_by=created_by,
                    status=InformationRequestStatus.DRAFT,
                )
        except IntegrityError:
            raise ActiveInformationRequestExistsError(
                "This Registration already has an active Information Request."
            ) from None

        for item in items:
            RequestItem.objects.create(
                information_request=information_request,
                kind=item["kind"],
                field_code=item.get("field_code", ""),
                document_type=item.get("document_type", ""),
                is_required=item.get("is_required", True),
                instructions_en=item.get("instructions_en", "")[:500],
                instructions_fr=item.get("instructions_fr", "")[:500],
                instructions_ar=item.get("instructions_ar", "")[:500],
            )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(created_by, "pk", None),
                action_code=action_codes.REVIEW_INFORMATION_REQUEST_DRAFTED,
                target_type="InformationRequest",
                target_uuid=information_request.pk,
                event_edition_id=locked_registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return information_request


def send_information_request(
    *,
    information_request: InformationRequest,
    expected_version: int,
    actor,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> InformationRequest:
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration, then the request.
        registration = _lock_registration_of(InformationRequest, pk=information_request.pk)
        locked = _lock_versioned(
            InformationRequest, pk=information_request.pk, expected_version=expected_version
        )
        if locked.status != InformationRequestStatus.DRAFT:
            raise InvalidStateTransitionError(
                f"InformationRequest cannot be sent from {locked.status}."
            )
        _require_registration_open(registration)
        locked.status = InformationRequestStatus.SENT
        locked.sent_at = timezone.now()
        _bump_version(locked, extra_fields=["status", "sent_at"])
        _set_registration_status(
            locked.registration_id,
            public_status=RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
            internal_status=RegistrationInternalStatus.AWAITING_APPLICANT,
        )
        PersistentOutboxPublisher().enqueue(
            OutboxMessage(
                event_type="reviews.information_request_sent",
                aggregate_type="InformationRequest",
                aggregate_id=str(locked.pk),
                payload={"purpose": locked.purpose},
            )
        )
        _queue_registration_communication(
            registration=locked.registration,
            purpose_code=CommunicationPurpose.INFORMATION_REQUEST,
            context={
                "public_reference": locked.registration.public_reference,
                "event_name": locked.registration.event_edition.display_name(
                    locked.registration.preferred_language
                ),
            },
            idempotency_key=f"information-request:{locked.pk}",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_INFORMATION_REQUEST_SENT,
                target_type="InformationRequest",
                target_uuid=locked.pk,
                event_edition_id=locked.registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return locked


def cancel_information_request(
    *,
    information_request: InformationRequest,
    expected_version: int,
    actor,
    reason: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> InformationRequest:
    """Cancellation NEVER creates an automatic NOT_APPROVED decision (Phase 2
    Prompt 3 §7.1) -- it only closes the request and routes the case back
    to ordinary review."""
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration, then the request.
        registration = _lock_registration_of(InformationRequest, pk=information_request.pk)
        locked = _lock_versioned(
            InformationRequest, pk=information_request.pk, expected_version=expected_version
        )
        if locked.status not in InformationRequestStatus.active_statuses():
            raise InvalidStateTransitionError(
                f"InformationRequest cannot be cancelled from {locked.status}."
            )
        _require_registration_open(registration)
        locked.status = InformationRequestStatus.CANCELLED
        locked.cancelled_at = timezone.now()
        locked.cancellation_reason = reason[:300]
        _bump_version(locked, extra_fields=["status", "cancelled_at", "cancellation_reason"])
        _set_registration_status(
            locked.registration_id,
            public_status=RegistrationPublicStatus.UNDER_REVIEW,
            internal_status=RegistrationInternalStatus.REVIEW_IN_PROGRESS,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_INFORMATION_REQUEST_CANCELLED,
                target_type="InformationRequest",
                target_uuid=locked.pk,
                event_edition_id=locked.registration.event_edition_id,
                result="SUCCESS",
                reason_code=reason[:100],
                correlation_id=correlation_id,
            )
        )
    return locked


def close_information_request(
    *,
    information_request: InformationRequest,
    expected_version: int,
    actor,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> InformationRequest:
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        # Lock order (IDV-Q-C1): the Registration, then the request.
        _lock_registration_of(InformationRequest, pk=information_request.pk)
        locked = _lock_versioned(
            InformationRequest, pk=information_request.pk, expected_version=expected_version
        )
        if locked.status != InformationRequestStatus.SUBMITTED:
            raise InvalidStateTransitionError(
                f"InformationRequest cannot be closed from {locked.status}."
            )
        locked.status = InformationRequestStatus.CLOSED
        locked.closed_at = timezone.now()
        locked.closed_by = actor
        _bump_version(locked, extra_fields=["status", "closed_at", "closed_by"])
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_INFORMATION_REQUEST_CLOSED,
                target_type="InformationRequest",
                target_uuid=locked.pk,
                event_edition_id=locked.registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return locked


def save_information_response_draft(
    *, information_request: InformationRequest, person, answers: dict, correlation_id: str = ""
) -> InformationRequest:
    if information_request.registration.person_id != person.pk:
        raise RegistrationOwnershipError("This Information Request does not belong to this person.")
    if information_request.status not in (
        InformationRequestStatus.SENT,
        InformationRequestStatus.RESPONSE_IN_PROGRESS,
        InformationRequestStatus.OVERDUE,
    ):
        raise ValueError("This Information Request cannot accept a draft response right now.")
    with transaction.atomic():
        locked = InformationRequest.objects.select_for_update().get(pk=information_request.pk)
        if locked.registration.person_id != person.pk:
            raise RegistrationOwnershipError(
                "This Information Request does not belong to this person."
            )
        if locked.status not in (
            InformationRequestStatus.SENT,
            InformationRequestStatus.RESPONSE_IN_PROGRESS,
            InformationRequestStatus.OVERDUE,
        ):
            raise InvalidStateTransitionError(
                "This Information Request cannot accept a draft response right now."
            )
        locked.draft_snapshot_json = answers
        if locked.status == InformationRequestStatus.SENT:
            locked.status = InformationRequestStatus.RESPONSE_IN_PROGRESS
        _bump_version(locked, extra_fields=["draft_snapshot_json", "status"])
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=person.pk,
                action_code=action_codes.REVIEW_INFORMATION_RESPONSE_DRAFT_SAVED,
                target_type="InformationRequest",
                target_uuid=locked.pk,
                event_edition_id=locked.registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return locked


def _canonical_response_hash(snapshot: dict) -> str:
    import json

    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_information_response(
    *,
    information_request: InformationRequest,
    person,
    answers: list[dict],
    correlation_id: str = "",
) -> InformationResponse:
    """Create the immutable response snapshot (Phase 2 Prompt 3 §7.3).

    `answers` is a list of `{"request_item_id": UUID, "value": str | None,
    "document": Document | None}`, one entry per requested item this
    participant is answering. Idempotent: a retried submission with the
    SAME `idempotency_key` (derived from the request's own id -- a
    Registration Context has at most one response per request) returns the
    existing response rather than raising.
    """
    if information_request.registration.person_id != person.pk:
        raise RegistrationOwnershipError("This Information Request does not belong to this person.")

    idempotency_key = f"information-response:{information_request.pk}"
    existing = InformationResponse.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    request_items_by_id = {item.pk: item for item in information_request.items.all()}
    answers_by_item_id: dict = {}
    for answer in answers:
        request_item_id = answer.get("request_item_id")
        if request_item_id not in request_items_by_id:
            raise InvalidInformationResponseError(
                "A response item does not belong to this Information Request."
            )
        if request_item_id in answers_by_item_id:
            raise InvalidInformationResponseError("A requested item was answered more than once.")
        answers_by_item_id[request_item_id] = answer

    for request_item_id, request_item in request_items_by_id.items():
        answer = answers_by_item_id.get(request_item_id, {})
        value = (answer.get("value") or "").strip()
        document = answer.get("document")
        if request_item.is_required:
            if request_item.kind == RequestItemKind.DOCUMENT_UPLOAD and document is None:
                raise InvalidInformationResponseError("A required document is missing.")
            if request_item.kind != RequestItemKind.DOCUMENT_UPLOAD and not value:
                raise InvalidInformationResponseError("A required response value is missing.")
        if document is not None:
            expected_purpose = f"REQUESTED_EVIDENCE:{request_item.pk}"
            if (
                request_item.kind != RequestItemKind.DOCUMENT_UPLOAD
                or document.registration_id != information_request.registration_id
                or document.person_id != person.pk
                or document.purpose_code != expected_purpose
            ):
                raise DocumentPurposeMismatchError(
                    "The uploaded document does not match this requested item."
                )
    snapshot: dict[str, dict[str, str | int]] = {}
    touches_identity = False
    with transaction.atomic():
        # Lock the InformationRequest row FIRST (Phase 2 Prompt 3 §12) --
        # this serializes concurrent submissions for the SAME request, so
        # the idempotency re-check just below is race-free: a second
        # concurrent caller blocks here until the first commits, then sees
        # the already-created response and returns it, rather than racing
        # `InformationResponse.objects.create(...)` and hitting a raw
        # `IntegrityError` on the unique `idempotency_key`/one-to-one
        # `information_request` columns.
        #
        # IDV-Q-C1: the Registration row is locked before the request (the
        # lock order of every command that ends review work), since this
        # submission also changes the Registration's status.
        registration = _lock_registration_of(InformationRequest, pk=information_request.pk)
        locked_request = InformationRequest.objects.select_for_update().get(
            pk=information_request.pk
        )
        existing = InformationResponse.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
        if locked_request.status not in (
            InformationRequestStatus.SENT,
            InformationRequestStatus.RESPONSE_IN_PROGRESS,
            InformationRequestStatus.OVERDUE,
        ):
            raise ValueError("This Information Request is not currently accepting a response.")
        if registration.public_status in CLOSED_PUBLIC_STATUSES:
            raise ValueError("This Information Request is not currently accepting a response.")

        response = InformationResponse.objects.create(
            information_request=locked_request,
            submitted_by_person=person,
            snapshot_json={},
            snapshot_hash="",
            submitted_at=timezone.now(),
            idempotency_key=idempotency_key,
        )
        key_provider = get_key_provider()
        integrity_key_version = key_provider.current_blind_index_key_version()
        for request_item_id, answer in answers_by_item_id.items():
            request_item = request_items_by_id[request_item_id]
            document = answer.get("document")
            value = (answer.get("value") or "").strip()
            if not value and document is None:
                continue
            ResponseItem.objects.create(
                response=response,
                request_item=request_item,
                value_encrypted=value[:2000],
                document=document,
            )
            if document is not None:
                protected_value = document.stored_object.sha256
                kind = "document"
            else:
                protected_value = value
                kind = "value"
            snapshot[str(request_item.pk)] = {
                "kind": kind,
                "content_hmac": compute_blind_index(
                    f"REVIEW_RESPONSE:{information_request.pk}:{request_item.pk}:"
                    f"{kind}:{protected_value}",
                    version=integrity_key_version,
                    provider=key_provider,
                ),
                "key_version": integrity_key_version,
            }
            if request_item.field_code in IDENTITY_SENSITIVE_FIELD_CODES:
                touches_identity = True

        response.snapshot_json = snapshot
        response.snapshot_hash = _canonical_response_hash(snapshot)
        response.save(update_fields=["snapshot_json", "snapshot_hash"])

        locked_request.status = InformationRequestStatus.SUBMITTED
        locked_request.draft_snapshot_json = {}
        _bump_version(locked_request, extra_fields=["status", "draft_snapshot_json"])

        new_internal_status = (
            RegistrationInternalStatus.VERIFICATION_PENDING
            if touches_identity
            else RegistrationInternalStatus.REVIEW_IN_PROGRESS
        )
        _set_registration_status(
            information_request.registration_id,
            public_status=RegistrationPublicStatus.SUBMITTED,
            internal_status=new_internal_status,
        )
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=person.pk,
                action_code=action_codes.REVIEW_INFORMATION_RESPONSE_SUBMITTED,
                target_type="InformationRequest",
                target_uuid=information_request.pk,
                event_edition_id=information_request.registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        _queue_registration_communication(
            registration=information_request.registration,
            purpose_code=CommunicationPurpose.INFORMATION_RESPONSE_CONFIRMATION,
            context={
                "public_reference": information_request.registration.public_reference,
                "event_name": information_request.registration.event_edition.display_name(
                    information_request.registration.preferred_language
                ),
            },
            idempotency_key=f"information-response-confirmation:{response.pk}",
        )
    return response


# ---------------------------------------------------------------------------
# Decision history
# ---------------------------------------------------------------------------


def record_not_approved_decision(
    *,
    registration: Registration,
    expected_version: int,
    internal_reason_code: str,
    decided_by,
    internal_note: str = "",
    participant_reason_code: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> RegistrationDecision:
    if not internal_reason_code:
        raise ValueError("A NOT_APPROVED decision requires an internal reason code.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_registration = _lock_versioned(
            Registration, pk=registration.pk, expected_version=expected_version
        )
        current = RegistrationDecision.objects.filter(
            registration=locked_registration, is_current=True
        ).first()
        if (
            locked_registration.public_status == RegistrationPublicStatus.NOT_APPROVED
            and current is not None
            and current.outcome == RegistrationDecisionOutcome.NOT_APPROVED
        ):
            return current
        if locked_registration.public_status not in (
            RegistrationPublicStatus.SUBMITTED,
            RegistrationPublicStatus.UNDER_REVIEW,
        ):
            raise InvalidStateTransitionError(
                f"A NOT_APPROVED decision cannot be recorded from "
                f"{locked_registration.public_status}."
            )
        # Reopening clears `is_current`, but the next decision must still
        # point back to the latest historical decision so the supersession
        # chain is never broken across review rounds.
        previous = (
            RegistrationDecision.objects.filter(registration=locked_registration)
            .order_by("-sequence")
            .first()
        )
        if previous is not None:
            RegistrationDecision.objects.filter(
                registration=locked_registration, is_current=True
            ).update(is_current=False)
        next_sequence = (
            RegistrationDecision.objects.filter(registration=locked_registration).count() + 1
        )
        decision = RegistrationDecision.objects.create(
            registration=locked_registration,
            sequence=next_sequence,
            outcome=RegistrationDecisionOutcome.NOT_APPROVED,
            internal_reason_code=internal_reason_code[:64],
            internal_note_encrypted=internal_note[:2000],
            participant_reason_code=participant_reason_code[:64],
            is_current=True,
            supersedes=previous,
            decided_by=decided_by,
            decided_at=timezone.now(),
            event_edition_id=locked_registration.event_edition_id,
            organization_id=locked_registration.source_organization_id,
        )
        locked_registration.public_status = RegistrationPublicStatus.NOT_APPROVED
        locked_registration.internal_status = RegistrationInternalStatus.CLOSED
        _bump_version(locked_registration, extra_fields=["public_status", "internal_status"])
        _terminate_open_review_work(
            locked_registration,
            case_status=ReviewCaseStatus.COMPLETED,
            request_reason="registration_not_approved",
        )
        _close_identity_work(
            locked_registration, actor_user=decided_by, correlation_id=correlation_id
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(decided_by, "pk", None),
                action_code=action_codes.REVIEW_DECISION_RECORDED,
                target_type="Registration",
                target_uuid=locked_registration.pk,
                event_edition_id=locked_registration.event_edition_id,
                result="SUCCESS",
                # Bounded, non-sensitive: the reason CODE only, never the
                # free-text internal note (Phase 2 Prompt 3 §13).
                after_summary={
                    "outcome": RegistrationDecisionOutcome.NOT_APPROVED,
                    "internal_reason_code": internal_reason_code[:64],
                },
                correlation_id=correlation_id,
            )
        )
        _queue_registration_communication(
            registration=locked_registration,
            purpose_code=CommunicationPurpose.DECISION_STATUS,
            context={
                "public_reference": locked_registration.public_reference,
                "event_name": locked_registration.event_edition.display_name(
                    locked_registration.preferred_language
                ),
                "decision_label": locked_registration.get_public_status_display(),
            },
            idempotency_key=f"decision-status:{decision.pk}",
        )
    return decision


def record_approved_decision(
    *,
    registration: Registration,
    expected_version: int,
    decided_by,
    attendance_category: str | None,
    participant_reason_code: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> RegistrationDecision:
    """Prompt 3/Prompt 4 boundary: approval requires Prompt 4's
    Participant Role, Badge Type, and Access Profile assignments to already exist on this
    Registration. Fails atomically -- BEFORE any write -- with
    `ApprovalRequiresAssignmentsError` when they are absent; no placeholder
    assignment, no default assignment, no approval bypass is ever created.
    Once they exist, this behaves exactly like `record_not_approved_decision`:
    locks the Registration by `expected_version`, supersedes any prior
    decision, and records the new current one.

    Owner decision IDV-Q1 (2026-10-02): approval also requires a current,
    valid, verified identity for this registration
    (`apps.people.selectors.clearance`), checked here under the Registration
    lock with the identity case locked too, so an identity command cannot
    change it before the approval commits. Otherwise it fails atomically,
    before any write, with `ApprovalRequiresVerifiedIdentityError` (and a
    DENIED audit event). The identity decision itself stays in the identity
    history; the approval audit records only its status and source.

    Attendance (`apps.accreditation.attendance`): `attendance_category` is
    the explicit choice of all three conference days or only the two days
    after the opening day. There is no default; a missing or unknown value
    is refused (after the version, state, assignment and identity checks, so
    their refusals keep precedence) and nothing is written. The entitlement
    is recorded in the same
    transaction as the decision, after the policy lock for an opening-day
    choice, and a full opening day refuses the whole approval
    (`OpeningDayCapacityReachedError`, audited) -- it is never downgraded.
    The decision notification states the authorized days.
    """
    from apps.accreditation.attendance import (
        AttendanceError,
        OpeningDayCapacityReachedError,
        _record_capacity_refusal,
    )
    from apps.accreditation.services import has_required_assignments_for_approval

    audit_recorder = audit_recorder or PersistentAuditRecorder()
    try:
        return _record_approved_decision_atomically(
            registration=registration,
            expected_version=expected_version,
            decided_by=decided_by,
            attendance_category=attendance_category,
            participant_reason_code=participant_reason_code,
            audit_recorder=audit_recorder,
            correlation_id=correlation_id,
            assignment_predicate=has_required_assignments_for_approval,
        )
    except OpeningDayCapacityReachedError:
        from apps.accreditation.attendance import policy_for

        _record_capacity_refusal(
            recorder=audit_recorder,
            actor=decided_by,
            registration=registration,
            policy=policy_for(registration.event_edition_id),
            correlation_id=correlation_id,
        )
        raise
    except AttendanceError as refusal:
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(decided_by, "pk", None),
                action_code=action_codes.REVIEW_APPROVAL_BLOCKED_ATTENDANCE,
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=registration.event_edition_id,
                result="DENIED",
                reason_code=refusal.code,
                correlation_id=correlation_id,
            )
        )
        raise
    except ApprovalRequiresVerifiedIdentityError as refusal:
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(decided_by, "pk", None),
                action_code=action_codes.REVIEW_APPROVAL_BLOCKED_IDENTITY,
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=registration.event_edition_id,
                result="DENIED",
                reason_code=refusal.code,
                correlation_id=correlation_id,
            )
        )
        raise
    except ApprovalRequiresAssignmentsError:
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(decided_by, "pk", None),
                action_code=action_codes.REVIEW_APPROVAL_BLOCKED_PENDING_ASSIGNMENTS,
                target_type="Registration",
                target_uuid=registration.pk,
                result="DENIED",
                correlation_id=correlation_id,
            )
        )
        raise ApprovalRequiresAssignmentsError(
            "An APPROVED decision requires a current Participant Role assignment "
            "and current Badge Type and Access Profile assignments on this "
            "Registration Context. "
            "No approval bypass is authorized."
        ) from None


def _record_approved_decision_atomically(
    *,
    registration: Registration,
    expected_version: int,
    decided_by,
    attendance_category: str,
    participant_reason_code: str,
    audit_recorder: AuditRecorder,
    correlation_id: str,
    assignment_predicate,
) -> RegistrationDecision:
    from apps.accreditation import attendance

    with transaction.atomic():
        locked_registration = _lock_versioned(
            Registration, pk=registration.pk, expected_version=expected_version
        )
        current = RegistrationDecision.objects.filter(
            registration=locked_registration, is_current=True
        ).first()
        if (
            locked_registration.public_status == RegistrationPublicStatus.APPROVED
            and current is not None
            and current.outcome == RegistrationDecisionOutcome.APPROVED
        ):
            return current
        if locked_registration.public_status not in (
            RegistrationPublicStatus.SUBMITTED,
            RegistrationPublicStatus.UNDER_REVIEW,
        ):
            raise InvalidStateTransitionError(
                f"An APPROVED decision cannot be recorded from {locked_registration.public_status}."
            )
        if not assignment_predicate(locked_registration):
            raise ApprovalRequiresAssignmentsError
        # IDV-Q1: the identity case is locked after the Registration (lock
        # order) and must be cleared now.
        from apps.people.selectors.clearance import identity_clearance

        clearance = identity_clearance(locked_registration.pk, lock=True)
        if not clearance.cleared:
            raise ApprovalRequiresVerifiedIdentityError(clearance.code)
        previous = (
            RegistrationDecision.objects.filter(registration=locked_registration)
            .order_by("-sequence")
            .first()
        )
        if previous is not None:
            RegistrationDecision.objects.filter(
                registration=locked_registration, is_current=True
            ).update(is_current=False)
        next_sequence = (
            RegistrationDecision.objects.filter(registration=locked_registration).count() + 1
        )
        decision = RegistrationDecision.objects.create(
            registration=locked_registration,
            sequence=next_sequence,
            outcome=RegistrationDecisionOutcome.APPROVED,
            internal_reason_code="ELIGIBLE",
            participant_reason_code=participant_reason_code[:64],
            is_current=True,
            supersedes=previous,
            decided_by=decided_by,
            decided_at=timezone.now(),
            event_edition_id=locked_registration.event_edition_id,
            organization_id=locked_registration.source_organization_id,
        )
        # Attendance: after the Registration (and identity) locks, the policy
        # lock for an opening-day choice; refused when that day is full.
        entitlement = attendance.grant_at_approval(
            registration=locked_registration,
            category=attendance_category,
            decided_by=decided_by,
            decision_id=decision.pk,
            audit_recorder=audit_recorder,
            correlation_id=correlation_id,
        )
        locked_registration.public_status = RegistrationPublicStatus.APPROVED
        locked_registration.internal_status = RegistrationInternalStatus.CLOSED
        _bump_version(locked_registration, extra_fields=["public_status", "internal_status"])
        _terminate_open_review_work(
            locked_registration,
            case_status=ReviewCaseStatus.COMPLETED,
            request_reason="registration_approved",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(decided_by, "pk", None),
                action_code=action_codes.REVIEW_DECISION_RECORDED,
                target_type="Registration",
                target_uuid=locked_registration.pk,
                event_edition_id=locked_registration.event_edition_id,
                result="SUCCESS",
                after_summary={
                    "outcome": RegistrationDecisionOutcome.APPROVED,
                    "identity_status": clearance.status,
                    "identity_source": clearance.source,
                    "attendance_category": entitlement.category,
                },
                correlation_id=correlation_id,
            )
        )
        # The decision notification states the authorized days (same
        # idempotency key as the generic decision message it replaces). The
        # policy row is still locked by `grant_at_approval` in this
        # transaction, so these are exactly the days the grant accepted.
        attendance.queue_attendance_notification(
            registration=locked_registration,
            entitlement=entitlement,
            policy=attendance.policy_for(locked_registration.event_edition_id),
            purpose_code=CommunicationPurpose.APPROVAL_ATTENDANCE,
            idempotency_key=f"decision-status:{decision.pk}",
        )
    return decision


# ---------------------------------------------------------------------------
# Final identity rejection: participation outcome (owner decision IDV-Q2)
# ---------------------------------------------------------------------------

#: The participation decision codes a final identity rejection records.
IDENTITY_REJECTION_INTERNAL_REASON = "IDENTITY_REJECTED"
IDENTITY_REJECTION_PARTICIPANT_REASON = "IDENTITY_NOT_VERIFIED"


def record_identity_rejection_outcome(
    *,
    registration: Registration,
    decided_by,
    identity_decision_id,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> RegistrationDecision:
    """The participation consequence of a final identity rejection (IDV-Q2).

    MUST run inside the identity rejection's transaction, with `registration`
    already locked by it (lock order: Person, Registration, identity case,
    then the review cases and information requests, as in
    `record_not_approved_decision`).

    * A NOT_APPROVED participation decision is recorded (internal reason
      `IDENTITY_REJECTED`, participant reason `IDENTITY_NOT_VERIFIED`). It
      supersedes the current one, an earlier approval included, so the
      participant's status, the staff view and every eligibility check agree.
    * Open review work ends, as for any NOT_APPROVED decision.
    * The context is marked non-current. That releases its deduplication key,
      so the participant can register again from the same account. Nothing
      is deleted, and the rejected registration keeps its history.
    * One notification is queued with the dedicated identity-rejection
      template (idempotency key bound to the identity decision): it says
      that the registration was rejected and how to register again, and
      names no reason, result, identifier or note.

    The identity history (the REJECT `IdentityDecision`) and the participation
    history (this `RegistrationDecision`) stay separate records.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    if registration.public_status not in (
        RegistrationPublicStatus.SUBMITTED,
        RegistrationPublicStatus.UNDER_REVIEW,
        RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
        RegistrationPublicStatus.APPROVED,
    ):
        raise InvalidStateTransitionError(
            f"An identity rejection cannot close a {registration.public_status} registration."
        )
    previous = (
        RegistrationDecision.objects.filter(registration=registration).order_by("-sequence").first()
    )
    if previous is not None:
        RegistrationDecision.objects.filter(registration=registration, is_current=True).update(
            is_current=False
        )
    next_sequence = RegistrationDecision.objects.filter(registration=registration).count() + 1
    decision = RegistrationDecision.objects.create(
        registration=registration,
        sequence=next_sequence,
        outcome=RegistrationDecisionOutcome.NOT_APPROVED,
        internal_reason_code=IDENTITY_REJECTION_INTERNAL_REASON,
        participant_reason_code=IDENTITY_REJECTION_PARTICIPANT_REASON,
        is_current=True,
        supersedes=previous,
        decided_by=decided_by,
        decided_at=timezone.now(),
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
    )
    previous_status = registration.public_status
    registration.public_status = RegistrationPublicStatus.NOT_APPROVED
    registration.internal_status = RegistrationInternalStatus.CLOSED
    registration.is_current_context = False
    _bump_version(
        registration, extra_fields=["public_status", "internal_status", "is_current_context"]
    )
    _terminate_open_review_work(
        registration,
        case_status=ReviewCaseStatus.COMPLETED,
        request_reason="identity_rejected",
    )
    audit_recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(decided_by, "pk", None),
            action_code=action_codes.REVIEW_DECISION_RECORDED,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            result="SUCCESS",
            before_summary={"public_status": previous_status},
            after_summary={
                "outcome": RegistrationDecisionOutcome.NOT_APPROVED,
                "internal_reason_code": IDENTITY_REJECTION_INTERNAL_REASON,
                "origin": "identity_rejection",
                "identity_decision": str(identity_decision_id),
            },
            correlation_id=correlation_id,
        )
    )
    _queue_registration_communication(
        registration=registration,
        purpose_code=CommunicationPurpose.IDENTITY_REJECTION,
        context={
            "public_reference": registration.public_reference,
            "event_name": registration.event_edition.display_name(registration.preferred_language),
        },
        idempotency_key=f"identity-rejection:{identity_decision_id}",
    )
    return decision


# ---------------------------------------------------------------------------
# Reopening, withdrawal, operational cancellation
# ---------------------------------------------------------------------------

REOPENABLE_PUBLIC_STATUSES = (
    RegistrationPublicStatus.APPROVED,
    RegistrationPublicStatus.NOT_APPROVED,
    RegistrationPublicStatus.WITHDRAWN,
)


def reopen_registration(
    *,
    registration: Registration,
    expected_version: int,
    reason: str,
    actor,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ReviewCase:
    if not reason:
        raise ValueError("Reopening requires a reason.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked = _lock_versioned(
            Registration, pk=registration.pk, expected_version=expected_version
        )
        if locked.public_status not in REOPENABLE_PUBLIC_STATUSES:
            raise ValueError(f"A {locked.public_status} registration cannot be reopened.")
        from apps.people.models import IdentityStatus, IdentityVerification

        if IdentityVerification.objects.filter(
            registration=locked, status=IdentityStatus.REJECTED
        ).exists():
            # IDV-Q2: a final identity rejection is final for this
            # registration; the participant registers again instead.
            raise ValueError(
                "This registration's identity was finally rejected, so it cannot be reopened. "
                "The participant can register again from the same account."
            )
        if not locked.is_current_context:
            # Owner correction (2026-10-04): the participant registered again
            # after withdrawing, which released this context. It is history;
            # reopening it would make a second live registration.
            raise ValueError(
                "This registration was replaced by a newer registration of the same "
                "participant, so it cannot be reopened."
            )
        RegistrationDecision.objects.filter(registration=locked, is_current=True).update(
            is_current=False
        )
        locked.public_status = RegistrationPublicStatus.UNDER_REVIEW
        locked.internal_status = RegistrationInternalStatus.REVIEW_IN_PROGRESS
        locked.withdrawn_at = None
        locked.cancelled_at = None
        _bump_version(
            locked,
            extra_fields=["public_status", "internal_status", "withdrawn_at", "cancelled_at"],
        )
        new_case = open_review_case(
            registration=locked,
            case_type=ReviewCaseType.STANDARD,
            queue_code="GENERAL",
            actor=actor,
            audit_recorder=audit_recorder,
            correlation_id=correlation_id,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_REGISTRATION_REOPENED,
                target_type="Registration",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                reason_code=reason[:100],
                correlation_id=correlation_id,
            )
        )
    return new_case


def withdraw_registration(
    *,
    registration: Registration,
    person,
    expected_version: int,
    correlation_id: str = "",
) -> Registration:
    if registration.person_id != person.pk:
        raise RegistrationOwnershipError(
            "This Registration Context does not belong to this person."
        )
    with transaction.atomic():
        locked = _lock_versioned(
            Registration, pk=registration.pk, expected_version=expected_version
        )
        if locked.public_status == RegistrationPublicStatus.WITHDRAWN:
            return locked
        from apps.registrations.services import is_identity_rejected

        if is_identity_rejected(locked):
            # IDV-Q2: a final identity rejection is terminal. The workspace
            # offers no withdrawal for it; a direct request changes nothing.
            raise InvalidStateTransitionError(
                "A registration closed by a final identity rejection cannot be withdrawn."
            )
        locked.public_status = RegistrationPublicStatus.WITHDRAWN
        locked.internal_status = RegistrationInternalStatus.CLOSED
        locked.withdrawn_at = timezone.now()
        _bump_version(locked, extra_fields=["public_status", "internal_status", "withdrawn_at"])
        _terminate_open_review_work(
            locked,
            case_status=ReviewCaseStatus.CANCELLED,
            request_reason="participant_withdrew_registration",
        )
        _close_identity_work(locked, actor_person=person, correlation_id=correlation_id)
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=person.pk,
                action_code=action_codes.REVIEW_PARTICIPANT_WITHDRAWAL,
                target_type="Registration",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return locked


def cancel_registration_operationally(
    *,
    registration: Registration,
    expected_version: int,
    reason: str,
    actor,
    correlation_id: str = "",
) -> Registration:
    if not reason:
        raise ValueError("Operational cancellation requires a reason.")
    with transaction.atomic():
        locked = _lock_versioned(
            Registration, pk=registration.pk, expected_version=expected_version
        )
        locked.public_status = RegistrationPublicStatus.WITHDRAWN
        locked.internal_status = RegistrationInternalStatus.CLOSED
        locked.cancelled_at = timezone.now()
        _bump_version(locked, extra_fields=["public_status", "internal_status", "cancelled_at"])
        _terminate_open_review_work(
            locked,
            case_status=ReviewCaseStatus.CANCELLED,
            request_reason="registration_cancelled_operationally",
        )
        _close_identity_work(locked, actor_user=actor, correlation_id=correlation_id)
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REVIEW_OPERATIONAL_CANCELLATION,
                target_type="Registration",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                reason_code=reason[:100],
                correlation_id=correlation_id,
            )
        )
    return locked
