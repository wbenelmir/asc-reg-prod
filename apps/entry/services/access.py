"""The online admission evaluator (Flow §10.5-§10.8, FR-ENT-005/013/014).

`assess_context` is PURE: it reads, it never writes, and it never makes the
admission decision -- the operator does (Gate G7). It evaluates the EXACT
selected Registration Context and never merges privileges from any other
context the same person holds (BR-ENT-001).

Every blocking condition is collected, not just the first one. The primary
result shown to the operator is the most severe blocker (DENIED, then
STALE, then MANUAL_REVIEW), but an override must cover EVERY blocker: an
override approved for, say, an expired pass can never quietly admit
someone who is also at the wrong zone or under a restriction.

Check order (evaluation stops early only for the two conditions that make
every other check meaningless):

1. Event Edition of the context equals the checkpoint's      (stop)
2. Registration approved, current, not withdrawn/cancelled   (stop)
2b. Attendance days for today, once attendance enforcement is active
   (`apps.accreditation.attendance.admission_problem`; never overrideable)
3. Person- or context-level security restriction (DENY)
4. Digital Entry Pass state and validity window
5. Current Badge Type and Access Profile assignments; pass snapshot
   still matches them (otherwise STALE)
6. Access Profile active and inside its validity window
7. Access rules for this zone (incl. ancestors) and gate
8. Prior admission evidence and the re-entry policy
9. Security restriction (MANUAL_REVIEW severity)
10. Unverified identity reference (identity lookups only)

Step 8's evidence is every admission Entry Event of the context PLUS any
synchronized offline admission the server kept only in its `SyncOperation`
(Phase 4 Prompt 3 correction 5, decision D3 option S1:
`apps.entry.selectors.admissions`). `prior_entry` stays the latest Entry
Event, or None: an admission without an Entry Event is never shown as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.accreditation.attendance import admission_problem as attendance_admission_problem
from apps.accreditation.models import (
    AccessProfileAssignment,
    AccessRule,
    AccessRuleAssignment,
    AccessRuleEffect,
    AccessRuleEventType,
    AssignmentStatus,
    BadgeTypeAssignment,
    ReentryPolicy,
)
from apps.badges.models import DigitalEntryPass, DigitalEntryPassStatus
from apps.badges.services import credential_is_time_valid
from apps.entry.models import (
    NEVER_OVERRIDEABLE_REASON_CODES,
    EntryDecision,
    EntryEvent,
    EntryEventType,
    EntryReasonCode,
    EntryResult,
    RestrictionSeverity,
)
from apps.entry.selectors.admissions import latest_unlinked_admission
from apps.entry.services.restrictions import active_restrictions_for
from apps.events.services import zone_and_ancestor_ids
from apps.registrations.selectors import is_active_approved_context

_SEVERITY_ORDER = (EntryResult.DENIED, EntryResult.STALE, EntryResult.MANUAL_REVIEW)


@dataclass(frozen=True)
class Blocker:
    result: str
    reason_code: str


@dataclass(frozen=True)
class Assessment:
    """One evaluated Registration Context at one checkpoint."""

    registration: object
    result: str
    reason_code: str
    blockers: tuple[Blocker, ...] = ()
    advisory_codes: tuple[str, ...] = ()
    credential: DigitalEntryPass | None = None
    badge_assignment: BadgeTypeAssignment | None = None
    access_assignment: AccessProfileAssignment | None = None
    prior_entry: EntryEvent | None = None
    #: The latest synchronized offline admission kept without an Entry Event
    #: (correction 5, D3 S1): it counts for step 8, never as `prior_entry`.
    unlinked_prior_admission: object | None = None
    restriction_categories: tuple[str, ...] = ()
    restriction_blocks_override: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_admittable(self) -> bool:
        return self.result in (EntryResult.ALLOWED, EntryResult.ALLOWED_WITH_ADVISORY)

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(b.reason_code for b in self.blockers)

    @property
    def may_be_overridden(self) -> bool:
        """True if an override is possible IN PRINCIPLE for this assessment.

        The configured catalogue must still list every blocker code; this
        property only applies the fixed, non-configurable exclusions.
        """
        if self.is_admittable or not self.blockers:
            return False
        if self.restriction_blocks_override:
            return False
        return not any(code in NEVER_OVERRIDEABLE_REASON_CODES for code in self.blocker_codes)

    def signature(self) -> tuple:
        """What must not have changed between verification and decision --
        including a newer admission known only from a synchronized
        operation, exactly as a newer Entry Event changes `prior_entry`."""
        return (
            self.result,
            self.reason_code,
            tuple(sorted(self.blocker_codes)),
            tuple(sorted(self.advisory_codes)),
            str(getattr(self.credential, "pk", "")),
            str(getattr(self.prior_entry, "pk", "")),
            str(getattr(self.unlinked_prior_admission, "pk", "")),
        )


def _finish(registration, blockers: list[Blocker], advisories: list[str], **extra) -> Assessment:
    if blockers:
        primary = next(b for severity in _SEVERITY_ORDER for b in blockers if b.result == severity)
        return Assessment(
            registration=registration,
            result=primary.result,
            reason_code=primary.reason_code,
            blockers=tuple(blockers),
            advisory_codes=tuple(advisories),
            **extra,
        )
    result = EntryResult.ALLOWED_WITH_ADVISORY if advisories else EntryResult.ALLOWED
    reason = advisories[0] if advisories else EntryReasonCode.NONE
    return Assessment(
        registration=registration,
        result=result,
        reason_code=reason,
        advisory_codes=tuple(advisories),
        **extra,
    )


def credential_blocker(credential: DigitalEntryPass | None, *, now) -> Blocker | None:
    """Map a credential's status and window to a blocker (None = usable)."""
    if credential is None:
        return Blocker(EntryResult.MANUAL_REVIEW, EntryReasonCode.NO_ACTIVE_PASS)
    status = credential.status
    if status == DigitalEntryPassStatus.REVOKED:
        return Blocker(EntryResult.DENIED, EntryReasonCode.PASS_REVOKED)
    if status == DigitalEntryPassStatus.REPLACED:
        return Blocker(EntryResult.STALE, EntryReasonCode.PASS_REPLACED)
    if status == DigitalEntryPassStatus.SUSPENDED:
        return Blocker(EntryResult.DENIED, EntryReasonCode.PASS_SUSPENDED)
    if status == DigitalEntryPassStatus.INACTIVE:
        return Blocker(EntryResult.MANUAL_REVIEW, EntryReasonCode.PASS_INACTIVE)
    if status == DigitalEntryPassStatus.EXPIRED:
        return Blocker(EntryResult.DENIED, EntryReasonCode.PASS_EXPIRED)
    if not credential_is_time_valid(credential, now=now):
        if now < credential.valid_from:
            return Blocker(EntryResult.DENIED, EntryReasonCode.PASS_NOT_YET_VALID)
        return Blocker(EntryResult.DENIED, EntryReasonCode.PASS_EXPIRED)
    return None


def context_credential(registration) -> DigitalEntryPass | None:
    """The credential a non-QR lookup evaluates: the current one, else the
    most recent historical one (so a revoked pass reads as REVOKED rather
    than as "no pass")."""
    from apps.badges.models import NON_TERMINAL_STATUSES

    queryset = DigitalEntryPass.objects.select_related("event_edition").filter(
        registration=registration
    )
    current = queryset.filter(status__in=NON_TERMINAL_STATUSES).first()
    if current is not None:
        return current
    return queryset.order_by("-credential_version").first()


def _current(model, registration, *, now):
    return (
        model.objects.filter(registration=registration, status=AssignmentStatus.CURRENT)
        .filter(effective_from__lte=now)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
        .first()
    )


def _in_window(obj, now) -> bool:
    if obj.valid_from is not None and now < obj.valid_from:
        return False
    if obj.valid_until is not None and now >= obj.valid_until:
        return False
    return True


def _rule_blocker(*, registration, access_profile, checkpoint, now) -> Blocker | None:
    direct_rule_ids = list(
        AccessRuleAssignment.objects.filter(
            registration=registration, status=AssignmentStatus.CURRENT
        )
        .filter(effective_from__lte=now)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
        .values_list("access_rule_id", flat=True)
    )
    source = Q(pk__in=direct_rule_ids)
    if access_profile is not None:
        source |= Q(access_profile=access_profile)
    rules = [
        rule
        for rule in AccessRule.objects.filter(source)
        .filter(
            event_edition_id=registration.event_edition_id,
            is_active=True,
            zone_id__in=zone_and_ancestor_ids(checkpoint.zone),
            event_type__in=[AccessRuleEventType.ENTRY, AccessRuleEventType.ANY],
        )
        .filter(Q(gate__isnull=True) | Q(gate_id=checkpoint.gate.pk))
        if _in_window(rule, now)
    ]
    if any(rule.effect == AccessRuleEffect.DENY for rule in rules):
        return Blocker(EntryResult.DENIED, EntryReasonCode.ACCESS_RULE_DENY)
    if not any(rule.effect == AccessRuleEffect.ALLOW for rule in rules):
        # Fail closed: no configured allow rule for this checkpoint means no
        # entry here, never "entry by default".
        return Blocker(EntryResult.DENIED, EntryReasonCode.WRONG_ZONE)
    return None


def latest_admission(registration) -> EntryEvent | None:
    return (
        EntryEvent.objects.select_related("gate")
        .filter(
            registration=registration,
            event_type=EntryEventType.ENTRY,
            decision=EntryDecision.ADMIT,
        )
        .order_by("-occurred_at")
        .first()
    )


def assess_context(
    *,
    registration,
    checkpoint,
    presented_credential: DigitalEntryPass | None = None,
    identity_verified: bool | None = None,
    now=None,
) -> Assessment:
    """Evaluate one exact Registration Context at `checkpoint`.

    `presented_credential` is the credential resolved from a verified QR
    signature; for every other lookup method it is None and the context's
    own credential is evaluated. `identity_verified` is None unless the
    context was found through a NIN/passport lookup.
    """
    now = now or timezone.now()
    blockers: list[Blocker] = []
    advisories: list[str] = []

    # 1. Event Edition (never overrideable; nothing else is meaningful).
    if registration.event_edition_id != checkpoint.event_edition.pk:
        return _finish(registration, [Blocker(EntryResult.DENIED, EntryReasonCode.WRONG_EVENT)], [])
    if (
        presented_credential is not None
        and presented_credential.event_edition_id != checkpoint.event_edition.pk
    ):
        return _finish(registration, [Blocker(EntryResult.DENIED, EntryReasonCode.WRONG_EVENT)], [])

    # 2. Registration state (never overrideable). The shared definition in
    # `apps.registrations.selectors`, also used by pass and badge commands.
    if not is_active_approved_context(registration):
        return _finish(
            registration,
            [Blocker(EntryResult.DENIED, EntryReasonCode.REGISTRATION_NOT_APPROVED)],
            [],
        )

    # 2b. Attendance days (apps.accreditation.attendance), only once
    # enforcement is active for the edition: never overrideable, and every
    # check below still applies (attendance never widens access).
    attendance_problem = attendance_admission_problem(
        registration=registration, event_edition=checkpoint.event_edition, at=now
    )
    if attendance_problem:
        blockers.append(Blocker(EntryResult.DENIED, attendance_problem))

    # 3. Security restrictions (person-level overrides every context).
    restrictions = list(active_restrictions_for(registration, now=now))
    deny_restrictions = [r for r in restrictions if r.severity == RestrictionSeverity.DENY_ENTRY]
    review_restrictions = [
        r for r in restrictions if r.severity == RestrictionSeverity.MANUAL_REVIEW
    ]
    if deny_restrictions:
        blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.SECURITY_RESTRICTION))
    restriction_blocks_override = any(not r.is_overrideable for r in restrictions)

    # 4. Credential.
    credential = (
        presented_credential
        if presented_credential is not None
        else context_credential(registration)
    )
    credential_problem = credential_blocker(credential, now=now)
    if credential_problem is not None:
        blockers.append(credential_problem)

    # 5. Assignments, and whether the pass snapshot still matches them.
    badge_assignment = _current(BadgeTypeAssignment, registration, now=now)
    access_assignment = _current(AccessProfileAssignment, registration, now=now)
    access_profile = None
    if badge_assignment is None or access_assignment is None:
        blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.NO_ACCESS_ASSIGNMENT))
    else:
        access_profile = access_assignment.access_profile
        if credential is not None and (
            credential.badge_assignment_id != badge_assignment.pk
            or credential.access_assignment_id != access_assignment.pk
        ):
            blockers.append(Blocker(EntryResult.STALE, EntryReasonCode.ASSIGNMENT_CHANGED))

    # 6. Access Profile state and window.
    if access_profile is not None:
        if not access_profile.is_active:
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.NO_ACCESS_ASSIGNMENT))
        elif not _in_window(access_profile, now):
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.OUTSIDE_TIME_WINDOW))

    # 7. Zone and gate rules.
    rule_problem = _rule_blocker(
        registration=registration, access_profile=access_profile, checkpoint=checkpoint, now=now
    )
    if rule_problem is not None:
        blockers.append(rule_problem)

    # 8. Prior admission: advisory by default, denial only under SINGLE_ENTRY.
    # The evidence is the latest admission Entry Event or the latest offline
    # admission kept only in its synchronized operation, whichever is later.
    prior = latest_admission(registration)
    unlinked = latest_unlinked_admission(registration)
    last_admitted_at = max(
        (e.occurred_at for e in (prior, unlinked) if e is not None), default=None
    )
    if last_admitted_at is not None:
        if (
            access_profile is not None
            and access_profile.reentry_policy == ReentryPolicy.SINGLE_ENTRY
        ):
            blockers.append(Blocker(EntryResult.DENIED, EntryReasonCode.ALREADY_ADMITTED))
        else:
            advisories.append(EntryReasonCode.PRIOR_ENTRY)
            if (now - last_admitted_at).total_seconds() <= settings.ENTRY_RECENT_REENTRY_SECONDS:
                advisories.append(EntryReasonCode.RECENT_REENTRY)

    # 9. Review-level restriction.
    if review_restrictions:
        blockers.append(Blocker(EntryResult.MANUAL_REVIEW, EntryReasonCode.RESTRICTION_REVIEW))

    # 10. Identity reference not verified.
    if identity_verified is False:
        blockers.append(Blocker(EntryResult.MANUAL_REVIEW, EntryReasonCode.IDENTITY_UNVERIFIED))

    return _finish(
        registration,
        blockers,
        advisories,
        credential=credential,
        badge_assignment=badge_assignment,
        access_assignment=access_assignment,
        prior_entry=prior,
        unlinked_prior_admission=unlinked,
        restriction_categories=tuple(sorted({r.category for r in restrictions})),
        restriction_blocks_override=restriction_blocks_override,
    )
