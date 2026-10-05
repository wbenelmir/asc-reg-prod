"""Real-browser checks for UXR-C1.

* UXR-F01: the selected identity-document panel stays visible after validation
  errors (htmx-boosted swaps, a genuine full-page POST, and no JavaScript).
* UXR-F02: the public Privacy Notice and Terms page and its links.
* UXR-F03 / UXR-F04: localized country order shared by the native and the
  enhanced list, the isolated calling-code token, and `dir=auto` free text.

Chromium against the real `live_server`, synthetic data only. Phone and desktop
sizes are viewport emulation, not real devices. Screenshots go to
`var/test_artifacts/phase4/uxr_c1/screenshots/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings
from playwright.sync_api import expect

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_call
from tests.browser.helpers import fill_date, select_choice, switch_language

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "uxr_c1"
SHOTS = SHOTS / "screenshots"
SUBMIT = "#main-content button[type=submit]"
OTP_SETTINGS = {"OTP_GENERATOR_BACKEND": "apps.accounts.otp.DeterministicTestOtpGenerator"}
SIZES = {"phone": {"width": 390, "height": 844}, "desktop": {"width": 1366, "height": 900}}
COUNTRIES = (
    ("DZ", "Algeria", "Algérie", "الجزائر"),
    ("FR", "France", "France", "فرنسا"),
    ("DE", "Germany", "Allemagne", "ألمانيا"),
    ("ZA", "South Africa", "Afrique du Sud", "جنوب أفريقيا"),
    ("AE", "United Arab Emirates", "Émirats arabes unis", "الإمارات العربية المتحدة"),
    ("ES", "Spain", "Espagne", "إسبانيا"),
    ("EG", "Egypt", "Égypte", "مصر"),
    ("TN", "Tunisia", "Tunisie", "تونس"),
    ("MA", "Morocco", "Maroc", "المغرب"),
    ("SN", "Senegal", "Sénégal", "السنغال"),
)

PANELS_JS = """() => {
  const state = {};
  for (const panel of document.querySelectorAll('[data-identity-panel]')) {
    const controls = Array.from(panel.querySelectorAll('input, select, textarea'));
    state[panel.getAttribute('data-identity-panel')] = {
      visible: panel.offsetParent !== null,
      enabled: controls.length > 0 && controls.every(c => !c.disabled),
      disabled: controls.every(c => c.disabled),
    };
  }
  return state;
}"""


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _seed_countries() -> None:
    def create():
        from apps.core.models import Country

        for code, name, name_fr, name_ar in COUNTRIES:
            Country.objects.update_or_create(
                code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
            )

    database_call(create)


def _login(live_server, page, email: str, language: str) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    if language != "en":
        switch_language(page, language)
    with override_settings(**OTP_SETTINGS):
        page.fill("#id_email", email)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")


def _press_submit(page, *, keyboard: bool = False) -> None:
    """A pointer click, or, where scripting is off and the pointer-click
    stability check cannot settle, focus plus Enter (still a real user action)."""
    if keyboard:
        page.focus(SUBMIT)
        page.keyboard.press("Enter")
    else:
        page.locator(SUBMIT).click()


def _submit_expecting_errors(page, *, keyboard: bool = False) -> None:
    """Submit and wait for the NEW error page: the summary already on screen
    is marked first, so the wait cannot return before the response replaced it
    (boosted swap or full page load)."""
    page.evaluate(
        "() => { const s = document.getElementById('error-summary');"
        " if (s) s.setAttribute('data-stale', 'true'); }"
    )
    _press_submit(page, keyboard=keyboard)
    page.wait_for_selector("#error-summary:not([data-stale])")


def _submit_to(page, url_glob: str, *, keyboard: bool = False) -> None:
    """Submit and wait for the next step; on failure, report the page's own
    error summary instead of a bare timeout."""
    _press_submit(page, keyboard=keyboard)
    try:
        page.wait_for_url(url_glob, timeout=15000)
    except Exception as error:  # noqa: BLE001 - re-raised with the page's errors
        summary = page.locator("#error-summary")
        detail = (
            summary.evaluate(r"el => el.textContent.replace(/\s+/g, ' ')")
            if summary.count()
            else "(no error summary)"
        )
        raise AssertionError(f"not submitted: {detail}") from error


def _panels(page) -> dict:
    return page.evaluate(PANELS_JS)


def _assert_only(page, active: str) -> None:
    other = "PASSPORT" if active == "NIN" else "NIN"
    state = _panels(page)
    assert state[active]["visible"] and state[active]["enabled"], state
    assert not state[other]["visible"] and state[other]["disabled"], state


def _focus_from_summary(page, field_id: str) -> None:
    link = page.locator(f"#error-summary a[href='#{field_id}']")
    expect(link).to_have_count(1)
    link.click()
    assert page.evaluate("() => document.activeElement.id") == field_id
    assert page.locator(f"#{field_id}").is_visible()


@pytest.mark.parametrize("size", ["phone", "desktop"])
@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_nin_panel_survives_errors_switching_and_a_corrected_resubmission(
    live_server, page, seeded_open_event, seeded_legal_notices, language, size
) -> None:
    _seed_countries()
    page.set_viewport_size(SIZES[size])
    _login(live_server, page, f"uxrc1-nin-{language}-{size}@example.com", language)
    page.goto(f"{live_server.url}/register/identity/")
    page.check("#id_identity_path_0")
    _assert_only(page, "NIN")
    page.fill("#id_given_names", "أمينة")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.fill("#id_nin_value", "12345")
    page.evaluate("() => { window.__sameDocument = true; }")
    _submit_expecting_errors(page)
    # A boosted swap (same document), not a reload.
    assert page.evaluate("() => window.__sameDocument === true")
    _assert_only(page, "NIN")
    assert page.input_value("#id_nin_value") == "12345"  # entered value preserved
    _focus_from_summary(page, "id_nin_value")
    _shot(page, f"identity-nin-error-{language}-{size}")
    # The same errors again: still the NIN panel.
    _submit_expecting_errors(page)
    _assert_only(page, "NIN")
    # Switching path and back after an error re-render.
    page.check("#id_identity_path_1")
    _assert_only(page, "PASSPORT")
    page.check("#id_identity_path_0")
    _assert_only(page, "NIN")
    # Corrected resubmission.
    page.fill("#id_given_names", "Amina")
    page.fill("#id_nin_value", "0012 3456 7890 1234 56")
    _submit_to(page, "**/register/contact/**")

    def stored():
        from apps.people.models import IdentityIdentifier

        return list(IdentityIdentifier.objects.values_list("identifier_type", flat=True))

    assert database_call(stored) == ["NIN"]


@pytest.mark.parametrize(("language", "size"), [("en", "desktop"), ("ar", "phone")])
def test_the_passport_panel_survives_errors(
    live_server, page, seeded_open_event, seeded_legal_notices, language, size, tmp_path
) -> None:
    from apps.documents.tests.factories import make_test_photo

    _seed_countries()
    page.set_viewport_size(SIZES[size])
    _login(live_server, page, f"uxrc1-passport-{language}-{size}@example.com", language)
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Zoé")
    page.fill("#id_family_name", "Martin")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    select_choice(page, "id_nationality_code", "FR")
    page.check("#id_identity_path_1")
    _assert_only(page, "PASSPORT")
    page.fill("#id_passport_number", "ab 1234567")
    select_choice(page, "id_passport_country_code", "FR")
    _submit_expecting_errors(page)  # no expiry date
    _assert_only(page, "PASSPORT")
    assert page.input_value("#id_passport_number") == "ab 1234567"
    _focus_from_summary(page, "id_passport_expires_at_day")
    _shot(page, f"identity-passport-error-{language}-{size}")
    _submit_expecting_errors(page)  # repeated
    _assert_only(page, "PASSPORT")
    fill_date(page, "id_passport_expires_at", "2032-07-03")
    # IDV-08 (A13-02): the passport identity page is required on this path; a
    # browser never keeps a chosen file across an error re-render.
    image = tmp_path / "passport-page.png"
    image.write_bytes(make_test_photo().read())
    page.set_input_files("#id_passport_identity_page", str(image))
    _submit_to(page, "**/register/contact/**")


def test_a_genuine_full_page_post_keeps_the_selected_panel(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    """htmx is not loaded, so the form is submitted by the browser itself and
    the error page is a new document (the window marker is gone)."""
    _seed_countries()
    _login(live_server, page, "uxrc1-fullpost@example.com", "en")
    page.route("**/vendor/htmx/htmx.min.js", lambda route: route.abort())
    page.goto(f"{live_server.url}/register/identity/")
    assert page.evaluate("() => typeof window.htmx") == "undefined"
    page.check("#id_identity_path_0")
    page.fill("#id_given_names", "أمينة")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.fill("#id_nin_value", "12345")
    page.evaluate("() => { window.__sameDocument = true; }")
    with page.expect_navigation(wait_until="load"):
        page.locator(SUBMIT).click()
    assert page.evaluate("() => window.__sameDocument === undefined")  # a new document
    page.wait_for_selector("#error-summary")
    _assert_only(page, "NIN")
    _focus_from_summary(page, "id_nin_value")
    _shot(page, "identity-full-page-post-error-en-desktop")


def test_without_javascript_both_panels_stay_usable(
    live_server, browser, seeded_open_event, seeded_legal_notices
) -> None:
    from tests.browser.helpers import no_js_page_at_otp_verify

    _seed_countries()
    with override_settings(**OTP_SETTINGS):
        context, page = no_js_page_at_otp_verify(browser, live_server.url, "uxrc1-nojs@example.com")
        try:
            page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
            page.locator(SUBMIT).click()
            page.wait_for_url("**/workspace/**")
            page.goto(f"{live_server.url}/register/identity/")
            state = _panels(page)
            assert state["NIN"]["visible"] and state["PASSPORT"]["visible"]
            page.check("#id_identity_path_0")
            page.fill("#id_given_names", "أمينة")
            page.fill("#id_family_name", "Benali")
            fill_date(page, "id_date_of_birth", "1990-03-07")
            page.fill("#id_nin_value", "12345")
            _submit_expecting_errors(page, keyboard=True)
            state = _panels(page)
            assert state["NIN"]["visible"] and state["NIN"]["enabled"]
            assert state["PASSPORT"]["visible"]  # the no-JavaScript fallback
            _shot(page, "identity-no-javascript-error-en-desktop")
            page.fill("#id_given_names", "Amina")
            page.fill("#id_nin_value", "123456789012345678")
            _submit_to(page, "**/register/contact/**", keyboard=True)
        finally:
            context.close()


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_public_legal_page_is_linked_and_readable(
    live_server, page, seeded_legal_notices, language
) -> None:
    page.set_viewport_size(SIZES["phone"])
    page.goto(f"{live_server.url}/accounts/start/")
    if language != "en":
        switch_language(page, language)
    # A-10 (P4-4, FOOTER-01 option (a)): the footer copy of the link is removed.
    expect(page.locator("footer [data-footer-legal]")).to_have_count(0)
    with page.expect_navigation(wait_until="load"):
        page.locator("[data-legal-link] a").click()
    assert page.url.endswith("/legal/")
    assert page.locator("html").get_attribute("lang") == language
    texts = page.locator(".asc-legal-text")
    expect(texts).to_have_count(2)
    assert f"[TEST] PRIVACY_NOTICE content in {language}." in texts.first.inner_text()
    assert page.locator("[data-legal-fallback], [data-legal-unavailable]").count() == 0
    assert page.locator("meta[name=robots]").get_attribute("content") == "noindex, nofollow"
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    response = page.request.get(f"{live_server.url}/legal/")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert response.headers["cache-control"] == "private, no-store"
    _shot(page, f"legal-page-{language}-phone")


def test_arabic_country_order_calling_code_and_free_text_direction(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries()
    page.set_viewport_size(SIZES["phone"])
    _login(live_server, page, "uxrc1-bidi@example.com", "ar")
    page.goto(f"{live_server.url}/register/identity/")
    native = page.evaluate(
        "() => Array.from(document.getElementById('id_nationality_code_native').options)"
        ".filter(o => o.value).map(o => o.textContent.trim())"
    )
    assert native[:3] == ["إسبانيا", "الإمارات العربية المتحدة", "الجزائر"]
    box = page.locator("#id_nationality_code")
    box.click()
    page.keyboard.press("ArrowDown")
    listbox = page.locator(f"#{box.get_attribute('aria-controls')}")
    enhanced = [t.strip() for t in listbox.locator("[role=option]").all_inner_texts()]
    assert enhanced == native  # the enhanced list shares the native order
    page.keyboard.press("Escape")
    page.check("#id_identity_path_0")
    page.fill("#id_given_names", "Amina")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.fill("#id_nin_value", "123456789012345678")
    _submit_to(page, "**/register/contact/**")
    page.wait_for_selector("#id_mobile_country_code_native", state="attached")
    label = page.locator("#id_mobile_country_code").input_value()
    assert label == "الجزائر ⁦(+213)⁩"
    page.fill("#id_mobile_number", "+33 6 12 34 56 78")
    expect(page.locator("#id_mobile_country_code")).to_have_value("فرنسا ⁦(+33)⁩")
    # The token is laid out left to right inside the right-to-left label: the
    # "+" is drawn to the left of "33" (the defect drew "(33+)").
    order = page.evaluate(
        """() => { const input = document.getElementById('id_mobile_country_code');
             const span = document.createElement('span');
             span.dir = 'rtl';
             span.textContent = input.value;
             document.body.appendChild(span);
             const text = span.firstChild, value = input.value;
             const at = (i) => { const r = document.createRange(); r.setStart(text, i);
                                 r.setEnd(text, i + 1); return r.getBoundingClientRect().left; };
             const plus = value.indexOf('+'), three = value.indexOf('3');
             const result = {plus: at(plus), three: at(three)};
             span.remove(); return result; }"""
    )
    assert order["plus"] < order["three"]
    page.locator("#id_mobile_country_code").click()
    _shot(page, "contact-calling-code-ar-phone")
    page.keyboard.press("Escape")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/professional/**")
    bio = page.locator("#id_biography")
    assert bio.get_attribute("dir") == "auto"
    bio.fill("Short biography.")
    assert bio.evaluate("el => getComputedStyle(el).direction") == "ltr"
    bio.fill("سيرة قصيرة.")
    assert bio.evaluate("el => getComputedStyle(el).direction") == "rtl"
