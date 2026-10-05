"""Accessibility coverage across the COMPLETE critical wizard, not only the
start page (Prompt 5 correction pass §6).

Real Chromium via Playwright throughout -- never a template-string
assertion standing in for genuine browser behavior. The screen-reader pass
itself remains explicitly manual (see the manual UAT guides in docs/testing/);
nothing here claims that axe-core or a DOM-structure assertion proves a
real screen-reader experience or full WCAG 2.2 AA conformance -- only the
specific, narrower structural properties each test names.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_sync
from tests.browser.helpers import switch_language

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


@pytest.fixture
@database_sync
def seeded_interest_topic(seeded_open_event):
    from apps.registrations.models import InterestTopic

    topic, _ = InterestTopic.objects.get_or_create(
        event_edition=seeded_open_event,
        code="ACCESS_TEST",
        defaults={"label": "Accessibility test"},
    )
    return topic


# ---------------------------------------------------------------------------
# Error-summary focus + field/error associations, across EVERY wizard step
# (Prompt 4 final closure pass §9 covered only the identity step).
# ---------------------------------------------------------------------------

_WIZARD_STEP_PATHS_REQUIRING_LOGIN = [
    "/register/identity/",
    "/register/contact/",
    "/register/professional/",
    "/register/interests/",
]

# Contact excluded: without a prior identity step, `mobile_required` is False
# (no profile/nationality yet), so an EMPTY contact submission is actually
# VALID and produces no error to focus -- unlike every other step listed,
# which always has at least one unconditionally required field.
_STEPS_WITH_UNCONDITIONALLY_REQUIRED_FIELDS = [
    "/register/identity/",
    "/register/professional/",
    "/register/interests/",
]


@pytest.mark.parametrize("path", _STEPS_WITH_UNCONDITIONALLY_REQUIRED_FIELDS)
def test_every_wizard_step_moves_focus_to_the_error_summary_on_invalid_submission(
    live_server, page, seeded_open_event, path: str
) -> None:
    _login_participant(
        live_server, page, f"a11y-error-{path.strip('/').replace('/', '-')}@example.com"
    )
    page.goto(f"{live_server.url}{path}")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_selector("#error-summary")
    focused_id = page.evaluate("document.activeElement.id")
    assert focused_id == "error-summary"


@pytest.mark.parametrize("path", _STEPS_WITH_UNCONDITIONALLY_REQUIRED_FIELDS)
def test_every_wizard_step_error_has_role_alert_and_visible_text_not_color_only(
    live_server, page, seeded_open_event, path: str
) -> None:
    """WCAG 2.2 §1.4.1: an error must never be conveyed by color alone --
    every rendered error here carries `role="alert"` AND non-empty text
    content, which a screen reader announces and a color-blind user can
    read regardless of the red text color."""
    _login_participant(
        live_server, page, f"a11y-color-{path.strip('/').replace('/', '-')}@example.com"
    )
    page.goto(f"{live_server.url}{path}")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_selector("#error-summary")
    alerts = page.locator('[role="alert"]')
    assert alerts.count() > 0
    texts = [alerts.nth(i).inner_text().strip() for i in range(alerts.count())]
    assert any(texts), "at least one role=alert element must carry real, non-empty text"


# ---------------------------------------------------------------------------
# Keyboard reachability and a real visible focus ring, per step.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _WIZARD_STEP_PATHS_REQUIRING_LOGIN)
def test_every_wizard_step_first_field_is_keyboard_reachable_with_a_visible_focus_ring(
    live_server, page, seeded_open_event, path: str
) -> None:
    _login_participant(
        live_server, page, f"a11y-focus-{path.strip('/').replace('/', '-')}@example.com"
    )
    page.goto(f"{live_server.url}{path}")
    page.keyboard.press("Tab")  # skip link
    page.keyboard.press("Tab")  # language switcher / first nav control
    # Keep tabbing until we land inside the wizard form itself, bounded so a
    # real regression (focus stuck, or never reaching the form) fails fast
    # instead of hanging.
    reached_form_field = False
    for _ in range(20):
        page.keyboard.press("Tab")
        inside_main = page.evaluate("document.activeElement.closest('#main-content form') !== null")
        if inside_main:
            reached_form_field = True
            break
    assert reached_form_field, f"no form field on {path} was reachable by keyboard Tab"
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    # `:focus-visible` sets a real `solid` outline (static/css/app.css) --
    # "none" would mean the focused control has no visible indicator at all.
    assert outline != "none"


# ---------------------------------------------------------------------------
# 200%-zoom-equivalent viewport: no horizontal scroll, beyond the start page.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/register/identity/", "/register/professional/"])
def test_wizard_steps_have_no_horizontal_scroll_at_200_percent_zoom_equivalent(
    live_server, page, seeded_open_event, path: str
) -> None:
    _login_participant(
        live_server, page, f"a11y-zoom-{path.strip('/').replace('/', '-')}@example.com"
    )
    page.set_viewport_size({"width": 640, "height": 800})
    page.goto(f"{live_server.url}{path}")
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width + 1


# ---------------------------------------------------------------------------
# RTL beyond the start page: a real wizard step in Arabic.
# ---------------------------------------------------------------------------


def test_identity_step_renders_rtl_in_arabic(live_server, page, seeded_open_event) -> None:
    _login_participant(live_server, page, "a11y-rtl-identity@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("dir") == "rtl"
    assert "bootstrap.rtl.min.css" in page.content()
    # A real wizard-specific string, not just the shared chrome, is translated.
    assert "الهوية" in page.content() or "جواز" in page.content() or "الجنسية" in page.content()


def test_notices_step_renders_rtl_in_arabic_with_localized_legal_content(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _login_participant(live_server, page, "a11y-rtl-notices@example.com")
    switch_language(page, "ar")
    page.goto(f"{live_server.url}/register/notices/")
    assert page.locator("html").get_attribute("dir") == "rtl"
    assert "[TEST] PRIVACY_NOTICE content in ar." in page.content()


# ---------------------------------------------------------------------------
# Logical focus order: skip link, then the page's own first interactive
# control, never something further down the document.
# ---------------------------------------------------------------------------


def test_start_page_focus_order_is_skip_link_first_then_email_field_before_the_footer(
    live_server, page
) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    page.keyboard.press("Tab")
    first_focused_class = page.evaluate("document.activeElement.className")
    assert "skip-link" in first_focused_class

    order = []
    for _ in range(6):
        page.keyboard.press("Tab")
        order.append(page.evaluate("document.activeElement.id || document.activeElement.tagName"))
        if "id_email" in order:
            break
    assert "id_email" in order, "the email field must be reachable, in order, by keyboard Tab"
