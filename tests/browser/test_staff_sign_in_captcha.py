"""Staff sign-in security image in a real browser (`apps.accounts.captcha_guard`,
`static/js/asc-captcha.js`).

* "New image" replaces the challenge in place: the boosted page navigation
  must not take the click, the typed email stays, the answer is cleared and
  focused, the change is announced, and the new image is the one that works.
* A wrong answer shows a new image, keeps the email and links the error.
* Without JavaScript "New image" reloads the page with a new image.
* Arabic at phone width: right-to-left, nothing overflows horizontally.

Chromium against `live_server`, synthetic data only; answers are read from the
isolated test database (a real person reads the image). Screenshots go to
`var/test_artifacts/asc2026_update/staff_captcha/`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import solve_staff_captcha

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "asc2026_update"
SHOTS = SHOTS / "staff_captcha"
SIGN_IN = "/accounts/ops/sign-in/"
EMAIL = "browser-captcha@example.com"
KEY = "input[name=captcha_key]"
SUBMIT = "#main-content button[type=submit]"


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _staff() -> None:
    from apps.accounts.models import OperationalUser, OperationalUserStatus
    from apps.reviews.tests.conftest import TEST_OPERATIONAL_PASSWORD

    database_call(
        lambda: OperationalUser.objects.create_user(
            email=EMAIL, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
        )
    )


def _password() -> str:
    from apps.reviews.tests.conftest import TEST_OPERATIONAL_PASSWORD

    return TEST_OPERATIONAL_PASSWORD


def _image_loaded(page) -> bool:
    return page.evaluate(
        "() => { const i = document.querySelector('[data-captcha-image]');"
        " return i.complete && i.naturalWidth === 180; }"
    )


def test_new_image_replaces_the_challenge_in_place(live_server, page) -> None:
    _staff()
    page.goto(f"{live_server.url}{SIGN_IN}")
    page.evaluate("() => { window.__samePage = true; }")
    page.fill("#id_email", EMAIL)
    first = page.locator(KEY).input_value()

    page.locator("[data-captcha-refresh]").click()

    expect(page.locator(KEY)).not_to_have_value(first)
    assert page.evaluate("() => window.__samePage === true")  # no navigation, no swap
    expect(page.locator("#id_email")).to_have_value(EMAIL)
    expect(page.locator("#id_captcha_answer")).to_be_focused()
    expect(page.locator("[data-captcha-status]")).to_have_text(
        "New security image loaded. Type its characters."
    )
    second = page.locator(KEY).input_value()
    assert second in page.locator("[data-captcha-image]").get_attribute("src")
    page.wait_for_function(
        "() => { const i = document.querySelector('[data-captcha-image]');"
        " return i.complete && i.naturalWidth > 0; }"
    )
    assert _image_loaded(page)
    _shot(page, "desktop-en-after-new-image")

    page.fill("#id_password", _password())
    solve_staff_captcha(page)
    page.locator(SUBMIT).click()
    page.wait_for_load_state("networkidle")
    expect(page).not_to_have_url(re.compile(re.escape(SIGN_IN) + "$"))


def test_a_wrong_answer_shows_a_new_image_and_keeps_the_email(live_server, page) -> None:
    _staff()
    page.goto(f"{live_server.url}{SIGN_IN}")
    first = page.locator(KEY).input_value()
    page.fill("#id_email", EMAIL)
    page.fill("#id_password", _password())
    solve_staff_captcha(page)
    answer = page.locator("#id_captcha_answer").input_value()
    page.fill("#id_captcha_answer", answer + "9")  # one character too many: never valid
    page.locator(SUBMIT).click()
    page.wait_for_load_state("networkidle")

    expect(page).to_have_url(re.compile(re.escape(SIGN_IN)))
    expect(page.locator(KEY)).not_to_have_value(first)
    expect(page.locator("#id_email")).to_have_value(EMAIL)
    expect(page.locator("#id_captcha_answer")).to_have_value("")
    expect(page.locator("#id_captcha_answer")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#id_captcha_answer_error")).to_be_visible()
    _shot(page, "desktop-en-wrong-answer")

    page.fill("#id_password", _password())
    solve_staff_captcha(page)
    page.locator(SUBMIT).click()
    page.wait_for_load_state("networkidle")
    expect(page).not_to_have_url(re.compile(re.escape(SIGN_IN) + "$"))


def test_without_javascript_new_image_reloads_the_page(live_server, browser) -> None:
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{SIGN_IN}")
        first = page.locator(KEY).input_value()
        page.locator("[data-captcha-refresh]").click()
        page.wait_for_load_state("load")
        assert page.locator(KEY).input_value() != first
        assert _image_loaded(page)
    finally:
        context.close()


def test_arabic_at_phone_width_is_right_to_left_without_overflow(live_server, browser) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844})
    context.add_cookies([{"name": "django_language", "value": "ar", "url": live_server.url}])
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{SIGN_IN}")
        expect(page.locator("html")).to_have_attribute("dir", "rtl")
        page.wait_for_function(
            "() => { const i = document.querySelector('[data-captcha-image]');"
            " return i.complete && i.naturalWidth > 0; }"
        )
        expect(page.locator("[data-captcha-image]")).to_be_visible()
        expect(page.locator("[data-captcha-refresh]")).to_be_visible()
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0
        _shot(page, "phone-ar")
    finally:
        context.close()
