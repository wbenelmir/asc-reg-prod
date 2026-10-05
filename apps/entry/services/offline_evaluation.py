"""Authoritative re-evaluation of an offline operation AT ITS OCCURRENCE TIME
(Phase 4 Prompt 3, ADR-0024 §5).

`apps.entry.services.access.assess_context` evaluates a context NOW, against
current statuses. A synchronized offline admission must instead be judged
against what was true when it physically happened, so the conflict it may
reveal is attributed correctly: a pass revoked AFTER the admission does not
make that admission wrong, while one revoked BEFORE it -- but after the
device's last knowledge -- is exactly the "revoked after last sync"
conflict of Flow §11.8.

`evaluate_at` therefore follows the same ten steps as `assess_context`
(event, registration, DENY restriction, credential, assignments and pass
snapshot, Access Profile, zone and gate rules, prior admission, REVIEW
restriction), with the time-dependent facts read as of `at`:

* the pass status from its own status timestamps (`revoked_at`,
  `replaced_at`, `suspended_at`/`resumed_at`, `expired_at`, `activated_at`);
* restrictions that existed at `at` (created before it, in force at it,
  and not revoked before it);
* the Badge Type and Access Profile assignments whose effective window
  contains `at` (change and revoke close that window; rows are never
  deleted);
* withdrawal and cancellation from their timestamps.

Mutable rules and registration state are evaluated from current rows. This
alone is NOT a historical reconstruction: a later edit could also relax a
rule. The synchronization classifier therefore checks the package change
journal and routes uncertain historical admissions to reconciliation. The
evaluation writes nothing.

Step 8's prior admissions are the context's admission Entry Events AND the
synchronized offline admissions kept only in their `SyncOperation`
(correction 5, decision D3 option S1: `apps.entry.selectors.admissions`),
both strictly before `at`, never the operation being assessed.
`prior_admission` stays an Entry Event (it becomes the new event's
`prior_entry_event`); `prior_admission_at` is the latest of the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.db.models import Q

from apps.accreditation.models import (
    AccessProfileAssignment,
    BadgeTypeAssignment,
    ReentryPolicy,
)
from apps.badges.models import DigitalEntryPassStatus
from apps.badges.services import credential_is_time_valid
from apps.entry.models import (
    EntryDecision,
    EntryEvent,
    EntryEventType,
    EntryReasonCode,
    EntryResult,
    RestrictionSeverity,
    RestrictionStatus,
    SecurityRestriction,
)
from apps.entry.selectors.admissions import latest_unlinked_admission
from apps.entry.services.access import Blocker, _in_window, _rule_blocker

_SEVERITY_ORDER = (EntryResult.DENIED, EntryResult.STALE, EntryResult.MANUAL_REVIEW)
RESTRICTION_REASONS = frozenset(
    {EntryReasonCode.SECURITY_RESTRICTION, EntryReasonCode.RESTRICTION_REVIEW}
)


def _epoch(value):
    return int(value.timestamp()) if value is not None else None


def at_or_before(moment, at) -> bool:
    """True if `moment` happened no later than `at`, at ONE-SECOND granularity.

    A device records its operations in whole epoch seconds, so a fact from
    the same second as the operation counts as already true then. For a
    revocation, suspension or withdrawal that is the conservative reading
    (it counts against an admission); for an activation it avoids refusing
    an admission of a pass activated within that same second."""
    return moment is not None and moment.replace(microsecond=0) <= at


def credential_status_at(credential, at) -> str:
    """The pass status as it stood at `at`, from its own status timestamps."""
    status = credential.status
    terminal = {
        DigitalEntryPassStatus.REVOKED: credential.revoked_at,
        DigitalEntryPassStatus.REPLACED: credential.replaced_at,
        DigitalEntryPassStatus.EXPIRED: credential.expired_at,
    }
    if status in terminal:
        changed = terminal[status]
        if changed is None or at_or_before(changed, at):
            return status
    suspended = credential.suspended_at
    resumed = credential.resumed_at
    if at_or_before(suspended, at):
        resumed_before = resumed is not None and suspended <= resumed and at_or_before(resumed, at)
        if not resumed_before:
            return DigitalEntryPassStatus.SUSPENDED
    if status == DigitalEntryPassStatus.INACTIVE:
        return DigitalEntryPassStatus.INACTIVE
    if credential.activated_at is not None and not at_or_before(credential.activated_at, at):
        return DigitalEntryPassStatus.INACTIVE
    return DigitalEntryPassStatus.ACTIVE


def status_changed_at(credential, status: str):
    return {
        DigitalEntryPassStatus.REVOKED: credential.revoked_at,
        DigitalEntryPassStatus.REPLACED: credential.replaced_at,
        DigitalEntryPassStatus.SUSPENDED: credential.suspended_at,
        DigitalEntryPassStatus.EXPIRED: credential.expired_at,
    }.get(status)


def restrictions_at(registration, at):
    """Every restriction that existed and was in force at `at`."""
    target = Q(registration_id=registration.pk)
    if registration.person_id is not None:
        target |= Q(person_id=registration.person_id)
    # One-second granularity (see `at_or_before`): a restriction created or
    # starting within the operation's own second already applies.
    upper = at + timedelta(seconds=1)
    return list(
        SecurityRestriction.objects.filter(target)
        .filter(Q(event_edition__isnull=True) | Q(event_edition_id=registration.event_edition_id))
        .filter(created_at__lt=upper, starts_at__lt=upper)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=at))
        .filter(
            Q(status=RestrictionStatus.ACTIVE)
            | Q(status=RestrictionStatus.REVOKED, revoked_at__gt=at)
        )
    )


def assignment_at(model, registration, at):
    """The assignment whose effective window contains `at` (any status: a
    change or revocation closes the window, it never deletes the row)."""
    return (
        model.objects.filter(registration=registration, effective_from__lte=at)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=at))
        .order_by("-effective_from")
        .first()
    )


def approved_at(registration, at) -> bool:
    """Approved at `at`. Like the rest of the registration state, the identity
    clearance (owner decision IDV-Q1) is read from the current rows: a
    synchronized admission of a registration whose identity is no longer
    cleared is never confirmed as approved."""
    from apps.people.selectors.clearance import identity_clearance
    from apps.registrations.models import RegistrationPublicStatus

    return (
        registration.public_status == RegistrationPublicStatus.APPROVED
        and bool(registration.is_current_context)
        and registration.person_id is not None
        and not at_or_before(registration.withdrawn_at, at)
        and not at_or_before(registration.cancelled_at, at)
        and identity_clearance(registration.pk).cleared
    )


def _status_blocker(status: str) -> Blocker | None:
    return {
        DigitalEntryPassStatus.REVOKED: Blocker(EntryResult.DENIED, EntryReasonCode.PASS_REVOKED),
        DigitalEntryPassStatus.REPLACED: Blocker(EntryResult.STALE, EntryReasonCode.PASS_REPLACED),
        DigitalEntryPassStatus.SUSPENDED: Blocker(
            EntryResult.DENIED, EntryReasonCode.PASS_SUSPENDED
        ),
        DigitalEntryPassStatus.INACTIVE: Blocker(
            EntryResult.MANUAL_REVIEW, EntryReasonCode.PASS_INACTIVE
        ),
        DigitalEntryPassStatus.EXPIRED: Blocker(EntryResult.DENIED, EntryReasonCode.PASS_EXPIRED),
    }.get(status)


@dataclass(frozen=True)
class ServerEvaluation:
    result: str
    reason_code: str
    blockers: tuple[Blocker, ...]
    advisories: tuple[str, ...]
    pass_status_at: str
    badge_assignment: object | None
    access_profile: object | None
    reentry_policy: str
    restriction_blocks_override: bool
    prior_admission: EntryEvent | None
    #: The latest earlier admission kept only in a synchronized operation.
    prior_unlinked_admission: object | None = None

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(b.reason_code for b in self.blockers)

    @property
    def is_admittable(self) -> bool:
        return self.result in (EntryResult.ALLOWED, EntryResult.ALLOWED_WITH_ADVISORY)

    @property
    def prior_admission_at(self):
        """The latest earlier admission step 8 used, from either source."""
        return max(
            (
                e.occurred_at
                for e in (self.prior_admission, self.prior_unlinked_admission)
                if e is not None
            ),
            default=None,
        )

    def as_json(self) -> dict:
        """Codes and times only -- never a name, identifier value or reason."""
        return {
            "result": self.result,
            "reason": self.reason_code,
            "blockers": sorted(set(self.blocker_codes)),
            "advisories": sorted(set(self.advisories)),
            "pass_status_at_occurrence": self.pass_status_at,
            "reentry_policy": self.reentry_policy,
            "restriction_blocks_override": self.restriction_blocks_override,
            "prior_admission_at": _epoch(self.prior_admission_at),
            "prior_unlinked_admission_at": _epoch(
                getattr(self.prior_unlinked_admission, "occurred_at", None)
            ),
        }


def _finish(blockers, advisories, **extra) -> ServerEvaluation:
    if blockers:
        primary = next(b for severity in _SEVERITY_ORDER for b in blockers if b.result == severity)
        result, reason = primary.result, primary.reason_code
    else:
        result = EntryResult.ALLOWED_WITH_ADVISORY if advisories else EntryResult.ALLOWED
        reason = advisories[0] if advisories else EntryReasonCode.NONE
    return ServerEvaluation(
        result=result,
        reason_code=reason,
        blockers=tuple(blockers),
        advisories=tuple(advisories),
        **extra,
    )


def prior_admission_before(registration, at, *, exclude_operation_id: str = ""):
    return (
        EntryEvent.objects.filter(
            registration=registration,
            event_type=EntryEventType.ENTRY,
            decision=EntryDecision.ADMIT,
            occurred_at__lt=at,
        )
        .exclude(operation_id=exclude_operation_id)
        .order_by("-occurred_at")
        .first()
    )


def evaluate_at(
    *, registration, credential, event_edition, gate, zone, at, exclude_operation=None
) -> ServerEvaluation:
    """Evaluate timestamped facts at `at` and the available mutable context.
    `exclude_operation` is the synchronized operation being assessed."""
    blockers: list[Blocker] = []
    advisories: list[str] = []
    status = credential_status_at(credential, at)
    common = {
        "pass_status_at": status,
        "badge_assignment": None,
        "access_profile": None,
        "reentry_policy": "",
        "restriction_blocks_override": False,
        "prior_admission": None,
        "prior_unlinked_admission": None,
    }
    # 1. Event Edition.
    if (
        registration.event_edition_id != event_edition.pk
        or credential.event_edition_id != event_edition.pk
    ):
        return _finish([Blocker(EntryResult.DENIED, EntryReasonCode.WRONG_EVENT)], [], **common)
    # 2. Registration state.
    if not approved_at(registration, at):
        return _finish(
            [Blocker(EntryResult.DENIED, EntryReasonCode.REGISTRATION_NOT_APPROVED)], [], **common
        )
    # 3. Restrictions that existed at `at`.
    restrictions = restrictions_at(registration, at)
    if any(r.severity == RestrictionSeverity.DENY_ENTRY for r in restrictions):
        blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.SECURITY_RESTRICTION))
    restriction_blocks_override = any(not r.is_overrideable for r in restrictions)
    # 4. Credential status and window at `at`.
    problem = _status_blocker(status)
    if problem is None and not credential_is_time_valid(credential, now=at):
        problem = Blocker(
            EntryResult.DENIED,
            EntryReasonCode.PASS_NOT_YET_VALID
            if at < credential.valid_from
            else EntryReasonCode.PASS_EXPIRED,
        )
    if problem is not None:
        blockers.append(problem)
    # 5. Assignments at `at`, and whether the pass snapshot matched them.
    badge = assignment_at(BadgeTypeAssignment, registration, at)
    access = assignment_at(AccessProfileAssignment, registration, at)
    profile = None
    if badge is None or access is None:
        blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.NO_ACCESS_ASSIGNMENT))
    else:
        profile = access.access_profile
        if (
            credential.badge_assignment_id != badge.pk
            or credential.access_assignment_id != access.pk
        ):
            blockers.append(Blocker(EntryResult.STALE, EntryReasonCode.ASSIGNMENT_CHANGED))
    # 6. Access Profile state and window.
    if profile is not None:
        if not profile.is_active:
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.NO_ACCESS_ASSIGNMENT))
        elif not _in_window(profile, at):
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.OUTSIDE_TIME_WINDOW))
    # 7. Zone and gate rules.
    rule_problem = _rule_blocker(
        registration=registration,
        access_profile=profile,
        checkpoint=SimpleNamespace(zone=zone, gate=gate),
        now=at,
    )
    if rule_problem is not None:
        blockers.append(rule_problem)
    # 8. Prior admission BEFORE `at`: advisory, or a denial under SINGLE_ENTRY.
    # An Entry Event, or an admission kept only in its synchronized operation.
    prior = prior_admission_before(registration, at)
    unlinked = latest_unlinked_admission(registration, before=at, exclude=exclude_operation)
    last_admitted_at = max(
        (e.occurred_at for e in (prior, unlinked) if e is not None), default=None
    )
    reentry = getattr(profile, "reentry_policy", "") or ""
    if last_admitted_at is not None:
        if reentry == ReentryPolicy.SINGLE_ENTRY:
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.ALREADY_ADMITTED))
        else:
            advisories.append(EntryReasonCode.PRIOR_ENTRY)
            if (at - last_admitted_at).total_seconds() <= settings.ENTRY_RECENT_REENTRY_SECONDS:
                advisories.append(EntryReasonCode.RECENT_REENTRY)
    # 9. Review-level restriction.
    if any(r.severity == RestrictionSeverity.MANUAL_REVIEW for r in restrictions):
        blockers.append(Blocker(EntryResult.MANUAL_REVIEW, EntryReasonCode.RESTRICTION_REVIEW))
    return _finish(
        blockers,
        advisories,
        pass_status_at=status,
        badge_assignment=badge,
        access_profile=profile,
        reentry_policy=reentry,
        restriction_blocks_override=restriction_blocks_override,
        prior_admission=prior,
        prior_unlinked_admission=unlinked,
    )
