"""P4-4-C3 (owner decision MFA-01 revised on 2026-10-01, amendment A-11):
operational users sign in with email and password, and participants keep the
email OTP.

The sign-in path has no environment branch: staging and production run this
same view and service (their settings are checked in a subprocess by
`tests/foundation/test_p4_4_c3_settings_and_deploy.py`). The existing P4-4
modules keep covering the attempt limits, the audit and the session clocks;
the tests here pin what the revision must not change. Synthetic data only.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import mfa, participant_auth, session_expiry
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.core.testing import otp_request_data

pytestmark = pytest.mark.django_db

SIGN_IN = reverse("accounts:operational-sign-in")
EMAIL = "p44c3-staff@example.test"
GOOD = "p44c3-correct-horse-battery"
ACCEPTING = "apps.accounts.tests.mfa_double.AcceptingStepUpBackend"


@pytest.fixture
def staff():
    return OperationalUser.objects.create_user(
        email=EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )


def _sign_in(password=GOOD, *, address="198.51.100.31"):
    client = Client(REMOTE_ADDR=address)
    response = staff_sign_in(client, EMAIL, password)
    return client, response


@pytest.mark.parametrize("backend", [None, ACCEPTING])
def test_email_and_password_alone_create_the_operational_session(staff, settings, backend) -> None:
    """With or without a step-up provider, sign-in never asks for or consults MFA."""
    settings.MFA_BACKEND = backend
    with (
        patch.object(mfa, "verify_step_up", side_effect=AssertionError("step-up at sign-in")),
        patch.object(mfa, "get_mfa_backend", side_effect=AssertionError("provider at sign-in")),
    ):
        client, response = _sign_in()
    assert response.status_code == 302
    assert client.session[SESSION_KEY] == str(staff.pk)
    assert session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY in client.session
    assert AuditEvent.objects.filter(
        action_code=action_codes.OPERATIONAL_SIGN_IN_SUCCEEDED
    ).exists()


def test_a_wrong_password_still_creates_no_session(staff) -> None:
    client, response = _sign_in("p44c3-wrong-password")
    assert response.status_code == 200
    assert SESSION_KEY not in client.session


@override_settings(OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL=2)
def test_the_password_attempt_limit_still_applies(staff) -> None:
    for _ in range(2):
        _sign_in("p44c3-wrong-password")
    client, response = _sign_in(GOOD)
    assert response.status_code == 429
    assert SESSION_KEY not in client.session


@pytest.mark.parametrize(
    "change",
    [
        {"status": OperationalUserStatus.SUSPENDED},
        {"active_until": timezone.now() - datetime.timedelta(minutes=1)},
    ],
)
def test_suspended_and_expired_accounts_still_get_no_session(staff, change) -> None:
    OperationalUser.objects.filter(pk=staff.pk).update(**change)
    client, _response = _sign_in()
    assert SESSION_KEY not in client.session


def test_a_password_session_carries_no_permission_or_scope_by_itself(staff) -> None:
    """Authorization is unchanged: signing in grants no operational page."""
    client, _response = _sign_in()
    for name in ("registrations:ops-intake-list", "communications:operations-messages"):
        assert client.get(reverse(name)).status_code in (403, 404)


def test_the_participant_otp_is_unchanged_and_unaffected_by_staff_passwords(staff) -> None:
    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        requested = client.post(
            reverse("accounts:otp-request"),
            otp_request_data("p44c3-participant@example.com", client=client),
        )
        assert requested.status_code == 302
        # A staff password is never accepted as a participant code.
        wrong = client.post(reverse("accounts:otp-verify"), {"code": GOOD})
        assert not participant_auth.is_participant_authenticated(wrong.wsgi_request)
        verified = client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    assert verified.status_code == 302
    request = client.get(reverse("registrations:workspace")).wsgi_request
    assert participant_auth.is_participant_authenticated(request)
    # The participant session is not an operational one.
    assert SESSION_KEY not in client.session
