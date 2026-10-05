"""Accreditation assignment commands: assign, change, revoke, bulk
operations, and the Phase 3 eligibility boundary (Phase 2 Prompt 4).

Every material command locks the parent Registration row first (serializing
every accreditation mutation for that registration, across all four
assignment kinds, exactly like `apps.reviews.services._lock_versioned`
serializes review-domain mutations) then re-checks the target assignment
state under that lock -- a database constraint is always the backstop, but
the lock means a concurrent racer is normally rejected with a controlled
domain error, never a raw `IntegrityError`.
"""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accreditation.models import (
    AccessProfileAssignment,
    AccessRuleAssignment,
    AssignmentStatus,
    BadgeTypeAssignment,
    BulkAssignmentKind,
    BulkAssignmentOperation,
    BulkOperationStatus,
    ParticipantRoleAssignment,
)
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.registrations.models import Registration, RegistrationPublicStatus


class AssignmentAlreadyCurrentError(Exception):
    """Raised when the target already has a CURRENT assignment for this
    exact (registration, reference object) -- or, for badge type, for this
    registration at all (Phase 2 Prompt 4 exclusivity rules)."""


class AssignmentNotFoundError(Exception):
    """Raised when there is no CURRENT assignment to change or revoke."""


class InvalidEffectiveRangeError(Exception):
    """Raised when `effective_until` is not strictly after `effective_from`."""


class InvalidReferenceError(Exception):
    """Raised when reference data is inactive, wrong-event, or inconsistent."""


class StaleVersionError(Exception):
    """Raised when a caller's `expected_version` no longer matches the
    locked assignment row's current version."""


def _validate_range(effective_from, effective_until) -> None:
    if effective_until is not None and effective_until <= effective_from:
        raise InvalidEffectiveRangeError("effective_until must be strictly after effective_from.")


_KIND_CONFIG = {
    BulkAssignmentKind.PARTICIPANT_ROLE: {
        "model": ParticipantRoleAssignment,
        "ref_field": "role",
        "related_name": "role_assignments",
        "exclusive_per_registration": True,
        "assigned_code": action_codes.PARTICIPANT_ROLE_ASSIGNED,
        "changed_code": action_codes.PARTICIPANT_ROLE_CHANGED,
        "revoked_code": action_codes.PARTICIPANT_ROLE_REVOKED,
    },
    BulkAssignmentKind.BADGE_TYPE: {
        "model": BadgeTypeAssignment,
        "ref_field": "badge_type",
        "related_name": "badge_assignments",
        "exclusive_per_registration": True,
        "assigned_code": action_codes.BADGE_TYPE_ASSIGNED,
        "changed_code": action_codes.BADGE_TYPE_CHANGED,
        "revoked_code": action_codes.BADGE_TYPE_REVOKED,
    },
    BulkAssignmentKind.ACCESS_PROFILE: {
        "model": AccessProfileAssignment,
        "ref_field": "access_profile",
        "related_name": "access_profile_assignments",
        "exclusive_per_registration": True,
        "assigned_code": action_codes.ACCESS_PROFILE_ASSIGNED,
        "changed_code": action_codes.ACCESS_PROFILE_CHANGED,
        "revoked_code": action_codes.ACCESS_PROFILE_REVOKED,
    },
    BulkAssignmentKind.ACCESS_RULE: {
        "model": AccessRuleAssignment,
        "ref_field": "access_rule",
        "related_name": "access_rule_assignments",
        "exclusive_per_registration": False,
        "assigned_code": action_codes.ACCESS_RULE_ASSIGNED,
        "changed_code": action_codes.ACCESS_RULE_CHANGED,
        "revoked_code": action_codes.ACCESS_RULE_REVOKED,
    },
}


def _lock_valid_reference(*, config, reference_obj, event_edition_id):
    """Return a locked, active reference belonging to the target event."""
    reference_model = config["model"]._meta.get_field(config["ref_field"]).related_model
    locked_reference = (
        reference_model.objects.select_for_update()
        .filter(
            pk=getattr(reference_obj, "pk", reference_obj),
            event_edition_id=event_edition_id,
            is_active=True,
        )
        .first()
    )
    if locked_reference is None:
        raise InvalidReferenceError(
            "The selected reference is inactive or belongs to a different event."
        )
    linked_profile = getattr(locked_reference, "access_profile", None)
    if linked_profile is not None and (
        linked_profile.event_edition_id != event_edition_id or not linked_profile.is_active
    ):
        raise InvalidReferenceError(
            "The selected access rule has an inactive or inconsistently scoped profile."
        )
    return locked_reference


def assign(
    *,
    kind: str,
    registration: Registration,
    reference_obj,
    actor,
    effective_from=None,
    effective_until=None,
    reason: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
):
    """Create one new CURRENT assignment. Raises `AssignmentAlreadyCurrentError`
    if one already exists for this exclusivity scope (Phase 2 Prompt 4 §4)."""
    config = _KIND_CONFIG[kind]
    model = config["model"]
    effective_from = effective_from or timezone.now()
    _validate_range(effective_from, effective_until)
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)
        reference_obj = _lock_valid_reference(
            config=config,
            reference_obj=reference_obj,
            event_edition_id=locked_registration.event_edition_id,
        )
        existing_filter = {"registration": locked_registration, "status": AssignmentStatus.CURRENT}
        if not config["exclusive_per_registration"]:
            existing_filter[config["ref_field"]] = reference_obj
        existing = model.objects.select_for_update().filter(**existing_filter).first()
        if existing is not None:
            raise AssignmentAlreadyCurrentError(
                f"A CURRENT {kind} assignment already exists for this scope."
            )
        try:
            with transaction.atomic():
                assignment = model.objects.create(
                    registration=locked_registration,
                    event_edition_id=locked_registration.event_edition_id,
                    organization_id=locked_registration.source_organization_id,
                    status=AssignmentStatus.CURRENT,
                    effective_from=effective_from,
                    effective_until=effective_until,
                    reason=reason[:300],
                    created_by=actor,
                    **{config["ref_field"]: reference_obj},
                )
        except IntegrityError:
            raise AssignmentAlreadyCurrentError(
                f"A CURRENT {kind} assignment already exists for this scope."
            ) from None
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=config["assigned_code"],
                target_type="Registration",
                target_uuid=locked_registration.pk,
                event_edition_id=locked_registration.event_edition_id,
                result="SUCCESS",
                after_summary={"reference_object_id": str(reference_obj.pk)},
                correlation_id=correlation_id,
            )
        )
    return assignment


def change(
    *,
    kind: str,
    current_assignment,
    new_reference_obj,
    expected_version: int,
    actor,
    reason: str = "",
    effective_from=None,
    effective_until=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
):
    """Supersede `current_assignment` and create a new CURRENT one pointing
    to `new_reference_obj` -- the prior row is preserved, never deleted."""
    config = _KIND_CONFIG[kind]
    model = config["model"]
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    effective_from = effective_from or now
    _validate_range(effective_from, effective_until)
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(
            pk=current_assignment.registration_id
        )
        locked = model.objects.select_for_update().get(
            pk=current_assignment.pk, registration=locked_registration
        )
        if locked.version != expected_version:
            raise StaleVersionError(f"Expected version {expected_version}, found {locked.version}.")
        if locked.status != AssignmentStatus.CURRENT:
            raise AssignmentNotFoundError("This assignment is not currently active.")
        new_reference_obj = _lock_valid_reference(
            config=config,
            reference_obj=new_reference_obj,
            event_edition_id=locked_registration.event_edition_id,
        )
        if getattr(locked, f"{config['ref_field']}_id") == new_reference_obj.pk:
            raise AssignmentAlreadyCurrentError("The requested reference is already current.")
        locked.status = AssignmentStatus.SUPERSEDED
        locked.effective_until = now
        locked.ended_by = actor
        locked.ended_at = now
        locked.end_reason = reason[:300]
        locked.version = locked.version + 1
        locked.save(
            update_fields=[
                "status",
                "effective_until",
                "ended_by",
                "ended_at",
                "end_reason",
                "version",
                "updated_at",
            ]
        )

        try:
            with transaction.atomic():
                new_assignment = model.objects.create(
                    registration=locked_registration,
                    event_edition_id=locked_registration.event_edition_id,
                    organization_id=locked_registration.source_organization_id,
                    status=AssignmentStatus.CURRENT,
                    effective_from=effective_from,
                    effective_until=effective_until,
                    reason=reason[:300],
                    created_by=actor,
                    supersedes=locked,
                    **{config["ref_field"]: new_reference_obj},
                )
        except IntegrityError:
            raise AssignmentAlreadyCurrentError(
                f"A CURRENT {kind} assignment already exists for this scope."
            ) from None
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=config["changed_code"],
                target_type="Registration",
                target_uuid=locked.registration_id,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                before_summary={
                    "reference_object_id": str(getattr(locked, config["ref_field"]).pk)
                },
                after_summary={"reference_object_id": str(new_reference_obj.pk)},
                correlation_id=correlation_id,
            )
        )
    return new_assignment


def revoke(
    *,
    kind: str,
    current_assignment,
    expected_version: int,
    actor,
    reason: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
):
    if not reason:
        raise ValueError("Revocation requires a reason.")
    config = _KIND_CONFIG[kind]
    model = config["model"]
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(
            pk=current_assignment.registration_id
        )
        locked = model.objects.select_for_update().get(
            pk=current_assignment.pk, registration=locked_registration
        )
        if locked.version != expected_version:
            raise StaleVersionError(f"Expected version {expected_version}, found {locked.version}.")
        if locked.status != AssignmentStatus.CURRENT:
            raise AssignmentNotFoundError("This assignment is not currently active.")
        locked.status = AssignmentStatus.REVOKED
        now = timezone.now()
        locked.effective_until = now
        locked.ended_by = actor
        locked.ended_at = now
        locked.end_reason = reason[:300]
        locked.version = locked.version + 1
        locked.save(
            update_fields=[
                "status",
                "effective_until",
                "ended_by",
                "ended_at",
                "end_reason",
                "version",
                "updated_at",
            ]
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=config["revoked_code"],
                target_type="Registration",
                target_uuid=locked.registration_id,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                reason_code=reason[:100],
                correlation_id=correlation_id,
            )
        )
    return locked


def expire_due_assignments(
    *, now=None, audit_recorder: AuditRecorder | None = None, correlation_id: str = ""
) -> int:
    """Mark every CURRENT assignment whose `effective_until` has passed as
    EXPIRED -- a plain, idempotent, callable sweep (no Celery, per
    ADR-0009 local posture), safe to run repeatedly. Returns the count of
    rows expired across all four assignment kinds."""
    now = now or timezone.now()
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    expired_total = 0
    for config in _KIND_CONFIG.values():
        model = config["model"]
        due_rows = list(
            model.objects.filter(
                status=AssignmentStatus.CURRENT,
                effective_until__isnull=False,
                effective_until__lte=now,
            ).values_list("pk", "registration_id")
        )
        for assignment_id, registration_id in due_rows:
            with transaction.atomic():
                Registration.objects.select_for_update().get(pk=registration_id)
                assignment = (
                    model.objects.select_for_update()
                    .filter(
                        pk=assignment_id,
                        status=AssignmentStatus.CURRENT,
                        effective_until__isnull=False,
                        effective_until__lte=now,
                    )
                    .first()
                )
                if assignment is None:
                    continue
                assignment.status = AssignmentStatus.EXPIRED
                assignment.ended_at = now
                assignment.end_reason = "effective_period_ended"
                assignment.version += 1
                assignment.save(
                    update_fields=[
                        "status",
                        "ended_at",
                        "end_reason",
                        "version",
                        "updated_at",
                    ]
                )
                audit_recorder.record(
                    AuditRecord(
                        actor_type="SYSTEM",
                        action_code=action_codes.ASSIGNMENT_EXPIRED,
                        target_type=model.__name__,
                        target_uuid=assignment.pk,
                        event_edition_id=assignment.event_edition_id,
                        result="SUCCESS",
                        correlation_id=correlation_id,
                    )
                )
                expired_total += 1
    return expired_total


# ---------------------------------------------------------------------------
# Bulk operations (preview -> execute)
# ---------------------------------------------------------------------------


def preview_bulk_assignment(
    *,
    kind: str,
    registration_ids: list,
    reference_obj,
    requested_by,
    reason: str = "",
    idempotency_key: str | None = None,
) -> BulkAssignmentOperation:
    """Step 1-4 of the bulk workflow (Phase 2 Prompt 4 §5): validate every
    targeted registration WITHOUT mutating anything, and persist the exact
    resulting scope + per-row outcome so `execute_bulk_assignment` can
    never silently act on a different set."""
    config = _KIND_CONFIG[kind]
    model = config["model"]
    registration_ids = list(dict.fromkeys(registration_ids))
    registrations = list(
        Registration.objects.filter(pk__in=registration_ids).select_related("event_edition")
    )

    with transaction.atomic():
        reference_obj = _lock_valid_reference(
            config=config,
            reference_obj=reference_obj,
            event_edition_id=reference_obj.event_edition_id,
        )

    if idempotency_key is None:
        idempotency_key = uuid.uuid4().hex
    existing = BulkAssignmentOperation.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    preview_results = []
    accepted_ids = []
    for reg_id in registration_ids:
        reg_id_str = str(reg_id)
        registration = next((r for r in registrations if str(r.pk) == reg_id_str), None)
        if registration is None:
            preview_results.append(
                {"registration_id": reg_id_str, "eligible": False, "reason": "NOT_FOUND"}
            )
            continue
        if registration.event_edition_id != reference_obj.event_edition_id:
            preview_results.append(
                {"registration_id": reg_id_str, "eligible": False, "reason": "WRONG_EVENT"}
            )
            continue
        already_current_filter = {"registration": registration, "status": AssignmentStatus.CURRENT}
        if not config["exclusive_per_registration"]:
            already_current_filter[config["ref_field"]] = reference_obj
        if model.objects.filter(**already_current_filter).exists():
            preview_results.append(
                {
                    "registration_id": reg_id_str,
                    "eligible": False,
                    "reason": "ALREADY_CURRENT",
                }
            )
            continue
        preview_results.append(
            {
                "registration_id": reg_id_str,
                "registration_version": registration.version,
                "eligible": True,
                "reason": "",
            }
        )
        accepted_ids.append(reg_id_str)

    accepted_organizations = {
        registration.source_organization_id
        for registration in registrations
        if str(registration.pk) in accepted_ids
    }
    operation_organization_id = (
        accepted_organizations.pop() if len(accepted_organizations) == 1 else None
    )

    operation = BulkAssignmentOperation.objects.create(
        kind=kind,
        reference_object_id=reference_obj.pk,
        event_edition=reference_obj.event_edition,
        organization_id=operation_organization_id,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        reason=reason[:300],
        target_registration_ids=accepted_ids,
        preview_results=preview_results,
        status=BulkOperationStatus.PREVIEWED,
        previewed_at=timezone.now(),
    )
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(requested_by, "pk", None),
            action_code=action_codes.BULK_ASSIGNMENT_PREVIEWED,
            target_type="BulkAssignmentOperation",
            target_uuid=operation.pk,
            event_edition_id=reference_obj.event_edition_id,
            result="SUCCESS",
            after_summary={
                "accepted_count": len(accepted_ids),
                "rejected_count": len(preview_results) - len(accepted_ids),
            },
        )
    )
    return operation


def execute_bulk_assignment(
    *, operation: BulkAssignmentOperation, actor, audit_recorder: AuditRecorder | None = None
) -> BulkAssignmentOperation:
    """Step 5-8 of the bulk workflow: execute EXACTLY the previewed scope,
    re-validating each row's current state at execution time (Phase 2
    Prompt 4 §5 "must not silently expand beyond the previewed scope").
    Idempotent: re-executing an already-EXECUTED operation returns it
    unchanged."""
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_operation = BulkAssignmentOperation.objects.select_for_update().get(pk=operation.pk)
        if locked_operation.status == BulkOperationStatus.EXECUTED:
            return locked_operation
        config = _KIND_CONFIG[locked_operation.kind]
        model = config["model"]
        reference_model = model._meta.get_field(config["ref_field"]).related_model
        reference_obj = reference_model.objects.get(pk=locked_operation.reference_object_id)
        preview_versions = {
            row["registration_id"]: row.get("registration_version")
            for row in locked_operation.preview_results
            if row.get("eligible")
        }

        execution_results = []
        for reg_id in locked_operation.target_registration_ids:
            try:
                registration = Registration.objects.select_for_update().get(pk=reg_id)
                if preview_versions.get(str(reg_id)) != registration.version:
                    execution_results.append(
                        {
                            "registration_id": str(reg_id),
                            "success": False,
                            "reason": "STALE_REGISTRATION",
                        }
                    )
                    continue
                assign(
                    kind=locked_operation.kind,
                    registration=registration,
                    reference_obj=reference_obj,
                    actor=actor,
                    reason=locked_operation.reason,
                    audit_recorder=audit_recorder,
                )
                execution_results.append(
                    {"registration_id": str(reg_id), "success": True, "reason": ""}
                )
            except AssignmentAlreadyCurrentError:
                execution_results.append(
                    {
                        "registration_id": str(reg_id),
                        "success": False,
                        "reason": "ALREADY_CURRENT",
                    }
                )
            except InvalidReferenceError:
                execution_results.append(
                    {
                        "registration_id": str(reg_id),
                        "success": False,
                        "reason": "INVALID_TARGET",
                    }
                )
            except Registration.DoesNotExist:
                execution_results.append(
                    {
                        "registration_id": str(reg_id),
                        "success": False,
                        "reason": "INVALID_TARGET",
                    }
                )

        locked_operation.execution_results = execution_results
        locked_operation.status = BulkOperationStatus.EXECUTED
        locked_operation.executed_at = timezone.now()
        locked_operation.save(
            update_fields=["execution_results", "status", "executed_at", "updated_at"]
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.BULK_ASSIGNMENT_EXECUTED,
                target_type="BulkAssignmentOperation",
                target_uuid=locked_operation.pk,
                event_edition_id=locked_operation.event_edition_id,
                result="SUCCESS",
                after_summary={
                    "success_count": sum(1 for r in execution_results if r["success"]),
                    "failure_count": sum(1 for r in execution_results if not r["success"]),
                },
            )
        )
    return locked_operation


def has_required_assignments_for_approval(registration: Registration) -> bool:
    """Whether `registration` currently holds the assignments an APPROVED
    decision requires (Phase 2 Prompt 3/4 boundary): at least one CURRENT
    `ParticipantRoleAssignment`, exactly one CURRENT `BadgeTypeAssignment`,
    and exactly one CURRENT `AccessProfileAssignment`. Checked independently
    of the registration's OWN `public_status` (this runs BEFORE that status
    is set to `APPROVED`).
    """
    from apps.accreditation.selectors import (
        current_access_profile_assignments,
        current_badge_assignment,
        current_role_assignments,
    )

    return (
        current_role_assignments(registration).exists()
        and current_badge_assignment(registration) is not None
        and current_access_profile_assignments(registration).exists()
    )


# ---------------------------------------------------------------------------
# Phase 3 eligibility boundary (deterministic, deny-by-default)
# ---------------------------------------------------------------------------


class EligibilityReason:
    ELIGIBLE = "ELIGIBLE"
    NOT_APPROVED = "NOT_APPROVED"
    NO_CURRENT_ROLE = "NO_CURRENT_ROLE"
    NO_CURRENT_BADGE = "NO_CURRENT_BADGE"
    NO_CURRENT_ACCESS_PROFILE = "NO_CURRENT_ACCESS_PROFILE"
    BADGE_OUT_OF_SCOPE = "BADGE_OUT_OF_SCOPE"
    # Owner decision IDV-Q1: an approval never outlives the identity it needs.
    IDENTITY_NOT_CLEARED = "IDENTITY_NOT_CLEARED"


def evaluate_eligibility(registration: Registration, *, correlation_id: str = "") -> dict:
    """The ONE reviewed, server-side eligibility boundary Phase 3 may call
    (Phase 2 Prompt 4 §8). Deterministic and deny-by-default: returns
    `{"eligible": bool, "reason": str}` only -- never a pass, QR payload,
    or badge credential of any kind. Requires the registration's current
    `RegistrationDecision` to be `APPROVED`, a cleared identity (owner
    decision IDV-Q1: current, valid, verified), and at least one CURRENT,
    non-expired, correctly-scoped `ParticipantRoleAssignment` and exactly
    one CURRENT `BadgeTypeAssignment`.
    """
    from apps.accreditation.selectors import (
        current_access_profile_assignments,
        current_badge_assignment,
        current_role_assignments,
    )
    from apps.people.selectors.clearance import identity_clearance

    now = timezone.now()

    def _not_expired(assignment) -> bool:
        # Defensive, real-time check (Phase 2 Prompt 4 §8 "reject...
        # expired... assignment state") -- never relies solely on the
        # periodic `expire_due_assignments()` sweep having already run.
        return assignment.effective_until is None or assignment.effective_until > now

    result = {"eligible": False, "reason": EligibilityReason.NOT_APPROVED}
    if registration.public_status != RegistrationPublicStatus.APPROVED:
        result = {"eligible": False, "reason": EligibilityReason.NOT_APPROVED}
    elif not identity_clearance(registration.pk).cleared:
        # IDV-Q1: a current, valid, verified identity, read from the database.
        result = {"eligible": False, "reason": EligibilityReason.IDENTITY_NOT_CLEARED}
    else:
        roles = [a for a in current_role_assignments(registration) if _not_expired(a)]
        badge = current_badge_assignment(registration)
        access_profiles = [
            a for a in current_access_profile_assignments(registration) if _not_expired(a)
        ]
        badge_valid = badge is not None and _not_expired(badge)
        if not roles:
            result = {"eligible": False, "reason": EligibilityReason.NO_CURRENT_ROLE}
        elif not badge_valid:
            result = {"eligible": False, "reason": EligibilityReason.NO_CURRENT_BADGE}
        elif not access_profiles:
            result = {
                "eligible": False,
                "reason": EligibilityReason.NO_CURRENT_ACCESS_PROFILE,
            }
        elif badge.event_edition_id != registration.event_edition_id:
            result = {"eligible": False, "reason": EligibilityReason.BADGE_OUT_OF_SCOPE}
        else:
            result = {"eligible": True, "reason": EligibilityReason.ELIGIBLE}

    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="SYSTEM",
            action_code=action_codes.ELIGIBILITY_EVALUATED,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            result="SUCCESS" if result["eligible"] else "DENIED",
            after_summary={"reason": result["reason"]},
            correlation_id=correlation_id,
        )
    )
    return result
