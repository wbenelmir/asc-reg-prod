"""Badge Type participant-visibility tests (Phase 2 Prompt 4 §6):
conservative, configurable, default-hidden."""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.accreditation.models import BulkAssignmentKind
from apps.accreditation.selectors import participant_safe_badge_projection
from apps.accreditation.services import assign
from apps.core.testing import otp_request_data

from .conftest import make_operational_user_with_membership, make_registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator(event, organization):
    return make_operational_user_with_membership(
        email="badge-coordinator@example.com",
        group_name="Accreditation Coordinators",
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


def test_selector_hides_badge_by_default(registration, badge_type, coordinator):
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )
    assert participant_safe_badge_projection(registration) is None


def test_selector_reveals_badge_only_when_explicitly_visible(
    registration, visible_badge_type, coordinator
):
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=visible_badge_type,
        actor=coordinator,
    )
    projection = participant_safe_badge_projection(registration)
    assert projection == {"code": "VIP", "name": "VIP"}


def test_workspace_page_hides_badge_by_default(
    event, organization, badge_type, coordinator, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("badge-hidden@example.com")
    registration = make_registration(event=event, organization=organization, person=person)
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "badge-hidden@example.com")
    response = client.get(reverse("registrations:workspace"))
    assert response.status_code == 200
    assert "Standard" not in response.content.decode()
    assert response.context["registrations"][0].visible_badge is None


def test_workspace_page_shows_badge_when_configured_visible(
    event, organization, visible_badge_type, coordinator, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("badge-visible@example.com")
    registration = make_registration(event=event, organization=organization, person=person)
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=visible_badge_type,
        actor=coordinator,
    )

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "badge-visible@example.com")
    response = client.get(reverse("registrations:workspace"))
    assert response.status_code == 200
    assert "VIP" in response.content.decode()


def test_arabic_locale_still_hides_a_non_visible_badge(
    event, organization, badge_type, coordinator, django_capture_on_commit_callbacks
):
    from apps.people.services import resolve_or_create_participant_for_email

    person = resolve_or_create_participant_for_email("badge-hidden-ar@example.com")
    registration = make_registration(event=event, organization=organization, person=person)
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )

    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, "badge-hidden-ar@example.com")
    response = client.get(reverse("registrations:workspace"), HTTP_ACCEPT_LANGUAGE="ar")
    assert response.status_code == 200
    assert "Standard" not in response.content.decode()
