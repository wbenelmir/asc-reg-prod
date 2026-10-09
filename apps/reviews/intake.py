"""Entry of verified registrations into participation review.

A submitted registration whose identity has just been verified (by the
Ministry service, manually, by the Algerian staff-assisted exception or on
the NIN-exemption route) must reach the participation Review queue. Identity
verification and participation review stay separate operations: this module
never approves participation, never issues an invitation, a pass or a badge
and never queues a communication. An authorized participation decision
(`apps.reviews.services.record_approved_decision` /
`record_not_approved_decision`) is still required.

One shared rule (`entry_problem`) decides eligibility for:

* the identity success paths (`enqueue_for_participation_review`, called
  inside the identity transaction, so identity success and case creation
  commit or roll back together);
* the backfill of registrations verified before this link existed
  (`run_backfill`, `manage.py backfill_review_queue`);
* the decision workbook (`apps.reviews.workbook`), which only offers rows this
  rule still accepts.

Eligible (an explicit allowlist, everything else is excluded):

* public status SUBMITTED or UNDER_REVIEW, with its INITIAL submission
  evidence, and the current context of the participant;
* internal status PENDING_ASSIGNMENT, ASSIGNED, VERIFICATION_PENDING,
  REVIEW_IN_PROGRESS or QUALIFICATION_COMPLETE;
* no current participation decision;
* no active information request or correction (ADDITIONAL_INFORMATION_REQUIRED,
  AWAITING_APPLICANT);
* no duplicate review (DUPLICATE_REVIEW) and no open case other than the
  STANDARD / GENERAL one (duplicate, restricted, specialist, identity...);
* a cleared identity (`apps.people.selectors.clearance`, owner decision
  IDV-Q1): verified, current identifier, document not expired, accepted source.

For an eligible registration the one open STANDARD / GENERAL case is reused,
or created with `open_review_case`; a SUBMITTED registration moves to
UNDER_REVIEW and a VERIFICATION_PENDING internal status follows the case
(QUEUED -> PENDING_ASSIGNMENT, ASSIGNED -> ASSIGNED, started -> REVIEW_IN_PROGRESS).
Nothing else of the registration is written. A registration with more than one
open STANDARD case (not produced by any current path) is reported and left
unchanged; no uniqueness constraint is added, because every creator of a
STANDARD case holds the Registration row lock (`reopen_registration` only
for a closed registration, this module only for an open one).

Lock order: the Registration row (`FOR NO KEY UPDATE`, which the identity
worker and `verify_identity_manually` already hold), then the identity case
(`identity_clearance(lock=True)`), then the review cases. No network call is
made, and the backfill uses one short transaction per registration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSubmission,
    RegistrationSubmissionKind,
)
from apps.reviews.models import (
    InformationRequest,
    InformationRequestStatus,
    RegistrationDecision,
    ReviewCase,
    ReviewCaseStatus,
    ReviewCaseType,
    ReviewQueueCode,
)

logger = logging.getLogger("asc2026.reviews")

#: Public statuses a registration may have while it awaits a participation decision.
ENTRY_PUBLIC_STATUSES = (
    RegistrationPublicStatus.SUBMITTED,
    RegistrationPublicStatus.UNDER_REVIEW,
)

#: Internal statuses compatible with an ordinary participation review.
ENTRY_INTERNAL_STATUSES = (
    RegistrationInternalStatus.PENDING_ASSIGNMENT,
    RegistrationInternalStatus.ASSIGNED,
    RegistrationInternalStatus.VERIFICATION_PENDING,
    RegistrationInternalStatus.REVIEW_IN_PROGRESS,
    RegistrationInternalStatus.QUALIFICATION_COMPLETE,
)

#: What a VERIFICATION_PENDING registration becomes, by the status of its case.
_INTERNAL_AFTER_VERIFICATION = {
    ReviewCaseStatus.QUEUED: RegistrationInternalStatus.PENDING_ASSIGNMENT,
    ReviewCaseStatus.ASSIGNED: RegistrationInternalStatus.ASSIGNED,
    ReviewCaseStatus.IN_PROGRESS: RegistrationInternalStatus.REVIEW_IN_PROGRESS,
    ReviewCaseStatus.WAITING: RegistrationInternalStatus.REVIEW_IN_PROGRESS,
}


class EntryExclusion:
    """Why a registration does not enter participation review (codes only)."""

    NOT_SUBMITTED = "NOT_SUBMITTED"
    WITHDRAWN = "WITHDRAWN"
    FINAL_DECISION = "FINAL_DECISION"
    NOT_CURRENT_CONTEXT = "NOT_CURRENT_CONTEXT"
    CORRECTION_OR_INFORMATION_REQUESTED = "CORRECTION_OR_INFORMATION_REQUESTED"
    UNEXPECTED_INTERNAL_STATUS = "UNEXPECTED_INTERNAL_STATUS"
    DUPLICATE_REVIEW = "DUPLICATE_REVIEW"
    SPECIAL_REVIEW_OPEN = "SPECIAL_REVIEW_OPEN"
    MULTIPLE_OPEN_STANDARD_CASES = "MULTIPLE_OPEN_STANDARD_CASES"
    # Identity: the codes of `apps.people.selectors.clearance.ClearanceCode`
    # (NO_IDENTITY_CASE, IDENTITY_NOT_VERIFIED, IDENTITY_REJECTED, ...).


class EntryOutcome:
    CREATED = "CREATED"
    EXISTING_CASE = "EXISTING_CASE"
    EXCLUDED = "EXCLUDED"


class EntryTrigger:
    IDENTITY_API_VERIFIED = "IDENTITY_API_VERIFIED"
    IDENTITY_MANUALLY_VERIFIED = "IDENTITY_MANUALLY_VERIFIED"
    BACKFILL = "BACKFILL"


@dataclass(frozen=True)
class EntryResult:
    outcome: str
    reason: str = ""
    case_id: object = None
    #: True when the registration's public or internal status changed.
    transitioned: bool = False


def _open_cases(registration_id, *, lock: bool):
    queryset = ReviewCase.objects.filter(
        registration_id=registration_id, status__in=ReviewCaseStatus.open_statuses()
    ).order_by("opened_at", "pk")
    if lock:
        queryset = queryset.select_for_update(no_key=True)
    return list(queryset)


def _is_standard(case) -> bool:
    return case.case_type == ReviewCaseType.STANDARD and case.queue_code == ReviewQueueCode.GENERAL


@dataclass(frozen=True)
class EntryFacts:
    """What the entry rule needs besides the Registration row itself."""

    has_initial_submission: bool
    has_current_decision: bool
    has_active_information_request: bool
    #: `apps.people.selectors.clearance.ClearanceCode` of the identity case.
    clearance_code: str
    identity_status: str = ""
    identity_version: int | None = None
    #: Open review cases, oldest first.
    open_cases: tuple = ()


def evaluate_entry(registration: Registration, facts: EntryFacts) -> str:
    """The ONE entry rule: the exclusion code for `registration`, "" when eligible.

    The checks run in a fixed order so reports are stable. Shared by the
    identity success paths, the backfill and the decision workbook.
    """
    from apps.people.selectors.clearance import ClearanceCode

    status = registration.public_status
    if status == RegistrationPublicStatus.DRAFT:
        return EntryExclusion.NOT_SUBMITTED
    if status == RegistrationPublicStatus.WITHDRAWN:
        return EntryExclusion.WITHDRAWN
    if status in (RegistrationPublicStatus.APPROVED, RegistrationPublicStatus.NOT_APPROVED):
        return EntryExclusion.FINAL_DECISION
    if status == RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED:
        return EntryExclusion.CORRECTION_OR_INFORMATION_REQUESTED
    if status not in ENTRY_PUBLIC_STATUSES:  # pragma: no cover - every status is listed above
        return EntryExclusion.UNEXPECTED_INTERNAL_STATUS
    if registration.submitted_at is None or not facts.has_initial_submission:
        return EntryExclusion.NOT_SUBMITTED
    if facts.has_current_decision:
        return EntryExclusion.FINAL_DECISION
    if not registration.is_current_context:
        return EntryExclusion.NOT_CURRENT_CONTEXT
    if (
        registration.internal_status == RegistrationInternalStatus.AWAITING_APPLICANT
        or facts.has_active_information_request
    ):
        return EntryExclusion.CORRECTION_OR_INFORMATION_REQUESTED
    if registration.internal_status == RegistrationInternalStatus.DUPLICATE_REVIEW:
        return EntryExclusion.DUPLICATE_REVIEW
    if registration.internal_status not in ENTRY_INTERNAL_STATUSES:
        return EntryExclusion.UNEXPECTED_INTERNAL_STATUS
    if facts.clearance_code != ClearanceCode.CLEARED:
        return facts.clearance_code
    if any(not _is_standard(case) for case in facts.open_cases):
        return EntryExclusion.SPECIAL_REVIEW_OPEN
    if len(facts.open_cases) > 1:
        return EntryExclusion.MULTIPLE_OPEN_STANDARD_CASES
    return ""


def entry_facts(registration: Registration, *, lock: bool = False) -> EntryFacts:
    """Load the facts of one registration. With `lock=True` the caller already
    holds the Registration row; the identity case, then the open review cases,
    are locked after it (lock order, as in `identity_clearance(lock=True)`)."""
    from apps.people.models import IdentityVerification
    from apps.people.selectors.clearance import evaluate_clearance

    verifications = IdentityVerification.objects.select_related(
        "current_revision__identifier"
    ).filter(registration_id=registration.pk)
    if lock:
        verifications = verifications.select_for_update(no_key=True, of=("self",))
    verification = verifications.first()
    return EntryFacts(
        has_initial_submission=RegistrationSubmission.objects.filter(
            registration_id=registration.pk, submission_kind=RegistrationSubmissionKind.INITIAL
        ).exists(),
        has_current_decision=RegistrationDecision.objects.filter(
            registration_id=registration.pk, is_current=True
        ).exists(),
        has_active_information_request=InformationRequest.objects.filter(
            registration_id=registration.pk,
            status__in=InformationRequestStatus.active_statuses(),
        ).exists(),
        clearance_code=evaluate_clearance(verification).code,
        identity_status=getattr(verification, "status", ""),
        identity_version=getattr(verification, "version", None),
        open_cases=tuple(_open_cases(registration.pk, lock=lock)),
    )


def bulk_entry_facts(registrations) -> dict:
    """`entry_facts` for many registrations in a fixed number of queries
    (read only, no lock): registration id -> EntryFacts."""
    from apps.people.models import IdentityVerification
    from apps.people.selectors.clearance import evaluate_clearance

    ids = [registration.pk for registration in registrations]
    if not ids:
        return {}
    initial = set(
        RegistrationSubmission.objects.filter(
            registration_id__in=ids, submission_kind=RegistrationSubmissionKind.INITIAL
        ).values_list("registration_id", flat=True)
    )
    decided = set(
        RegistrationDecision.objects.filter(registration_id__in=ids, is_current=True).values_list(
            "registration_id", flat=True
        )
    )
    requested = set(
        InformationRequest.objects.filter(
            registration_id__in=ids, status__in=InformationRequestStatus.active_statuses()
        ).values_list("registration_id", flat=True)
    )
    verifications = {
        verification.registration_id: verification
        for verification in IdentityVerification.objects.select_related(
            "current_revision__identifier"
        ).filter(registration_id__in=ids)
    }
    cases: dict = {}
    for case in ReviewCase.objects.filter(
        registration_id__in=ids, status__in=ReviewCaseStatus.open_statuses()
    ).order_by("opened_at", "pk"):
        cases.setdefault(case.registration_id, []).append(case)
    facts = {}
    for registration_id in ids:
        verification = verifications.get(registration_id)
        facts[registration_id] = EntryFacts(
            has_initial_submission=registration_id in initial,
            has_current_decision=registration_id in decided,
            has_active_information_request=registration_id in requested,
            clearance_code=evaluate_clearance(verification).code,
            identity_status=getattr(verification, "status", ""),
            identity_version=getattr(verification, "version", None),
            open_cases=tuple(cases.get(registration_id, ())),
        )
    return facts


def entry_problem(registration: Registration, *, lock: bool = False) -> tuple[str, list]:
    """The exclusion code for `registration` ("" when eligible) and its open cases."""
    facts = entry_facts(registration, lock=lock)
    return evaluate_entry(registration, facts), list(facts.open_cases)


def _audit_entry(
    recorder: AuditRecorder,
    *,
    registration: Registration,
    case: ReviewCase,
    trigger: str,
    reused: bool,
    previous_public_status: str,
    actor=None,
    correlation_id: str = "",
) -> None:
    recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if actor is not None else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_codes.REVIEW_CASE_ENQUEUED,
            target_type="ReviewCase",
            target_uuid=case.pk,
            event_edition_id=registration.event_edition_id,
            result="SUCCESS",
            reason_code=trigger,
            before_summary={"public_status": previous_public_status},
            after_summary={
                "public_status": registration.public_status,
                "internal_status": registration.internal_status,
                "case_reused": reused,
            },
            correlation_id=correlation_id,
        )
    )


def enqueue_for_participation_review(
    registration_id,
    *,
    trigger: str,
    actor=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> EntryResult:
    """Make an eligible registration wait for its participation decision.

    Idempotent and safe under retries: the Registration row is locked, every
    condition is checked again under that lock, an open STANDARD / GENERAL
    case is reused, and nothing is written when nothing changes. Runs in the
    caller's transaction when there is one (the identity success paths), so
    identity success and queue entry commit together. Never raises for an
    ineligible registration: it returns `EXCLUDED` with the reason.
    """
    from apps.reviews.services import open_review_case

    recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        registration = Registration.objects.select_for_update(no_key=True).get(pk=registration_id)
        problem, cases = entry_problem(registration, lock=True)
        if problem:
            return EntryResult(EntryOutcome.EXCLUDED, reason=problem)
        reused = bool(cases)
        if reused:
            case = cases[0]
        else:
            case = open_review_case(
                registration=registration,
                case_type=ReviewCaseType.STANDARD,
                queue_code=ReviewQueueCode.GENERAL,
                actor=actor,
                audit_recorder=recorder,
                correlation_id=correlation_id,
            )
        previous_public_status = registration.public_status
        fields: list[str] = []
        if registration.public_status == RegistrationPublicStatus.SUBMITTED:
            registration.public_status = RegistrationPublicStatus.UNDER_REVIEW
            fields.append("public_status")
        if registration.internal_status == RegistrationInternalStatus.VERIFICATION_PENDING:
            registration.internal_status = _INTERNAL_AFTER_VERIFICATION[case.status]
            fields.append("internal_status")
        if fields:
            registration.version += 1
            registration.save(update_fields=[*fields, "version", "updated_at"])
        if fields or not reused:
            _audit_entry(
                recorder,
                registration=registration,
                case=case,
                trigger=trigger,
                reused=reused,
                previous_public_status=previous_public_status,
                actor=actor,
                correlation_id=correlation_id,
            )
    return EntryResult(
        EntryOutcome.EXISTING_CASE if reused else EntryOutcome.CREATED,
        case_id=case.pk,
        transitioned=bool(fields),
    )


# ---------------------------------------------------------------------------
# Backfill (manage.py backfill_review_queue)
# ---------------------------------------------------------------------------

DEFAULT_BACKFILL_BATCH_SIZE = 200
MAX_BACKFILL_BATCH_SIZE = 1000
#: Lock wait bound for one backfill transaction; a busy row is retried, then reported.
BACKFILL_LOCK_TIMEOUT = "5s"
BACKFILL_ATTEMPTS = 3


@dataclass
class BackfillReport:
    """Aggregate counts only -- never a reference, a name or an identifier."""

    applied: bool
    event_code: str = ""
    examined: int = 0
    #: Eligible and without an open STANDARD case (created with --apply).
    eligible_missing_case: int = 0
    #: Eligible with an open STANDARD case already (reused).
    existing_case: int = 0
    #: Registrations whose status moves to UNDER_REVIEW (or leaves VERIFICATION_PENDING).
    status_transitions: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    #: --apply only: cases actually created / transitions actually made.
    created: int = 0
    transitioned: int = 0
    #: --apply only: registrations that changed between the scan and the locked
    #: recheck and were excluded there (counted by reason in `excluded_at_apply`).
    excluded_at_apply: dict[str, int] = field(default_factory=dict)
    #: --apply only: rows still locked after every attempt (left unchanged).
    conflicts: int = 0

    def as_dict(self) -> dict:
        return {
            "mode": "apply" if self.applied else "dry-run",
            "event": self.event_code or "ALL",
            "examined": self.examined,
            "eligible_missing_case": self.eligible_missing_case,
            "existing_case": self.existing_case,
            "status_transitions": self.status_transitions,
            "excluded_total": sum(self.excluded.values()),
            "excluded": dict(sorted(self.excluded.items())),
            "created": self.created,
            "transitioned": self.transitioned,
            "excluded_at_apply": dict(sorted(self.excluded_at_apply.items())),
            "conflicts": self.conflicts,
        }


def _would_transition(registration: Registration) -> bool:
    return registration.public_status == RegistrationPublicStatus.SUBMITTED or (
        registration.internal_status == RegistrationInternalStatus.VERIFICATION_PENDING
    )


def _apply_one(registration_id, *, correlation_id: str) -> EntryResult | None:
    """One short transaction with a bounded lock wait, retried on a transient
    conflict. Returns None when the row stayed busy after every attempt."""
    for attempt in range(1, BACKFILL_ATTEMPTS + 1):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = %s", [BACKFILL_LOCK_TIMEOUT])
                return enqueue_for_participation_review(
                    registration_id, trigger=EntryTrigger.BACKFILL, correlation_id=correlation_id
                )
        except OperationalError as error:  # lock timeout, deadlock, serialization
            logger.warning(
                "review backfill conflict attempt=%s error=%s", attempt, type(error).__name__
            )
    return None


def run_backfill(
    *,
    event_edition=None,
    apply: bool = False,
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    correlation_id: str = "",
) -> BackfillReport:
    """Classify every registration in scope and, with `apply`, enqueue the eligible ones.

    Registrations are read in primary-key order, `batch_size` at a time
    (keyset pagination, no long-lived cursor or table lock). The dry run
    writes nothing. With `apply`, each eligible registration is rechecked and
    changed in its own short transaction by `enqueue_for_participation_review`,
    the same service the identity success paths use, so new submissions and
    identity verifications can continue meanwhile and a repeated run changes
    nothing more. No communication, invitation, pass, badge or Ministry call.
    """
    batch_size = max(1, min(int(batch_size), MAX_BACKFILL_BATCH_SIZE))
    report = BackfillReport(applied=apply, event_code=getattr(event_edition, "code", ""))
    queryset = Registration.objects.order_by("pk")
    if event_edition is not None:
        queryset = queryset.filter(event_edition=event_edition)
    last_pk = None
    correlation_id = correlation_id or f"review-backfill:{timezone.now():%Y%m%dT%H%M%S}"
    while True:
        page = queryset if last_pk is None else queryset.filter(pk__gt=last_pk)
        batch = list(page[:batch_size])
        if not batch:
            break
        last_pk = batch[-1].pk
        for registration in batch:
            report.examined += 1
            problem, cases = entry_problem(registration)
            if problem:
                report.excluded[problem] = report.excluded.get(problem, 0) + 1
                continue
            if cases:
                report.existing_case += 1
            else:
                report.eligible_missing_case += 1
            if _would_transition(registration):
                report.status_transitions += 1
            if not apply or (cases and not _would_transition(registration)):
                continue
            result = _apply_one(registration.pk, correlation_id=correlation_id)
            if result is None:
                report.conflicts += 1
            elif result.outcome == EntryOutcome.EXCLUDED:
                report.excluded_at_apply[result.reason] = (
                    report.excluded_at_apply.get(result.reason, 0) + 1
                )
            else:
                report.created += int(result.outcome == EntryOutcome.CREATED)
                report.transitioned += int(result.transitioned)
    return report
