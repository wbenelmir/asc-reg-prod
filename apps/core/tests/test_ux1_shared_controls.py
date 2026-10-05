"""UX-1 shared presentation controls (M02, M04, M05, M06, M09, M10, M23).

Presentation contract only: these tests prove what the server renders and
parses. The real-browser behaviour (paste, keyboard, layout, RTL) is covered
by `tests/browser/test_ux1_shared_controls.py`.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest
from django.core import mail
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import translation

from apps.accounts.forms import OtpVerifyForm
from apps.accounts.otp import DeterministicTestOtpGenerator, deliver_otp
from apps.core.context_processors import configured_public_base_url
from apps.core.middleware.private_pages import (
    NOINDEX_VALUE,
    PRIVATE_CACHE_VALUE,
    PrivatePageProtectionMiddleware,
)
from apps.core.models import Country
from apps.core.normalization import normalize_digit_code, normalize_digits
from apps.core.testing import otp_request_data
from apps.core.widgets import DayMonthYearWidget
from apps.registrations.forms import (
    ContactStepForm,
    IdentityStepForm,
    InterestsStepForm,
    ProfessionalStepForm,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# The exact Arabic event name (decision S-04), with hamza-under-alef in the second word.
ARABIC_EVENT_NAME = "المؤتمر الإفريقي للمؤسسات الناشئة ASC"

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Digit normalization (S-02) and the one-time code (S-01)
# ---------------------------------------------------------------------------


def test_arabic_indic_and_extended_arabic_indic_digits_become_ascii() -> None:
    assert normalize_digits("٠١٢٣٤٥٦٧٨٩") == "0123456789"
    assert normalize_digits("۰۱۲۳۴۵۶۷۸۹") == "0123456789"


def test_other_unicode_decimal_digits_are_never_converted() -> None:
    # Fullwidth and Devanagari digits are confusable, so the strict check rejects them.
    assert normalize_digits("１２３") == "１２３"
    assert normalize_digits("१२३") == "१२३"


def test_code_separators_are_only_the_listed_ones() -> None:
    assert normalize_digit_code(" 12-34 56  ") == "123456"
    assert normalize_digit_code("12.34") == "12.34"
    assert normalize_digit_code("12_34") == "12_34"


@pytest.mark.parametrize(
    "typed",
    ["123456", "123 456", "123-456", " 123456 ", "١٢٣٤٥٦", "۱۲۳۴۵۶", "١٢٣ ٤٥٦", "123 456"],
)
def test_otp_form_accepts_a_six_digit_code_in_any_supported_form(typed: str) -> None:
    form = OtpVerifyForm({"code": typed})
    assert form.is_valid(), form.errors
    assert re.fullmatch(r"[0-9]{6}", form.cleaned_data["code"])


@pytest.mark.parametrize(
    "typed",
    ["", "12345", "1234567", "12345a", "１２３４５６", "१२३४५६", "12 34 5", "12.3456", "abcdef"],
)
def test_otp_form_rejects_everything_that_is_not_six_digits(typed: str) -> None:
    form = OtpVerifyForm({"code": typed})
    assert not form.is_valid()
    assert form.errors["code"]


def test_otp_control_is_one_accessible_input_with_the_documented_attributes() -> None:
    html = str(OtpVerifyForm()["code"])
    assert html.count("<input") == 1
    for fragment in (
        'type="text"',
        'inputmode="numeric"',
        'autocomplete="one-time-code"',
        'pattern="[0-9]{6}"',
        'dir="ltr"',
        'data-asc-otp="6"',
        'maxlength="12"',
    ):
        assert fragment in html


def test_otp_view_accepts_a_pasted_code_with_a_space(client: Client) -> None:
    from apps.accounts import participant_auth
    from apps.people.services import resolve_or_create_participant_for_email

    email = "ux1-paste@example.com"
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        code = DeterministicTestOtpGenerator.FIXED_VALUE
        response = client.post(reverse("accounts:otp-verify"), {"code": f"{code[:3]} {code[3:]}"})
    assert response.status_code == 302
    assert participant_auth.PARTICIPANT_SESSION_KEY in client.session
    assert resolve_or_create_participant_for_email(email) is not None


def test_otp_verify_page_shows_one_code_input_and_no_second_text_control(client: Client) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(
            reverse("accounts:otp-request"), otp_request_data("ux1-page@example.com", client=client)
        )
        html = client.get(reverse("accounts:otp-verify")).content.decode()
    assert html.count('name="code"') == 1
    assert 'autocomplete="one-time-code"' in html
    assert "data-asc-otp" in html


# ---------------------------------------------------------------------------
# Day / Month / Year (S-08)
# ---------------------------------------------------------------------------


def _widget_value(**parts: str) -> str | None:
    data = {f"dob_{key}": value for key, value in parts.items()}
    return DayMonthYearWidget().value_from_datadict(data, {}, "dob")


def test_parts_combine_into_an_iso_date() -> None:
    assert _widget_value(day="07", month="03", year="1990") == "1990-03-07"
    assert _widget_value(day="7", month="3", year="1990") == "1990-03-07"


def test_arabic_indic_date_parts_are_read_as_ascii() -> None:
    assert _widget_value(day="٠٧", month="٠٣", year="١٩٩٠") == "1990-03-07"
    assert _widget_value(day="۰۷", month="۰۳", year="۱۹۹۰") == "1990-03-07"


def test_single_key_post_is_passed_through_unchanged() -> None:
    assert (
        DayMonthYearWidget().value_from_datadict({"dob": "1990-03-07"}, {}, "dob") == "1990-03-07"
    )
    assert DayMonthYearWidget().value_from_datadict({}, {}, "dob") is None


def test_all_empty_parts_read_as_an_empty_value() -> None:
    assert _widget_value(day="", month="", year="") == ""
    assert _widget_value(day=" ", month=" ", year=" ") == ""


@pytest.mark.parametrize(
    ("day", "month", "year"),
    [
        ("5", "", "1990"),
        ("ab", "03", "1990"),
        ("07", "03", "90"),
        ("07", "03", "19900"),
        ("1", "2", "٣"),
    ],
)
def test_an_implausible_entry_is_returned_as_typed_so_the_iso_parse_fails(
    day: str, month: str, year: str
) -> None:
    value = _widget_value(day=day, month=month, year=year)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or "") is None
    assert "/" in value


def _identity_post(**overrides: str) -> dict[str, str]:
    data = {
        "given_names": "Amine",
        "family_name": "Benali",
        "date_of_birth_day": "07",
        "date_of_birth_month": "03",
        "date_of_birth_year": "1990",
        "nationality_code": "DZ",
        "country_of_residence": "DZ",
        "identity_path": "NIN",
        "nin_value": "123456789012345678",
    }
    data.update(overrides)
    return data


@pytest.fixture
def countries(db):
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})


def test_identity_form_parses_the_three_parts_into_the_date(countries) -> None:
    form = IdentityStepForm(_identity_post())
    assert form.is_valid(), form.errors
    assert form.cleaned_data["date_of_birth"].isoformat() == "1990-03-07"


def test_identity_form_still_accepts_the_single_iso_key(countries) -> None:
    data = _identity_post()
    for part in ("day", "month", "year"):
        del data[f"date_of_birth_{part}"]
    data["date_of_birth"] = "1990-03-07"
    form = IdentityStepForm(data)
    assert form.is_valid(), form.errors


@pytest.mark.parametrize(
    "parts",
    [
        {"date_of_birth_day": "31", "date_of_birth_month": "02"},
        {"date_of_birth_day": "00"},
        {"date_of_birth_month": "13"},
        {"date_of_birth_year": "90"},
        {"date_of_birth_day": "", "date_of_birth_month": "", "date_of_birth_year": ""},
    ],
)
def test_identity_form_rejects_a_date_that_is_not_real(countries, parts) -> None:
    form = IdentityStepForm(_identity_post(**parts))
    assert not form.is_valid()
    assert "date_of_birth" in form.errors


def test_the_typed_parts_come_back_after_a_failed_validation(countries) -> None:
    form = IdentityStepForm(
        _identity_post(date_of_birth_day="31", date_of_birth_month="02", date_of_birth_year="1990")
    )
    assert not form.is_valid()
    html = str(form["date_of_birth"])
    assert 'value="31"' in html and 'value="02"' in html and 'value="1990"' in html


def test_date_control_renders_three_labelled_numeric_parts_in_day_month_year_order(
    countries,
) -> None:
    html = str(IdentityStepForm()["date_of_birth"])
    assert html.count("<input") == 3
    assert html.index("_day") < html.index("_month") < html.index("_year")
    for part, maxlength in (("day", "2"), ("month", "2"), ("year", "4")):
        assert f'id="id_date_of_birth_{part}"' in html
        assert f'name="date_of_birth_{part}"' in html
        assert f'for="id_date_of_birth_{part}"' in html
        assert f'maxlength="{maxlength}"' in html
    assert html.count('inputmode="numeric"') == 3
    assert 'autocomplete="bday-day"' in html
    assert 'autocomplete="bday-month"' in html
    assert 'autocomplete="bday-year"' in html
    # S-06: no placeholder on a date part.
    assert "placeholder" not in html


def test_the_field_label_points_at_the_first_part(countries) -> None:
    field = IdentityStepForm()["date_of_birth"]
    assert field.id_for_label == "id_date_of_birth_day"


def test_date_parts_have_their_own_labels_in_every_language(countries) -> None:
    expected = {"en": ("Day", "Month", "Year"), "fr": ("Jour", "Mois", "Année")}
    for language, labels in expected.items():
        with translation.override(language):
            html = str(IdentityStepForm()["date_of_birth"])
        for label in labels:
            assert f">{label}</label>" in html
    with translation.override("ar"):
        html = str(IdentityStepForm()["date_of_birth"])
    for label in ("اليوم", "الشهر", "السنة"):
        assert f">{label}</label>" in html


# ---------------------------------------------------------------------------
# Placeholders (M06, S-06)
# ---------------------------------------------------------------------------


def _placeholder(form, name: str) -> str | None:
    match = re.search(r'placeholder="([^"]*)"', str(form[name]))
    return match.group(1) if match else None


PLACEHOLDERS = {
    "en": {
        "given_names": "e.g. Amina",
        "family_name": "e.g. Benali",
        "nin_value": "18 digits",
        "passport_number": "e.g. AB1234567",
    },
    "fr": {
        "given_names": "ex. Amina",
        "family_name": "ex. Benali",
        "nin_value": "18 chiffres",
        "passport_number": "ex. AB1234567",
    },
    "ar": {
        "given_names": "مثال: Amina",
        "family_name": "مثال: Benali",
        "nin_value": "18 رقمًا",
        "passport_number": "مثال: AB1234567",
    },
}


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_identity_placeholders_follow_the_active_language(countries, language: str) -> None:
    with translation.override(language):
        form = IdentityStepForm()
        for name, expected in PLACEHOLDERS[language].items():
            assert _placeholder(form, name) == expected, (language, name)


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_every_translated_placeholder_is_a_synthetic_example(countries, language: str) -> None:
    with translation.override(language):
        forms_ = [
            IdentityStepForm(),
            ProfessionalStepForm(),
            InterestsStepForm(),
            ContactStepForm(),
        ]
        for form in forms_:
            for name in form.fields:
                value = _placeholder(form, name)
                if value is None:
                    continue
                assert value.strip(), (language, name)
                # No real-looking email or phone number is ever a placeholder.
                assert not re.search(r"@(?!example\.)", value), (language, name, value)


def test_professional_and_interest_placeholders_are_localized(countries) -> None:
    with translation.override("fr"):
        form = ProfessionalStepForm()
        assert _placeholder(form, "job_title") == "ex. Chef de produit"
        assert _placeholder(form, "department") == "ex. Recherche et développement"
        assert _placeholder(form, "organization_name") == "ex. Example Technologies SARL"
        assert _placeholder(form, "organization_website") == "https://www.example.org"
    with translation.override("ar"):
        form = ProfessionalStepForm()
        assert _placeholder(form, "job_title") == "مثال: Product Manager"


def test_no_placeholder_on_select_date_checkbox_or_file_controls(countries) -> None:
    identity = IdentityStepForm()
    professional = ProfessionalStepForm()
    for form, names in (
        (identity, ("nationality_code", "country_of_residence", "passport_country_code")),
        (identity, ("date_of_birth", "passport_expires_at")),
        (professional, ("sector", "country_code", "organization_type", "profile_photo")),
    ):
        for name in names:
            assert _placeholder(form, name) is None, name


def test_text_inputs_that_take_latin_text_use_dir_auto(countries) -> None:
    form = IdentityStepForm()
    for name in ("given_names", "family_name"):
        assert 'dir="auto"' in str(form[name])


def test_mobile_placeholder_is_the_national_example_of_the_selected_region(countries) -> None:
    form = ContactStepForm(initial={"mobile_country_code": "FR"})
    example = _placeholder(form, "mobile_number")
    assert example and example.startswith("0")  # French national mobile format
    assert 'data-region-source="mobile_country_code"' in str(form["mobile_number"])
    assert "data-region-examples" in str(form["mobile_number"])


def test_mobile_placeholder_is_absent_until_a_region_is_known(countries) -> None:
    assert _placeholder(ContactStepForm(), "mobile_number") is None


def test_placeholder_colour_meets_contrast_on_the_field_surface() -> None:
    css = (PROJECT_ROOT / "static" / "css" / "asc-ui.css").read_text(encoding="utf-8")
    match = re.search(r"\.asc-ui \.form-control::placeholder \{[^}]*?color: (#[0-9a-fA-F]{6})", css)
    assert match, "placeholder colour rule missing"

    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    ratio = (luminance("#ffffff") + 0.05) / (luminance(match.group(1)) + 0.05)
    assert ratio >= 4.5, ratio


# ---------------------------------------------------------------------------
# Searchable selects (M09, S-07)
# ---------------------------------------------------------------------------


def test_country_and_sector_selects_opt_in_to_search_and_keep_the_native_select(countries) -> None:
    for form, names in (
        (IdentityStepForm(), ("nationality_code", "country_of_residence", "passport_country_code")),
        (ContactStepForm(), ("mobile_country_code",)),
        (ProfessionalStepForm(), ("sector", "country_code")),
    ):
        for name in names:
            html = str(form[name])
            assert html.startswith("<select"), name
            assert 'data-asc-combobox="true"' in html
            assert 'data-combobox-threshold="8"' in html


def test_the_server_still_rejects_a_choice_outside_the_queryset(countries) -> None:
    form = IdentityStepForm(_identity_post(nationality_code="ZZ"))
    assert not form.is_valid()
    assert "nationality_code" in form.errors


def test_combobox_script_is_shipped_and_keeps_the_value_in_the_native_select() -> None:
    script = (PROJECT_ROOT / "static" / "js" / "asc-enhance.js").read_text(encoding="utf-8")
    for token in (
        "role",
        "combobox",
        "aria-activedescendant",
        "aria-expanded",
        "ArrowDown",
        "Escape",
        "Home",
    ):
        assert token in script
    assert "select.value = value" in script


# ---------------------------------------------------------------------------
# Private-page protection and the public head (M05, S-05)
# ---------------------------------------------------------------------------


def test_start_page_is_the_only_indexable_page_and_carries_its_metadata(client: Client) -> None:
    response = client.get(reverse("accounts:otp-request"))
    html = response.content.decode()
    assert response.status_code == 200
    assert "X-Robots-Tag" not in response.headers
    assert '<meta name="robots" content="index, follow">' in html
    assert '<meta name="description" content="' in html
    assert 'property="og:title"' in html
    assert 'rel="icon"' in html and "asc-mark.svg" in html
    assert "asc-mark-32.png" in html
    assert 'rel="apple-touch-icon"' in html and "asc-mark-180.png" in html
    assert "<title>Start registration &middot; ASC 2026</title>" in html or (
        "<title>Start registration · ASC 2026</title>" in html
    )
    assert 'rel="canonical"' not in html  # no configured public origin


@override_settings(PUBLIC_BASE_URL="https://registration.example.org")
def test_canonical_and_social_url_appear_only_from_a_configured_https_origin(
    client: Client,
) -> None:
    html = client.get(reverse("accounts:otp-request")).content.decode()
    assert '<link rel="canonical" href="https://registration.example.org/accounts/start/">' in html
    assert 'property="og:url" content="https://registration.example.org/accounts/start/"' in html


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://localhost",
        "http://registration.example.org",
        "https://registration.invalid",
        "",
    ],
)
def test_a_local_or_insecure_origin_is_never_used_for_canonical_urls(value: str) -> None:
    with override_settings(PUBLIC_BASE_URL=value):
        assert configured_public_base_url() == ""


def test_no_hreflang_is_emitted_because_languages_share_one_url(client: Client) -> None:
    assert "hreflang" not in client.get(reverse("accounts:otp-request")).content.decode()


def test_every_other_page_is_noindex_and_not_shared_cacheable(client: Client) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(
            reverse("accounts:otp-request"),
            otp_request_data("ux1-robots@example.com", client=client),
        )
        verify = client.get(reverse("accounts:otp-verify"))
    for response in (
        verify,
        client.get(reverse("accounts:operational-sign-in")),
        client.get("/workspace/", follow=False),
        client.get("/no-such-page-anywhere/"),
    ):
        assert response.headers["X-Robots-Tag"] == NOINDEX_VALUE
        assert "no-store" in response.headers["Cache-Control"]
        assert "private" in response.headers["Cache-Control"]
    assert '<meta name="robots" content="noindex, nofollow">' in verify.content.decode()


def test_middleware_never_overrides_a_cache_policy_a_view_set() -> None:
    from django.http import HttpResponse

    def view(request):
        response = HttpResponse("ok")
        response["Cache-Control"] = "no-cache"
        return response

    request = RequestFactory().get("/entry/sw.js")
    response = PrivatePageProtectionMiddleware(view)(request)
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["X-Robots-Tag"] == NOINDEX_VALUE


def test_middleware_adds_the_private_cache_policy_when_none_exists() -> None:
    from django.http import HttpResponse

    request = RequestFactory().get("/workspace/anything/")
    response = PrivatePageProtectionMiddleware(lambda r: HttpResponse("ok"))(request)
    assert response.headers["Cache-Control"] == PRIVATE_CACHE_VALUE


def test_robots_txt_allows_only_the_start_page(client: Client) -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    lines = response.content.decode().splitlines()
    assert lines == ["User-agent: *", "Allow: /accounts/start/", "Disallow: /"]
    assert "Sitemap" not in response.content.decode()
    assert "X-Robots-Tag" not in response.headers
    assert client.post("/robots.txt").status_code == 405


def test_favicon_ico_redirects_to_the_compact_mark_instead_of_a_404(client: Client) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 301
    assert response["Location"].endswith("img/brand/asc-mark-32.png")


def test_brand_mark_assets_exist_and_have_the_documented_sizes() -> None:
    from PIL import Image

    brand = PROJECT_ROOT / "static" / "img" / "brand"
    small = Image.open(brand / "asc-mark-32.png")
    touch = Image.open(brand / "asc-mark-180.png")
    assert small.size == (32, 32) and "A" in small.mode  # transparent tab icon
    assert touch.size == (180, 180) and "A" not in touch.mode  # opaque touch icon


def test_the_svg_mark_is_the_logo_graphic_copied_byte_for_byte() -> None:
    brand = PROJECT_ROOT / "static" / "img" / "brand"
    logo_paths = re.findall(r"<path\b[^>]*/>", (brand / "asc-logo.svg").read_text(encoding="utf-8"))
    mark_paths = re.findall(r"<path\b[^>]*/>", (brand / "asc-mark.svg").read_text(encoding="utf-8"))
    assert len(logo_paths) == 36
    assert mark_paths == logo_paths[24:]
    assert 'viewBox="302 -5 250 250"' in (brand / "asc-mark.svg").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Official footer links (M23, S-13)
# ---------------------------------------------------------------------------


OFFICIAL_LINKS = ("https://africanstartupconference.org/", "https://mkesm.gov.dz/")
# UX-2 (C-07, delegated decision): the contact and social items verified on
# the official conference site on 2026-09-29. Nothing else may appear.
VERIFIED_CONTACT_LINKS = (
    "mailto:info-africanstartupconference@startup.dz",
    "tel:+213770848390",
    "https://www.facebook.com/africanstartupconference",
    "https://x.com/africanstartupc",
    "https://www.linkedin.com/company/the-african-startup-conference",
    "https://www.instagram.com/africanstartupconference",
    "https://www.youtube.com/@AfricanStartupConference",
)


def _footer(html: str) -> str:
    start = html.index('<footer class="asc-footer">')
    return html[start : html.index("</footer>", start)]


# P4-4, owner decision FOOTER-01 option (a), amendment A-10
# (docs/execution/UX_REMARKS_DECISION_GATE.md §17.7): the shared footer carries
# no link, contact or social item. These tests replace the M23/S-13, C-07 and
# UXR-F02 footer assertions; the removed items must not come back unreviewed.
# FOOTER-02 (2026-10-04, §17.12) supersedes only the EMPTY partial: it now
# shows the two official institutional lines (apps/core/tests/
# test_official_footer.py), still with no link.
@pytest.mark.parametrize(
    "url_name",
    ["accounts:otp-request", "accounts:operational-sign-in"],
)
def test_the_public_shell_footer_carries_no_link_under_amendment_a10(
    client: Client, url_name
) -> None:
    footer = _footer(client.get(reverse(url_name)).content.decode())
    assert "ASC 2026 Registration Platform" in footer  # the footer itself still renders
    assert "<a " not in footer and "asc-footer-links" not in footer
    assert "data-footer-contact" not in footer and "data-footer-legal" not in footer
    for link in OFFICIAL_LINKS + VERIFIED_CONTACT_LINKS:
        assert link not in footer


def test_error_page_footer_carries_no_link_under_amendment_a10(client: Client) -> None:
    footer = _footer(client.get("/no-such-page-anywhere/").content.decode())
    assert "ASC 2026 Registration Platform" in footer
    assert "<a " not in footer
    for link in OFFICIAL_LINKS:
        assert link not in footer


def test_the_footer_partial_loads_no_third_party_resource() -> None:
    partial = (PROJECT_ROOT / "templates" / "partials" / "footer_official.html").read_text(
        encoding="utf-8"
    )
    for tag in ("<script", "<img", "<iframe", "<link", "<style", "<object", "<embed"):
        assert tag not in partial
    assert re.findall(r'(?:href|src)="([^"]+)"', partial) == []
    # FOOTER-02 (superseding A-10's empty partial): the two official lines and
    # nothing else -- no link, no image, no script.
    from django.template.loader import render_to_string

    rendered = render_to_string("partials/footer_official.html")
    assert "<a " not in rendered
    assert (
        re.sub(r"<[^>]+>", " ", rendered).split()
        == (
            "Ministère de l'Économie de la Connaissance, des Start-up et des Micro-entreprise "
            "Direction des Systèmes d’Information (DSI)"
        ).split()
    )


def test_the_footer_has_no_leftover_link_text_in_any_language(client: Client) -> None:
    # English last: a request leaves its language active in the test thread,
    # and some later tests assume the default language without activating it.
    for language, removed in (
        ("ar", "الموقع الرسمي للمؤتمر"),
        ("fr", "Site officiel de la conférence"),
        ("en", "Official conference website"),
    ):
        client.cookies["django_language"] = language
        footer = _footer(client.get(reverse("accounts:otp-request")).content.decode())
        assert removed not in footer
        assert "asc-footer-host" not in footer


# ---------------------------------------------------------------------------
# The Arabic event name (M04, S-04, D-03)
# ---------------------------------------------------------------------------


def test_the_arabic_name_is_exact_and_uses_hamza_under_alef() -> None:
    assert unicodedata.normalize("NFC", ARABIC_EVENT_NAME) == ARABIC_EVENT_NAME
    assert "إ" in ARABIC_EVENT_NAME  # ALEF WITH HAMZA BELOW in the second word
    assert ARABIC_EVENT_NAME.endswith(" ASC")
    assert len(ARABIC_EVENT_NAME) == 37


def test_arabic_catalog_uses_the_full_name_where_the_event_is_named() -> None:
    from django.utils.translation import gettext

    with translation.override("ar"):
        assert gettext("African Startup Conference") == ARABIC_EVENT_NAME
        assert gettext("ASC 2026 Registration Platform") == f"منصة تسجيل {ARABIC_EVENT_NAME} 2026"
        assert gettext("ASC 2026 entry pass") == f"تصريح دخول {ARABIC_EVENT_NAME} 2026"
        subtitle = gettext(
            "Your registrations for ASC events, with their current status and any action needed"
            " from you."
        )
        assert ARABIC_EVENT_NAME in subtitle
        assert "فعاليات" not in subtitle


def test_the_generic_arabic_wording_is_gone_from_the_catalog_and_the_templates() -> None:
    generic = "فعاليات ASC"
    for path in (PROJECT_ROOT / "locale" / "ar" / "LC_MESSAGES" / "django.po",):
        assert generic not in path.read_text(encoding="utf-8")
    for template in (PROJECT_ROOT / "templates").rglob("*.html"):
        assert generic not in template.read_text(encoding="utf-8")


def test_english_and_french_keep_the_proper_name() -> None:
    from django.utils.translation import gettext

    for language in ("en", "fr"):
        with translation.override(language):
            assert gettext("African Startup Conference") == "African Startup Conference"


def test_the_arabic_public_footer_shows_the_full_name_inside_the_page(client: Client) -> None:
    client.cookies["django_language"] = "ar"
    html = client.get(reverse("accounts:otp-request")).content.decode()
    assert ARABIC_EVENT_NAME in html
    assert "فعاليات" not in html


def test_the_otp_email_is_localized_and_names_the_event_in_full_in_arabic() -> None:
    mail.outbox.clear()
    with translation.override("ar"):
        deliver_otp(channel="EMAIL", recipient_value="ux1-mail@example.com", otp_value="123456")
    message = mail.outbox[-1]
    assert ARABIC_EVENT_NAME in message.subject
    assert "123456" in message.body

    mail.outbox.clear()
    with translation.override("en"):
        deliver_otp(channel="EMAIL", recipient_value="ux1-mail@example.com", otp_value="123456")
    assert mail.outbox[-1].subject == "Your ASC 2026 verification code"
    assert mail.outbox[-1].body == "Your verification code is 123456. It will expire shortly."


def test_arabic_title_carries_the_compact_edition_suffix_only(client: Client) -> None:
    client.cookies["django_language"] = "ar"
    html = client.get(reverse("accounts:otp-request")).content.decode()
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert title.endswith("ASC 2026")
    assert ARABIC_EVENT_NAME not in title  # D-03: compact form in the tab title
