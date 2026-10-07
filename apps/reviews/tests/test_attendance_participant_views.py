"""What a participant reads about attendance days: My registrations, the
registration page and the language of the wording (apps.accreditation.
attendance). Synthetic data."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.accreditation.models import AttendanceCategory
from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.people.services import resolve_or_create_participant_for_email
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.services import record_approved_decision

from .conftest import make_operational_user_with_membership, make_registration
from .test_participant_views import _otp_login

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(event):
    return make_operational_user_with_membership(
        email="attendance-participant-manager@example.com",
        group_name="Accreditation Managers",
        event_edition=event,
    )


def _registration(event, manager, email):
    person = resolve_or_create_participant_for_email(email)
    registration = make_registration(event=event, person=person)
    configure_attendance(event)
    assign_approval_prerequisites(registration, manager)
    make_verified_identity_case(registration)
    registration.refresh_from_db()
    return registration


def _approve(registration, manager, category):
    registration.refresh_from_db()
    record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=manager,
        attendance_category=category,
    )


def _workspace(django_capture_on_commit_callbacks, email, language="en"):
    client = Client()
    _otp_login(client, django_capture_on_commit_callbacks, email)
    client.cookies["django_language"] = language
    return client, client.get(reverse("registrations:workspace")).content.decode()


def test_a_submitted_request_shows_no_attendance_days(
    event, manager, django_capture_on_commit_callbacks
):
    _registration(event, manager, "attendance-submitted@example.com")
    _client, page = _workspace(
        django_capture_on_commit_callbacks, "attendance-submitted@example.com"
    )
    assert 'data-registration-guidance="SUBMITTED"' in page
    assert "data-attendance=" not in page


def test_my_registrations_and_the_registration_page_state_the_days(
    event, manager, django_capture_on_commit_callbacks
):
    registration = _registration(event, manager, "attendance-following@example.com")
    _approve(registration, manager, AttendanceCategory.FOLLOWING_TWO_DAYS)
    client, page = _workspace(
        django_capture_on_commit_callbacks, "attendance-following@example.com"
    )
    assert 'data-attendance="FOLLOWING_TWO_DAYS"' in page
    assert (
        "Your participation is approved for Sunday 6 December 2026 and Monday 7 December 2026. "
        "This approval does not include the opening day, Saturday 5 December 2026."
    ) in page
    detail = client.get(
        reverse("registrations:confirmation", kwargs={"reference": registration.public_reference})
    ).content.decode()
    assert 'data-attendance="FOLLOWING_TWO_DAYS"' in detail
    assert "the opening day, Saturday 5 December 2026" in detail


def test_an_unclassified_approval_reads_as_pending_never_guessed(
    event, manager, django_capture_on_commit_callbacks
):
    registration = _registration(event, manager, "attendance-legacy@example.com")
    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    _client, page = _workspace(django_capture_on_commit_callbacks, "attendance-legacy@example.com")
    assert 'data-attendance="PENDING"' in page
    assert "confirming which conference days it covers" in page
    assert "December 2026" not in page


@pytest.mark.parametrize(
    "language, expected",
    [
        ("fr", "samedi 5 décembre 2026"),
        ("ar", "5 ديسمبر 2026"),
    ],
)
def test_the_days_are_written_in_the_reader_language(
    event, manager, django_capture_on_commit_callbacks, language, expected
):
    email = f"attendance-{language}@example.com"
    registration = _registration(event, manager, email)
    _approve(registration, manager, AttendanceCategory.ALL_CONFERENCE_DAYS)
    _client, page = _workspace(django_capture_on_commit_callbacks, email, language)
    assert 'data-attendance="ALL_CONFERENCE_DAYS"' in page
    assert expected in page
    assert "Your participation is approved for the opening day" not in page
