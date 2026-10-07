"""End-to-end wizard, workspace, i18n/RTL, and operations-intake authorization tests.

Uses `django_capture_on_commit_callbacks` (pytest-django) rather than
`django_db(transaction=True)`, deliberately: the latter flushes tables
between tests (TransactionTestCase semantics) and does NOT re-run the
one-off `RunPython` data migrations (events.0002/privacy.0002/
communications.0002/registrations.0002) that this whole flow depends on --
only the first such test in a run would still see that seed data.
"""

from __future__ import annotations

import re

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.accounts.tests.sign_in import staff_sign_in
from apps.core.models import Country, Sector
from apps.core.testing import otp_request_data
from apps.documents.tests.factories import make_test_photo
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.registrations.tests.factories import notices_post_data

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_countries():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


def _professional_step_payload(**overrides) -> dict:
    payload = {
        "organization_name": "Test Org",
        "organization_type": "COMPANY",
        "job_title": "Founder",
        "department": "Management",
        "sector": "TECH",
        "country_code": "DZ",
        "organization_website": "https://example.com",
        "professional_profile_url": "https://example.com/in/profile",
        "biography": "A short professional biography.",
        "operating_scope": "NATIONAL",
    }
    payload.update(overrides)
    return payload


def _otp_login(client: Client, django_capture_on_commit_callbacks, email: str) -> None:
    from django.test import override_settings

    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        response = client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    assert response.status_code == 302
    assert response.url == reverse("registrations:workspace")


def test_otp_login_creates_person_and_reaches_workspace(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, django_capture_on_commit_callbacks, "new-participant@example.com")
    response = client.get(reverse("registrations:workspace"))
    assert response.status_code == 200


def test_unauthenticated_user_is_redirected_from_wizard_steps(client: Client) -> None:
    response = client.get(reverse("registrations:step-identity"))
    assert response.status_code == 302
    assert response.url == reverse("accounts:otp-request")


def test_full_registration_wizard_end_to_end(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, django_capture_on_commit_callbacks, "wizard-e2e@example.com")

    identity_response = client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Amine",
            "family_name": "Benali",
            "date_of_birth": "1990-01-01",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN",
            "nin_value": "123456789012345678",
        },
    )
    assert identity_response.status_code == 302

    contact_response = client.post(
        reverse("registrations:step-contact"),
        {"mobile_country_code": "DZ", "mobile_number": "0551234567"},
    )
    assert contact_response.status_code == 302

    professional_response = client.post(
        reverse("registrations:step-professional"),
        {**_professional_step_payload(), "profile_photo": make_test_photo()},
    )
    if professional_response.status_code != 302:
        raise AssertionError(professional_response.context["form"].errors)

    from apps.registrations.models import InterestTopic

    topic = InterestTopic.objects.filter(event_edition__code="ASC2026").first()
    interests_response = client.post(
        reverse("registrations:step-interests"),
        {
            "interest_topics": [str(topic.pk)],
            "objectives_text": "Meet investors",
            "accessibility_needs_text": "",
        },
    )
    assert interests_response.status_code == 302

    review_response = client.post(reverse("registrations:step-review"))
    assert review_response.status_code == 302

    with django_capture_on_commit_callbacks(execute=True):
        notices_response = client.post(
            reverse("registrations:step-notices"),
            notices_post_data(),
        )
    assert notices_response.status_code == 302
    assert "/register/confirmation/" in notices_response.url

    registration = Registration.objects.filter(event_edition__code="ASC2026").latest("created_at")
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    assert len(mail.outbox) >= 1  # confirmation email delivered


def test_submitting_at_the_notices_step_with_a_field_removed_out_of_band_is_rejected(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    """Simulates a caller that bypasses the wizard's own step-by-step field
    requirements (e.g. a direct database edit, or a future alternate client) --
    the view must catch `IncompleteRegistrationError`, show a generic message,
    and redirect to the earliest incomplete step, never create a submission
    (Prompt 4 final closure pass §1)."""
    _otp_login(client, django_capture_on_commit_callbacks, "bypass-attempt@example.com")
    client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Amine",
            "family_name": "Benali",
            "date_of_birth": "1990-01-01",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN",
            "nin_value": "123456789012345678",
        },
    )
    client.post(
        reverse("registrations:step-contact"),
        {"mobile_country_code": "DZ", "mobile_number": "0551234567"},
    )
    client.post(
        reverse("registrations:step-professional"),
        {**_professional_step_payload(), "profile_photo": make_test_photo()},
    )
    from apps.registrations.models import InterestTopic

    topic = InterestTopic.objects.filter(event_edition__code="ASC2026").first()
    client.post(
        reverse("registrations:step-interests"),
        {
            "interest_topics": [str(topic.pk)],
            "objectives_text": "Meet investors",
            "accessibility_needs_text": "",
        },
    )
    client.post(reverse("registrations:step-review"))

    registration = Registration.objects.get(
        event_edition__code="ASC2026", profile__submitted_given_names="Amine"
    )
    # Simulate an out-of-band removal of a required field, bypassing every
    # wizard form's own required-field validation entirely.
    registration.interests.all().delete()

    response = client.post(
        reverse("registrations:step-notices"),
        notices_post_data(),
    )
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-interests")
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.DRAFT
    from apps.registrations.models import RegistrationSubmission

    assert RegistrationSubmission.objects.filter(registration=registration).count() == 0


def test_double_submit_returns_the_same_confirmation_not_a_second_registration(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, django_capture_on_commit_callbacks, "double-submit@example.com")
    client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Sara",
            "family_name": "K",
            "date_of_birth": "1991-01-01",
            "nationality_code": "FR",
            "country_of_residence": "FR",
            "identity_path": "PASSPORT",
            "passport_number": "X1234567",
            "passport_country_code": "FR",
            "passport_expires_at": "2030-01-01",
            # IDV-3 (A13-02): required on the passport path.
            "passport_identity_page": make_test_photo(),
        },
    )
    # UX-2 (D-01): a mobile number is required for every participant.
    client.post(
        reverse("registrations:step-contact"),
        {"mobile_country_code": "FR", "mobile_number": "06 12 34 56 78"},
    )
    client.post(
        reverse("registrations:step-professional"),
        {**_professional_step_payload(country_code="FR"), "profile_photo": make_test_photo()},
    )
    from apps.registrations.models import InterestTopic

    topic = InterestTopic.objects.filter(event_edition__code="ASC2026").first()
    client.post(
        reverse("registrations:step-interests"),
        {"interest_topics": [str(topic.pk)], "objectives_text": "Meet mentors"},
    )
    client.post(reverse("registrations:step-review"))

    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(
            reverse("registrations:step-notices"),
            notices_post_data(),
        )
    with django_capture_on_commit_callbacks(execute=True):
        second = client.post(
            reverse("registrations:step-notices"),
            notices_post_data(),
        )
    assert first.url == second.url
    reference = first.url.rstrip("/").rsplit("/", 1)[-1]
    from apps.registrations.models import RegistrationSubmission
    from apps.registrations.services import initial_submission_operation_key

    registration = Registration.objects.get(public_reference=reference)
    assert RegistrationSubmission.objects.filter(registration=registration).count() == 1

    # Prompt 6 P6-H-02: the view derives its idempotency key solely from the
    # Registration's own id -- never a random UUID cached in the session
    # under `submit_key_<pk>`.
    assert not any(key.startswith("submit_key_") for key in client.session.keys())
    submission = RegistrationSubmission.objects.get(registration=registration)
    assert submission.idempotency_key == initial_submission_operation_key(registration.pk)


# ---------------------------------------------------------------------------
# Workspace context isolation (Prompt 4 final closure pass §5)
# ---------------------------------------------------------------------------


def test_continue_resumes_the_selected_context_only_among_two_drafts(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    """A participant with two separate Draft registrations for two events must
    have each Continue action resume EXACTLY the one selected, never whichever
    happened to be last active in the session."""
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.people import services as people_services
    from apps.registrations.services import get_or_create_active_draft

    _otp_login(client, django_capture_on_commit_callbacks, "two-drafts@example.com")
    person = people_services.resolve_or_create_participant_for_email("two-drafts@example.com")

    other_event = EventEdition.objects.create(
        code="OTHEREVT",
        name="Other Event",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    asc_event = EventEdition.objects.filter(code="ASC2026").get()

    draft_asc = get_or_create_active_draft(person=person, event_edition=asc_event)
    draft_asc.current_step = "contact"
    draft_asc.save(update_fields=["current_step"])
    draft_other = get_or_create_active_draft(person=person, event_edition=other_event)
    draft_other.current_step = "professional"
    draft_other.save(update_fields=["current_step"])

    response = client.post(reverse("registrations:continue", kwargs={"pk": draft_asc.pk}))
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-contact")

    response = client.post(reverse("registrations:continue", kwargs={"pk": draft_other.pk}))
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-professional")


def test_continue_requires_post(client: Client, django_capture_on_commit_callbacks) -> None:
    _otp_login(client, django_capture_on_commit_callbacks, "continue-get@example.com")
    from apps.events.selectors import current_event_edition
    from apps.people import services as people_services
    from apps.registrations.services import get_or_create_active_draft

    person = people_services.resolve_or_create_participant_for_email("continue-get@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=current_event_edition())
    response = client.get(reverse("registrations:continue", kwargs={"pk": draft.pk}))
    assert response.status_code == 405


def test_continue_rejects_a_registration_belonging_to_another_participant(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    from apps.events.selectors import current_event_edition
    from apps.people import services as people_services
    from apps.registrations.services import get_or_create_active_draft

    other_person = people_services.resolve_or_create_participant_for_email("owner@example.com")
    other_draft = get_or_create_active_draft(
        person=other_person, event_edition=current_event_edition()
    )

    _otp_login(client, django_capture_on_commit_callbacks, "not-the-owner@example.com")
    response = client.post(reverse("registrations:continue", kwargs={"pk": other_draft.pk}))
    assert response.status_code == 404


def test_arabic_page_renders_rtl_direction(client: Client) -> None:
    client.post(
        reverse("set-language"), {"language": "ar", "next": reverse("accounts:otp-request")}
    )
    response = client.get(reverse("accounts:otp-request"))
    content = response.content.decode("utf-8")
    assert 'dir="rtl"' in content
    assert "bootstrap.rtl.min.css" in content


def test_french_page_uses_ltr_and_french_stylesheet(client: Client) -> None:
    client.post(
        reverse("set-language"), {"language": "fr", "next": reverse("accounts:otp-request")}
    )
    response = client.get(reverse("accounts:otp-request"))
    content = response.content.decode("utf-8")
    assert 'dir="ltr"' in content
    assert "Envoyer le code de vérification" in content


def test_english_is_the_default_language(client: Client) -> None:
    response = client.get(reverse("accounts:otp-request"))
    assert "Send verification code" in response.content.decode("utf-8")


def test_switching_language_mid_wizard_updates_the_active_registrations_preferred_language(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    """Prompt 5 correction pass §5: the active language must update the
    participant/registration preference so legal notices and confirmation
    delivery use the intended language -- and must never duplicate a Draft,
    Person, acceptance, submission, or message."""
    from apps.people.models import Person
    from apps.people.services import resolve_or_create_participant_for_email

    _otp_login(client, django_capture_on_commit_callbacks, "lang-sync@example.com")
    client.get(reverse("registrations:step-identity"))  # establishes the active Draft
    person = resolve_or_create_participant_for_email("lang-sync@example.com")
    registration_before = Registration.objects.filter(person=person).count()
    person_count_before = Person.objects.count()

    response = client.post(
        reverse("set-language"),
        {"language": "fr", "next": reverse("registrations:step-identity")},
    )
    assert response.status_code == 302

    registration = Registration.objects.get(person=person)
    assert registration.preferred_language == "fr"
    person.refresh_from_db()
    assert person.preferred_language == "fr"
    assert Registration.objects.filter(person=person).count() == registration_before
    assert Person.objects.count() == person_count_before


def test_switching_language_while_unauthenticated_does_not_error(client: Client) -> None:
    response = client.post(
        reverse("set-language"), {"language": "ar", "next": reverse("accounts:otp-request")}
    )
    assert response.status_code == 302


def test_page_has_skip_link_and_landmark_navigation(client: Client) -> None:
    response = client.get(reverse("accounts:otp-request"))
    content = response.content.decode("utf-8")
    assert "Skip to main content" in content
    assert "<main" in content
    assert 'id="main-content"' in content
    assert "<nav" in content


def test_form_field_has_an_associated_label(client: Client) -> None:
    response = client.get(reverse("accounts:otp-request"))
    content = response.content.decode("utf-8")
    assert re.search(r'<label for="id_email"', content)
    assert 'id="id_email"' in content


def test_invalid_field_widget_is_described_by_its_error() -> None:
    from apps.registrations.forms import IdentityStepForm

    form = IdentityStepForm(data={})
    assert not form.is_valid()
    attributes = form.fields["given_names"].widget.attrs
    assert attributes["aria-invalid"] == "true"
    assert "id_given_names_error" in attributes["aria-describedby"].split()


@pytest.mark.parametrize(
    ("language", "expected"),
    [("fr", "Soumise"), ("ar", "تم الإرسال")],
)
def test_public_status_is_localized(language: str, expected: str) -> None:
    from django.utils import translation

    registration = Registration(public_status=RegistrationPublicStatus.SUBMITTED)
    with translation.override(language):
        assert registration.get_public_status_display() == expected


# ---------------------------------------------------------------------------
# Operations intake authorization
# ---------------------------------------------------------------------------


def _make_ops_user(email: str) -> OperationalUser:
    return OperationalUser.objects.create_user(
        email=email,
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )


def _sign_in(client: Client, email: str) -> None:
    staff_sign_in(client, email, "__test_password__")


def _grant_scoped_view_permission(user: OperationalUser) -> None:
    """Authorize `user` the ONLY way `has_scoped_permission` recognizes: an active
    `ScopedGroupMembership` whose OWN Group grants the permission (Prompt 4 final
    closure pass §6) -- never a bare `user.user_permissions.add(...)`."""
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership

    group = Group.objects.create(name=f"Intake-{user.pk}")
    permission = Permission.objects.get(
        content_type__app_label="registrations", codename="view_registration"
    )
    group.permissions.add(permission)
    ScopedGroupMembership.objects.create(user=user, group=group, granted_by=user)


def test_ops_intake_requires_sign_in(client: Client) -> None:
    response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302
    assert reverse("accounts:operational-sign-in") in response.url


def test_ops_intake_requires_permission(client: Client) -> None:
    _make_ops_user("no-permission@example.com")
    _sign_in(client, "no-permission@example.com")
    response = client.get(reverse("registrations:ops-intake-list"))
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403


def test_authorized_ops_user_sees_the_intake_list(client: Client) -> None:
    user = _make_ops_user("has-permission@example.com")
    _grant_scoped_view_permission(user)
    _sign_in(client, "has-permission@example.com")
    response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 200


def test_permission_from_one_group_never_combines_with_scope_from_another(client: Client) -> None:
    """A user in Group A (which grants `view_registration`, no scoped membership) AND
    separately holding an active `ScopedGroupMembership` through unrelated Group B (which
    does NOT grant the permission) must be denied -- the two must never combine
    (Prompt 4 final closure pass §6)."""
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership

    user = _make_ops_user("split-grant@example.com")
    group_with_permission = Group.objects.create(name="HasPermissionOnly")
    permission = Permission.objects.get(
        content_type__app_label="registrations", codename="view_registration"
    )
    group_with_permission.permissions.add(permission)
    user.groups.add(group_with_permission)

    group_without_permission = Group.objects.create(name="ScopedButNoPermission")
    ScopedGroupMembership.objects.create(user=user, group=group_without_permission, granted_by=user)

    _sign_in(client, "split-grant@example.com")
    response = client.get(reverse("registrations:ops-intake-list"))
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403


def test_deleted_pending_invitation_link_fails_generically_instead_of_falling_through(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    """A session `invitation_link_id` that no longer resolves to ANY row
    (the link was deleted between initial resolution and consumption) MUST
    be treated exactly like every other invalid/revoked/expired invitation
    -- the generic unavailable response -- never silently fall through to
    an ordinary OPEN registration draft (Phase 2 Prompt 2 V2 correction
    pass "fail generically when a pending invitation link disappears")."""
    from apps.events.models import EventEdition
    from apps.invitations.models import InvitationCampaignStatus
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        issue_initial_link,
    )
    from apps.organizations.models import Organization, OrganizationType

    event = EventEdition.objects.filter(code="ASC2026").get()
    organization = Organization.objects.create(
        official_name="Deleted Link Org",
        normalized_name="deleted link org",
        organization_type=OrganizationType.OTHER,
    )
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Deleted Link Campaign",
            public_reference="CAMP-DELLINK-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    link, _raw_token = issue_initial_link(campaign)
    link_id = link.pk
    link.delete()

    _otp_login(client, django_capture_on_commit_callbacks, "deleted-link-participant@example.com")
    session = client.session
    session["invitation_link_id"] = str(link_id)
    session.save()

    response = client.get(reverse("registrations:step-identity"))
    assert response.status_code == 200
    assert "This invitation is not available" in response.content.decode()
    # Never silently fell through to creating an ordinary OPEN draft.
    assert Registration.objects.filter(event_edition=event).count() == 0
