"""The Ministry NIN diagnostics page in a real browser.

The provider's HTTPS transport is replaced by a scripted fake inside the test
process (the live server runs in the same process), so no request ever leaves
for the Ministry. Covered:

* the three checks are separate (configuration shown on load, authentication
  and lookup each behind their own button);
* authentication success (with its HTTP status) and failure, lookup found,
  `identite: null`, redirect refused and invalid input, each with its own
  announced result (`role=status`, or `role=alert` for a refusal);
* loading and duplicate-submission protection (one request for a double
  click; the form is marked busy meanwhile);
* labels and keyboard use (the number field by its label, Enter submits);
* English, French and Arabic, right-to-left, desktop and phone width without
  horizontal overflow; the number is never shown again.

Screenshots go to `var/test_artifacts/asc2026_update/nin_diagnostics/`.
Synthetic accounts, numbers and tokens only.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import solve_staff_captcha

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "asc2026_update"
SHOTS = SHOTS / "nin_diagnostics"
PAGE = "/ops/integrations/ministry-nin/"
EMAIL = "browser-diagnostics@example.test"
TEST_NIN = "990000000000000088"
ISSUED = "synthetic-browser-bearer-ABCDEFGHIJKLMNOP"
CONFIGURED_B = "synthetic-browser-value"
OFFICIAL = {
    "NIN_PROVIDER_BACKEND": "apps.people.nin_provider.MinistryNinProvider",
    "MINISTRY_NIN_API_BASE_URL": "https://ministry.example.test",
    "MINISTRY_NIN_API_USERNAME": "synthetic-browser-user",
    "MINISTRY_NIN_API_PASSWORD": CONFIGURED_B,
    "MINISTRY_NIN_API_AUTH_TOKEN_PATH": "token",
    "MINISTRY_NIN_API_AUTH_EXPIRY_PATH": "",
    "MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT": "",
    "NIN_DIAGNOSTICS_MAX_PER_OPERATOR": 100,
    "NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT": 200,
}


def _json(status, document):
    from apps.people.nin_provider import HttpResponse

    return HttpResponse(status=status, body=json.dumps(document).encode())


class ScriptedTransport:
    """Stands in for `HttpsTransport`: answers from scripts, records calls."""

    lock = threading.Lock()
    calls: list = []
    auth: list = []
    lookup: list = []
    delay = 0.0

    def __init__(self, config):
        self.config = config

    def request(self, method, path, *, headers, body):
        with ScriptedTransport.lock:
            ScriptedTransport.calls.append((method, path))
            script = ScriptedTransport.auth if method == "POST" else ScriptedTransport.lookup
            answer = script.pop(0) if len(script) > 1 else script[0]
        if ScriptedTransport.delay:
            time.sleep(ScriptedTransport.delay)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def scripted(monkeypatch, settings):
    from apps.people import nin_diagnostics, nin_provider

    for name, value in OFFICIAL.items():
        setattr(settings, name, value)
    ScriptedTransport.calls = []
    ScriptedTransport.auth = [_json(200, {"token": ISSUED})]
    ScriptedTransport.lookup = [_json(200, {"identite": None})]
    ScriptedTransport.delay = 0.0
    monkeypatch.setattr(nin_provider, "HttpsTransport", ScriptedTransport)
    nin_provider.clear_token_cache()
    nin_diagnostics.reset_limiters()
    yield ScriptedTransport
    nin_provider.clear_token_cache()
    nin_diagnostics.reset_limiters()


def _operator():
    from django.contrib.auth.models import Group

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
    from apps.reviews.tests.conftest import TEST_OPERATIONAL_PASSWORD

    def create():
        user = OperationalUser.objects.create_user(
            email=EMAIL, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
        )
        ScopedGroupMembership.objects.create(
            user=user,
            group=Group.objects.get(name="Integration Diagnostics Operators"),
            granted_by=user,
        )

    database_call(create)
    return TEST_OPERATIONAL_PASSWORD


def _sign_in(page, live_server, language="en"):
    choice = _operator()
    page.context.add_cookies(
        [
            {
                "name": django_settings.LANGUAGE_COOKIE_NAME,
                "value": language,
                "url": live_server.url,
            }
        ]
    )
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", EMAIL)
    page.fill("#id_password", choice)
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    page.goto(f"{live_server.url}{PAGE}")
    page.wait_for_load_state("networkidle")


def _result(page, outcome):
    result = page.locator(f"#diagnostic-result[data-diagnostic-outcome='{outcome}']")
    expect(result).to_be_visible(timeout=15_000)
    return result


def _shot(page, name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _no_overflow(page):
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


def _never_shown(page):
    html = page.content()
    for secret in (TEST_NIN, ISSUED, CONFIGURED_B, "ministry.example.test", "DIAGNOSTIQUE"):
        assert secret not in html, secret


def test_each_check_has_its_own_action_and_result(live_server, page, scripted):
    page.set_viewport_size({"width": 1280, "height": 900})
    _sign_in(page, live_server)
    # Opening the page sends nothing; the three checks are distinct.
    assert scripted.calls == []
    for section in ("configuration", "authentication", "lookup"):
        expect(page.locator(f"[data-diagnostic-section='{section}']")).to_be_visible()
    expect(page.locator("[data-readiness='ready']")).to_be_visible()
    _shot(page, "en-desktop-1-configuration")

    page.get_by_role("button", name="Test authentication").click()
    result = _result(page, "AUTH_OK")
    expect(result).to_have_attribute("role", "status")
    expect(result).to_contain_text("Authentication succeeded")
    expect(result).to_contain_text("200")
    assert scripted.calls == [("POST", "/api/auth/")]
    _never_shown(page)
    _shot(page, "en-desktop-2-authentication-success")

    scripted.lookup = [
        _json(
            200,
            {
                "identite": {
                    "nin": TEST_NIN,
                    "nom_f": "DIAGNOSTIQUE",
                    "pren_f": "NAVIGATEUR",
                    "d_nais": "01/01/1990",
                    "presume": "False",
                }
            },
        )
    ]
    field = page.get_by_label("Authorized test identity number (NIN)")
    field.fill(TEST_NIN)
    field.press("Enter")  # keyboard submission
    result = _result(page, "IDENTITY_FOUND")
    expect(result).to_contain_text("identity found")
    expect(page.get_by_label("Authorized test identity number (NIN)")).to_have_value("")
    _never_shown(page)
    _shot(page, "en-desktop-3-lookup-found")

    scripted.lookup = [_json(301, {"location": "elsewhere"})]
    page.get_by_label("Authorized test identity number (NIN)").fill(TEST_NIN)
    page.get_by_role("button", name="Test lookup").click()
    result = _result(page, "REDIRECT_REFUSED")
    expect(result).to_have_attribute("role", "alert")
    expect(result).to_contain_text("301")
    _never_shown(page)
    _shot(page, "en-desktop-4-lookup-redirect-refused")

    page.get_by_label("Authorized test identity number (NIN)").fill("12345")
    page.get_by_role("button", name="Test lookup").click()
    result = _result(page, "INVALID_INPUT")
    expect(result).to_contain_text("18 digits")
    lookups = [call for call in scripted.calls if call[0] == "GET"]
    assert len(lookups) == 2  # the invalid number was never sent
    _shot(page, "en-desktop-5-invalid-number")


def test_a_failed_authentication_is_announced_as_an_alert(live_server, page, scripted):
    scripted.auth = [_json(400, {"error": "synthetic"})]
    page.set_viewport_size({"width": 1280, "height": 900})
    _sign_in(page, live_server)
    page.get_by_role("button", name="Test authentication").click()
    result = _result(page, "AUTH_FAILED")
    expect(result).to_have_attribute("role", "alert")
    expect(result).to_contain_text("400")
    expect(result).to_contain_text("auth_unexpected_status")
    _shot(page, "en-desktop-6-authentication-failed")


def test_a_double_click_sends_one_request_and_shows_the_busy_state(live_server, page, scripted):
    page.set_viewport_size({"width": 1280, "height": 900})
    _sign_in(page, live_server)
    scripted.delay = 1.5
    # Record the form's state just after submission (the page then navigates
    # to the result, so the record is kept in sessionStorage).
    page.evaluate(
        """() => {
          const form = document.querySelector("[data-diagnostic-section='authentication'] form");
          form.addEventListener("submit", () => setTimeout(() => {
            sessionStorage.setItem("ascBusy", JSON.stringify({
              busy: form.getAttribute("aria-busy"),
              disabled: form.querySelector("button[type=submit]").disabled,
            }));
          }, 50));
        }"""
    )
    page.get_by_role("button", name="Test authentication").dblclick()
    _result(page, "AUTH_OK")
    state = json.loads(page.evaluate("() => sessionStorage.getItem('ascBusy')"))
    assert state == {"busy": "true", "disabled": True}
    # The second click of the double click sent nothing.
    assert [call for call in scripted.calls if call[0] == "POST"] == [("POST", "/api/auth/")]


@pytest.mark.parametrize(
    ("language", "width", "height"),
    [("fr", 1280, 900), ("ar", 1280, 900), ("en", 390, 844), ("fr", 390, 844), ("ar", 390, 844)],
)
def test_languages_right_to_left_and_phone_width(
    live_server, page, scripted, language, width, height
):
    page.set_viewport_size({"width": width, "height": height})
    _sign_in(page, live_server, language)
    assert page.evaluate("() => document.documentElement.lang") == language
    assert page.evaluate("() => document.documentElement.dir") == (
        "rtl" if language == "ar" else "ltr"
    )
    assert _no_overflow(page)
    form = page.locator("[data-diagnostic-section='authentication'] form")
    form.locator("button[type=submit]").click()
    _result(page, "AUTH_OK")
    lookup = page.locator("[data-diagnostic-section='lookup'] form")
    lookup.locator("input[name=nin]").fill(TEST_NIN)
    lookup.locator("button[type=submit]").click()
    result = _result(page, "IDENTITY_NOT_FOUND")
    expect(result).to_have_attribute("role", "status")
    assert _no_overflow(page)
    _never_shown(page)
    size = "phone" if width < 600 else "desktop"
    _shot(page, f"{language}-{size}-lookup-no-identity")
