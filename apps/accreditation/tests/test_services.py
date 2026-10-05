"""Service-layer tests for accreditation assignments (Phase 2 Prompt 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accreditation.models import (
    AssignmentStatus,
    BulkAssignmentKind,
    BulkOperationStatus,
)
from apps.accreditation.services import (
    AssignmentAlreadyCurrentError,
    AssignmentNotFoundError,
    InvalidEffectiveRangeError,
    InvalidReferenceError,
    StaleVersionError,
    assign,
    change,
    evaluate_eligibility,
    execute_bulk_assignment,
    expire_due_assignments,
    preview_bulk_assignment,
    revoke,
)

from .conftest import make_operational_user_with_membership, make_registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator(event, organization):
    return make_operational_user_with_membership(
        email="coordinator@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )


def test_assign_role_creates_current_assignment(registration, role, coordinator):
    assignment = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assert assignment.status == AssignmentStatus.CURRENT
    assert assignment.event_edition_id == registration.event_edition_id
    assert assignment.organization_id == registration.source_organization_id


def test_only_one_role_can_be_current_per_registration(registration, role, other_role, coordinator):
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    with pytest.raises(AssignmentAlreadyCurrentError):
        assign(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            registration=registration,
            reference_obj=other_role,
            actor=coordinator,
        )
    assert registration.role_assignments.filter(status=AssignmentStatus.CURRENT).count() == 1


def test_assigning_the_same_role_twice_is_rejected(registration, role, coordinator):
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    with pytest.raises(AssignmentAlreadyCurrentError):
        assign(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            registration=registration,
            reference_obj=role,
            actor=coordinator,
        )


def test_assign_rejects_inactive_and_cross_event_reference(
    registration, role, other_event, coordinator
):
    from apps.accreditation.models import ParticipantRole

    role.is_active = False
    role.save(update_fields=["is_active"])
    with pytest.raises(InvalidReferenceError):
        assign(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            registration=registration,
            reference_obj=role,
            actor=coordinator,
        )

    foreign_role = ParticipantRole.objects.create(
        event_edition=other_event, code="FOREIGN", name="Foreign event role"
    )
    with pytest.raises(InvalidReferenceError):
        assign(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            registration=registration,
            reference_obj=foreign_role,
            actor=coordinator,
        )


def test_badge_type_is_exclusive_per_registration(
    registration, badge_type, visible_badge_type, coordinator
):
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    with pytest.raises(AssignmentAlreadyCurrentError):
        assign(
            kind=BulkAssignmentKind.BADGE_TYPE,
            registration=registration,
            reference_obj=visible_badge_type,
            actor=coordinator,
        )


def test_access_profile_is_exclusive_per_registration(
    registration, access_profile, event, coordinator
):
    from apps.accreditation.models import AccessProfile

    second_profile = AccessProfile.objects.create(
        event_edition=event, code="SECOND-PROFILE", name="Second profile"
    )
    assign(
        kind=BulkAssignmentKind.ACCESS_PROFILE,
        registration=registration,
        reference_obj=access_profile,
        actor=coordinator,
    )
    with pytest.raises(AssignmentAlreadyCurrentError):
        assign(
            kind=BulkAssignmentKind.ACCESS_PROFILE,
            registration=registration,
            reference_obj=second_profile,
            actor=coordinator,
        )


def test_database_constraint_backstops_the_exclusivity_rule(registration, role, coordinator):
    from apps.accreditation.models import ParticipantRoleAssignment

    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ParticipantRoleAssignment.objects.create(
                registration=registration,
                role=role,
                event_edition_id=registration.event_edition_id,
                status=AssignmentStatus.CURRENT,
                effective_from=timezone.now(),
                created_by=coordinator,
            )


def test_change_supersedes_and_preserves_history(registration, role, other_role, coordinator):
    first = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    second = change(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        current_assignment=first,
        new_reference_obj=other_role,
        expected_version=first.version,
        actor=coordinator,
        reason="Reassigned to speaker.",
    )
    first.refresh_from_db()
    assert first.status == AssignmentStatus.SUPERSEDED
    assert first.effective_until is not None
    assert second.status == AssignmentStatus.CURRENT
    assert second.supersedes_id == first.pk
    assert registration.role_assignments.count() == 2  # both rows preserved


def test_change_with_stale_version_raises(registration, role, other_role, coordinator):
    first = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    with pytest.raises(StaleVersionError):
        change(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            current_assignment=first,
            new_reference_obj=other_role,
            expected_version=first.version + 1,
            actor=coordinator,
        )


def test_revoke_requires_a_reason(registration, role, coordinator):
    first = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    with pytest.raises(ValueError):
        revoke(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            current_assignment=first,
            expected_version=first.version,
            actor=coordinator,
            reason="",
        )


def test_revoke_terminates_without_deleting(registration, role, coordinator):
    first = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    revoke(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        current_assignment=first,
        expected_version=first.version,
        actor=coordinator,
        reason="No longer attending.",
    )
    first.refresh_from_db()
    assert first.status == AssignmentStatus.REVOKED
    assert first.effective_until is not None
    assert first.ended_by_id == coordinator.pk
    assert first.ended_at is not None
    assert first.end_reason == "No longer attending."
    # After revocation, a fresh assignment of the SAME role is allowed again.
    fresh = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assert fresh.status == AssignmentStatus.CURRENT


def test_revoking_an_already_revoked_assignment_is_rejected(registration, role, coordinator):
    first = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    revoke(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        current_assignment=first,
        expected_version=first.version,
        actor=coordinator,
        reason="First revoke.",
    )
    first.refresh_from_db()
    with pytest.raises(AssignmentNotFoundError):
        revoke(
            kind=BulkAssignmentKind.PARTICIPANT_ROLE,
            current_assignment=first,
            expected_version=first.version,
            actor=coordinator,
            reason="Second revoke.",
        )


def test_invalid_effective_range_is_rejected(registration, role, coordinator):
    with pytest.raises(InvalidEffectiveRangeError):
        from apps.accreditation.services import _validate_range

        _validate_range(timezone.now(), timezone.now() - timedelta(days=1))


def test_expire_due_assignments_is_idempotent(registration, role, coordinator):
    assignment = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
        effective_from=timezone.now() - timedelta(days=2),
    )
    # Still strictly after effective_from (satisfies the CHECK constraint)
    # but already in the past relative to "now" -- genuinely due to expire.
    assignment.effective_until = timezone.now() - timedelta(seconds=1)
    assignment.save(update_fields=["effective_until"])

    first_pass = expire_due_assignments()
    second_pass = expire_due_assignments()
    assert first_pass == 1
    assert second_pass == 0
    assignment.refresh_from_db()
    assert assignment.status == AssignmentStatus.EXPIRED


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------


def test_bulk_preview_rejects_wrong_event_and_already_current(
    event, other_event, organization, role, coordinator
):
    good = make_registration(event=event, organization=organization)
    wrong_event = make_registration(event=other_event, organization=organization)
    already_assigned = make_registration(event=event, organization=organization)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=already_assigned,
        reference_obj=role,
        actor=coordinator,
    )

    operation = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk, wrong_event.pk, already_assigned.pk],
        reference_obj=role,
        requested_by=coordinator,
    )
    assert operation.status == BulkOperationStatus.PREVIEWED
    assert operation.target_registration_ids == [str(good.pk)]
    reasons = {r["registration_id"]: r["reason"] for r in operation.preview_results}
    assert reasons[str(wrong_event.pk)] == "WRONG_EVENT"
    assert reasons[str(already_assigned.pk)] == "ALREADY_CURRENT"


def test_bulk_execute_applies_only_the_previewed_scope(event, organization, role, coordinator):
    good = make_registration(event=event, organization=organization)
    operation = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk],
        reference_obj=role,
        requested_by=coordinator,
    )
    executed = execute_bulk_assignment(operation=operation, actor=coordinator)
    assert executed.status == BulkOperationStatus.EXECUTED
    assert good.role_assignments.filter(status=AssignmentStatus.CURRENT, role=role).exists()


def test_bulk_execute_revalidates_state_and_reports_per_row_failure(
    event, organization, role, coordinator
):
    good = make_registration(event=event, organization=organization)
    operation = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk],
        reference_obj=role,
        requested_by=coordinator,
    )
    # Drift: assign the SAME role directly between preview and execute.
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=good,
        reference_obj=role,
        actor=coordinator,
    )

    executed = execute_bulk_assignment(operation=operation, actor=coordinator)
    assert executed.execution_results[0]["success"] is False
    assert executed.execution_results[0]["reason"] == "ALREADY_CURRENT"
    # Never created a SECOND current assignment.
    assert good.role_assignments.filter(status=AssignmentStatus.CURRENT, role=role).count() == 1


def test_bulk_execute_is_idempotent_on_retry(event, organization, role, coordinator):
    good = make_registration(event=event, organization=organization)
    operation = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk],
        reference_obj=role,
        requested_by=coordinator,
    )
    execute_bulk_assignment(operation=operation, actor=coordinator)
    operation.refresh_from_db()
    second_call = execute_bulk_assignment(operation=operation, actor=coordinator)
    assert second_call.pk == operation.pk
    assert good.role_assignments.filter(status=AssignmentStatus.CURRENT, role=role).count() == 1


def test_bulk_preview_is_idempotent_on_the_same_key(event, organization, role, coordinator):
    good = make_registration(event=event, organization=organization)
    first = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk],
        reference_obj=role,
        requested_by=coordinator,
        idempotency_key="fixed-key",
    )
    second = preview_bulk_assignment(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration_ids=[good.pk],
        reference_obj=role,
        requested_by=coordinator,
        idempotency_key="fixed-key",
    )
    assert first.pk == second.pk


# ---------------------------------------------------------------------------
# Eligibility boundary
# ---------------------------------------------------------------------------


def _verified_identity(registration) -> None:
    """Owner decision IDV-Q1: eligibility also needs a verified identity."""
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)


def test_eligibility_denies_by_default_when_not_approved(registration):
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NOT_APPROVED"}


def test_eligibility_denies_without_a_current_role(registration, badge_type, coordinator):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NO_CURRENT_ROLE"}


def test_eligibility_denies_without_a_current_badge(registration, role, coordinator):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NO_CURRENT_BADGE"}


def test_eligibility_denies_an_expired_assignment(registration, role, badge_type, coordinator):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    badge_assignment = assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
        effective_from=timezone.now() - timedelta(days=2),
    )
    badge_assignment.effective_until = timezone.now() - timedelta(seconds=1)
    badge_assignment.save(update_fields=["effective_until"])
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NO_CURRENT_BADGE"}


def test_eligibility_denies_without_a_current_access_profile(
    registration, role, badge_type, coordinator
):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NO_CURRENT_ACCESS_PROFILE"}


def test_future_assignments_do_not_satisfy_eligibility(
    registration, role, badge_type, access_profile, coordinator
):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    future = timezone.now() + timedelta(days=1)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
        effective_from=future,
    )
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.ACCESS_PROFILE,
        registration=registration,
        reference_obj=access_profile,
        actor=coordinator,
    )
    assert evaluate_eligibility(registration) == {
        "eligible": False,
        "reason": "NO_CURRENT_ROLE",
    }


def test_eligibility_succeeds_when_approved_with_required_assignments(
    registration, role, badge_type, access_profile, coordinator
):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.ACCESS_PROFILE,
        registration=registration,
        reference_obj=access_profile,
        actor=coordinator,
    )
    result = evaluate_eligibility(registration)
    assert result == {"eligible": True, "reason": "ELIGIBLE"}


def test_eligibility_denies_a_revoked_role(registration, role, badge_type, coordinator):
    from apps.registrations.models import RegistrationPublicStatus

    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _verified_identity(registration)
    role_assignment = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    revoke(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        current_assignment=role_assignment,
        expected_version=role_assignment.version,
        actor=coordinator,
        reason="No longer needed.",
    )
    result = evaluate_eligibility(registration)
    assert result == {"eligible": False, "reason": "NO_CURRENT_ROLE"}
