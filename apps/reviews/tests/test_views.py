"""View-level authorization, concurrency, and UI tests (Phase 2 Prompt 3)."""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse
from django.utils.translation import override as override_language

from apps.reviews.models import ReviewCaseType
from apps.reviews.services import create_information_request, open_review_case

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


def _grant_required_assignments(*, registration, event, actor):
    """Create the exact Prompt 4 Participant Role/Badge Type/Access Profile
    trio `record_approved_decision` requires, mirroring
    `apps.reviews.tests.test_services.test_approved_decision_succeeds_once_prompt4_assignments_exist`
    -- never a shortcut around the real eligibility check."""
    from apps.accreditation.models import AccessProfile, BadgeType, ParticipantRole
    from apps.accreditation.services import assign

    suffix = uuid.uuid4().hex[:8].upper()
    role = ParticipantRole.objects.create(
        event_edition=event, code=f"ROLE-{suffix}", name="Delegate"
    )
    badge = BadgeType.objects.create(event_edition=event, code=f"BADGE-{suffix}", name="Standard")
    profile = AccessProfile.objects.create(
        event_edition=event, code=f"PROFILE-{suffix}", name="Standard access"
    )
    assign(kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=actor)
    assign(kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=actor)
    assign(kind="ACCESS_PROFILE", registration=registration, reference_obj=profile, actor=actor)
    # Owner decision IDV-Q1: approval also requires a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)


def test_queue_list_requires_sign_in():
    client = Client()
    response = client.get(reverse("reviews:queue-list"))
    assert response.status_code == 302


def test_queue_list_is_scoped_and_paginated(event, organization, other_organization):
    for index in range(30):
        registration = make_registration(
            event=event, organization=organization, public_reference=f"REV-VIS-{index:03d}"
        )
        open_review_case(
            registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
        )
    hidden_registration = make_registration(
        event=event, organization=other_organization, public_reference="REV-HIDDEN-001"
    )
    open_review_case(
        registration=hidden_registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    user = make_operational_user_with_membership(
        email="queue-scope@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(reverse("reviews:queue-list"))
    assert response.status_code == 200
    content = response.content.decode()
    assert "REV-VIS-000" in content
    assert "REV-HIDDEN-001" not in content
    # Bounded page size -- 30 visible cases never all render on one page.
    assert response.context["page"].paginator.num_pages > 1


def test_queue_list_query_count_is_bounded_regardless_of_case_count(
    event, organization, django_assert_max_num_queries
):
    """Phase 2 Prompt 6 §5.D: the operational queue must not regress into
    an N+1 pattern as the number of cases grows -- `queue_list`'s own
    `select_related`/`prefetch_related` must keep the query count flat."""
    for index in range(20):
        registration = make_registration(
            event=event, organization=organization, public_reference=f"REV-QCOUNT-{index:03d}"
        )
        open_review_case(
            registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
        )
    user = make_operational_user_with_membership(
        email="queue-query-count@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    with django_assert_max_num_queries(20):
        response = client.get(reverse("reviews:queue-list"))
    assert response.status_code == 200


def test_case_detail_returns_404_for_an_out_of_scope_case(event, organization, other_organization):
    registration = make_registration(event=event, organization=other_organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    user = make_operational_user_with_membership(
        email="detail-404@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(reverse("reviews:case-detail", kwargs={"pk": case.pk}))
    assert response.status_code == 404


def test_assign_denied_for_a_user_with_only_view_scope(event, organization):
    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    viewer = make_operational_user_with_membership(
        email="view-only-assign@example.com",
        group_name="Registration Reviewers",  # no assign_reviewcase permission
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.post(
        reverse("reviews:case-assign", kwargs={"pk": case.pk}),
        {"assigned_user_id": str(viewer.pk), "expected_version": case.version},
    )
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    case.refresh_from_db()
    assert case.assignments.count() == 0


def test_assign_denied_for_write_scope_in_a_different_organization(
    event, organization, other_organization
):
    registration = make_registration(event=event, organization=other_organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="wrong-org-manager@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,  # scoped to the WRONG organization
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.post(
        reverse("reviews:case-assign", kwargs={"pk": case.pk}),
        {"assigned_user_id": str(manager.pk), "expected_version": case.version},
    )
    assert response.status_code == 404
    assert case.assignments.count() == 0


def test_stale_version_produces_a_visible_conflict_response(event, organization):
    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="conflict-manager@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.post(
        reverse("reviews:case-assign", kwargs={"pk": case.pk}),
        {"assigned_user_id": str(manager.pk), "expected_version": case.version + 999},
    )
    assert response.status_code == 409


def test_view_only_user_never_sees_the_decision_control(event, organization):
    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    viewer = make_operational_user_with_membership(
        email="view-only-decision@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.get(reverse("reviews:case-detail", kwargs={"pk": case.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    assert reverse("reviews:case-decision", kwargs={"pk": case.pk}) not in content
    # Phase 2 Prompt 8 (P7-H-01): the APPROVED control is gated by the exact
    # same permission and must never leak to a view-only user either.
    assert reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}) not in content


def test_manager_sees_the_decision_control(event, organization):
    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="sees-decision@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.get(reverse("reviews:case-detail", kwargs={"pk": case.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    assert reverse("reviews:case-decision", kwargs={"pk": case.pk}) in content
    # Phase 2 Prompt 8 (P7-H-01): an authorized, correctly scoped reviewer
    # sees the APPROVED control in the same operational context as the
    # existing NOT_APPROVED control -- never on a participant-facing page.
    assert reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}) in content
    assert "Record Approved decision" in content


# ---------------------------------------------------------------------------
# APPROVED decision endpoint (Phase 2 Prompt 8, closing P7-H-01)
# ---------------------------------------------------------------------------


def test_approve_decision_succeeds_for_an_authorized_reviewer(event, organization):
    from apps.registrations.models import RegistrationPublicStatus
    from apps.reviews.models import RegistrationDecision, RegistrationDecisionOutcome

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-success@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )

    assert response.status_code == 302
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.APPROVED
    decision = RegistrationDecision.objects.get(registration=registration, is_current=True)
    assert decision.outcome == RegistrationDecisionOutcome.APPROVED


def test_approve_decision_creates_immutable_decision_history_and_audit_evidence(
    event, organization
):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.reviews.models import RegistrationDecision, RegistrationDecisionOutcome

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-audit@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )

    decisions = RegistrationDecision.objects.filter(registration=registration)
    assert decisions.count() == 1
    decision = decisions.get()
    assert decision.outcome == RegistrationDecisionOutcome.APPROVED
    assert decision.is_current is True
    assert decision.sequence == 1
    assert decision.decided_by_id == manager.pk
    assert AuditEvent.objects.filter(
        action_code=action_codes.REVIEW_DECISION_RECORDED,
        target_uuid=registration.pk,
        result="SUCCESS",
    ).exists()


def test_approve_decision_blocked_without_required_assignments(event, organization):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-missing-assignments@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
        follow=True,
    )

    assert response.status_code == 200
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED
    messages = [str(message) for message in response.context["messages"]]
    assert any("cannot be approved yet" in message for message in messages)


def test_approve_decision_denied_for_a_user_with_only_view_scope(event, organization):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    viewer = make_operational_user_with_membership(
        email="approve-view-only@example.com",
        group_name="Registration Reviewers",  # no add_registrationdecision permission
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )

    # Signed in without the permission: the 403 page, never a sign-in

    # redirect (UI/UX Completion Gate F3).

    assert response.status_code == 403
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_approve_decision_denied_for_wrong_event_scope(event, other_event, organization):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-wrong-event@example.com",
        group_name="Accreditation Managers",
        event_edition=other_event,  # scoped to a DIFFERENT event
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )

    assert response.status_code == 404
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_approve_decision_denied_for_wrong_organization_scope(
    event, organization, other_organization
):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=other_organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-wrong-org@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,  # scoped to the WRONG organization
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )

    assert response.status_code == 404
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_approve_decision_stale_version_rejected_without_overwriting_state(event, organization):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-stale@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version + 999},
    )

    assert response.status_code == 409
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


@pytest.mark.parametrize("payload", [{}, {"expected_version": "not-an-integer"}])
def test_approve_decision_requires_a_valid_explicit_version(event, organization, payload):
    """Missing/malformed transport input fails closed instead of using the
    latest database version or raising an unhandled ValueError."""
    from apps.registrations.models import RegistrationPublicStatus
    from apps.reviews.models import RegistrationDecision

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email=f"approve-invalid-version-{uuid.uuid4().hex[:8]}@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}), payload
    )

    assert response.status_code == 409
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED
    assert not RegistrationDecision.objects.filter(registration=registration).exists()


def test_approve_decision_ignores_unapproved_participant_reason_input(event, organization):
    """The direct approval UI has no participant-reason field; a crafted POST
    cannot smuggle one into immutable participant-facing decision evidence."""
    from apps.reviews.models import RegistrationDecision

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-crafted-reason@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {
            "expected_version": registration.version,
            "participant_reason_code": "CALLER_CONTROLLED",
        },
    )

    assert response.status_code == 302
    decision = RegistrationDecision.objects.get(registration=registration, is_current=True)
    assert decision.participant_reason_code == ""


def test_approve_decision_resubmission_with_current_version_does_not_duplicate_decision(
    event, organization
):
    from apps.reviews.models import RegistrationDecision

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-resubmit@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    first = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )
    assert first.status_code == 302
    registration.refresh_from_db()

    # A resubmission (e.g. a duplicate network retry) using the NOW-CURRENT
    # version must be an idempotent no-op -- never a second current decision.
    second = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
    )
    assert second.status_code == 302
    assert RegistrationDecision.objects.filter(registration=registration).count() == 1
    assert (
        RegistrationDecision.objects.filter(registration=registration, is_current=True).count() == 1
    )


def test_approve_decision_get_request_is_not_allowed(event, organization):
    from apps.registrations.models import RegistrationPublicStatus

    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-get@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    _grant_required_assignments(registration=registration, event=event, actor=manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    response = client.get(reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}))

    assert response.status_code == 405
    registration.refresh_from_db()
    assert registration.public_status != RegistrationPublicStatus.APPROVED


def test_approve_decision_control_renders_translated_in_french_and_arabic(event, organization):
    registration = make_registration(event=event, organization=organization)
    case = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="approve-i18n@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)

    with override_language("fr"):
        response = client.get(
            reverse("reviews:case-detail", kwargs={"pk": case.pk}), HTTP_ACCEPT_LANGUAGE="fr"
        )
    assert response.status_code == 200
    assert "Enregistrer une décision Approuvé" in response.content.decode()

    with override_language("ar"):
        response = client.get(
            reverse("reviews:case-detail", kwargs={"pk": case.pk}), HTTP_ACCEPT_LANGUAGE="ar"
        )
    assert response.status_code == 200
    assert "تسجيل قرار الموافقة" in response.content.decode()


def test_information_request_mutation_never_combines_write_scope_with_other_view_scope(
    event, organization, other_organization
):
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership
    from apps.reviews.models import InformationRequestPurpose, RequestItemKind

    target_registration = make_registration(event=event, organization=other_organization)
    user = make_operational_user_with_membership(
        email="split-info-scope@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    view_only = Group.objects.create(name="Prompt3 Info View Only")
    view_only.permissions.add(
        Permission.objects.get(
            content_type__app_label="reviews", codename="view_informationrequest"
        )
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=view_only,
        event_edition=event,
        organization=other_organization,
        granted_by=user,
    )
    information_request = create_information_request(
        registration=target_registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=user,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(
        reverse("reviews:information-request-send", kwargs={"pk": information_request.pk}),
        {"expected_version": information_request.version},
    )
    assert response.status_code == 404
    information_request.refresh_from_db()
    assert information_request.status == "DRAFT"


def test_registration_cancel_never_combines_write_scope_with_other_view_scope(
    event, organization, other_organization
):
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership

    target_registration = make_registration(event=event, organization=other_organization)
    user = make_operational_user_with_membership(
        email="split-cancel-scope@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    view_only = Group.objects.create(name="Prompt3 Registration View Only")
    view_only.permissions.add(
        Permission.objects.get(
            content_type__app_label="registrations", codename="view_registration"
        )
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=view_only,
        event_edition=event,
        organization=other_organization,
        granted_by=user,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(
        reverse("reviews:registration-cancel", kwargs={"pk": target_registration.pk}),
        {"expected_version": target_registration.version, "reason": "Not authorized here."},
    )
    assert response.status_code == 404
    target_registration.refresh_from_db()
    assert target_registration.cancelled_at is None


def test_reviewer_can_close_a_submitted_information_response(event, organization):
    from apps.reviews.models import (
        InformationRequestPurpose,
        InformationRequestStatus,
        RequestItemKind,
    )

    registration = make_registration(event=event, organization=organization)
    reviewer = make_operational_user_with_membership(
        email="close-response@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    information_request.status = InformationRequestStatus.SUBMITTED
    information_request.save(update_fields=["status"])
    client = Client()
    sign_in_operational(client, reviewer.email_normalized)
    response = client.post(
        reverse("reviews:information-request-close", kwargs={"pk": information_request.pk}),
        {"expected_version": information_request.version},
    )
    assert response.status_code == 302
    information_request.refresh_from_db()
    assert information_request.status == InformationRequestStatus.CLOSED
