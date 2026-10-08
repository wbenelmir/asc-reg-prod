"""Participant appearance themes (responsive), in a real browser.

The participant pages offer four themes through the Appearance menu beside
the language control (`partials/appearance_switcher.html`,
`static/js/asc-appearance.js`, `static/css/asc-appearance.css`):

* with nothing saved, phones (below 768 px) use Aurora Dark and wider
  screens, tablets included, use Color & Card;
* every theme applies on both layouts, at once, and stays selected across
  participant pages and reloads; each layout keeps its own preference;
* switching never sends a request, navigates, submits, re-renders the form,
  moves focus elsewhere or drops a typed value or a selected file;
* missing, invalid or unreadable storage never breaks a page;
* the menu works with the keyboard and returns focus to its button;
* phones down to 320 px have no horizontal overflow; staff pages and print
  keep the standard presentation.

Screenshots go to `var/test_artifacts/responsive_ui/`. Synthetic data only;
Chromium against `live_server`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import override_settings
from playwright.sync_api import expect

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_call
from tests.browser.helpers import fill_date, select_choice
from tests.browser.test_uxrc1_identity_panels_and_legal import (
    _seed_countries,
    _submit_expecting_errors,
    _submit_to,
)

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "responsive_ui"
SUBMIT = "#main-content button[type=submit]"
OTP_SETTINGS = {"OTP_GENERATOR_BACKEND": "apps.accounts.otp.DeterministicTestOtpGenerator"}
SIZES = {
    "mobile": {"width": 390, "height": 844},
    "desktop": {"width": 1366, "height": 900},
    "tablet": {"width": 820, "height": 1180},
}
THEMES = ("aurora", "fresh", "glass", "color")
DEFAULTS = {"mobile": "glass", "desktop": "color"}
KEYS = {"mobile": "asc2026.appearance.mobile", "desktop": "asc2026.appearance.desktop"}
NAMES = {
    "en": {
        "aurora": "Aurora Dark",
        "fresh": "Fresh Light",
        "glass": "Glass Dark",
        "color": "Color & Card",
    },
    "fr": {
        "aurora": "Aurore sombre",
        "fresh": "Clair frais",
        "glass": "Verre sombre",
        "color": "Couleur et carte",
    },
    "ar": {
        "aurora": "الشفق الداكن",
        "fresh": "فاتح منعش",
        "glass": "زجاجي داكن",
        "color": "ألوان وبطاقة",
    },
}
APPEARANCE = {"en": "Appearance", "fr": "Apparence", "ar": "المظهر"}
EDITION = {"en": "5th edition", "fr": "5e édition", "ar": "الدورة الخامسة"}

STATE = """() => {
  const root = document.documentElement;
  const control = document.querySelector('[data-asc-appearance]');
  const checked = document.querySelector('input[name=asc_appearance]:checked');
  const trigger = document.querySelector('[data-asc-appearance-trigger]');
  let mobile = 'unreadable', desktop = 'unreadable';
  try {
    mobile = localStorage.getItem('asc2026.appearance.mobile');
    desktop = localStorage.getItem('asc2026.appearance.desktop');
  } catch (e) {}
  return {
    theme: root.getAttribute('data-asc-theme'), tone: root.getAttribute('data-asc-tone'),
    layout: root.getAttribute('data-asc-layout'),
    scope: root.getAttribute('data-asc-appearance-scope'),
    control: !!control && !control.hidden, checked: checked ? checked.value : null,
    checkedCount: document.querySelectorAll('input[name=asc_appearance]:checked').length,
    expanded: trigger ? trigger.getAttribute('aria-expanded') : null,
    menuOpen: !!document.querySelector('[data-asc-appearance-menu]:not([hidden])'),
    mobile, desktop,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
    dir: root.dir, identity: (document.querySelector('[data-conference-identity]') || {}).innerText,
  };
}"""

GEOMETRY = """() => {
  const box = (el) => { const r = el.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
            width: r.width, height: r.height}; };
  const trigger = document.querySelector('[data-asc-appearance-trigger]');
  const menu = document.querySelector('[data-asc-appearance-menu]');
  const email = document.querySelector('#id_email');
  const submit = document.querySelector('#main-content button[type=submit]');
  return {
    vw: window.innerWidth, scrollWidth: document.documentElement.scrollWidth,
    trigger: box(trigger), menu: menu.hidden ? null : box(menu),
    options: Array.from(document.querySelectorAll('.asc-appearance-option'))
      .map(o => box(o).height),
    lang: box(document.querySelector('#language-switcher-select')),
    emailFont: email ? parseFloat(getComputedStyle(email).fontSize) : null,
    submit: submit ? box(submit) : null,
  };
}"""


HEADER_ROW = """() => {
  const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
  const brand = rect('.asc-brand');
  const trigger = rect('[data-asc-appearance-trigger]');
  const signOut = rect('.asc-appbar-end form:last-child button');
  return {sameRow: trigger.top < brand.bottom && signOut.top < brand.bottom,
          triggerH: trigger.height};
}"""


def _shot(page, name: str, *, full_page: bool = True) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full_page)


def _state(page) -> dict:
    return page.evaluate(STATE)


def _set_language(page, live_server, language: str) -> None:
    page.context.add_cookies(
        [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url}]
    )


def _fresh_start(page, live_server, size: str, language: str = "en") -> None:
    """The start page with nothing saved on the device."""
    page.set_viewport_size(SIZES[size])
    _set_language(page, live_server, language)
    page.goto(f"{live_server.url}/accounts/start/")
    page.evaluate("() => localStorage.clear()")
    page.reload()
    page.wait_for_load_state("load")


def _choose(page, theme: str) -> None:
    """Open the menu with the pointer and click a theme's row."""
    page.locator("[data-asc-appearance-trigger]").click()
    expect(page.locator("[data-asc-appearance-menu]")).to_be_visible()
    page.locator(f".asc-appearance-option:has(input[value={theme}])").click()
    expect(page.locator("html")).to_have_attribute("data-asc-theme", theme)
    expect(page.locator("[data-asc-appearance-menu]")).to_be_hidden()


def _focused_is_trigger(page) -> bool:
    return page.evaluate("() => document.activeElement.hasAttribute('data-asc-appearance-trigger')")


def _watch_requests(page) -> list:
    seen: list[str] = []
    page.on("request", lambda request: seen.append(f"{request.method} {request.url}"))
    return seen


def _wait_for_human_check(page) -> None:
    page.wait_for_function(
        "() => { const f = document.querySelector('form[data-human-check-state]');"
        " return !f || f.getAttribute('data-human-check-state') === 'solved'; }",
        timeout=20_000,
    )


def _sign_in(page, live_server, email: str) -> None:
    with override_settings(**OTP_SETTINGS):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")


# --------------------------------------------------------------------------
# 1. Defaults, every theme on both layouts, persistence across pages/reloads
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["fr", "ar"])
@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_defaults_and_every_theme_on_the_email_page(live_server, page, size, language) -> None:
    _fresh_start(page, live_server, size, language)
    other = "desktop" if size == "mobile" else "mobile"

    state = _state(page)
    assert state["scope"] == "participant" and state["control"], state
    assert state["layout"] == size and state["theme"] == DEFAULTS[size], state
    assert state["checked"] == DEFAULTS[size] and state["checkedCount"] == 1, state
    assert state[size] is None and state[other] is None, "nothing is saved before a choice"
    assert state["dir"] == ("rtl" if language == "ar" else "ltr")
    assert "2026" in state["identity"] and EDITION[language] in state["identity"], state
    # One logo instance in the header (the panel artwork is a separate illustration).
    assert page.locator("header img[src$='asc-logo.svg']").count() == 1
    trigger = page.get_by_role("button", name=re.compile(APPEARANCE[language]))
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_have_accessible_name(re.compile(re.escape(NAMES[language][DEFAULTS[size]])))

    for theme in THEMES:
        _choose(page, theme)
        state = _state(page)
        assert state["theme"] == theme and state["checked"] == theme, state
        assert state["tone"] == ("dark" if theme in ("aurora", "glass") else "light")
        assert state[size] == theme and state[other] is None, state
        assert state["expanded"] == "false" and not state["menuOpen"], state
        assert _focused_is_trigger(page), "focus returns to the Appearance button"
        assert not state["overflow"], (size, theme)
        # The closed menu is out of the accessibility tree; opened, the
        # labelled radio of the current theme is the checked one.
        page.locator("[data-asc-appearance-trigger]").click()
        expect(page.get_by_role("radio", name=NAMES[language][theme])).to_be_checked()
        expect(page.get_by_role("radio", name=NAMES[language][theme])).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("[data-asc-appearance-menu]")).to_be_hidden()
        page.wait_for_function(
            "() => { const i = document.querySelector('.asc-auth-aside-lockup');"
            " return !i || i.offsetParent === null || i.complete; }"  # hidden panel on phones
        )
        _shot(page, f"email-{size}-{language}-{theme}")

    # Kept across participant pages (the legal page, through its link) and reloads.
    page.locator("[data-legal-link] a").click()
    page.wait_for_url("**/legal/**")
    page.wait_for_load_state("load")
    state = _state(page)
    assert state["theme"] == "color" and state["checked"] == "color" and state["control"], state
    page.reload()
    page.wait_for_load_state("load")
    assert _state(page)["theme"] == "color"
    page.go_back()
    page.wait_for_load_state("load")
    assert _state(page)["theme"] == "color"


# --------------------------------------------------------------------------
# 2. Switching keeps values, files, focus and state; nothing is sent
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_switching_keeps_values_files_and_sends_nothing(
    live_server, page, seeded_open_event, seeded_legal_notices, size, tmp_path
) -> None:
    from apps.documents.tests.factories import make_test_photo

    _seed_countries()
    _fresh_start(page, live_server, size, "en")
    page.evaluate("() => { window.__sameDocument = true; }")

    # Email page: typed address and the automatic human check are untouched.
    page.fill("#id_email", "appearance-switch@example.com")
    _wait_for_human_check(page)
    seen = _watch_requests(page)
    _choose(page, "glass")
    _choose(page, "fresh")
    page.wait_for_timeout(300)
    assert seen == [], seen
    assert page.input_value("#id_email") == "appearance-switch@example.com"
    assert page.evaluate("() => window.__sameDocument === true")
    assert page.get_attribute("form[data-human-check-state]", "data-human-check-state") == "solved"

    # OTP page: a partly typed code survives a switch; the code still works.
    with override_settings(**OTP_SETTINGS):
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.wait_for_load_state("load")
        assert _state(page)["theme"] == "fresh"
        _shot(page, f"otp-{size}-en-fresh-after-switch")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE[:3])
        page.evaluate("() => { window.__sameDocument = true; }")
        seen.clear()
        _choose(page, DEFAULTS[size])
        page.wait_for_timeout(300)
        assert seen == [] and page.evaluate("() => window.__sameDocument === true"), seen
        assert page.input_value("#id_code") == DeterministicTestOtpGenerator.FIXED_VALUE[:3]
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")

    # Identity step: typed fields, the chosen document panel and a selected
    # synthetic file all survive switching (pointer and keyboard).
    page.goto(f"{live_server.url}/register/identity/")
    page.wait_for_load_state("load")
    page.fill("#id_given_names", "Zoé")
    page.fill("#id_family_name", "Martin")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    select_choice(page, "id_nationality_code", "FR")
    page.check("#id_identity_path_1")
    page.fill("#id_passport_number", "ab 1234567")
    select_choice(page, "id_passport_country_code", "FR")
    image = tmp_path / "passport-page.png"
    image.write_bytes(make_test_photo().read())
    page.set_input_files("#id_passport_identity_page", str(image))
    page.evaluate("() => { window.__sameDocument = true; }")
    seen.clear()

    _choose(page, "aurora" if size == "desktop" else "color")
    page.focus("[data-asc-appearance-trigger]")
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)
    assert seen == [], seen
    assert page.evaluate("() => window.__sameDocument === true")
    assert _focused_is_trigger(page)
    assert page.input_value("#id_given_names") == "Zoé"
    assert page.input_value("#id_family_name") == "Martin"
    assert page.input_value("#id_passport_number") == "ab 1234567"
    assert page.is_checked("#id_identity_path_1")
    assert page.evaluate(
        "() => { const f = document.querySelector('#id_passport_identity_page').files;"
        " return f.length === 1 && f[0].name === 'passport-page.png'; }"
    ), "the selected file stays selected"
    # The registration progress is unchanged (still on step 1 of 6).
    assert page.locator(".asc-stepper li").first.get_attribute("aria-current") == "step"
    assert page.locator(".asc-stepper [aria-current=step]").count() == 1

    # Validation, the boosted error re-render and the next step still work.
    _submit_expecting_errors(page)  # no expiry date
    assert page.locator("#error-summary").is_visible()
    assert _state(page)["theme"] == page.evaluate(f"() => localStorage.getItem('{KEYS[size]}')")
    _shot(page, f"registration-{size}-en-validation-after-switch")
    fill_date(page, "id_passport_expires_at", "2032-07-03")
    page.set_input_files("#id_passport_identity_page", str(image))
    _submit_to(page, "**/register/contact/**")


# --------------------------------------------------------------------------
# 3. Keyboard operation and closing behaviour of the menu
# --------------------------------------------------------------------------


def test_the_menu_works_with_the_keyboard_and_returns_focus(live_server, page) -> None:
    _fresh_start(page, live_server, "desktop", "en")
    trigger = page.locator("[data-asc-appearance-trigger]")
    menu = page.locator("[data-asc-appearance-menu]")

    trigger.focus()
    page.keyboard.press("Enter")
    expect(menu).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    assert page.evaluate("() => document.activeElement.value") == "color"
    expect(page.get_by_role("group", name="Appearance")).to_be_visible()

    # Arrow keys change the theme live; the menu stays open.
    page.keyboard.press("ArrowUp")
    expect(page.locator("html")).to_have_attribute("data-asc-theme", "glass")
    expect(menu).to_be_visible()
    expect(page.get_by_role("radio", name="Glass Dark")).to_be_checked()

    # Escape closes and returns focus to the button.
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")
    assert _focused_is_trigger(page)
    assert _state(page)["desktop"] == "glass"

    # Space opens; Space on another option chooses it and closes.
    page.keyboard.press("Space")
    expect(menu).to_be_visible()
    page.keyboard.press("ArrowUp")
    page.keyboard.press("Space")
    expect(menu).to_be_hidden()
    assert _focused_is_trigger(page)
    assert _state(page)["theme"] == "fresh"

    # Tab out of the open menu closes it and moves on (no focus trap).
    page.keyboard.press("Enter")
    expect(menu).to_be_visible()
    page.keyboard.press("Tab")
    expect(menu).to_be_hidden()
    assert page.evaluate("() => document.activeElement.id") == "language-switcher-select"

    # A click outside closes it without choosing anything.
    trigger.click()
    expect(menu).to_be_visible()
    page.mouse.click(5, SIZES["desktop"]["height"] - 5)
    expect(menu).to_be_hidden()
    assert _state(page)["theme"] == "fresh"

    # Clicking the theme already shown closes the menu too.
    trigger.click()
    page.locator(".asc-appearance-option:has(input[value=fresh])").click()
    expect(menu).to_be_hidden()
    assert _focused_is_trigger(page)


# --------------------------------------------------------------------------
# 4. Missing, invalid and unavailable storage
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_invalid_saved_values_fall_back_to_the_defaults(live_server, page, size) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _fresh_start(page, live_server, size, "en")
    page.evaluate(
        """() => { localStorage.setItem('asc2026.appearance.mobile', 'neon');
                   localStorage.setItem('asc2026.appearance.desktop', '"><img src=x>'); }"""
    )
    page.reload()
    page.wait_for_load_state("load")
    state = _state(page)
    assert state["theme"] == DEFAULTS[size] and state["checked"] == DEFAULTS[size], state
    assert page.locator("#id_email").is_visible() and not state["overflow"]
    _choose(page, "glass")
    assert _state(page)[size] == "glass"
    assert errors == [], errors


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_unavailable_storage_never_breaks_the_page(browser, live_server, size) -> None:
    context = browser.new_context(viewport=SIZES[size])
    # Storage access refused (blocked site data, some private modes).
    context.add_init_script(
        """Object.defineProperty(window, 'localStorage', { configurable: true,
             get() { throw new DOMException('blocked', 'SecurityError'); } });"""
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{live_server.url}/accounts/start/")
        page.wait_for_load_state("load")
        state = _state(page)
        assert state["theme"] == DEFAULTS[size] and state["control"], state
        assert state["mobile"] == "unreadable"
        page.fill("#id_email", "no-storage@example.com")
        _choose(page, "glass")  # applies for this page
        assert _state(page)["checked"] == "glass"
        assert page.input_value("#id_email") == "no-storage@example.com"
        page.reload()
        page.wait_for_load_state("load")
        assert _state(page)["theme"] == DEFAULTS[size]
        assert errors == [], errors
    finally:
        context.close()


# --------------------------------------------------------------------------
# 5. Independent layout preferences across repeated resizing
# --------------------------------------------------------------------------


def test_each_layout_keeps_its_own_preference_while_resizing(live_server, page) -> None:
    _fresh_start(page, live_server, "mobile", "en")
    page.fill("#id_email", "resize@example.com")
    _choose(page, "glass")  # mobile
    page.set_viewport_size(SIZES["desktop"])
    expect(page.locator("html")).to_have_attribute("data-asc-layout", "desktop")
    assert _state(page)["theme"] == "color"  # desktop default, untouched by the mobile choice
    _choose(page, "fresh")  # desktop
    expected = {"mobile": "glass", "desktop": "fresh"}

    for _ in range(3):
        for width, height, layout in (
            (375, 812, "mobile"),
            (820, 1180, "desktop"),  # tablet: desktop category
            (767, 900, "mobile"),
            (768, 900, "desktop"),
            (320, 640, "mobile"),
            (1366, 900, "desktop"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            expect(page.locator("html")).to_have_attribute("data-asc-layout", layout)
            state = _state(page)
            assert state["theme"] == expected[layout] and state["checked"] == expected[layout], (
                width,
                state,
            )
            assert state["mobile"] == "glass" and state["desktop"] == "fresh", state
            assert not state["overflow"], (width, state)
    assert page.input_value("#id_email") == "resize@example.com"

    page.set_viewport_size(SIZES["tablet"])
    _shot(page, "email-tablet-en-desktop-preference")


# --------------------------------------------------------------------------
# 6. Phone widths, RTL, touch targets and field text
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["fr", "ar"])
@pytest.mark.parametrize("width", [320, 375, 390])
def test_phone_widths_have_no_overflow_and_usable_controls(
    live_server, page, width, language
) -> None:
    _fresh_start(page, live_server, "mobile", language)
    page.set_viewport_size({"width": width, "height": 800})
    for theme in THEMES:
        _choose(page, theme)
        geometry = page.evaluate(GEOMETRY)
        assert geometry["scrollWidth"] <= geometry["vw"], (theme, geometry)
        assert geometry["trigger"]["height"] >= 44 and geometry["trigger"]["width"] >= 44, geometry
        assert geometry["lang"]["right"] <= geometry["vw"] and geometry["lang"]["left"] >= 0
        assert geometry["emailFont"] >= 16, geometry
        assert geometry["submit"]["height"] >= 44, geometry
        page.locator("[data-asc-appearance-trigger]").click()
        geometry = page.evaluate(GEOMETRY)
        menu = geometry["menu"]
        assert menu and menu["left"] >= 0 and menu["right"] <= geometry["vw"], geometry
        assert all(height >= 44 for height in geometry["options"]), geometry
        assert geometry["scrollWidth"] <= geometry["vw"], geometry
        if theme == "aurora" and width in (320, 375):
            _shot(page, f"email-mobile-{language}-{width}-menu-open", full_page=False)
        page.keyboard.press("Escape")


# --------------------------------------------------------------------------
# 7. Signed-in pages: defaults, RTL, the workspace header
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_signed_in_pages_in_the_default_theme(
    live_server, page, seeded_open_event, seeded_legal_notices, size
) -> None:
    _seed_countries()
    _fresh_start(page, live_server, size, "en")
    page.fill("#id_email", f"appearance-default-{size}@example.com")
    with override_settings(**OTP_SETTINGS):
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.wait_for_load_state("load")
        assert _state(page)["theme"] == DEFAULTS[size]
        _shot(page, f"otp-{size}-en-default")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")

    page.goto(f"{live_server.url}/register/identity/")
    page.wait_for_load_state("load")
    state = _state(page)
    assert state["theme"] == DEFAULTS[size] and state["control"] and not state["overflow"], state
    _shot(page, f"registration-identity-{size}-en-default")
    page.fill("#id_given_names", "Amina")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1991-05-04")
    select_choice(page, "id_nationality_code", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    _submit_to(page, "**/register/contact/**")
    _shot(page, f"registration-contact-{size}-en-default")

    page.locator(".asc-topnav a").first.click()
    page.wait_for_url("**/workspace/**")
    page.wait_for_load_state("load")
    state = _state(page)
    assert state["theme"] == DEFAULTS[size] and not state["overflow"], state
    _shot(page, f"my-registrations-{size}-en-default")

    if size == "desktop":
        # The workspace header stays on one row where it fitted before.
        for language in ("en", "fr", "ar"):
            _set_language(page, live_server, language)
            page.reload()
            page.wait_for_load_state("load")
            rows = page.evaluate(HEADER_ROW)
            assert rows["sameRow"] and rows["triggerH"] >= 44, (language, rows)

    # Arabic (RTL): the workspace and a form page, menu open inside the viewport.
    _set_language(page, live_server, "ar")
    page.goto(f"{live_server.url}/register/contact/")
    page.wait_for_load_state("load")
    state = _state(page)
    assert state["dir"] == "rtl" and state["theme"] == DEFAULTS[size] and not state["overflow"]
    page.locator("[data-asc-appearance-trigger]").click()
    geometry = page.evaluate(GEOMETRY)
    assert geometry["menu"]["left"] >= 0 and geometry["menu"]["right"] <= geometry["vw"], geometry
    _shot(page, f"registration-contact-{size}-ar-default-menu-open", full_page=False)
    page.keyboard.press("Escape")
    page.goto(f"{live_server.url}/workspace/")
    page.wait_for_load_state("load")
    assert not _state(page)["overflow"]
    _shot(page, f"my-registrations-{size}-ar-default")


# --------------------------------------------------------------------------
# 8. Staff pages and print keep the standard presentation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_staff_pages_keep_the_standard_presentation(live_server, page, size) -> None:
    _fresh_start(page, live_server, size, "en")
    _choose(page, "glass")
    for path in ("/accounts/ops/sign-in/", "/accounts/setup/browser-check-0000000000/"):
        page.goto(f"{live_server.url}{path}")
        page.wait_for_load_state("load")
        state = _state(page)
        assert state["scope"] is None and state["theme"] is None and state["tone"] is None, state
        assert page.locator("[data-asc-appearance]").count() == 0
        canvas = page.evaluate(
            "() => getComputedStyle(document.body).getPropertyValue('--asc-canvas').trim()"
        )
        assert canvas == "#f5f7fa", canvas


def test_the_entry_pass_keeps_its_qr_and_print_presentation(live_server, page) -> None:
    from tests.browser.test_digital_entry_pass import _build_active_pass, _sign_in_participant

    person, credential = _build_active_pass()
    series_public_id = database_call(lambda: credential.series.public_id)
    _sign_in_participant(page, live_server, person)
    page.set_viewport_size(SIZES["mobile"])
    page.goto(f"{live_server.url}/my-passes/")
    page.wait_for_load_state("load")
    assert _state(page)["theme"] == DEFAULTS["mobile"]
    page.wait_for_function(
        "() => { const i = document.querySelector('img[data-pass-qr]');"
        " return i && i.complete && i.naturalWidth > 0; }"
    )
    qr_background = page.locator("img[data-pass-qr]").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    )
    assert qr_background == "rgb(255, 255, 255)", "the QR code stays on white"
    _shot(page, "entry-pass-mobile-en-default")

    page.goto(f"{live_server.url}/my-passes/{series_public_id}/print/")
    page.wait_for_load_state("load")
    sheet = page.locator(".pass-print-sheet")
    assert sheet.evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(255, 255, 255)"

    # Print: identical computed styles with and without the theme attributes.
    page.emulate_media(media="print")
    styles = """() => Array.from(document.querySelectorAll(
        'body, .pass-print-sheet, .pass-print-sheet *')).map(n => {
          const s = getComputedStyle(n);
          return [s.color, s.backgroundColor, s.backgroundImage, s.display].join('|'); })"""
    themed = page.evaluate(styles)
    page.evaluate(
        """() => ['data-asc-theme', 'data-asc-tone', 'data-asc-layout', 'data-asc-appearance-scope']
             .forEach(a => document.documentElement.removeAttribute(a))"""
    )
    plain = page.evaluate(styles)
    assert themed == plain, "printing ignores the participant theme"
    page.emulate_media(media="screen")


# --------------------------------------------------------------------------
# 9. Review A01, RUI-01: the header controls stay readable in every state
# --------------------------------------------------------------------------

#: Contrast of the trigger's text on its effective surface: its own
#: (possibly translucent) background over the header's colour, or, on the
#: Color & Card gradient header, over each gradient stop.
TRIGGER_CONTRAST = """(stops) => {
  const parse = (c) => { const m = c.match(/[0-9.]+/g).map(Number);
    return {r: m[0], g: m[1], b: m[2], a: m.length > 3 ? m[3] : 1}; };
  const over = (top, base) => ({r: top.r * top.a + base.r * (1 - top.a),
    g: top.g * top.a + base.g * (1 - top.a), b: top.b * top.a + base.b * (1 - top.a), a: 1});
  const lum = (c) => { const f = (v) => { v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05); };
  const trigger = document.querySelector('[data-asc-appearance-trigger]');
  const style = getComputedStyle(trigger);
  const canvas = parse(getComputedStyle(document.body).backgroundColor);
  const header = parse(getComputedStyle(document.querySelector('.asc-appbar')).backgroundColor);
  const bases = stops.length ? stops.map(parse) : [over(header, canvas)];
  const fg = parse(style.color), bg = parse(style.backgroundColor);
  return {min: Math.min(...bases.map(base => ratio(fg, over(bg, base)))),
          background: style.backgroundColor, color: style.color,
          outline: style.outlineStyle + ' ' + style.outlineWidth};
}"""
GRADIENT_STOPS = {"color": ["rgb(15, 122, 82)", "rgb(10, 111, 134)", "rgb(36, 87, 168)"]}


@pytest.mark.parametrize("size", ["mobile", "desktop"])
def test_the_appearance_trigger_is_readable_in_every_state(live_server, page, size) -> None:
    _fresh_start(page, live_server, size, "fr")
    trigger = page.locator("[data-asc-appearance-trigger]")

    # The reported case: Fresh Light, pointer resting on the trigger, then
    # Glass Dark. Read at once, without waiting for any transition.
    _choose(page, "fresh")
    trigger.hover()
    page.wait_for_timeout(300)
    trigger.click()
    page.locator(".asc-appearance-option:has(input[value=glass])").click()
    measured = page.evaluate(TRIGGER_CONTRAST, [])
    assert measured["min"] >= 4.5, ("glass right after fresh", measured)

    for theme in THEMES:
        _choose(page, theme)
        stops = GRADIENT_STOPS.get(theme, [])
        page.mouse.move(1, SIZES[size]["height"] - 1)
        trigger.blur()
        page.wait_for_timeout(250)
        states = {"normal": page.evaluate(TRIGGER_CONTRAST, stops)}
        trigger.hover()
        page.wait_for_timeout(250)
        states["hover"] = page.evaluate(TRIGGER_CONTRAST, stops)
        page.mouse.down()
        page.wait_for_timeout(250)
        states["active"] = page.evaluate(TRIGGER_CONTRAST, stops)
        page.mouse.up()  # opens the menu
        expect(page.locator("[data-asc-appearance-menu]")).to_be_visible()
        page.wait_for_timeout(250)
        states["open"] = page.evaluate(TRIGGER_CONTRAST, stops)
        page.keyboard.press("Escape")  # focus back on the trigger, focus-visible
        page.mouse.move(1, SIZES[size]["height"] - 1)
        page.wait_for_timeout(250)
        states["focus"] = page.evaluate(TRIGGER_CONTRAST, stops)
        for state, measured in states.items():
            assert measured["min"] >= 4.5, (theme, state, measured)
        assert _focused_is_trigger(page)
        assert states["focus"]["outline"].startswith("solid 3px"), states["focus"]

    # Fresh evidence for the review: Glass Dark with the trigger focused
    # (French desktop, Arabic mobile).
    language = "fr" if size == "desktop" else "ar"
    if language == "ar":
        _set_language(page, live_server, "ar")
        page.reload()
        page.wait_for_load_state("load")
    # Chosen with the keyboard (Fresh Light, then ArrowDown to Glass Dark and
    # Enter), so the returned focus is keyboard focus with its visible ring;
    # after a pointer choice browsers rightly show no focus ring.
    _choose(page, "fresh")
    page.mouse.move(1, SIZES[size]["height"] - 1)
    trigger.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("html")).to_have_attribute("data-asc-theme", "glass")
    page.wait_for_timeout(250)
    assert _focused_is_trigger(page)
    measured = page.evaluate(TRIGGER_CONTRAST, [])
    assert measured["min"] >= 4.5 and measured["outline"].startswith("solid 3px"), measured
    _shot(page, f"email-{size}-{language}-glass-trigger-focused", full_page=False)
