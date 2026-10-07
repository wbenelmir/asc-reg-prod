"""Real-browser (Playwright) OTP-to-submission happy path (Prompt 5 §"browser-level
OTP-to-submission happy path").

The HTTP-client-level equivalent already exists (`apps/registrations/tests/
test_views.py::test_full_registration_wizard_end_to_end`), proving the service and
view logic. This test proves the SAME journey renders and submits correctly through
an actual Chromium browser -- real form widgets, real file upload, real redirects --
which a Django test client can never demonstrate.

This test creates its own `InterestTopic` explicitly rather than relying on the
`registrations.0002_seed_interest_topics` migration's seed row surviving: `live_server`
transactional tests flush all tables after each test without re-running data
migrations, so only the very first `transaction=True` test in a pytest process would
still see that seed data (see `apps/registrations/tests/test_views.py`'s own module
docstring for the same caveat). Running this file after `test_i18n_rtl_accessibility.py`
in the same invocation would otherwise see an empty `InterestTopic` table.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.documents.tests.factories import make_test_photo
from apps.registrations.models import InterestTopic
from tests.browser.database import database_call
from tests.browser.helpers import fill_date, select_choice

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


def test_algerian_nin_registration_happy_path_reaches_confirmation(
    live_server, page, seeded_open_event, seeded_legal_notices, isolated_private_storage, tmp_path
) -> None:
    database_call(
        lambda: InterestTopic.objects.get_or_create(
            event_edition=seeded_open_event,
            code="STARTUPS",
            defaults={"label": "Startups"},
        )
    )

    _login_participant(live_server, page, "browser-happy-path@example.com")

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

    page.select_option("#id_mobile_country_code", "DZ")
    page.fill("#id_mobile_number", "0551234567")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/professional/**")

    page.fill("#id_organization_name", "Startup Nation")
    select_choice(page, "id_organization_type", "COMPANY")
    page.check("input[name=operating_scope][value=NATIONAL]")
    page.fill("#id_job_title", "Founder")
    page.fill("#id_department", "Management")
    page.select_option("#id_sector", "TECH")
    page.select_option("#id_country_code", "DZ")
    page.fill("#id_organization_website", "https://example.com")
    page.fill("#id_professional_profile_url", "https://example.com/in/profile")
    page.fill("#id_biography", "A short professional biography.")
    photo_path = tmp_path / "photo.png"
    photo_path.write_bytes(make_test_photo().read())
    page.set_input_files("#id_profile_photo", str(photo_path))
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/interests/**")

    page.locator("#main-content input[name=interest_topics]").first.check()
    page.fill("#id_objectives_text", "Meet investors and mentors")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/review/**")

    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/notices/**")

    page.check("#id_accept_privacy_notice")
    page.check("#id_accept_terms")
    page.check("#id_accept_data_processing")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/confirmation/**")

    assert "Registration details" in page.content()
    assert "Request received — awaiting acceptance" in page.content()
    assert "does not mean your participation has been approved" in page.content()
    assert page.locator("strong.ltr-embed").count() == 1
