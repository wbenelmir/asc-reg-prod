"""PWA shell, service worker, manifest and offline administration views
(Phase 4 Prompt 2). PostgreSQL, Django test client.
"""

from __future__ import annotations

import json
import re

import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.tests.mfa_double import ACCEPTED_TEST_RESPONSE
from apps.entry.models import DeviceWipeOrder, EntryDeviceStatus, OfflineEventSetting
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db


def _login(user, language="en") -> Client:
    from apps.accounts import session_expiry

    client = Client()
    client.force_login(user)
    session = client.session
    now = timezone.now().isoformat()
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = language
    return client


# ---------------------------------------------------------------------------
# Service worker and manifest
# ---------------------------------------------------------------------------


def test_service_worker_is_scoped_to_entry_and_caches_only_the_shell(offline_on):
    response = Client().get("/entry/sw.js")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert "Service-Worker-Allowed" not in response  # scope can never exceed /entry/
    body = response.content.decode()
    precache = json.loads(re.search(r"const PRECACHE = (\[.*?\]);", body).group(1))
    assert "/entry/offline/" in precache
    for url in precache:
        assert url == "/entry/offline/" or url.startswith(django_settings.STATIC_URL), url
    for sensitive in ("/entry/verify", "/entry/api", "/entry/photo", "/ops/"):
        assert all(not url.startswith(sensitive) for url in precache)
    assert "indexedDB" not in body  # the worker never touches local records
    assert "self.skipWaiting(" not in body  # an update never interrupts an operation


def test_disabled_offline_serves_a_self_removing_worker(settings):
    settings.ENTRY_OFFLINE_ENABLED = False
    body = Client().get("/entry/sw.js").content.decode()
    assert "unregister()" in body and "caches.delete" in body
    assert "indexedDB" not in body
    assert "PRECACHE" not in body


def test_manifest_scope_and_start_url(offline_on):
    manifest = Client().get("/entry/manifest.webmanifest").json()
    assert manifest["scope"] == "/entry/"
    assert manifest["start_url"] == "/entry/offline/"
    assert manifest["display"] == "standalone"


# ---------------------------------------------------------------------------
# The shell is static: safe to cache
# ---------------------------------------------------------------------------


def test_shell_html_is_identical_for_every_visitor(offline_on, device_admin, operator):
    for user, name in (
        (device_admin, "Synthetic Admin Zeta"),
        (operator, "Synthetic Operator Yara"),
    ):
        user.display_name = name
        user.save(update_fields=["display_name"])
    anonymous = Client().get("/entry/offline/").content
    admin = _login(device_admin).get("/entry/offline/").content
    user = _login(operator).get("/entry/offline/").content
    assert anonymous == admin == user
    text = anonymous.decode()
    assert "csrfmiddlewaretoken" not in text
    assert device_admin.display_name not in text and operator.display_name not in text
    # Prompt 3 adds the local QR form. Its controls cannot serialize a QR
    # into a native HTTP submission, and no visitor-specific value is cached.
    forms = re.findall(r"<form\b([^>]*)>(.*?)</form>", text, re.S)
    assert len(forms) == 1
    attributes, controls = forms[0]
    assert 'id="offline-verify-form"' in attributes
    assert re.search(r"\bhidden\b", attributes)
    assert not re.search(r"\b(?:action|method)\s*=", attributes)
    assert 'id="offline-qr"' in controls
    assert not re.search(r"\b(?:name|value|formaction)\s*=", controls)


def test_shell_carries_config_and_strings_but_no_secret(offline_on):
    response = Client().get("/entry/offline/")
    assert response["Cache-Control"].startswith("no-cache")
    text = response.content.decode()
    config = json.loads(
        re.search(
            r'<script id="asc-offline-config" type="application/json">(.*?)</script>', text, re.S
        ).group(1)
    )
    assert config["enabled"] is True
    assert config["scope"] == "/entry/"
    assert set(config["permitted"]) == {
        "ONLINE",
        "OFFLINE_READY",
        "OFFLINE_ACTIVE",
        "SYNCING",
        "STALE",
        "EXPIRED",
        "BLOCKED",
    }
    assert config["validity"]["STANDARD"]["expires_after_seconds"] == 8 * 60 * 60
    assert "-----BEGIN" not in text


@pytest.mark.parametrize(
    ("language", "direction", "heading"),
    [
        ("en", "ltr", "Offline readiness"),
        ("fr", "ltr", "Préparation hors ligne"),
        ("ar", "rtl", "الجاهزية دون اتصال"),
    ],
)
def test_shell_is_localized_with_rtl(offline_on, language, direction, heading):
    client = Client()
    client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = language
    text = client.get("/entry/offline/").content.decode()
    assert f'lang="{language}"' in text and f'dir="{direction}"' in text
    assert heading in text


def test_shell_when_disabled_loads_no_script(settings):
    settings.ENTRY_OFFLINE_ENABLED = False
    text = Client().get("/entry/offline/").content.decode()
    assert 'id="offline-disabled"' in text
    assert "entry-offline.js" not in text


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


def test_event_toggle_requires_its_permission(offline_on, event, device_admin, operator):
    denied = _login(operator).post(
        f"/ops/entry/events/{event.pk}/offline/", {"enabled": "1", "reason_code": "REHEARSAL"}
    )
    assert denied.status_code in (302, 403, 404)
    assert not OfflineEventSetting.objects.filter(event_edition=event, enabled=True).exists()
    ok = _login(device_admin).post(
        f"/ops/entry/events/{event.pk}/offline/", {"enabled": "1", "reason_code": "REHEARSAL"}
    )
    assert ok.status_code == 302
    assert OfflineEventSetting.objects.get(event_edition=event).enabled is True


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_device_detail_shows_readiness(offline_device, device_admin, active_pass, language):
    package, _ = offline_factories.download(offline_device)
    offline_factories.self_test(offline_device, admin=device_admin, package=package)
    response = _login(device_admin, language).get(
        f"/ops/entry/devices/{offline_device.device.public_id}/"
    )
    text = response.content.decode()
    assert response.status_code == 200
    assert "data-offline-readiness" in text
    assert 'data-offline-band="FRESH"' in text
    assert "data-block-offline" in text
    assert active_pass.jti not in text
    if language == "ar":
        assert 'dir="rtl"' in text


def test_block_offline_view(offline_device, device_admin, active_pass):
    package, _ = offline_factories.download(offline_device)
    offline_factories.self_test(offline_device, admin=device_admin, package=package)
    response = _login(device_admin).post(
        f"/ops/entry/devices/{offline_device.device.public_id}/block-offline/",
        {"expected_version": offline_device.device.version, "reason_code": "SECURITY_CONCERN"},
    )
    assert response.status_code == 302
    offline_device.device.refresh_from_db()
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED
    assert offline_device.device.offline_blocked_at is not None


def test_emergency_wipe_page_is_hidden_from_device_administrators(offline_device, device_admin):
    response = _login(device_admin).get(
        f"/ops/entry/devices/{offline_device.device.public_id}/emergency-wipe/"
    )
    assert response.status_code == 404


def test_emergency_wipe_flow_for_the_security_authority(offline_device, event):
    manager = factories.make_user(
        "wipe.manager@example.test", group_name="Security Restriction Managers", event=event
    )
    client = _login(manager)
    url = f"/ops/entry/devices/{offline_device.device.public_id}/emergency-wipe/"
    page = client.get(url)
    assert page.status_code == 200
    assert page["Cache-Control"].startswith("no-store")
    with override_settings(MFA_BACKEND="apps.accounts.tests.mfa_double.AcceptingStepUpBackend"):
        refused = client.post(
            url, {"reason_code": "DEVICE_LOST", "mfa_response": ACCEPTED_TEST_RESPONSE}
        )
        assert refused.status_code == 200  # confirmation missing: form invalid
        assert not DeviceWipeOrder.objects.exists()
        ordered = client.post(
            url,
            {
                "reason_code": "DEVICE_LOST",
                "confirm_evidence_loss": "on",
                "mfa_response": ACCEPTED_TEST_RESPONSE,
            },
        )
    assert ordered.status_code == 302
    order = DeviceWipeOrder.objects.get()
    assert order.ordered_by == manager
    # The MFA response is never echoed back into the page.
    again = client.get(url).content.decode()
    assert ACCEPTED_TEST_RESPONSE not in again


def test_emergency_wipe_without_mfa_is_refused(offline_device, event):
    manager = factories.make_user(
        "wipe.manager2@example.test", group_name="Security Restriction Managers", event=event
    )
    client = _login(manager)
    url = f"/ops/entry/devices/{offline_device.device.public_id}/emergency-wipe/"
    with override_settings(MFA_BACKEND=None):
        response = client.post(
            url,
            {
                "reason_code": "DEVICE_LOST",
                "confirm_evidence_loss": "on",
                "mfa_response": "anything",
            },
        )
    assert response.status_code == 200
    assert not DeviceWipeOrder.objects.exists()
    assert "MFA step-up is not available" in response.content.decode()
