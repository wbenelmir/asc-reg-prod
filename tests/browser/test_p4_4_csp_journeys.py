"""P4-4: the enforced Content Security Policy in a real browser.

Every test in `tests/browser/` that uses the `page` fixture already fails on any
CSP violation (`conftest._no_csp_or_permissions_policy_violation`). This module
adds what the other journeys do not show:

* a positive control: the browser really enforces the policy the server sends;
* the participant journey in EN, FR and AR at phone and desktop sizes: the
  ALTCHA widget and its worker, OTP, boosted navigation, an identity error
  re-render, the wizard progress bar and the public legal page;
* the former inline behaviours: auto-submitting selects (language switcher,
  observability window) and error-summary focus after a boosted swap.

Chromium against `live_server`, synthetic data only. Sizes are viewport
emulation, not real devices. Screenshots go to
`var/test_artifacts/phase4/p4_4/screenshots/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings
from playwright.sync_api import expect

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_call
from tests.browser.helpers import fill_date, switch_language

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "p4_4"
SHOTS = SHOTS / "screenshots"
SUBMIT = "#main-content button[type=submit]"
OTP_SETTINGS = {"OTP_GENERATOR_BACKEND": "apps.accounts.otp.DeterministicTestOtpGenerator"}
SIZES = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1366, "height": 900}}


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _seed_countries() -> None:
    def create():
        from apps.core.models import Country

        for code, name, name_fr, name_ar in (
            ("DZ", "Algeria", "Algérie", "الجزائر"),
            ("FR", "France", "France", "فرنسا"),
        ):
            Country.objects.update_or_create(
                code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
            )

    database_call(create)


def test_the_browser_enforces_the_policy_the_server_sends(live_server, browser) -> None:
    """Positive control, in a context of its own so the suite-wide collector
    does not treat the deliberate violations as a failure."""
    context = browser.new_context()
    page = context.new_page()
    messages: list[str] = []
    page.on("console", lambda message: messages.append(message.text))
    try:
        response = page.goto(f"{live_server.url}/accounts/start/")
        policy = response.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "unsafe" not in policy
        page.evaluate(
            """() => {
              window.__inlineRan = false;
              const script = document.createElement('script');
              script.textContent = 'window.__inlineRan = true;';
              document.body.appendChild(script);
              const probe = document.createElement('div');
              probe.innerHTML = '<img src="x" onerror="window.__handlerRan = true">';
              document.body.appendChild(probe);
              const styled = document.createElement('div');
              styled.id = 'p44-style-probe';
              styled.setAttribute('style', 'color: rgb(1, 2, 3)');
              document.body.appendChild(styled);
            }"""
        )
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.__inlineRan") is False
        assert page.evaluate("() => window.__handlerRan === undefined")
        color = page.evaluate(
            "() => getComputedStyle(document.getElementById('p44-style-probe')).color"
        )
        assert color != "rgb(1, 2, 3)"
        assert any("Content Security Policy" in text for text in messages), messages
    finally:
        context.close()


@pytest.mark.parametrize("size", ["phone", "desktop"])
@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_participant_journey_runs_under_the_policy(
    live_server, page, seeded_open_event, seeded_legal_notices, language, size
) -> None:
    _seed_countries()
    page.set_viewport_size(SIZES[size])
    response = page.goto(f"{live_server.url}/accounts/start/")
    assert "content-security-policy" in response.headers
    if language != "en":
        switch_language(page, language)  # a `data-auto-submit` select
    assert page.locator("html").get_attribute("lang") == language
    # The ALTCHA widget and its same-origin worker solve the check under the policy.
    with override_settings(**OTP_SETTINGS):
        page.fill("#id_email", f"p44-{language}-{size}@example.com")
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")
    page.goto(f"{live_server.url}/register/identity/")
    # The wizard progress bar is an SVG with a width attribute, drawn from the
    # start edge (right in Arabic).
    bar = page.locator("svg.asc-stepper-bar")
    expect(bar).to_be_visible()
    rect = bar.locator("rect")
    width = int(rect.get_attribute("width"))
    assert 0 < width < 100
    bar_box = bar.bounding_box()
    rect_box = rect.bounding_box()
    assert rect_box["width"] == pytest.approx(bar_box["width"] * width / 100, abs=2)
    if language == "ar":
        assert rect_box["x"] + rect_box["width"] == pytest.approx(
            bar_box["x"] + bar_box["width"], abs=2
        )
    else:
        assert rect_box["x"] == pytest.approx(bar_box["x"], abs=2)
    # A boosted error re-render keeps focus on the new error summary.
    page.check("#id_identity_path_0")
    page.fill("#id_given_names", "Amina")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.fill("#id_nin_value", "12345")
    page.evaluate("() => { window.__sameDocument = true; }")
    page.locator(SUBMIT).click()
    page.wait_for_selector("#error-summary")
    assert page.evaluate("() => window.__sameDocument === true")  # boosted, not reloaded
    expect(page.locator("#error-summary")).to_be_focused()
    expect(page.locator("#status-region")).not_to_have_text("")
    _shot(page, f"identity-error-under-csp-{language}-{size}")
    page.goto(f"{live_server.url}/legal/")
    expect(page.locator(".asc-legal-text")).to_have_count(2)
    _shot(page, f"legal-under-csp-{language}-{size}")


def test_the_observability_window_select_submits_without_an_inline_handler(
    live_server, page, entry_world
) -> None:
    from apps.accounts.models import OperationalUser
    from tests.browser.test_badge_stock import _sign_in_operational

    admin = database_call(
        lambda: OperationalUser.objects.get(email_normalized="device.admin@example.test")
    )
    _sign_in_operational(page, live_server, admin)
    event = entry_world["event"]
    page.goto(f"{live_server.url}/ops/entry/events/{event.pk}/observability/")
    select = page.locator("select#window")
    assert select.get_attribute("onchange") is None
    options = select.locator("option").evaluate_all("els => els.map(e => e.value)")
    target = options[-1]
    with page.expect_navigation(wait_until="load"):
        select.select_option(target)
    assert f"window={target}" in page.url
    assert page.locator("select#window").input_value() == target
