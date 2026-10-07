"""Attendance entitlements: which conference days an approved registration covers.

At approval an authorized operator explicitly chooses one of two
`AttendanceCategory` values: all three conference days, or only the two days
after the opening day. Opening-day places are limited per event edition
(`AttendancePolicy.opening_day_capacity`). This module is the single service
boundary for that rule; views, reviews, badges and entry call it and never
write the attendance models themselves.

* **Capacity is derived, never counted.** An opening-day place is held by
  every registration that is approved, current, not withdrawn and not
  cancelled and whose CURRENT entitlement is `ALL_CONFERENCE_DAYS`, whatever
  its origin (open, invitation, delegation, on behalf) and whether or not a
  pass exists. A withdrawal, a cancellation, a reopening or a NOT_APPROVED
  outcome releases the place by changing the registration status, so the
  same place can never be released twice.
* **Serialized.** Every command that may ADD an opening-day place locks the
  edition's `AttendancePolicy` row and counts under that lock, so two
  simultaneous approvals for the last place yield exactly one opening-day
  approval. Lock order: the Registration row first (as every review and
  accreditation command already does), then the AttendancePolicy row.
* **No default.** The category is always an explicit choice; a full opening
  day is refused with `OpeningDayCapacityReachedError`, never downgraded.
* **Legacy approvals stay unclassified** (no CURRENT row) until an
  authorized operator classifies them; nothing infers a category.
* **Controlled activation.** Admission enforces the days only once
  `activate_enforcement` has switched the policy on, which requires valid
  days and capacity, every active approval classified, the opening-day
  allocation within capacity and every handed-over badge marked accordingly.
  Until then admission behaves exactly as before this module existed.

Audit summaries carry categories, dates, counts and capacity only, never a
name or an identifier of the participant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _

from apps.accreditation.models import (
    AttendanceCategory,
    AttendanceEnforcementInterval,
    AttendanceEnforcementIntervalSource,
    AttendanceEntitlement,
    AttendanceEntitlementOrigin,
    AttendanceEntitlementStatus,
    AttendancePolicy,
)
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.communications.purposes import CommunicationPurpose

#: Day kinds of a moment, in the edition timezone.
OPENING_DAY = "OPENING_DAY"
FOLLOWING_DAY = "FOLLOWING_DAY"
NOT_A_CONFERENCE_DAY = "NOT_A_CONFERENCE_DAY"

#: Entry reason codes this module can return (the values of
#: `apps.entry.models.EntryReasonCode`; kept as strings so this module does
#: not import the entry app).
DAY_NOT_AUTHORIZED = "ATTENDANCE_DAY_NOT_AUTHORIZED"
UNCLASSIFIED = "ATTENDANCE_UNCLASSIFIED"
NOT_CONFERENCE_DAY = "ATTENDANCE_NOT_CONFERENCE_DAY"

#: Bounds of an operator's reason (stored in the entitlement and, shortened,
#: in the audit event; the form says not to include personal data).
REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 300

#: The reason code of offline packages revoked when enforcement changes.
OFFLINE_PACKAGE_REVOCATION_REASON = "ATTENDANCE_RULES_CHANGED"


class AttendanceError(Exception):
    """Base of every attendance refusal. `code` is stable and mapped to a
    localized message by the views; the message itself is developer-facing."""

    code = "ATTENDANCE_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


class AttendanceChoiceRequiredError(AttendanceError):
    code = "CHOICE_REQUIRED"


class AttendanceNotConfiguredError(AttendanceError):
    code = "NOT_CONFIGURED"


class OpeningDayCapacityReachedError(AttendanceError):
    code = "OPENING_DAY_FULL"


class AttendanceStateError(AttendanceError):
    code = "INVALID_STATE"


class AttendanceConcurrencyError(AttendanceError):
    code = "STALE"


class AttendancePolicyError(AttendanceError):
    code = "INVALID_POLICY"


class AttendanceActivationRefused(AttendanceError):
    code = "ACTIVATION_REFUSED"

    def __init__(self, readiness: Readiness) -> None:
        super().__init__("Attendance enforcement prerequisites are not met.")
        self.readiness = readiness


# ---------------------------------------------------------------------------
# Policy and day helpers
# ---------------------------------------------------------------------------


def policy_for(event_edition_id) -> AttendancePolicy | None:
    return AttendancePolicy.objects.filter(event_edition_id=event_edition_id).first()


def days_configured(policy: AttendancePolicy | None) -> bool:
    return bool(
        policy is not None
        and policy.opening_date is not None
        and policy.second_date is not None
        and policy.third_date is not None
    )


def is_configured(policy: AttendancePolicy | None) -> bool:
    """The three days and the opening-day capacity are all set."""
    return days_configured(policy) and policy.opening_day_capacity is not None


def conference_days(policy: AttendancePolicy) -> tuple[date, date, date]:
    return policy.opening_date, policy.second_date, policy.third_date


def authorized_dates(policy: AttendancePolicy, category: str) -> tuple[date, ...]:
    opening, second, third = conference_days(policy)
    if category == AttendanceCategory.ALL_CONFERENCE_DAYS:
        return (opening, second, third)
    return (second, third)


def local_date(moment: datetime, timezone_name: str) -> date:
    return moment.astimezone(ZoneInfo(timezone_name or "UTC")).date()


def day_kind(policy: AttendancePolicy, timezone_name: str, moment: datetime) -> str:
    """Which conference day `moment` falls on, by its local calendar date in
    the edition timezone (the boundary the offline packages also use)."""
    if not days_configured(policy):
        return NOT_A_CONFERENCE_DAY
    today = local_date(moment, timezone_name)
    if today == policy.opening_date:
        return OPENING_DAY
    if today in (policy.second_date, policy.third_date):
        return FOLLOWING_DAY
    return NOT_A_CONFERENCE_DAY


def category_covers(category: str, kind: str) -> bool:
    if kind == OPENING_DAY:
        return category == AttendanceCategory.ALL_CONFERENCE_DAYS
    if kind == FOLLOWING_DAY:
        return category in AttendanceCategory.values
    return False


def enforcement_active_at(policy: AttendancePolicy | None, moment: datetime) -> bool:
    """Whether enforcement applied at `moment` (the synchronization service
    judges an offline operation at its occurrence time).

    Read from the durable enforcement intervals (start included, end
    excluded), so every earlier activation cycle keeps its own period: a
    deactivation and a later reactivation never change how an operation from
    an earlier period is judged. An `UNCERTAIN` interval (history that was
    already lost when the intervals were introduced) counts as enforced."""
    if policy is None or policy.enforcement_activated_at is None:
        return False
    if (
        AttendanceEnforcementInterval.objects.filter(policy=policy, started_at__lte=moment)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=moment))
        .exists()
    ):
        return True
    # Switched on without an open period (only by a release that predates the
    # periods, e.g. during a code rollback): enforced from that activation.
    return (
        policy.enforcement_active
        and moment >= policy.enforcement_activated_at
        and not policy.enforcement_intervals.filter(ended_at__isnull=True).exists()
    )


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def seat_holding_q(prefix: str = "") -> Q:
    """Registrations that hold their attendance (and an opening-day place
    when classified so): approved, current, not withdrawn, not cancelled.

    Deliberately WITHOUT the identity clearance of
    `apps.registrations.selectors.active_approved_context_q`: a place belongs
    to the approval, and a temporarily uncleared identity must not free it
    for somebody else (admission still requires the clearance)."""
    from apps.registrations.models import RegistrationPublicStatus

    return Q(
        **{
            f"{prefix}public_status": RegistrationPublicStatus.APPROVED,
            f"{prefix}is_current_context": True,
            f"{prefix}withdrawn_at__isnull": True,
            f"{prefix}cancelled_at__isnull": True,
        }
    )


def current_entitlement(registration_id) -> AttendanceEntitlement | None:
    return AttendanceEntitlement.objects.filter(
        registration_id=registration_id, status=AttendanceEntitlementStatus.CURRENT
    ).first()


def entitlement_at(registration_id, moment: datetime) -> AttendanceEntitlement | None:
    """The entitlement in force at `moment` (history is never deleted)."""
    return (
        AttendanceEntitlement.objects.filter(
            registration_id=registration_id, effective_from__lte=moment
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=moment))
        .order_by("-effective_from")
        .first()
    )


def opening_allocation_count(event_edition_id) -> int:
    """Opening-day places held now: one per seat-holding registration with a
    CURRENT `ALL_CONFERENCE_DAYS` entitlement."""
    return (
        AttendanceEntitlement.objects.filter(
            event_edition_id=event_edition_id,
            status=AttendanceEntitlementStatus.CURRENT,
            category=AttendanceCategory.ALL_CONFERENCE_DAYS,
        )
        .filter(seat_holding_q("registration__"))
        .count()
    )


def _current_category_subquery(registration_ref: str = "pk"):
    return Subquery(
        AttendanceEntitlement.objects.filter(
            registration_id=OuterRef(registration_ref),
            status=AttendanceEntitlementStatus.CURRENT,
        ).values("category")[:1]
    )


def seat_holding_registrations(event_edition_id):
    """Every approved, current, non-withdrawn, non-cancelled registration of
    the edition, annotated with its current category (None = unclassified)."""
    from apps.registrations.models import Registration

    return (
        Registration.objects.filter(event_edition_id=event_edition_id)
        .filter(seat_holding_q())
        .annotate(attendance_category=_current_category_subquery())
    )


def unclassified_registrations(event_edition_id):
    return seat_holding_registrations(event_edition_id).filter(attendance_category__isnull=True)


def badge_marking_mismatches(event_edition_id):
    """Handed-over (ISSUED) physical badges of seat-holding registrations whose
    recorded attendance marking differs from the current entitlement,
    including a badge with no marking recorded at all."""
    from apps.badges.models import BadgeIssuance, BadgeIssuanceStatus

    return (
        BadgeIssuance.objects.filter(
            status=BadgeIssuanceStatus.ISSUED,
            registration__event_edition_id=event_edition_id,
        )
        .filter(seat_holding_q("registration__"))
        .annotate(current_category=_current_category_subquery("registration_id"))
        .filter(
            Q(current_category__isnull=True)
            | Q(attendance_marking="")
            | ~Q(attendance_marking=F("current_category"))
        )
    )


@dataclass(frozen=True)
class AttendanceCounts:
    approved: int
    all_days: int
    following_days: int
    unclassified: int
    opening_capacity: int | None
    badge_markings_pending: int

    @property
    def opening_remaining(self) -> int | None:
        if self.opening_capacity is None:
            return None
        return max(self.opening_capacity - self.all_days, 0)


def attendance_counts(event_edition_id, policy: AttendancePolicy | None = None) -> AttendanceCounts:
    from django.db.models import Count

    if policy is None:
        policy = policy_for(event_edition_id)
    rows = (
        seat_holding_registrations(event_edition_id)
        .values("attendance_category")
        .annotate(total=Count("pk"))
    )
    by_category = {row["attendance_category"]: row["total"] for row in rows}
    return AttendanceCounts(
        approved=sum(by_category.values()),
        all_days=by_category.get(AttendanceCategory.ALL_CONFERENCE_DAYS, 0),
        following_days=by_category.get(AttendanceCategory.FOLLOWING_TWO_DAYS, 0),
        unclassified=by_category.get(None, 0),
        opening_capacity=getattr(policy, "opening_day_capacity", None),
        badge_markings_pending=badge_marking_mismatches(event_edition_id).count(),
    )


# ---------------------------------------------------------------------------
# Readiness and activation
# ---------------------------------------------------------------------------

#: Readiness item codes, in display order.
READY_DAYS = "DAYS_CONFIGURED"
READY_CAPACITY = "CAPACITY_CONFIGURED"
READY_CLASSIFIED = "ALL_APPROVALS_CLASSIFIED"
READY_WITHIN_CAPACITY = "OPENING_DAY_WITHIN_CAPACITY"
READY_BADGES = "BADGE_MARKINGS_RECONCILED"
READY_EDITION_DATES = "EDITION_DATES_MATCH"
READY_OFFLINE = "OFFLINE_PACKAGES_REFRESHED"


@dataclass(frozen=True)
class ReadinessItem:
    code: str
    ok: bool
    blocking: bool
    count: int | None = None


@dataclass(frozen=True)
class Readiness:
    items: tuple[ReadinessItem, ...]

    @property
    def ready(self) -> bool:
        return all(item.ok for item in self.items if item.blocking)

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.items if item.blocking and not item.ok)


def _current_offline_package_count(event_edition_id) -> int:
    from apps.entry.models import OfflinePackage, OfflinePackageStatus

    return OfflinePackage.objects.filter(
        device__event_edition_id=event_edition_id, status=OfflinePackageStatus.READY
    ).count()


def readiness(event_edition, policy: AttendancePolicy | None = None) -> Readiness:
    """The activation prerequisites, re-evaluated from current data.

    Blocking: the three days, the capacity, every seat-holding registration
    classified, the opening-day allocation within capacity, and every
    handed-over badge marked with its current entitlement. Informational:
    whether the edition's own start and end dates contain the three days,
    and how many current offline packages activation will revoke (devices
    then fetch packages built under the new rules)."""
    if policy is None:
        policy = policy_for(event_edition.pk)
    counts = attendance_counts(event_edition.pk, policy)
    items = [
        ReadinessItem(READY_DAYS, days_configured(policy), True),
        ReadinessItem(
            READY_CAPACITY, getattr(policy, "opening_day_capacity", None) is not None, True
        ),
        ReadinessItem(READY_CLASSIFIED, counts.unclassified == 0, True, counts.unclassified),
        ReadinessItem(
            READY_WITHIN_CAPACITY,
            counts.opening_capacity is not None and counts.all_days <= counts.opening_capacity,
            True,
            counts.all_days,
        ),
        ReadinessItem(
            READY_BADGES, counts.badge_markings_pending == 0, True, counts.badge_markings_pending
        ),
    ]
    within_edition = False
    if days_configured(policy):
        first = local_date(event_edition.starts_at, event_edition.timezone)
        last = local_date(event_edition.ends_at, event_edition.timezone)
        within_edition = all(first <= day <= last for day in conference_days(policy))
    items.append(ReadinessItem(READY_EDITION_DATES, within_edition, False))
    packages = _current_offline_package_count(event_edition.pk)
    items.append(ReadinessItem(READY_OFFLINE, packages == 0, False, packages))
    return Readiness(items=tuple(items))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _audit(
    recorder: AuditRecorder,
    *,
    action_code: str,
    actor,
    target_type: str,
    target_uuid,
    event_edition_id,
    result: str = "SUCCESS",
    reason_code: str = "",
    before: dict | None = None,
    after: dict | None = None,
    correlation_id: str = "",
) -> None:
    recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=target_type,
            target_uuid=target_uuid,
            event_edition_id=event_edition_id,
            result=result,
            reason_code=reason_code[:100] or None,
            before_summary=before,
            after_summary=after,
            correlation_id=correlation_id,
        )
    )


def _normalize_reason(reason: str) -> str:
    reason = " ".join((reason or "").split())
    if not REASON_MIN_LENGTH <= len(reason) <= REASON_MAX_LENGTH:
        raise AttendanceStateError("A reason is required.", code="REASON_REQUIRED")
    return reason


def _require_category(category) -> str:
    if category not in AttendanceCategory.values:
        raise AttendanceChoiceRequiredError("Choose the attendance days explicitly.")
    return category


def _policy_for_grant(event_edition_id) -> AttendancePolicy:
    """The policy, LOCKED for every grant, classification or change -- not
    only an opening-day one -- always after the Registration lock.

    The lock serializes the grant with a change of the conference days
    (`update_policy` locks the same row and refuses once any entitlement
    exists): either the days change first and this grant reads and
    communicates the new days, or the grant commits first and the change of
    days is refused. It also keeps the opening-day count exact under
    simultaneous opening-day grants."""
    policy = (
        AttendancePolicy.objects.filter(event_edition_id=event_edition_id)
        .select_for_update()
        .first()
    )
    if not is_configured(policy):
        raise AttendanceNotConfiguredError(
            "The attendance days and the opening-day capacity are not configured."
        )
    return policy


def _require_opening_place(policy: AttendancePolicy) -> None:
    allocated = opening_allocation_count(policy.event_edition_id)
    if allocated >= policy.opening_day_capacity:
        raise OpeningDayCapacityReachedError(
            f"The opening day is full ({allocated}/{policy.opening_day_capacity})."
        )


def _record_capacity_refusal(*, recorder, actor, registration, policy, correlation_id) -> None:
    """The refusal is evidence too: written in its own transaction after the
    refused one rolled back (same discipline as the badge issuance denials)."""
    with transaction.atomic():
        _audit(
            recorder,
            action_code=action_codes.ATTENDANCE_OPENING_CAPACITY_REFUSED,
            actor=actor,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            result="DENIED",
            reason_code=OpeningDayCapacityReachedError.code,
            after={"opening_day_capacity": getattr(policy, "opening_day_capacity", None)},
            correlation_id=correlation_id,
        )


def grant_at_approval(
    *,
    registration,
    category,
    decided_by,
    decision_id,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> AttendanceEntitlement:
    """Record the entitlement chosen with an APPROVED decision.

    MUST run inside the approval's transaction with `registration` already
    locked (`apps.reviews.services.record_approved_decision`). Locks the
    policy after the registration when the choice is the opening day and
    refuses when that day is full; any refusal rolls the approval back."""
    recorder = audit_recorder or PersistentAuditRecorder()
    category = _require_category(category)
    policy = _policy_for_grant(registration.event_edition_id)
    if category == AttendanceCategory.ALL_CONFERENCE_DAYS:
        # The registration is not APPROVED yet inside this transaction, so an
        # earlier entitlement of its own (reopened, then approved again) is
        # not counted: the count is exactly the places held by others.
        _require_opening_place(policy)
    now = timezone.now()
    previous = (
        AttendanceEntitlement.objects.select_for_update()
        .filter(registration=registration, status=AttendanceEntitlementStatus.CURRENT)
        .first()
    )
    if previous is not None:
        previous.status = AttendanceEntitlementStatus.SUPERSEDED
        previous.effective_until = now
        previous.save(update_fields=["status", "effective_until", "updated_at"])
    entitlement = AttendanceEntitlement.objects.create(
        registration=registration,
        event_edition_id=registration.event_edition_id,
        category=category,
        status=AttendanceEntitlementStatus.CURRENT,
        origin=AttendanceEntitlementOrigin.APPROVAL,
        effective_from=now,
        decided_by=decided_by,
        decision_id=decision_id,
        supersedes=previous,
    )
    _audit(
        recorder,
        action_code=action_codes.ATTENDANCE_ENTITLEMENT_GRANTED,
        actor=decided_by,
        target_type="Registration",
        target_uuid=registration.pk,
        event_edition_id=registration.event_edition_id,
        before={"category": previous.category} if previous is not None else None,
        after={"category": category, "origin": AttendanceEntitlementOrigin.APPROVAL},
        correlation_id=correlation_id,
    )
    return entitlement


@dataclass(frozen=True)
class EntitlementChange:
    entitlement: AttendanceEntitlement
    previous_category: str | None
    changed: bool


def change_entitlement(
    *,
    registration,
    category,
    actor,
    reason: str,
    expected_entitlement_id: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> EntitlementChange:
    """Classify an earlier approval, or change the days of an approved one.

    `expected_entitlement_id` is the CURRENT entitlement id the operator saw
    ("" for an unclassified registration): anything else means the
    entitlement changed meanwhile and the command is refused as stale.
    Re-submitting the current category changes nothing and notifies nobody.
    An upgrade to the opening day locks the policy and needs a free place;
    a downgrade releases the place immediately. The participant receives one
    change notification per new entitlement (idempotency key bound to it)."""
    from apps.registrations.models import Registration

    recorder = audit_recorder or PersistentAuditRecorder()
    category = _require_category(category)
    reason = _normalize_reason(reason)
    try:
        with transaction.atomic():
            locked = Registration.objects.select_for_update().get(pk=registration.pk)
            if not Registration.objects.filter(pk=locked.pk).filter(seat_holding_q()).exists():
                raise AttendanceStateError(
                    "Only an approved, current registration has attendance days.",
                    code="NOT_APPROVED",
                )
            current = (
                AttendanceEntitlement.objects.select_for_update()
                .filter(registration=locked, status=AttendanceEntitlementStatus.CURRENT)
                .first()
            )
            if str(getattr(current, "pk", "") or "") != str(expected_entitlement_id or ""):
                raise AttendanceConcurrencyError("The attendance days changed meanwhile.")
            if current is not None and current.category == category:
                return EntitlementChange(
                    entitlement=current, previous_category=current.category, changed=False
                )
            policy = _policy_for_grant(locked.event_edition_id)
            if category == AttendanceCategory.ALL_CONFERENCE_DAYS:
                _require_opening_place(policy)
            now = timezone.now()
            if current is not None:
                current.status = AttendanceEntitlementStatus.SUPERSEDED
                current.effective_until = now
                current.save(update_fields=["status", "effective_until", "updated_at"])
            origin = (
                AttendanceEntitlementOrigin.LEGACY_CLASSIFICATION
                if current is None
                else AttendanceEntitlementOrigin.CHANGE
            )
            entitlement = AttendanceEntitlement.objects.create(
                registration=locked,
                event_edition_id=locked.event_edition_id,
                category=category,
                status=AttendanceEntitlementStatus.CURRENT,
                origin=origin,
                effective_from=now,
                reason=reason,
                decided_by=actor,
                supersedes=current,
            )
            _audit(
                recorder,
                action_code=(
                    action_codes.ATTENDANCE_ENTITLEMENT_CLASSIFIED
                    if current is None
                    else action_codes.ATTENDANCE_ENTITLEMENT_CHANGED
                ),
                actor=actor,
                target_type="Registration",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                reason_code=reason,
                before={"category": getattr(current, "category", None)},
                after={"category": category, "origin": origin},
                correlation_id=correlation_id,
            )
            queue_attendance_notification(
                registration=locked,
                entitlement=entitlement,
                policy=policy,
                purpose_code=CommunicationPurpose.ATTENDANCE_CHANGE,
                idempotency_key=f"attendance-change:{entitlement.pk}",
            )
    except OpeningDayCapacityReachedError:
        _record_capacity_refusal(
            recorder=recorder,
            actor=actor,
            registration=registration,
            policy=policy_for(registration.event_edition_id),
            correlation_id=correlation_id,
        )
        raise
    return EntitlementChange(
        entitlement=entitlement,
        previous_category=getattr(current, "category", None),
        changed=True,
    )


def _ensure_policy(event_edition_id) -> None:
    if AttendancePolicy.objects.filter(event_edition_id=event_edition_id).exists():
        return
    try:
        with transaction.atomic():
            AttendancePolicy.objects.create(event_edition_id=event_edition_id)
    except IntegrityError:  # created by a simultaneous request: use that row
        pass


def _policy_summary(policy: AttendancePolicy) -> dict:
    return {
        "opening_date": policy.opening_date.isoformat() if policy.opening_date else None,
        "second_date": policy.second_date.isoformat() if policy.second_date else None,
        "third_date": policy.third_date.isoformat() if policy.third_date else None,
        "opening_day_capacity": policy.opening_day_capacity,
    }


def update_policy(
    *,
    event_edition_id,
    actor,
    opening_date: date | None,
    second_date: date | None,
    third_date: date | None,
    opening_day_capacity: int | None,
    expected_version: int | None,
    reason: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> tuple[AttendancePolicy, bool]:
    """Set the three conference days and the opening-day capacity.

    * all three days or none, strictly increasing;
    * the days are frozen once any entitlement exists for the edition or
      while enforcement is active (participants were told those dates);
    * the capacity can never be set below the places already allocated, and
      cannot be cleared while places are allocated;
    * `expected_version` rejects a change made from a stale page.
    Returns the policy and whether anything changed (an identical submission
    is not audited again)."""
    recorder = audit_recorder or PersistentAuditRecorder()
    reason = _normalize_reason(reason)
    dates = (opening_date, second_date, third_date)
    if any(dates) and not all(dates):
        raise AttendancePolicyError("Enter all three days.", code="DAYS_INCOMPLETE")
    if all(dates) and not (opening_date < second_date < third_date):
        raise AttendancePolicyError("The days must follow each other.", code="DAYS_ORDER")
    if opening_day_capacity is not None and opening_day_capacity < 0:
        raise AttendancePolicyError("The capacity cannot be negative.", code="CAPACITY_INVALID")
    _ensure_policy(event_edition_id)
    with transaction.atomic():
        policy = AttendancePolicy.objects.select_for_update().get(event_edition_id=event_edition_id)
        if expected_version is not None and policy.version != expected_version:
            raise AttendanceConcurrencyError("The attendance settings changed meanwhile.")
        before = _policy_summary(policy)
        days_change = dates != (policy.opening_date, policy.second_date, policy.third_date)
        if days_change and (
            policy.enforcement_active
            or AttendanceEntitlement.objects.filter(event_edition_id=event_edition_id).exists()
        ):
            raise AttendancePolicyError(
                "The conference days can no longer change.", code="DAYS_LOCKED"
            )
        allocated = opening_allocation_count(event_edition_id)
        if opening_day_capacity is None and (allocated or policy.enforcement_active):
            raise AttendancePolicyError("A capacity is required.", code="CAPACITY_REQUIRED")
        if opening_day_capacity is not None and opening_day_capacity < allocated:
            raise AttendancePolicyError(
                f"The capacity cannot be lower than the {allocated} places already allocated.",
                code="CAPACITY_BELOW_ALLOCATED",
            )
        policy.opening_date, policy.second_date, policy.third_date = dates
        policy.opening_day_capacity = opening_day_capacity
        after = _policy_summary(policy)
        if after == before:
            return policy, False
        policy.updated_by = actor
        policy.version += 1
        policy.save()
        _audit(
            recorder,
            action_code=action_codes.ATTENDANCE_POLICY_UPDATED,
            actor=actor,
            target_type="EventEdition",
            target_uuid=event_edition_id,
            event_edition_id=event_edition_id,
            reason_code=reason,
            before=before,
            after=after | {"allocated": allocated},
            correlation_id=correlation_id,
        )
    return policy, True


def _revoke_offline_packages(event_edition_id, *, now) -> int:
    """Revoke the edition's current offline packages, so every device fetches
    a package built under the rules now in force (a device holding a revoked
    package cannot obtain deltas for it and refreshes)."""
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_packages import revoke_device_packages

    revoked = 0
    for device in EntryDevice.objects.select_for_update().filter(event_edition_id=event_edition_id):
        revoked += revoke_device_packages(device, reason=OFFLINE_PACKAGE_REVOCATION_REASON, now=now)
    return revoked


def activate_enforcement(
    *,
    event_edition,
    actor,
    expected_version: int,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> AttendancePolicy:
    """Switch admission enforcement on, after re-checking every prerequisite
    under the policy lock (no opening-day place can be added meanwhile).
    Revokes the edition's current offline packages in the same transaction.
    A refusal is audited with the unmet prerequisite codes."""
    recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        policy = (
            AttendancePolicy.objects.select_for_update()
            .filter(event_edition_id=event_edition.pk)
            .first()
        )
        if policy is None:
            refused = readiness(event_edition, None)
        else:
            if policy.version != expected_version:
                raise AttendanceConcurrencyError("The attendance settings changed meanwhile.")
            if policy.enforcement_active:
                return policy
            refused = readiness(event_edition, policy)
        if policy is None or not refused.ready:
            failure = refused
        else:
            failure = None
            now = timezone.now()
            policy.enforcement_active = True
            policy.enforcement_activated_at = now
            policy.enforcement_activated_by = actor
            policy.version += 1
            policy.updated_by = actor
            policy.save()
            AttendanceEnforcementInterval.objects.create(
                policy=policy, started_at=now, started_by=actor
            )
            packages = _revoke_offline_packages(event_edition.pk, now=now)
            _audit(
                recorder,
                action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATED,
                actor=actor,
                target_type="EventEdition",
                target_uuid=event_edition.pk,
                event_edition_id=event_edition.pk,
                after=_policy_summary(policy) | {"offline_packages_revoked": packages},
                correlation_id=correlation_id,
            )
    if failure is not None:
        with transaction.atomic():
            _audit(
                recorder,
                action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATION_REFUSED,
                actor=actor,
                target_type="EventEdition",
                target_uuid=event_edition.pk,
                event_edition_id=event_edition.pk,
                result="DENIED",
                reason_code=",".join(failure.blocking_codes),
                correlation_id=correlation_id,
            )
        raise AttendanceActivationRefused(failure)
    return policy


def deactivate_enforcement(
    *,
    event_edition,
    actor,
    expected_version: int,
    reason: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> AttendancePolicy:
    """Switch admission enforcement off (the documented rollback step).
    Entitlements, capacity and history are kept; offline packages are
    revoked so devices stop using data prepared under the attendance rules."""
    recorder = audit_recorder or PersistentAuditRecorder()
    reason = _normalize_reason(reason)
    with transaction.atomic():
        policy = (
            AttendancePolicy.objects.select_for_update()
            .filter(event_edition_id=event_edition.pk)
            .first()
        )
        if policy is None or not policy.enforcement_active:
            raise AttendanceStateError("Enforcement is not active.", code="NOT_ACTIVE")
        if policy.version != expected_version:
            raise AttendanceConcurrencyError("The attendance settings changed meanwhile.")
        now = timezone.now()
        policy.enforcement_active = False
        policy.enforcement_deactivated_at = now
        policy.enforcement_deactivated_by = actor
        policy.version += 1
        policy.updated_by = actor
        policy.save()
        closed = AttendanceEnforcementInterval.objects.filter(
            policy=policy, ended_at__isnull=True
        ).update(ended_at=now, ended_by=actor)
        if closed == 0 and policy.enforcement_activated_at < now:
            # Switched on by a release that predates the periods: record the
            # period from that activation instead of refusing the switch-off.
            AttendanceEnforcementInterval.objects.create(
                policy=policy,
                started_at=policy.enforcement_activated_at,
                started_by_id=policy.enforcement_activated_by_id,
                ended_at=now,
                ended_by=actor,
                source=AttendanceEnforcementIntervalSource.RECONSTRUCTED,
            )
        packages = _revoke_offline_packages(event_edition.pk, now=now)
        _audit(
            recorder,
            action_code=action_codes.ATTENDANCE_ENFORCEMENT_DEACTIVATED,
            actor=actor,
            target_type="EventEdition",
            target_uuid=event_edition.pk,
            event_edition_id=event_edition.pk,
            reason_code=reason,
            after={"offline_packages_revoked": packages},
            correlation_id=correlation_id,
        )
    return policy


# ---------------------------------------------------------------------------
# Admission (online and offline agree on these rules)
# ---------------------------------------------------------------------------


def admission_problem(*, registration, event_edition, at: datetime) -> str:
    """The attendance reason code that forbids admission at `at`, or "".

    Applies only while enforcement was active at `at`. The local calendar
    date of `at` in the edition timezone decides the day; an unclassified
    registration is never admitted, a `FOLLOWING_TWO_DAYS` one is refused on
    the opening day, and no participant is admitted on a day that is not one
    of the three conference days. Never widens anything: every other check
    of the admission evaluator still applies."""
    policy = policy_for(event_edition.pk)
    if not enforcement_active_at(policy, at):
        return ""
    kind = day_kind(policy, event_edition.timezone, at)
    if kind == NOT_A_CONFERENCE_DAY:
        return NOT_CONFERENCE_DAY
    entitlement = entitlement_at(registration.pk, at)
    if entitlement is None:
        return UNCLASSIFIED
    if not category_covers(entitlement.category, kind):
        return DAY_NOT_AUTHORIZED
    return ""


def registrations_authorized_on(event_edition, registration_ids, at: datetime) -> set | None:
    """For an offline package built at `at`: the registration ids whose
    attendance covers the local day of `at`, or None when enforcement is not
    active (no attendance filtering). A package never outlives the event day
    of its cutoff (`apps.entry.offline_contract.package_bands`)."""
    policy = policy_for(event_edition.pk)
    if not enforcement_active_at(policy, at):
        return None
    kind = day_kind(policy, event_edition.timezone, at)
    if kind == NOT_A_CONFERENCE_DAY:
        return set()
    categories = (
        [AttendanceCategory.ALL_CONFERENCE_DAYS]
        if kind == OPENING_DAY
        else list(AttendanceCategory.values)
    )
    return set(
        AttendanceEntitlement.objects.filter(
            registration_id__in=list(registration_ids),
            category__in=categories,
            effective_from__lte=at,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=at))
        .values_list("registration_id", flat=True)
    )


def registrations_withdrawn_for_package_day(
    event_edition, registration_ids, *, package_cutoff: datetime
) -> set:
    """For a critical delta: the registration ids a package (valid only on the
    local day of its cutoff) must stop admitting under the CURRENT
    entitlements, when enforcement is active now. Empty when it is not."""
    policy = policy_for(event_edition.pk)
    if policy is None or not policy.enforcement_active:
        return set()
    registration_ids = set(registration_ids)
    kind = day_kind(policy, event_edition.timezone, package_cutoff)
    if kind == NOT_A_CONFERENCE_DAY:
        return registration_ids
    categories = (
        [AttendanceCategory.ALL_CONFERENCE_DAYS]
        if kind == OPENING_DAY
        else list(AttendanceCategory.values)
    )
    authorized = set(
        AttendanceEntitlement.objects.filter(
            registration_id__in=list(registration_ids),
            status=AttendanceEntitlementStatus.CURRENT,
            category__in=categories,
        ).values_list("registration_id", flat=True)
    )
    return registration_ids - authorized


# ---------------------------------------------------------------------------
# Participant-facing wording (workspace, registration page, pass, badge, email)
# ---------------------------------------------------------------------------


STATE_PENDING = "PENDING"


def format_day(day: date) -> str:
    """A full, unambiguous localized date ("Saturday 5 December 2026")."""
    return formats.date_format(day, "l j F Y")


def attendance_statement(category: str, policy: AttendancePolicy) -> str:
    """The sentence a participant reads, in the active language."""
    opening, second, third = (format_day(day) for day in conference_days(policy))
    if category == AttendanceCategory.ALL_CONFERENCE_DAYS:
        return _(
            "Your participation is approved for the opening day and the following two "
            "conference days: %(day1)s, %(day2)s and %(day3)s."
        ) % {"day1": opening, "day2": second, "day3": third}
    return _(
        "Your participation is approved for %(day2)s and %(day3)s. This approval does not "
        "include the opening day, %(day1)s."
    ) % {"day1": opening, "day2": second, "day3": third}


def pending_statement() -> str:
    return _(
        "Your participation is approved. The organizers are confirming which conference days "
        "it covers. The authorized days will appear here and you will be notified by email."
    )


def short_label(category: str) -> str:
    """The visible marking text of a pass or a printable badge."""
    if category == AttendanceCategory.ALL_CONFERENCE_DAYS:
        return _("All three conference days")
    return _("Days 2 and 3 only, not the opening day")


@dataclass(frozen=True)
class ParticipantAttendance:
    """What a participant-facing surface shows for one approved registration."""

    state: str  # an AttendanceCategory value, or STATE_PENDING
    statement: str
    label: str
    days: tuple[str, ...]
    excluded_day: str

    @property
    def is_pending(self) -> bool:
        return self.state == STATE_PENDING


_UNSET = object()


def is_seat_holding(registration) -> bool:
    """In-memory twin of `seat_holding_q()` for an already-loaded row."""
    from apps.registrations.models import RegistrationPublicStatus

    return (
        registration.public_status == RegistrationPublicStatus.APPROVED
        and bool(registration.is_current_context)
        and registration.withdrawn_at is None
        and registration.cancelled_at is None
    )


def participant_attendance(
    registration, *, entitlement=_UNSET, policy=_UNSET
) -> ParticipantAttendance | None:
    """The attendance shown with an APPROVED registration, in the active
    language; None for any other status (a submission receipt never shows
    attendance days). An approved registration without a current
    entitlement shows the honest pending wording, never guessed dates.
    `entitlement` / `policy` may be passed when the caller already read them."""
    from apps.registrations.models import RegistrationPublicStatus

    if registration.public_status != RegistrationPublicStatus.APPROVED:
        return None
    if entitlement is _UNSET:
        entitlement = current_entitlement(registration.pk)
    if policy is _UNSET:
        policy = policy_for(registration.event_edition_id)
    if entitlement is None or not days_configured(policy):
        return ParticipantAttendance(
            state=STATE_PENDING, statement=pending_statement(), label="", days=(), excluded_day=""
        )
    days = tuple(format_day(day) for day in authorized_dates(policy, entitlement.category))
    excluded = (
        format_day(policy.opening_date)
        if entitlement.category == AttendanceCategory.FOLLOWING_TWO_DAYS
        else ""
    )
    return ParticipantAttendance(
        state=entitlement.category,
        statement=attendance_statement(entitlement.category, policy),
        label=short_label(entitlement.category),
        days=days,
        excluded_day=excluded,
    )


def statement_in(language: str, category: str, policy: AttendancePolicy) -> str:
    with translation.override(language or "en"):
        return attendance_statement(category, policy)


def queue_attendance_notification(
    *, registration, entitlement, policy, purpose_code: str, idempotency_key: str, context=None
):
    """Queue the participant notification for `entitlement` through the
    existing communication outbox (best effort: a missing verified email
    contact or template never fails the command). Rendered in the
    registration's own language."""
    from apps.reviews.services import _queue_registration_communication

    language = registration.preferred_language or "en"
    statement = statement_in(language, entitlement.category, policy)
    return _queue_registration_communication(
        registration=registration,
        purpose_code=purpose_code,
        context={
            "public_reference": registration.public_reference,
            "event_name": registration.event_edition.display_name(language),
            "attendance_statement": statement,
            **(context or {}),
        },
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Advisory helpers for the configuration page
# ---------------------------------------------------------------------------


def suggested_days(event_edition) -> tuple[date, date, date] | None:
    """The edition's first three local days, offered as a starting value on
    the configuration page only when the edition's own dates span at least
    three days. Never saved without an operator submitting it."""
    first = local_date(event_edition.starts_at, event_edition.timezone)
    last = local_date(event_edition.ends_at, event_edition.timezone)
    if (last - first).days < 2:
        return None
    return first, first + timedelta(days=1), first + timedelta(days=2)
