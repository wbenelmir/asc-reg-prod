"""Real-browser checks for UX-3 (M21 accommodation, M22 notices and consents).

Chromium against the real `live_server`, synthetic data only. Screenshots go
to `var/test_artifacts/phase4/ux_3/screenshots/`.
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

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "ux_3"
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
    from apps.privacy.models import ConsentPurpose
    from apps.registrations.models import InterestTopic

    def create():
        # `live_server` tests flush migration-seeded rows; re-create the two
        # UX-3 consent purposes the notices step records against.
        for code in ("PERSONAL_DATA_PROCESSING", "SENSITIVE_ACCOMMODATION_DATA"):
            ConsentPurpose.objects.get_or_create(code=code, defaults={"name": code})
        Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
        Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
        InterestTopic.objects.get_or_create(
            event_edition=event, code="STARTUPS", defaults={"label": "Startups"}
        )

    database_call(create)


def _walk_to_interests(live_server, page, tmp_path) -> None:
    from apps.documents.tests.factories import make_test_photo

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
    page.select_option("#id_mobile_country_code", "DZ")
    page.fill("#id_mobile_number", "0551234567")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/professional/**")
    page.fill("#id_organization_name", "Synthetic Labs")
    select_choice(page, "id_organization_type", "COMPANY")
    page.check("input[name=operating_scope][value=NATIONAL]")
    page.fill("#id_job_title", "Founder")
    page.fill("#id_department", "Management")
    page.select_option("#id_sector", "TECH")
    page.select_option("#id_country_code", "DZ")
    page.fill("#id_organization_website", "https://example.org")
    page.fill("#id_professional_profile_url", "https://example.org/profile")
    page.fill("#id_biography", "A short synthetic biography.")
    photo = tmp_path / "photo.png"
    photo.write_bytes(make_test_photo().read())
    page.set_input_files("#id_profile_photo", str(photo))
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/interests/**")


def test_accommodation_is_voluntary_and_its_consent_is_separate(
    live_server, page, seeded_open_event, seeded_legal_notices, isolated_private_storage, tmp_path
) -> None:
    _seed(seeded_open_event)
    _login(live_server, page, "ux3-browser@example.com")
    _walk_to_interests(live_server, page, tmp_path)

    details = page.locator("[data-accommodation-details]")
    # Waits for the boosted swap to run the page script: shown only for "Yes".
    expect(details).to_be_hidden()
    page.locator("#main-content input[name=interest_topics]").first.check()
    page.fill("#id_objectives_text", "Meet investors.")
    page.check("input[name=accommodation_answer][value=YES]")
    expect(details).to_be_visible()
    page.check("input[name=accommodation_categories][value=CAPTIONING]")
    note = page.locator("#id_accommodation_note")
    note.fill("Aisle seat, please.")
    counter = page.locator(".asc-char-counter")
    expect(counter).to_have_text("19 / 300")
    assert counter.get_attribute("aria-hidden") == "true"
    _shot(page, "interests-accommodation-yes-en")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/review/**")
    assert page.locator("text=Live captioning").count() == 1
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/notices/**")

    # Four separate, unticked statements (the fourth because support was described).
    for name in (
        "accept_privacy_notice",
        "accept_terms",
        "accept_data_processing",
        "accept_sensitive_data",
    ):
        box = page.locator(f"#id_{name}")
        assert box.count() == 1 and not box.is_checked()
    assert not page.locator("#id_marketing_consent").is_checked()
    _shot(page, "notices-four-statements-en")

    page.check("#id_accept_privacy_notice")
    page.check("#id_accept_terms")
    page.check("#id_accept_data_processing")
    page.locator(SUBMIT).click()  # sensitive consent missing
    page.wait_for_selector("#error-summary")
    assert "/register/notices/" in page.url
    page.check("#id_accept_sensitive_data")
    page.locator(SUBMIT).click()
    page.wait_for_url("**/register/confirmation/**")

    page.goto(f"{live_server.url}/workspace/")
    withdraw = page.locator("button:has-text('Withdraw consent for accessibility information')")
    assert withdraw.count() == 1
    withdraw.click()
    page.locator("#asc-confirm-dialog [data-confirm-accept]").click()
    page.wait_for_selector("text=Your consent is withdrawn.")
    _shot(page, "workspace-consent-withdrawn-en")

    from apps.registrations.models import AccommodationRequest

    request = database_call(lambda: AccommodationRequest.objects.get())
    assert request.withdrawn_at is not None and request.note_encrypted == ""


def test_the_arabic_interests_step_and_notices_read_right_to_left(
    live_server, page, seeded_open_event, seeded_legal_notices, isolated_private_storage, tmp_path
) -> None:
    _seed(seeded_open_event)
    _login(live_server, page, "ux3-ar@example.com")
    _walk_to_interests(live_server, page, tmp_path)
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("dir") == "rtl"
    page.check("input[name=accommodation_answer][value=YES]")
    page.fill("#id_accommodation_note", "مقعد قرب الممر من فضلكم")
    assert page.locator(".asc-char-counter bdi").get_attribute("dir") == "ltr"
    _shot(page, "interests-accommodation-yes-ar")
    # Switching language never carries the accommodation answers along.
    switch_language(page, "fr")
    assert page.locator("#id_accommodation_note").input_value() == ""
    assert not page.locator("input[name=accommodation_answer][value=YES]").is_checked()


def test_the_details_stay_visible_without_javascript(
    live_server,
    browser,
    seeded_open_event,
    seeded_legal_notices,
    isolated_private_storage,
    tmp_path,
) -> None:
    from tests.browser.helpers import no_js_page_at_otp_verify

    _seed(seeded_open_event)
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        context, page = no_js_page_at_otp_verify(browser, live_server.url, "ux3-nojs@example.com")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")
    try:
        page.goto(f"{live_server.url}/register/interests/")
        assert page.locator("[data-accommodation-details]").is_visible()
        assert page.locator("input[name=accommodation_categories]").count() == 10
    finally:
        context.close()
