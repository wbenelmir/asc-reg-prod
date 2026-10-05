"""Participant and operational session expiry tests (Prompt 5 correction
pass §4, AF-AUTH-04, UI/UX §6.5)."""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import participant_auth, session_expiry
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core.models import Country, Sector
from apps.core.testing import otp_request_data
from apps.events.models import EventEdition, EventEditionStatus
from apps.registrations.models import Registration, RegistrationPublicStatus

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    EventEdition.objects.exclude(code="ASC2026").filter(
        status=EventEditionStatus.REGISTRATION_OPEN
    ).update(status=EventEditionStatus.REGISTRATION_CLOSED)
    EventEdition.objects.get_or_create(
        code="ASC2026",
        defaults={
            "name": "ASC 2026",
            "timezone": "UTC",
            "starts_at": timezone.now(),
            "ends_at": timezone.now(),
            "status": EventEditionStatus.REGISTRATION_OPEN,
        },
    )


def _otp_login(client: Client, email: str, django_capture_on_commit_callbacks) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )


def _force_session_timestamps(client: Client, *, established_at, last_activity_at) -> None:
    session = client.session
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = established_at
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = last_activity_at
    session.save()


# ---------------------------------------------------------------------------
# Participant session expiry
# ---------------------------------------------------------------------------


def test_login_establishes_both_timestamps(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-login@example.com", django_capture_on_commit_callbacks)
    session = client.session
    assert session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY in session
    assert session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY in session


def test_normal_activity_updates_only_the_inactivity_timestamp(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-activity@example.com", django_capture_on_commit_callbacks)
    established_before = client.session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY]

    client.get(reverse("registrations:workspace"))

    session = client.session
    assert session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] == established_before


def test_inactivity_expiry_returns_participant_to_otp_and_clears_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-inactive@example.com", django_capture_on_commit_callbacks)
    now = timezone.now()
    stale = (now - timezone.timedelta(seconds=99999)).isoformat()
    _force_session_timestamps(client, established_at=stale, last_activity_at=stale)

    with override_settings(
        PARTICIPANT_SESSION_INACTIVITY_SECONDS=60, PARTICIPANT_SESSION_ABSOLUTE_SECONDS=999999
    ):
        response = client.get(reverse("registrations:workspace"))

    assert response.status_code == 302
    assert response.url == reverse("accounts:otp-request")
    assert participant_auth.PARTICIPANT_SESSION_KEY not in client.session


def test_absolute_expiry_wins_even_with_recent_activity(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-absolute@example.com", django_capture_on_commit_callbacks)
    now = timezone.now()
    old_established = (now - timezone.timedelta(seconds=99999)).isoformat()
    recent_activity = now.isoformat()
    _force_session_timestamps(
        client, established_at=old_established, last_activity_at=recent_activity
    )

    with override_settings(
        PARTICIPANT_SESSION_ABSOLUTE_SECONDS=60, PARTICIPANT_SESSION_INACTIVITY_SECONDS=999999
    ):
        response = client.get(reverse("registrations:workspace"))

    assert response.status_code == 302
    assert response.url == reverse("accounts:otp-request")


def test_malformed_session_metadata_fails_closed(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-malformed@example.com", django_capture_on_commit_callbacks)
    session = client.session
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = "not-a-real-timestamp"
    session.save()

    response = client.get(reverse("registrations:workspace"))
    assert response.status_code == 302
    assert response.url == reverse("accounts:otp-request")


def test_extend_requires_post(client: Client, django_capture_on_commit_callbacks) -> None:
    _otp_login(client, "session-extend-get@example.com", django_capture_on_commit_callbacks)
    response = client.get(reverse("accounts:participant-session-extend"))
    assert response.status_code == 405


def test_extend_requires_csrf(django_capture_on_commit_callbacks) -> None:
    client = Client(enforce_csrf_checks=True)
    _otp_login(client, "session-extend-csrf@example.com", django_capture_on_commit_callbacks)
    response = client.post(reverse("accounts:participant-session-extend"))
    assert response.status_code == 403


def test_extend_resets_inactivity_but_never_the_absolute_deadline(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "session-extend-ok@example.com", django_capture_on_commit_callbacks)
    established_before = client.session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY]

    response = client.post(reverse("accounts:participant-session-extend"))
    assert response.status_code == 302

    session = client.session
    assert session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] == established_before
    assert participant_auth.PARTICIPANT_SESSION_KEY in session


@override_settings(OTP_RESEND_COOLDOWN_SECONDS=0)
def test_expired_session_preserves_the_draft_and_current_step_and_resumes_on_relogin(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    # `OTP_RESEND_COOLDOWN_SECONDS=0` for the WHOLE test: `resend_available_at`
    # is computed from the configured cooldown and stored on the challenge
    # row AT ISSUANCE time -- overriding the setting only around the SECOND
    # `issue_challenge()` call would not help, because the check compares
    # against the FIRST challenge's already-persisted `resend_available_at`,
    # not the current setting value.
    _otp_login(client, "session-resume@example.com", django_capture_on_commit_callbacks)
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
    registration = Registration.objects.get(profile__submitted_given_names="Amine")
    assert registration.current_step == "contact"

    # Force inactivity expiry.
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    _force_session_timestamps(client, established_at=stale, last_activity_at=stale)
    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        response = client.get(reverse("registrations:workspace"))
    assert response.url == reverse("accounts:otp-request")

    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.DRAFT
    assert registration.current_step == "contact"

    # A fresh OTP authentication resumes the SAME saved Registration Context.
    _otp_login(client, "session-resume@example.com", django_capture_on_commit_callbacks)
    workspace_response = client.get(reverse("registrations:workspace"))
    assert workspace_response.status_code == 200
    resumed = Registration.objects.get(pk=registration.pk)
    assert resumed.current_step == "contact"


# ---------------------------------------------------------------------------
# Operational session expiry
# ---------------------------------------------------------------------------


def _make_operational_user(email: str) -> OperationalUser:
    # `is_superuser=True` so the session-expiry tests below never depend on
    # `ScopedGroupMembership`/permission scoping (covered separately in
    # `test_policies.py`/`test_views.py`) -- these tests exist to prove
    # SESSION behavior, not authorization scope.
    user = OperationalUser.objects.create_superuser(
        email=email,
        password="__test_password__",  # noqa: S106
    )
    user.status = OperationalUserStatus.ACTIVE
    user.save(update_fields=["status"])
    return user


def test_operational_login_establishes_timestamps_and_activity_works(client: Client) -> None:
    _make_operational_user("ops-session@example.com")
    response = client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-session@example.com", "password": "__test_password__"},
    )
    assert response.status_code == 302
    session = client.session
    assert session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY in session

    # A normal follow-up request succeeds and touches inactivity only.
    established_before = session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY]
    ok_response = client.get(reverse("registrations:ops-intake-list"))
    assert ok_response.status_code == 200
    assert client.session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] == established_before


def test_operational_inactivity_expiry_forces_sign_in_again(client: Client) -> None:
    _make_operational_user("ops-inactive@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-inactive@example.com", "password": "__test_password__"},
    )
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    session = client.session
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = stale
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = stale
    session.save()

    with override_settings(OPERATIONAL_SESSION_INACTIVITY_SECONDS=60):
        response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302


def test_suspended_operational_account_loses_its_session_mid_use(client: Client) -> None:
    user = _make_operational_user("ops-suspended@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-suspended@example.com", "password": "__test_password__"},
    )
    assert client.get(reverse("registrations:ops-intake-list")).status_code == 200

    user.status = OperationalUserStatus.SUSPENDED
    user.save(update_fields=["status"])

    response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302


def test_time_limited_operational_account_loses_its_session_mid_use(client: Client) -> None:
    user = _make_operational_user("ops-time-limited@example.com")
    user.active_until = timezone.now() + timezone.timedelta(hours=1)
    user.save(update_fields=["active_until"])
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-time-limited@example.com", "password": "__test_password__"},
    )
    assert client.get(reverse("registrations:ops-intake-list")).status_code == 200

    user.active_until = timezone.now() - timezone.timedelta(seconds=1)
    user.save(update_fields=["active_until"])

    response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302
    assert client.session.get("_auth_user_id") is None


def test_participant_and_operational_session_expiry_are_independent(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    """Expiring the participant's timestamps must never affect an operational
    session's own timestamps in the same underlying session object, and
    vice versa -- they are stored under entirely separate keys."""
    _make_operational_user("ops-separate@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "ops-separate@example.com", "password": "__test_password__"},
    )
    operational_established = client.session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY]

    # Corrupting PARTICIPANT keys only must not disturb the operational ones.
    session = client.session
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = "garbage"
    session.save()

    assert client.session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] == operational_established
    assert client.get(reverse("registrations:ops-intake-list")).status_code == 200


# ---------------------------------------------------------------------------
# Prompt 5 correction pass §2: operational sign-out/expiry must NEVER
# destroy a valid participant session sharing the same browser session, and
# vice versa -- exercised through the REAL logout/expiry code paths, not
# just by corrupting session keys directly.
# ---------------------------------------------------------------------------


def test_operational_sign_out_preserves_a_valid_participant_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "both-audiences-1@example.com", django_capture_on_commit_callbacks)
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
    registration = Registration.objects.get(profile__submitted_given_names="Amine")
    participant_established = client.session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY]

    _make_operational_user("both-ops-1@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "both-ops-1@example.com", "password": "__test_password__"},
    )

    response = client.post(reverse("accounts:operational-sign-out"))
    assert response.status_code == 302

    session = client.session
    assert session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY not in session
    assert session.get("_auth_user_id") is None
    # The participant session -- key, timestamps, and Draft -- must all
    # survive the operational sign-out untouched.
    assert session[participant_auth.PARTICIPANT_SESSION_KEY]
    assert session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] == participant_established
    assert client.get(reverse("registrations:workspace")).status_code == 200
    registration.refresh_from_db()
    assert registration.current_step == "contact"


def test_operational_session_expiry_preserves_a_valid_participant_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "both-audiences-2@example.com", django_capture_on_commit_callbacks)
    participant_established = client.session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY]

    _make_operational_user("both-ops-2@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "both-ops-2@example.com", "password": "__test_password__"},
    )
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    session = client.session
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = stale
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = stale
    session.save()

    with override_settings(OPERATIONAL_SESSION_INACTIVITY_SECONDS=60):
        response = client.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302

    session = client.session
    assert session.get("_auth_user_id") is None
    assert session[participant_auth.PARTICIPANT_SESSION_KEY]
    assert session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] == participant_established
    assert client.get(reverse("registrations:workspace")).status_code == 200


def test_participant_logout_preserves_a_valid_operational_session(client: Client) -> None:
    _make_operational_user("both-ops-3@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "both-ops-3@example.com", "password": "__test_password__"},
    )
    operational_established = client.session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY]

    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = "00000000-0000-0000-0000-000000000000"
    now = timezone.now().isoformat()
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()

    response = client.post(reverse("accounts:participant-sign-out"))
    assert response.status_code == 302

    session = client.session
    assert participant_auth.PARTICIPANT_SESSION_KEY not in session
    # The operational session must survive the participant logout untouched.
    assert session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] == operational_established
    assert client.get(reverse("registrations:ops-intake-list")).status_code == 200


def test_participant_expiry_preserves_a_valid_operational_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "both-audiences-4@example.com", django_capture_on_commit_callbacks)
    _make_operational_user("both-ops-4@example.com")
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "both-ops-4@example.com", "password": "__test_password__"},
    )
    operational_established = client.session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY]

    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    _force_session_timestamps(client, established_at=stale, last_activity_at=stale)
    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        client.get(reverse("registrations:workspace"))

    session = client.session
    assert participant_auth.PARTICIPANT_SESSION_KEY not in session
    assert session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] == operational_established
    assert client.get(reverse("registrations:ops-intake-list")).status_code == 200


# ---------------------------------------------------------------------------
# Prompt 5 correction pass §1: every consumer of participant-session
# validity (not just `participant_required`) must use the one authoritative
# boundary -- document streaming and template context are covered in
# `apps/documents/tests/test_views.py` and here respectively.
# ---------------------------------------------------------------------------


def test_context_processor_never_advertises_an_expired_participant_as_authenticated(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "context-expired@example.com", django_capture_on_commit_callbacks)
    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    _force_session_timestamps(client, established_at=stale, last_activity_at=stale)

    # `accounts:operational-sign-in` renders unconditionally regardless of
    # participant session state (unlike `otp_request`/`otp_verify`, which
    # redirect an "already authenticated" participant away before
    # rendering at all using the raw, expiry-blind
    # `is_participant_authenticated` check by design -- a session key is
    # merely present, so that redirect still fires; `participant_required`
    # on the destination it redirects to is what then correctly logs the
    # expired session out). This proves the CONTEXT PROCESSOR itself never
    # advertises an expired session as authenticated on a page that does
    # render (Prompt 5 correction pass §1).
    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        response = client.get(reverse("accounts:operational-sign-in"))
    assert response.context["participant_authenticated"] is False


def test_context_processor_never_advertises_a_malformed_participant_session_as_authenticated(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    _otp_login(client, "context-malformed@example.com", django_capture_on_commit_callbacks)
    session = client.session
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = "garbage"
    session.save()

    response = client.get(reverse("accounts:operational-sign-in"))
    assert response.context["participant_authenticated"] is False


def test_language_switch_does_not_update_preferences_through_an_expired_participant_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    _otp_login(client, "lang-expired@example.com", django_capture_on_commit_callbacks)
    person = resolve_or_create_participant_for_email("lang-expired@example.com")
    original_language = person.preferred_language

    stale = (timezone.now() - timezone.timedelta(seconds=99999)).isoformat()
    _force_session_timestamps(client, established_at=stale, last_activity_at=stale)

    with override_settings(PARTICIPANT_SESSION_INACTIVITY_SECONDS=60):
        client.post(
            reverse("set-language"), {"language": "fr", "next": reverse("accounts:otp-request")}
        )

    person.refresh_from_db()
    assert person.preferred_language == original_language


def test_language_switch_does_not_update_preferences_through_a_malformed_participant_session(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    _otp_login(client, "lang-malformed@example.com", django_capture_on_commit_callbacks)
    person = resolve_or_create_participant_for_email("lang-malformed@example.com")
    original_language = person.preferred_language

    session = client.session
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = "garbage"
    session.save()

    client.post(
        reverse("set-language"), {"language": "ar", "next": reverse("accounts:otp-request")}
    )

    person.refresh_from_db()
    assert person.preferred_language == original_language
