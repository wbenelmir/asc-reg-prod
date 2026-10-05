"""Real-browser checks for UX-2 (identity, contact, professional, interests).

Chromium against the real `live_server`, synthetic data only. Screenshots go
to `var/test_artifacts/phase4/ux_2/screenshots/`.
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

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "ux_2"
SHOTS = SHOTS / "screenshots"
SUBMIT = "#main-content button[type=submit]"


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _login(live_server, page, email: str) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")


def _seed(event) -> None:
    from apps.core.models import Country, Sector
    from apps.registrations.models import InterestTopic

    def create():
        for code, name in (("DZ", "Algeria"), ("FR", "France")):
            Country.objects.get_or_create(code=code, defaults={"name": name})
        Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
        for code, group, label in (
            ("VENTURE_CAPITAL", "FUNDING_INVESTMENT", "Venture capital"),
            ("AI", "INNOVATION", "Artificial intelligence"),
            ("B2B_MEETINGS", "NETWORKING", "Business-to-business meetings"),
        ):
            InterestTopic.objects.get_or_create(
                event_edition=event, code=code, defaults={"label": label, "group_code": group}
            )

    database_call(create)


def test_the_identity_rules_and_defaults_in_the_browser(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed(seeded_open_event)
    _login(live_server, page, "ux2-identity@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    # Algeria is preselected on a blank new form (S-09).
    assert page.locator("#id_nationality_code").input_value() == "DZ"
    page.fill("#id_given_names", "أمين")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "1234 5678 9012 3456 78")
    page.locator(SUBMIT).click()
    page.wait_for_selector("#error-summary")
    assert "Latin letters" in page.locator("#error-summary").inner_text()
    _shot(page, "identity-latin-error-en")
    page.fill("#id_given_names", "Amine")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/contact/**")


def test_a_pasted_international_number_switches_the_calling_country(
    live_server, page, seeded_open_event, seeded_legal_notices
) -> None:
    _seed(seeded_open_event)
    _login(live_server, page, "ux2-phone@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/contact/**")
    # The calling code starts from the country of residence (DZ).
    expect(page.locator("#id_mobile_country_code")).to_have_value("DZ")
    number = page.locator("#id_mobile_number")
    assert number.get_attribute("dir") == "ltr"
    number.fill("+33 6 12 34 56 78")
    expect(page.locator("#id_mobile_country_code")).to_have_value("FR")
    status = page.locator("#id_mobile_number ~ [role=status]").first
    assert "France" in status.inner_text()
    _shot(page, "contact-paste-switch-en")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/professional/**")

    from apps.registrations.models import RegistrationProfile

    contact = database_call(
        lambda: (
            RegistrationProfile.objects.select_related("declared_mobile_contact")
            .get()
            .declared_mobile_contact
        )
    )
    assert contact.country_code_id == "FR"


def test_professional_step_scope_optional_links_and_grouped_topics(
    live_server, page, seeded_open_event, seeded_legal_notices, isolated_private_storage, tmp_path
) -> None:
    from apps.documents.tests.factories import make_test_photo

    _seed(seeded_open_event)
    _login(live_server, page, "ux2-professional@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-03-07")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/contact/**")
    page.fill("#id_mobile_number", "0551234567")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/professional/**")

    page.fill("#id_organization_name", "Synthetic Labs SARL")
    select_choice(page, "id_organization_type", "SME")
    page.fill("#id_job_title", "Product Manager")
    page.select_option("#id_sector", "TECH")
    page.select_option("#id_country_code", "DZ")
    page.check("input[name=operating_scope][value=MULTINATIONAL]")
    page.fill("#id_organization_website", "example.org")
    bio = page.locator("#id_biography")
    bio.fill("Short biography.")
    expect(page.locator("#id_biography ~ .asc-char-counter")).to_have_text("16 / 500")
    photo = tmp_path / "photo.png"
    photo.write_bytes(make_test_photo().read())
    page.set_input_files("#id_profile_photo", str(photo))
    _shot(page, "professional-scope-en")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/interests/**")

    from apps.organizations.models import ProfessionalAffiliation

    affiliation = database_call(lambda: ProfessionalAffiliation.objects.get())
    assert affiliation.organization_website == "https://example.org"
    assert affiliation.operating_scope == "MULTINATIONAL"
    assert affiliation.department == ""

    # Grouped topics with a filter and removable chips.
    assert page.locator(".asc-topic-group-title").count() == 3
    page.locator("label[data-topic]:has-text('Artificial intelligence') input").check()
    chip = page.locator(".asc-topic-chip")
    expect(chip).to_have_count(1)
    assert chip.get_attribute("aria-label") == "Remove Artificial intelligence"
    page.locator(".asc-topic-filter input").fill("venture")
    expect(
        page.locator("label[data-topic]:has-text('Business-to-business meetings')")
    ).to_be_hidden()
    _shot(page, "interests-grouped-chips-en")
    chip.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".asc-topic-chip")).to_have_count(0)
    assert not page.locator(
        "label[data-topic]:has-text('Artificial intelligence') input"
    ).is_checked()
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("dir") == "rtl"
    _shot(page, "interests-grouped-ar")


def test_the_footer_shows_no_contact_block_on_a_phone_under_a10(live_server, page) -> None:
    """P4-4, FOOTER-01 option (a), amendment A-10: the C-07 block is removed."""
    page.set_viewport_size({"width": 375, "height": 800})
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.locator("[data-footer-contact]").count() == 0
    assert page.locator("footer.asc-footer a").count() == 0
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    _shot(page, "footer-contact-mobile-375")
