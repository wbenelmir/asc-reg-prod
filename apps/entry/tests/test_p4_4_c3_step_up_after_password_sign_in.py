"""P4-4-C3: the MFA-01 revision (email and password sign-in) does not weaken
the step-up of sensitive operations.

A Security Restriction Manager signs in through the real operational
sign-in form with a password only. The emergency device wipe must still fail
closed without a valid step-up: no provider, a rejecting provider, or no
step-up response at all. Synthetic data only.
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.models import DeviceWipeOrder
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db

REJECTING = "apps.accounts.tests.mfa_double.RejectingStepUpBackend"
ACCEPTING = "apps.accounts.tests.mfa_double.AcceptingStepUpBackend"


def _password_signed_in_manager(event) -> Client:
    manager = factories.make_user(
        "p44c3.wipe.manager@example.test", group_name="Security Restriction Managers", event=event
    )
    client = Client(REMOTE_ADDR="198.51.100.41")
    response = client.post(
        reverse("accounts:operational-sign-in"),
        {"email": manager.email_normalized, "password": factories.TEST_PASSWORD},
    )
    assert response.status_code == 302  # password alone signs in (MFA-01 revised)
    return client


def _wipe(client, offline_device, mfa_response):
    url = f"/ops/entry/devices/{offline_device.device.public_id}/emergency-wipe/"
    return client.post(
        url,
        {
            "reason_code": "DEVICE_LOST",
            "confirm_evidence_loss": "on",
            "mfa_response": mfa_response,
        },
    )


@pytest.mark.parametrize(
    ("backend", "mfa_response", "reason"),
    [
        (None, "anything", "MFA_UNAVAILABLE"),
        (REJECTING, "anything", "MFA_STEP_UP_FAILED"),
        (ACCEPTING, "", None),
    ],
)
def test_a_password_session_never_replaces_the_wipe_step_up(
    offline_device, event, backend, mfa_response, reason
) -> None:
    client = _password_signed_in_manager(event)
    with override_settings(MFA_BACKEND=backend):
        response = _wipe(client, offline_device, mfa_response)
    assert response.status_code == 200
    assert not DeviceWipeOrder.objects.exists()
    if reason:
        assert AuditEvent.objects.filter(
            action_code=action_codes.DEVICE_EMERGENCY_WIPE_REFUSED, reason_code=reason
        ).exists()
