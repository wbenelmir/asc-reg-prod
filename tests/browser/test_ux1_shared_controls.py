"""Real-browser (Playwright) checks for the UX-1 shared controls.

Covers M02 (one-time code, digit counter), M04 (Arabic event name), M05
(head, icons), M06 (placeholders), M09 (search-and-select), M10
(Day / Month / Year) and M23 (footer links) in a real Chromium: paste,
autofill, keyboard, RTL order, responsive layout and behaviour with scripting
switched off. Every value is synthetic. Screenshots go to
`var/test_artifacts/phase4/ux_1/screenshots/` (a folder of its own, so no
earlier evidence is overwritten).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core.models import Country
from tests.browser.database import database_call
from tests.browser.helpers import (
    fill_date,
    no_js_page_at_otp_verify,
    read_date,
    switch_language,
)

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "ux_1"
SHOTS = SHOTS / "screenshots"

ARABIC_EVENT_NAME = "المؤتمر الإفريقي للمؤسسات الناشئة ASC"
OTP_SETTINGS = {"OTP_GENERATOR_BACKEND": "apps.accounts.otp.DeterministicTestOtpGenerator"}
SUBMIT = "#main-content button[type=submit]"


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _open_verify_page(live_server, page, email: str) -> None:
    with override_settings(**OTP_SETTINGS):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")


def _login(live_server, page, email: str) -> None:
    _open_verify_page(live_server, page, email)
    page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
    page.locator(SUBMIT).click()
    page.wait_for_url("**/workspace/**")


def _paste(page, selector: str, text: str) -> None:
    page.evaluate(
        """([selector, text]) => {
            const input = document.querySelector(selector);
            input.focus();
            const data = new DataTransfer();
            data.setData("text", text);
            input.dispatchEvent(new ClipboardEvent("paste",
                {clipboardData: data, bubbles: true, cancelable: true}));
        }""",
        [selector, text],
    )


def _seed_countries(count: int = 0) -> None:
    def create() -> None:
        Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
        Country.objects.get_or_create(code="FR", defaults={"name": "France"})
        # Synthetic, user-assigned ISO codes (QM..QZ): never a real country.
        for index in range(count):
            code = "Q" + chr(ord("M") + index)
            Country.objects.get_or_create(code=code, defaults={"name": f"Testland {code}"})

    database_call(create)


def _complete_identity_with_nin(page, live_server, *, date=("07", "03", "1990")) -> None:
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    page.fill("#id_date_of_birth_day", date[0])
    page.fill("#id_date_of_birth_month", date[1])
    page.fill("#id_date_of_birth_year", date[2])
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()


# ---------------------------------------------------------------------------
# M02: the one-time code
# ---------------------------------------------------------------------------


def test_otp_is_one_input_drawn_as_six_cells_and_sanitizes_paste_and_autofill(
    live_server, page
) -> None:
    _open_verify_page(live_server, page, "ux1-otp@example.com")
    code = page.locator("#id_code")
    assert page.locator("#main-content input[type=text]").count() == 1
    assert page.locator("input[name=code]").count() == 1
    assert "asc-otp-input" in (code.get_attribute("class") or "")
    for attribute, expected in (
        ("autocomplete", "one-time-code"),
        ("inputmode", "numeric"),
        ("dir", "ltr"),
        ("pattern", "[0-9]{6}"),
    ):
        assert code.get_attribute(attribute) == expected

    # Six cells of 3rem plus their 0.5rem gaps, and the half-cell of leading
    # padding are about 350px at a 16px root, depending on the monospace font (the box
    # never scrolls a digit away).
    metrics = page.evaluate(
        """() => { const el = document.querySelector('#id_code');
                   el.value = '123456'; el.dispatchEvent(new Event('input', {bubbles: true}));
                   return {width: el.getBoundingClientRect().width,
                           overflow: el.scrollWidth - el.clientWidth, scroll: el.scrollLeft}; }"""
    )
    assert 336 <= metrics["width"] <= 364
    assert metrics["overflow"] <= 1 and metrics["scroll"] == 0
    page.fill("#id_code", "")
    _shot(page, "otp-en-desktop")

    # Paste with a separator, Arabic-Indic digits, an over-long entry, an autofill.
    _paste(page, "#id_code", "123 456")
    assert code.input_value() == "123456"
    code.fill("")
    code.press_sequentially("١٢٣٤٥٦")
    assert code.input_value() == "123456"
    code.fill("")
    code.press_sequentially("12345678")
    assert code.input_value() == "123456"
    code.fill("")
    code.press_sequentially("12a3-4")
    assert code.input_value() == "1234"
    page.evaluate(
        """() => { const el = document.querySelector('#id_code');
                   el.value = '987 654';
                   el.dispatchEvent(new Event('input', {bubbles: true})); }"""
    )
    assert code.input_value() == "987654"

    # Filling all six cells never submits the form on its own (WCAG 3.2.2).
    page.wait_for_timeout(400)
    assert "/verify/" in page.url


def test_otp_cells_keep_their_left_to_right_order_in_arabic_and_on_a_phone(
    live_server, page
) -> None:
    _open_verify_page(live_server, page, "ux1-otp-ar@example.com")
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("dir") == "rtl"
    code = page.locator("#id_code")
    assert page.evaluate("getComputedStyle(document.querySelector('#id_code')).direction") == "ltr"
    code.fill("")
    code.press_sequentially("123456")
    assert code.input_value() == "123456"
    _shot(page, "otp-ar-desktop")
    page.set_viewport_size({"width": 320, "height": 700})
    assert page.evaluate("document.querySelector('#id_code').scrollLeft") == 0
    box = code.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 320
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    assert overflow is False
    _shot(page, "otp-ar-mobile-320")


def test_the_otp_page_works_with_scripting_switched_off(live_server, browser) -> None:
    with override_settings(**OTP_SETTINGS):
        # The code request needs JavaScript since UX-4 (human check); the
        # verify page itself is used with JavaScript off.
        context, page = no_js_page_at_otp_verify(browser, live_server.url, "ux1-nojs@example.com")
        assert "asc-otp-input" not in (page.locator("#id_code").get_attribute("class") or "")
        code = DeterministicTestOtpGenerator.FIXED_VALUE
        page.fill("#id_code", f"{code[:3]} {code[3:]}")
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")
    context.close()


# ---------------------------------------------------------------------------
# M10: Day / Month / Year
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_date_parts_follow_the_page_direction_and_show_a_linkable_error(
    live_server, page, seeded_open_event, seeded_legal_notices, language
) -> None:
    _seed_countries()
    _login(live_server, page, f"ux1-date-{language}@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    if language != "en":
        switch_language(page, language)

    day = page.locator("#id_date_of_birth_day").bounding_box()
    month = page.locator("#id_date_of_birth_month").bounding_box()
    year = page.locator("#id_date_of_birth_year").bounding_box()
    if language == "ar":  # right to left: Day is the rightmost part
        assert day["x"] > month["x"] > year["x"]
    else:
        assert day["x"] < month["x"] < year["x"]
    assert page.locator(".asc-date-group legend").count() >= 1
    for part in ("day", "month", "year"):
        assert page.locator(f'label[for="id_date_of_birth_{part}"]').count() == 1
        assert page.locator(f"#id_date_of_birth_{part}").get_attribute("inputmode") == "numeric"
        assert page.locator(f"#id_date_of_birth_{part}").get_attribute("dir") == "ltr"
    _shot(page, f"identity-date-{language}")

    # An impossible date: the error summary links to the first part and its
    # message is announced; the typed parts come back unchanged.
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    page.fill("#id_date_of_birth_day", "31")
    page.fill("#id_date_of_birth_month", "02")
    page.fill("#id_date_of_birth_year", "1990")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()
    page.wait_for_selector("#error-summary")
    link = page.locator('#error-summary a[href="#id_date_of_birth_day"]')
    assert link.count() == 1
    assert page.locator("#id_date_of_birth_day").get_attribute("aria-invalid") == "true"
    assert page.locator("#id_date_of_birth_day").input_value() == "31"
    assert page.locator("#id_date_of_birth_month").input_value() == "02"
    assert page.locator("#id_date_of_birth_year").input_value() == "1990"
    link.click()
    assert page.evaluate("document.activeElement.id") == "id_date_of_birth_day"
    _shot(page, f"identity-date-error-{language}")


def test_arabic_indic_digits_are_accepted_and_stored_as_the_same_date(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries()
    _login(live_server, page, "ux1-date-digits@example.com")
    _complete_identity_with_nin(page, live_server, date=("٠٧", "٠٣", "١٩٩٠"))
    page.wait_for_url("**/register/contact/**")
    page.goto(f"{live_server.url}/register/identity/")
    assert read_date(page, "id_date_of_birth") == "1990-03-07"


def test_date_parts_work_with_scripting_switched_off(
    live_server, browser, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries()
    with override_settings(**OTP_SETTINGS):
        context, page = no_js_page_at_otp_verify(
            browser, live_server.url, "ux1-date-nojs@example.com"
        )
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/contact/**")
    context.close()


# ---------------------------------------------------------------------------
# M03 (widget only): the digit counter never edits the value
# ---------------------------------------------------------------------------


def test_the_nin_counter_only_counts_and_never_edits_what_was_typed(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries()
    _login(live_server, page, "ux1-nin@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.check("#id_identity_path_0")
    nin = page.locator("#id_nin_value")
    counter = page.locator(".asc-digit-counter")
    assert counter.inner_text().strip() == "0 / 18"
    assert nin.get_attribute("placeholder") == "18 digits"

    nin.fill("")
    nin.press_sequentially("0012 3456")
    assert nin.input_value() == "0012 3456"  # separators and leading zeros untouched
    assert counter.inner_text().strip() == "8 / 18"
    nin.fill("000000000000000001")
    assert nin.input_value() == "000000000000000001"
    assert counter.inner_text().strip() == "18 / 18"
    assert "is-complete" in (counter.get_attribute("class") or "")
    status = page.locator(".asc-digit-counter + [role=status]")
    assert status.inner_text().strip() == "18 of 18 digits entered."
    assert counter.get_attribute("aria-hidden") == "true"  # no per-key announcements
    assert nin.get_attribute("dir") == "ltr"


# ---------------------------------------------------------------------------
# M09: search-and-select
# ---------------------------------------------------------------------------


def test_long_lists_become_a_keyboard_accessible_search_and_select(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries(count=12)
    _login(live_server, page, "ux1-combobox@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    box = page.locator("#id_nationality_code")
    assert box.get_attribute("role") == "combobox"
    assert box.get_attribute("aria-expanded") == "false"
    assert box.get_attribute("aria-autocomplete") == "list"
    native = page.locator("#id_nationality_code_native")
    assert native.get_attribute("name") == "nationality_code"
    assert native.is_hidden()
    # The label follows the control that people reach.
    assert page.locator('label[for="id_nationality_code"]').count() == 1

    # Type-ahead filters; ArrowDown moves; Enter commits to the native select.
    box.click()
    box.press_sequentially("testland")
    listbox = page.locator("#id_nationality_code_listbox")
    assert listbox.is_visible()
    assert box.get_attribute("aria-expanded") == "true"
    assert listbox.locator("[role=option]").count() == 12
    first = box.get_attribute("aria-activedescendant")
    box.press("ArrowDown")
    assert box.get_attribute("aria-activedescendant") != first
    box.press("End")
    last = box.get_attribute("aria-activedescendant")
    box.press("Home")
    assert box.get_attribute("aria-activedescendant") != last
    box.press("Enter")
    chosen = native.input_value()
    assert chosen.startswith("Q")
    assert box.input_value().startswith("Testland")
    assert listbox.is_hidden()
    assert box.get_attribute("aria-expanded") == "false"

    # No match: a localized empty state; Escape restores the selection.
    box.click()
    box.fill("zzzz")
    assert "No matching choices" in listbox.inner_text()
    box.press("Escape")
    assert listbox.is_hidden()
    assert box.input_value().startswith("Testland")
    assert native.input_value() == chosen

    # Mouse selection, 44px targets, clear selection.
    box.click()
    box.fill("algeria")
    option = listbox.locator("[role=option]").first
    assert option.bounding_box()["height"] >= 44
    option.click()
    assert native.input_value() == "DZ"
    assert page.locator("#id_nationality_code_native").evaluate("e => e.value") == "DZ"
    _shot(page, "combobox-en-selected")

    # A chosen value survives a full validation round trip.
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.locator("#id_country_of_residence").click()
    page.locator("#id_country_of_residence").fill("fran")
    page.locator("#id_country_of_residence_listbox [role=option]").first.click()
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/contact/**")


def test_the_combobox_mirrors_direction_and_stays_reachable_by_keyboard_in_arabic(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries(count=12)
    _login(live_server, page, "ux1-combobox-ar@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    switch_language(page, "ar")
    box = page.locator("#id_nationality_code")
    toggle = page.locator("#id_nationality_code ~ .asc-combobox-toggle")
    input_box = box.bounding_box()
    toggle_box = toggle.bounding_box()
    # In RTL the caret sits at the visual left, which is the end of the box.
    assert toggle_box["x"] + toggle_box["width"] / 2 < input_box["x"] + input_box["width"] / 2
    box.focus()
    box.press("ArrowDown")
    assert box.get_attribute("aria-expanded") == "true"
    assert page.locator("#id_nationality_code_listbox [role=option]").count() == 14
    _shot(page, "combobox-ar-open")
    box.press("Escape")
    assert box.get_attribute("aria-expanded") == "false"


def test_a_chosen_country_survives_a_language_switch_in_the_search_box(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries(count=12)
    _login(live_server, page, "ux1-combobox-lang@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    box = page.locator("#id_nationality_code")
    box.click()
    box.fill("testland qn")
    page.locator("#id_nationality_code_listbox [role=option]").first.click()
    assert page.locator("#id_nationality_code_native").input_value() == "QN"
    for language in ("fr", "ar", "en"):
        switch_language(page, language)
        assert page.locator("#id_nationality_code_native").input_value() == "QN"
        assert page.locator("#id_nationality_code").input_value() == "Testland QN"


def test_short_lists_stay_native_selects(
    live_server, page, seeded_open_event, seeded_legal_notices
):
    _seed_countries()  # only two countries: below the threshold
    _login(live_server, page, "ux1-native@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    assert page.locator("select#id_nationality_code").count() == 1
    assert page.locator("#id_nationality_code_listbox").count() == 0


# ---------------------------------------------------------------------------
# M06: placeholders that follow the language, next to visible labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "given", "family", "nin"),
    [
        ("en", "e.g. Amina", "e.g. Benali", "18 digits"),
        ("fr", "ex. Amina", "ex. Benali", "18 chiffres"),
        ("ar", "مثال: Amina", "مثال: Benali", "18 رقمًا"),
    ],
)
def test_placeholders_follow_the_language_and_never_replace_a_label(
    live_server, page, seeded_open_event, seeded_legal_notices, language, given, family, nin
) -> None:
    _seed_countries()
    _login(live_server, page, f"ux1-placeholder-{language}@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    if language != "en":
        switch_language(page, language)
    assert page.locator("#id_given_names").get_attribute("placeholder") == given
    assert page.locator("#id_family_name").get_attribute("placeholder") == family
    assert page.locator("#id_nin_value").get_attribute("placeholder") == nin
    for field in ("given_names", "family_name", "nin_value", "passport_number"):
        assert page.locator(f'label[for="id_{field}"]').count() == 1
    for field in ("nationality_code", "country_of_residence"):
        assert page.locator(f"#id_{field}").get_attribute("placeholder") in (
            None,
            "Type to filter the list",
        )
    assert page.locator("#id_date_of_birth_day").get_attribute("placeholder") is None
    _shot(page, f"identity-placeholders-{language}")


def test_the_phone_placeholder_follows_the_selected_calling_country(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed_countries()
    _login(live_server, page, "ux1-phone@example.com")
    _complete_identity_with_nin(page, live_server)
    page.wait_for_url("**/register/contact/**")
    number = page.locator("#id_mobile_number")
    # UX-2 (S-10): the calling code starts from the country of residence, so
    # the Algerian example is shown as soon as the page opens.
    initial = number.get_attribute("placeholder")
    assert initial and initial.startswith("0")
    page.select_option("#id_mobile_country_code", "FR")
    french = number.get_attribute("placeholder")
    assert french and french.startswith("0")
    page.select_option("#id_mobile_country_code", "DZ")
    algerian = number.get_attribute("placeholder")
    assert algerian and algerian != french
    assert number.get_attribute("dir") == "ltr"
    assert number.get_attribute("inputmode") == "tel"
    assert number.get_attribute("autocomplete") == "tel-national"


# ---------------------------------------------------------------------------
# M04, M05, M23: name, head and footer
# ---------------------------------------------------------------------------


def test_the_public_head_declares_the_icons_the_robots_rule_and_a_localized_title(
    live_server, page
) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.title() == "Start registration · ASC 2026"
    assert page.locator('meta[name="robots"]').get_attribute("content") == "index, follow"
    assert page.locator('meta[name="description"]').count() == 1
    icons = page.evaluate(
        "Array.from(document.querySelectorAll('link[rel~=icon], link[rel=apple-touch-icon]'))"
        ".map(l => l.href)"
    )
    assert len(icons) == 3
    for href in icons:
        response = page.request.get(href)
        assert response.status == 200, href
    assert page.request.get(f"{live_server.url}/robots.txt").text().splitlines()[1] == (
        "Allow: /accounts/start/"
    )
    assert page.request.get(f"{live_server.url}/favicon.ico").status == 200
    # The tab icon is really loaded, not merely declared.
    assert page.evaluate("document.querySelector('link[rel~=icon]').href.endsWith('.svg')")

    switch_language(page, "ar")
    assert page.title().endswith("ASC 2026")
    assert (
        page.locator('meta[name="description"]').get_attribute("content").count(ARABIC_EVENT_NAME)
        == 1
    )


def test_private_pages_are_noindex_in_the_head_and_the_header(live_server, page) -> None:
    _open_verify_page(live_server, page, "ux1-robots-browser@example.com")
    assert page.locator('meta[name="robots"]').get_attribute("content") == "noindex, nofollow"
    response = page.request.get(f"{live_server.url}/accounts/verify/")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert "no-store" in response.headers["cache-control"]


def test_arabic_pages_name_the_event_in_full_and_the_footer_is_empty_under_a10(
    live_server, page
) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    switch_language(page, "ar")
    footer = page.locator("footer.asc-footer")
    text = footer.inner_text()
    assert "المؤتمر الإفريقي للمؤسسات الناشئة" in text or ARABIC_EVENT_NAME in (
        page.locator("img[alt]").first.get_attribute("alt") or ""
    )
    # P4-4, FOOTER-01 option (a), amendment A-10: no footer link until the
    # developer supplies a new footer design (replaces the M23 link checks).
    assert footer.locator("a").count() == 0
    assert footer.locator("bdi.asc-footer-host").count() == 0
    assert page.locator("img[alt]").first.get_attribute("alt") == ARABIC_EVENT_NAME
    _shot(page, "footer-ar-desktop")


def test_the_footer_fits_a_phone_without_horizontal_scrolling(live_server, page) -> None:
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    links = page.locator("footer.asc-footer a")
    for i in range(links.count()):
        assert links.nth(i).bounding_box()["height"] >= 44
    _shot(page, "footer-en-mobile-375")
