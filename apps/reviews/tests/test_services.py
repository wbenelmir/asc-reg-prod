"""Service-layer command tests (Phase 2 Prompt 3)."""

from __future__ import annotations

import pytest

from apps.registrations.models import RegistrationInternalStatus, RegistrationPublicStatus
from apps.reviews.models import (
    DuplicateOutcome,
    InformationRequestPurpose,
    InformationRequestStatus,
    RequestItemKind,
    ReviewCaseStatus,
    ReviewCaseType,
)
from apps.reviews.services import (
    ActiveInformationRequestExistsError,
    ApprovalRequiresAssignmentsError,
    InvalidChecklistItemError,
    InvalidInformationResponseError,
    InvalidRequestItemError,
    InvalidStateTransitionError,
    RegistrationOwnershipError,
    StaleVersionError,
    add_internal_note,
    assign_review_case,
    cancel_information_request,
    cancel_registration_operationally,
    change_review_case_status,
    close_information_request,
    create_checklist_definition,
    create_information_request,
    open_review_case,
    record_approved_decision,
    record_checklist_result,
    record_not_approved_decision,
    reopen_registration,
    resolve_duplicate_candidate,
    save_information_response_draft,
    send_information_request,
    submit_information_response,
    withdraw_registration,
)

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(event, organization):
    return make_operational_user_with_membership(
        email="reviewer@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )


@pytest.fixture
def manager(event, organization):
    return make_operational_user_with_membership(
        email="manager@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )


def test_open_review_case_derives_scope_from_registration(
    registration, event, organization, reviewer
):
    case = open_review_case(
        registration=registration,
        case_type=ReviewCaseType.STANDARD,
        queue_code="GENERAL",
        actor=reviewer,
    )
    assert case.event_edition_id == event.pk
    assert case.organization_id == organization.pk
    assert case.status == ReviewCaseStatus.QUEUED


def test_assign_then_reassign_preserves_history(registration, reviewer, manager):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    first = assign_review_case(
        review_case=case, expected_version=case.version, assigned_by=manager, assigned_user=reviewer
    )
    case.refresh_from_db()
    second = assign_review_case(
        review_case=case, expected_version=case.version, assigned_by=manager, assigned_user=manager
    )
    first.refresh_from_db()
    assert first.is_current is False
    assert first.ended_at is not None
    assert second.is_current is True
    assert case.assignments.count() == 2


def test_assign_with_stale_version_raises(registration, reviewer, manager):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    with pytest.raises(StaleVersionError):
        assign_review_case(
            review_case=case,
            expected_version=case.version + 1,
            assigned_by=manager,
            assigned_user=reviewer,
        )


def test_starting_review_updates_public_and_internal_status(registration, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    change_review_case_status(
        review_case=case,
        new_status=ReviewCaseStatus.IN_PROGRESS,
        expected_version=case.version,
        actor=reviewer,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert registration.internal_status == RegistrationInternalStatus.REVIEW_IN_PROGRESS


def test_closed_case_cannot_return_to_in_progress(registration, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    case.status = ReviewCaseStatus.COMPLETED
    case.save(update_fields=["status"])
    with pytest.raises(InvalidStateTransitionError):
        change_review_case_status(
            review_case=case,
            new_status=ReviewCaseStatus.IN_PROGRESS,
            expected_version=case.version,
            actor=reviewer,
        )


def test_change_status_to_in_progress_updates_registration_internal_status(registration, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    assign_review_case(
        review_case=case,
        expected_version=case.version,
        assigned_by=reviewer,
        assigned_user=reviewer,
    )
    case.refresh_from_db()
    change_review_case_status(
        review_case=case,
        new_status=ReviewCaseStatus.IN_PROGRESS,
        expected_version=case.version,
        actor=reviewer,
    )
    registration.refresh_from_db()
    assert registration.internal_status == RegistrationInternalStatus.REVIEW_IN_PROGRESS


def test_checklist_result_rejects_an_item_code_outside_the_definition(
    registration, event, reviewer
):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    definition = create_checklist_definition(
        event_edition=event, case_type=ReviewCaseType.STANDARD, version_label="v1"
    )
    with pytest.raises(InvalidChecklistItemError):
        record_checklist_result(
            review_case=case,
            checklist_definition=definition,
            item_code="NOT_A_REAL_CODE",
            result="PASS",
            actor=reviewer,
        )


def test_checklist_result_accepted_and_immutable_history(registration, event, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    definition = create_checklist_definition(
        event_edition=event, case_type=ReviewCaseType.STANDARD, version_label="v1"
    )
    code = definition.items[0]["code"]
    first = record_checklist_result(
        review_case=case,
        checklist_definition=definition,
        item_code=code,
        result="FAIL",
        actor=reviewer,
    )
    second = record_checklist_result(
        review_case=case,
        checklist_definition=definition,
        item_code=code,
        result="PASS",
        actor=reviewer,
    )
    # Both rows remain -- append-only, prior evidence stays queryable.
    assert case.checklist_results.filter(item_code=code).count() == 2
    assert first.pk != second.pk


def test_internal_note_is_never_participant_facing_and_is_bounded(registration, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    note = add_internal_note(review_case=case, note_text="x" * 5000, created_by=reviewer)
    assert len(note.note_encrypted) == 4000


def test_duplicate_resolution_never_deletes_or_merges_anything(registration, reviewer):
    from apps.people.services import resolve_or_create_participant_for_email

    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.DUPLICATE, queue_code="DUPLICATE"
    )
    other_person = resolve_or_create_participant_for_email("other-person@example.com")
    resolution = resolve_duplicate_candidate(
        review_case=case,
        outcome=DuplicateOutcome.DIFFERENT_PERSON,
        reviewed_by=reviewer,
        candidate_person=other_person,
        reason="Different birth date.",
    )
    assert resolution.candidate_person_id == other_person.pk
    # The other person's own account is completely untouched.
    other_person.refresh_from_db()


def test_duplicate_resolution_escalation_opens_a_restricted_case(registration, reviewer):
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.DUPLICATE, queue_code="DUPLICATE"
    )
    resolve_duplicate_candidate(
        review_case=case, outcome=DuplicateOutcome.ESCALATED_RESTRICTED, reviewed_by=reviewer
    )
    assert registration.review_cases.filter(case_type=ReviewCaseType.RESTRICTED).exists()


def test_not_approved_decision_sets_status_and_preserves_history(registration, manager):
    decision = record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="INCOMPLETE_INFORMATION",
        decided_by=manager,
        internal_note="Missing passport scan.",
        participant_reason_code="INCOMPLETE_INFORMATION",
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert registration.internal_status == RegistrationInternalStatus.CLOSED
    assert decision.is_current is True
    assert decision.internal_note_encrypted == "Missing passport scan."


def test_approved_decision_is_blocked_pending_prompt4_assignments(registration, manager):
    with pytest.raises(ApprovalRequiresAssignmentsError):
        record_approved_decision(
            registration=registration,
            expected_version=registration.version,
            decided_by=manager,
            attendance_category="FOLLOWING_TWO_DAYS",
        )
    registration.refresh_from_db()
    # Nothing committed -- fails atomically, before any write.
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_approved_decision_succeeds_once_prompt4_assignments_exist(
    registration, event, organization, manager
):
    from apps.accreditation.models import AccessProfile, BadgeType, ParticipantRole
    from apps.accreditation.services import assign

    role = ParticipantRole.objects.create(event_edition=event, code="DELEGATE2", name="Delegate")
    badge = BadgeType.objects.create(event_edition=event, code="STANDARD2", name="Standard")
    profile = AccessProfile.objects.create(
        event_edition=event, code="STANDARD-ACCESS2", name="Standard access"
    )
    assign(kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=manager)
    assign(kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=manager)
    assign(kind="ACCESS_PROFILE", registration=registration, reference_obj=profile, actor=manager)
    # Owner decision IDV-Q1: approval also requires a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)
    from apps.accreditation.tests.attendance_fixtures import configure_attendance

    configure_attendance(event)

    decision = record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=manager,
        attendance_category="FOLLOWING_TWO_DAYS",
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.APPROVED
    assert decision.is_current is True


def test_approved_decision_requires_access_profile_as_well(registration, event, manager):
    from apps.accreditation.models import BadgeType, ParticipantRole
    from apps.accreditation.services import assign

    role = ParticipantRole.objects.create(event_edition=event, code="ROLE-NO-PROFILE", name="Role")
    badge = BadgeType.objects.create(event_edition=event, code="BADGE-NO-PROFILE", name="Badge")
    assign(kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=manager)
    assign(kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=manager)

    with pytest.raises(ApprovalRequiresAssignmentsError):
        record_approved_decision(
            registration=registration,
            expected_version=registration.version,
            decided_by=manager,
            attendance_category="FOLLOWING_TWO_DAYS",
        )
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_reopen_supersedes_prior_decision_and_opens_a_new_case(registration, manager):
    record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="INCOMPLETE_INFORMATION",
        decided_by=manager,
    )
    registration.refresh_from_db()
    new_case = reopen_registration(
        registration=registration,
        expected_version=registration.version,
        reason="New evidence submitted.",
        actor=manager,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert registration.internal_status == RegistrationInternalStatus.REVIEW_IN_PROGRESS
    assert new_case.status == ReviewCaseStatus.QUEUED
    old_decision = registration.decisions.get(sequence=1)
    assert old_decision.is_current is False  # preserved, not deleted
    second_decision = record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="STILL_INCOMPLETE",
        decided_by=manager,
    )
    assert second_decision.supersedes_id == old_decision.pk


def test_withdraw_requires_ownership(registration):
    from apps.people.services import resolve_or_create_participant_for_email

    owner = resolve_or_create_participant_for_email("owner@example.com")
    stranger = resolve_or_create_participant_for_email("stranger@example.com")
    registration.person = owner
    registration.save(update_fields=["person"])
    with pytest.raises(RegistrationOwnershipError):
        withdraw_registration(
            registration=registration, person=stranger, expected_version=registration.version
        )


def test_withdraw_affects_only_the_selected_context(registration, event):
    from apps.people.services import resolve_or_create_participant_for_email

    from .conftest import make_registration

    person = resolve_or_create_participant_for_email("withdraw-owner@example.com")
    registration.person = person
    registration.save(update_fields=["person"])
    other = make_registration(event=event, person=person)

    withdraw_registration(
        registration=registration, person=person, expected_version=registration.version
    )
    registration.refresh_from_db()
    other.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert other.public_status != RegistrationPublicStatus.WITHDRAWN


def test_operational_cancellation_requires_a_reason(registration, manager):
    with pytest.raises(ValueError):
        cancel_registration_operationally(
            registration=registration,
            expected_version=registration.version,
            reason="",
            actor=manager,
        )


def test_operational_cancellation_maps_to_withdrawn_and_closed(registration, manager):
    cancel_registration_operationally(
        registration=registration,
        expected_version=registration.version,
        reason="Duplicate submission by mistake.",
        actor=manager,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert registration.internal_status == RegistrationInternalStatus.CLOSED
    assert registration.cancelled_at is not None


# ---------------------------------------------------------------------------
# Information Requests
# ---------------------------------------------------------------------------


def test_create_information_request_rejects_an_unknown_field_code(registration, reviewer):
    with pytest.raises(InvalidRequestItemError):
        create_information_request(
            registration=registration,
            purpose=InformationRequestPurpose.CLARIFICATION,
            message_en="Please clarify.",
            items=[{"kind": RequestItemKind.FIELD_CORRECTION, "field_code": "__import__('os')"}],
            created_by=reviewer,
        )


def test_only_one_active_request_at_a_time(registration, reviewer):
    create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="First.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    with pytest.raises(ActiveInformationRequestExistsError):
        create_information_request(
            registration=registration,
            purpose=InformationRequestPurpose.CLARIFICATION,
            message_en="Second.",
            items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
            created_by=reviewer,
        )


def test_submitted_request_must_be_closed_before_a_new_round(registration, reviewer):
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="First.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    information_request.status = InformationRequestStatus.SUBMITTED
    information_request.save(update_fields=["status"])
    with pytest.raises(ActiveInformationRequestExistsError):
        create_information_request(
            registration=registration,
            purpose=InformationRequestPurpose.CLARIFICATION,
            message_en="Second.",
            items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
            created_by=reviewer,
        )
    close_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    second = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Second.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    assert second.sequence == 2


def test_sending_a_request_updates_registration_status_and_never_double_creates_a_decision(
    registration, reviewer
):
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED
    assert registration.internal_status == RegistrationInternalStatus.AWAITING_APPLICANT
    assert registration.decisions.count() == 0


def test_cancelling_a_request_never_creates_a_not_approved_decision(registration, reviewer):
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    information_request.refresh_from_db()
    cancel_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
        reason="No longer needed.",
    )
    registration.refresh_from_db()
    assert registration.decisions.count() == 0
    assert registration.internal_status == RegistrationInternalStatus.REVIEW_IN_PROGRESS


def test_response_submission_is_idempotent_ownership_checked_and_resets_identity_verification(
    registration, reviewer
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("responder@example.com")
    registration.person = person
    registration.internal_status = RegistrationInternalStatus.AWAITING_APPLICANT
    registration.save(update_fields=["person", "internal_status"])

    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.IDENTITY_CORRECTION,
        message_en="Please correct your name.",
        items=[{"kind": RequestItemKind.FIELD_CORRECTION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    information_request.refresh_from_db()
    item = information_request.items.get()

    stranger = resolve_or_create_participant_for_email("stranger2@example.com")
    with pytest.raises(RegistrationOwnershipError):
        submit_information_response(
            information_request=information_request,
            person=stranger,
            answers=[{"request_item_id": item.pk, "value": "Amine", "document": None}],
        )

    first = submit_information_response(
        information_request=information_request,
        person=person,
        answers=[{"request_item_id": item.pk, "value": "Amine", "document": None}],
    )
    second = submit_information_response(
        information_request=information_request,
        person=person,
        answers=[{"request_item_id": item.pk, "value": "Amine", "document": None}],
    )
    assert first.pk == second.pk  # idempotent retry, never a second response
    snapshot_text = str(first.snapshot_json)
    assert "Amine" not in snapshot_text
    assert first.snapshot_json[str(item.pk)]["kind"] == "value"
    assert len(first.snapshot_json[str(item.pk)]["content_hmac"]) == 64

    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    # given_names is identity-sensitive -> verification returns to pending.
    assert registration.internal_status == RegistrationInternalStatus.VERIFICATION_PENDING


def test_required_response_item_cannot_be_omitted(registration, reviewer):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("required-response@example.com")
    registration.person = person
    registration.save(update_fields=["person"])
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please answer.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    with pytest.raises(InvalidInformationResponseError):
        submit_information_response(
            information_request=information_request,
            person=person,
            answers=[],
        )
    assert not hasattr(information_request, "response")


def test_save_draft_never_creates_the_immutable_response(registration, reviewer):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("drafter@example.com")
    registration.person = person
    registration.save(update_fields=["person"])
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )
    information_request.refresh_from_db()
    save_information_response_draft(
        information_request=information_request, person=person, answers={"draft": "not final"}
    )
    information_request.refresh_from_db()
    assert information_request.status == InformationRequestStatus.RESPONSE_IN_PROGRESS
    assert not hasattr(information_request, "response")
