"""Participant-facing Information Request and withdrawal view tests
(Phase 2 Prompt 3 §7.3, §10)."""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core.testing import otp_request_data
from apps.reviews.models import InformationRequestPurpose, InformationRequestStatus, RequestItemKind
from apps.reviews.services import create_information_request, send_information_request

from .conftest import make_operational_user_with_membership, make_registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(event, organization):
    return make_operational_user_with_membership(
        email="participant-view-reviewer@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )


def _otp_login(client: Client, django_capture_on_commit_callbacks, email: str) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )


def _sent_request(registration, reviewer):
    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify your objectives.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=reviewer,
    )
    return send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=reviewer,
    )


def test_participant_can_view_and_submit_their_own_request(
    event, organization, reviewer, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("my-own-request@example.com")
    registration = make_registration(event=event, organization=organization, person=person)
    information_request = _sent_request(registration, reviewer)

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "my-own-request@example.com")
    get_response = client.get(
        reverse("reviews:my-information-request", kwargs={"pk": information_request.pk})
    )
    assert get_response.status_code == 200

    item = information_request.items.get()
    post_response = client.post(
        reverse("reviews:my-information-request", kwargs={"pk": information_request.pk}),
        {"action": "submit", f"value_{item.pk}": "Amine"},
    )
    assert post_response.status_code == 302
    information_request.refresh_from_db()
    assert information_request.status == InformationRequestStatus.SUBMITTED


def test_participant_cannot_view_another_persons_request(
    event, organization, reviewer, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    owner = resolve_or_create_participant_for_email("owner-of-request@example.com")
    registration = make_registration(event=event, organization=organization, person=owner)
    information_request = _sent_request(registration, reviewer)

    resolve_or_create_participant_for_email("stranger-participant@example.com")
    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "stranger-participant@example.com")
    response = client.get(
        reverse("reviews:my-information-request", kwargs={"pk": information_request.pk})
    )
    assert response.status_code == 404


def test_participant_withdrawal_affects_only_the_selected_registration(
    event, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("withdraw-view@example.com")
    registration = make_registration(event=event, person=person)
    other = make_registration(event=event, person=person)

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "withdraw-view@example.com")
    response = client.post(
        reverse("reviews:my-registration-withdraw", kwargs={"pk": registration.pk}),
        {"expected_version": registration.version},
    )
    assert response.status_code == 302
    registration.refresh_from_db()
    other.refresh_from_db()
    assert registration.public_status == "WITHDRAWN"
    assert other.public_status != "WITHDRAWN"


def test_participant_cannot_withdraw_someone_elses_registration(
    event, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    owner = resolve_or_create_participant_for_email("real-owner@example.com")
    registration = make_registration(event=event, person=owner)
    resolve_or_create_participant_for_email("not-the-owner@example.com")

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "not-the-owner@example.com")
    response = client.post(
        reverse("reviews:my-registration-withdraw", kwargs={"pk": registration.pk}),
        {"expected_version": registration.version},
    )
    assert response.status_code == 404
    registration.refresh_from_db()
    assert registration.public_status != "WITHDRAWN"


def test_participant_workspace_never_exposes_the_operational_approve_control_or_reason(
    event, organization, reviewer, django_capture_on_commit_callbacks
):
    """Phase 2 Prompt 8 (P7-H-01) requirement 11: the new APPROVED control,
    its URL, and the internal-reason/eligibility text are operations-only --
    a participant viewing their OWN approved registration must see only the
    public status, never the operational decision control or its wording."""
    from apps.accreditation.models import AccessProfile, BadgeType, ParticipantRole
    from apps.accreditation.services import assign
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.services import record_approved_decision

    person = resolve_or_create_participant_for_email("approved-participant@example.com")
    registration = make_registration(event=event, organization=organization, person=person)

    role = ParticipantRole.objects.create(event_edition=event, code="P8-ROLE", name="Delegate")
    badge = BadgeType.objects.create(event_edition=event, code="P8-BADGE", name="Standard")
    profile = AccessProfile.objects.create(
        event_edition=event, code="P8-PROFILE", name="Standard access"
    )
    assign(kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=reviewer)
    assign(kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=reviewer)
    assign(kind="ACCESS_PROFILE", registration=registration, reference_obj=profile, actor=reviewer)
    # Owner decision IDV-Q1: approval also requires a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)
    from apps.accreditation.tests.attendance_fixtures import configure_attendance

    configure_attendance(event)
    record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=reviewer,
        attendance_category="FOLLOWING_TWO_DAYS",
    )
    registration.refresh_from_db()

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "approved-participant@example.com")
    response = client.get(reverse("registrations:workspace"))

    assert response.status_code == 200
    content = response.content.decode()
    assert registration.get_public_status_display() in content
    assert "/decision/approved/" not in content
    assert "Record Approved decision" not in content
    assert "cannot be approved yet" not in content
    assert "Participant Role" not in content
    assert "ELIGIBLE" not in content
