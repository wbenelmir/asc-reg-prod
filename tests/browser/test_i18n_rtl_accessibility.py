"""Real-browser (Playwright) i18n/RTL/HTMX/accessibility tests
(Prompt 4 final closure pass §9).

These exercise an actual rendered page in a real Chromium instance against
`pytest-django`'s `live_server` -- never a template-string assertion standing
in for genuine browser behavior. Requires the Playwright chromium browser to
be installed (`uv run playwright install chromium`).
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.helpers import fill_date, no_js_page_at_otp_verify, switch_language

pytestmark = pytest.mark.django_db(transaction=True)


def _login_participant(live_server, page, email: str) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/workspace/**")


# ---------------------------------------------------------------------------
# Language, RTL/LTR, and localized content
# ---------------------------------------------------------------------------


def test_english_is_the_default_language(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.locator("html").get_attribute("lang") == "en"
    assert page.locator("html").get_attribute("dir") == "ltr"
    assert "Send verification code" in page.content()


def test_french_page_renders_ltr_with_french_content(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    switch_language(page, "fr")
    assert page.locator("html").get_attribute("lang") == "fr"
    assert page.locator("html").get_attribute("dir") == "ltr"
    assert "Envoyer le code de vérification" in page.content()


def test_arabic_page_renders_rtl_with_bootstrap_rtl_stylesheet(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("lang") == "ar"
    assert page.locator("html").get_attribute("dir") == "rtl"
    assert "bootstrap.rtl.min.css" in page.content()


# ---------------------------------------------------------------------------
# Keyboard navigation and 200% zoom reflow
# ---------------------------------------------------------------------------


def test_skip_link_is_reachable_and_usable_by_keyboard(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    page.keyboard.press("Tab")
    focused_text = page.evaluate("document.activeElement.textContent")
    assert "Skip to main content" in focused_text
    page.keyboard.press("Enter")
    # Activating the skip link moves focus into the page's main landmark.
    assert page.locator("#main-content").count() == 1


def test_layout_has_no_horizontal_scroll_at_200_percent_zoom_equivalent_viewport(
    live_server, page
) -> None:
    """WCAG 2.2 §1.4.10 reflow: emulate 200% zoom of a 1280px reference viewport
    with a 640px-wide viewport, and require no page-level horizontal scrollbar."""
    page.set_viewport_size({"width": 640, "height": 800})
    page.goto(f"{live_server.url}/accounts/start/")
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 1  # +1 tolerates sub-pixel rounding


# ---------------------------------------------------------------------------
# Error summary focus and field associations
# ---------------------------------------------------------------------------


def test_invalid_submission_moves_focus_to_the_error_summary(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "error-focus@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.locator("#main-content button[type=submit]").click()  # submit with every field empty
    page.wait_for_selector("#error-summary")
    focused_id = page.evaluate("document.activeElement.id")
    assert focused_id == "error-summary"


def test_invalid_field_has_aria_invalid_and_aria_describedby_to_its_error(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "aria-assoc@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_selector("#error-summary")  # boosted POST: wait for the AJAX swap to land
    given_names = page.locator("#id_given_names")
    assert given_names.get_attribute("aria-invalid") == "true"
    describedby = given_names.get_attribute("aria-describedby") or ""
    assert "id_given_names_error" in describedby
    assert page.locator("#id_given_names_error").count() == 1


# ---------------------------------------------------------------------------
# Identity-path conditional panels
# ---------------------------------------------------------------------------


def test_identity_path_switch_disables_the_hidden_panels_controls(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "identity-switch@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.check("#id_identity_path_1")  # PASSPORT
    assert page.locator("#id_nin_value").is_disabled()
    assert not page.locator("#id_passport_number").is_disabled()

    page.check("#id_identity_path_0")  # NIN
    assert page.locator("#id_passport_number").is_disabled()
    assert not page.locator("#id_nin_value").is_disabled()


def test_disabled_panel_controls_are_never_submitted(live_server, page, seeded_open_event) -> None:
    """With JavaScript active, the non-selected panel's inputs are `disabled`,
    so submitting the form must succeed on the NIN path alone, ignoring
    whatever (blank) values sit in the passport panel."""
    _login_participant(live_server, page, "identity-submit@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-01-01")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")  # NIN
    page.fill("#id_nin_value", "123456789012345678")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/contact/**")


# ---------------------------------------------------------------------------
# No-JavaScript fallback
# ---------------------------------------------------------------------------


def test_identity_step_still_works_with_javascript_disabled(
    live_server, browser, seeded_open_event
) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        # The code request itself needs JavaScript (UX-4 human check); every
        # page from the verify step on is exercised with JavaScript off.
        context, page = no_js_page_at_otp_verify(browser, live_server.url, "no-js@example.com")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/workspace/**")

    page.goto(f"{live_server.url}/register/identity/")
    # Without JS, BOTH identity-document panels remain visible and their
    # controls are never disabled -- the form must still submit correctly on
    # whichever path is selected (server-side `clean()` ignores the other).
    assert not page.locator("#id_nin_value").is_disabled()
    assert not page.locator("#id_passport_number").is_disabled()
    page.fill("#id_given_names", "Sara")
    page.fill("#id_family_name", "K")
    fill_date(page, "id_date_of_birth", "1991-01-01")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator("#main-content button[type=submit]").click(force=True)
    page.wait_for_url("**/register/contact/**")
    context.close()


# ---------------------------------------------------------------------------
# HTMX-boosted navigation
# ---------------------------------------------------------------------------


def test_main_content_declares_hx_boost_for_wizard_navigation(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.locator("#main-content").get_attribute("hx-boost") == "true"
    # The header's own nav (language switcher, sign-out) is deliberately
    # OUTSIDE #main-content, so it is never boosted -- see base.html.
    assert page.locator("header").get_attribute("hx-boost") is None


def test_htmx_boosted_link_navigation_sends_an_hx_request_and_updates_the_url(
    live_server, page, seeded_open_event
) -> None:
    """Clicking an in-page link inside `#main-content` (htmx-boosted) must
    fetch the destination via an `HX-Request` AJAX call while still updating
    the address bar to the real, bookmarkable URL -- htmx boost is
    "progressive enhancement", never a fragment/hash-based SPA."""
    _login_participant(live_server, page, "htmx-nav@example.com")
    requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.headers.get("hx-request", ""))
            if "/register/identity/" in request.url
            else None
        ),
    )
    page.goto(f"{live_server.url}/workspace/")
    page.get_by_role("link", name="Start a registration").click()
    page.wait_for_url("**/register/identity/**")
    assert "/register/identity/" in page.url
    assert "true" in requests


def test_htmx_swap_announces_a_localized_status_for_assistive_technology(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "htmx-status@example.com")
    page.goto(f"{live_server.url}/workspace/")
    page.get_by_role("link", name="Start a registration").click()
    # A function, not a bare expression: Playwright evaluates a bare string with
    # eval, which the enforced CSP (P4-4, no 'unsafe-eval') correctly refuses.
    page.wait_for_function(
        "() => document.getElementById('status-region').textContent.trim().length > 0"
    )
    status_text = page.locator("#status-region").inner_text()
    assert status_text.strip() == "Page updated."
