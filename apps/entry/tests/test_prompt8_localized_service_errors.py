"""Phase 3 Prompt 8 (P8-01): entry device and checkpoint failures are localized.

Before this correction `apps.entry.views` rendered `str(exc)`, the entry
service's developer-facing English, on French and Arabic pages. Each test
drives a real failure through the real view in French and Arabic and
asserts the raw service text is absent and the translated message present.
Synthetic data only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape

from apps.accounts.tests.sign_in import staff_sign_in
from apps.core.service_errors import GENERIC_SERVICE_ERROR
from apps.entry import presentation
from apps.entry.services import EntryPermissionError, EntryStateError
from apps.entry.services.devices import DeviceConfigurationError, revoke_device
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db

LANGUAGES = ["fr", "ar"]


def _in(language, message) -> str:
    with translation.override(language):
        return str(message)


def _assert_localized(response, language, *, raw: str, expected) -> None:
    body = response.content.decode("utf-8")
    localized = _in(language, expected)
    assert localized != _in("en", expected)
    assert escape(localized) in body or localized in body
    assert raw not in body
    assert escape(raw) not in body


def _sign_in(client, user, language):
    staff_sign_in(client, user.email_normalized, factories.TEST_PASSWORD)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_device_expiry_beyond_the_maximum_is_localized(
    language, client, event, layout, device_admin, settings
):
    settings.ENTRY_DEVICE_MAX_ENROLLMENT_DAYS = 1
    _sign_in(client, device_admin, language)
    response = client.post(
        reverse("entry:device-list", args=[event.pk]),
        {
            "public_name": "Gate A tablet 7",
            "expires_at": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
            "gate_id": str(layout.gate_a.pk),
            "zone_ids": [str(layout.main.pk)],
            "verification_methods": ["QR"],
        },
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="The device expiry exceeds the maximum enrollment period.",
        expected=presentation._MESSAGES[(DeviceConfigurationError, "EXPIRY_TOO_LONG")],
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_opening_a_checkpoint_without_gate_permission_is_localized(
    language, client, event, layout, device_and_secret
):
    """Scoped to gate B only, the operator tries to open the gate-A device."""
    _device, secret = device_and_secret
    elsewhere = factories.make_user(
        "gate.b.only@example.test", group_name="Entry Operators", event=event, gate=layout.gate_b
    )
    _sign_in(client, elsewhere, language)
    client.cookies[settings.ENTRY_DEVICE_COOKIE_NAME] = secret
    response = client.post(
        reverse("entry:home"), {"action": "start", "zone_id": str(layout.main.pk)}, follow=True
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="You are not authorized to operate this checkpoint.",
        expected=presentation._MESSAGES[(EntryPermissionError, None)],
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_an_impossible_device_lifecycle_change_is_localized(language, client, device, device_admin):
    revoked = revoke_device(
        device=device,
        actor=device_admin,
        expected_version=device.version,
        reason_code="SECURITY_CONCERN",
    )
    _sign_in(client, device_admin, language)
    response = client.post(
        reverse("entry:device-resume", kwargs={"public_id": revoked.public_id}),
        {"expected_version": revoked.version, "reason_code": "ADMINISTRATIVE_ERROR"},
        follow=True,
    )
    assert response.status_code == 200
    _assert_localized(
        response,
        language,
        raw="This change is not permitted in the device's current state.",
        expected=presentation._MESSAGES[(EntryStateError, None)],
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_mapped_entry_message_is_translated(language):
    for message in [*presentation._MESSAGES.values(), GENERIC_SERVICE_ERROR]:
        assert _in(language, message) != _in("en", message), _in("en", message)


def test_an_unknown_checkpoint_reason_never_shows_the_raw_code():
    from apps.entry.views import _checkpoint_unavailable_message

    assert _checkpoint_unavailable_message("SOME_FUTURE_REASON") == str(GENERIC_SERVICE_ERROR)
