"""UI/UX Completion Gate, Checkpoint 1: behaviour and visual evidence.

Covers the representative vertical slice: the public/authentication shell
(OTP request and verification, operational sign-in), the complete
registration wizard and confirmation, the participant workspace, the staff
intake list and detail, the review queue and the review case detail, in
English, French and Arabic, at phone, tablet, desktop and wide widths.

Every capture asserts the shared layout contract (`_contract`): language and
direction, exactly one h1, decorative-only icons, the design-system body
class, the Thmanyah Arabic font on Arabic pages, the official logo's
537:240 proportions, and no page-level horizontal overflow at any width.
Screenshots go to `var/test_artifacts/phase3/ui-ux-checkpoint-1/`; they are
evidence for review, not a claim of human approval. All data is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.test import override_settings
from playwright.sync_api import expect

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_call, database_sync
from tests.browser.helpers import (
    fill_date,
    read_date,
    select_choice,
    solve_staff_captcha,
    switch_language,
)
from tests.browser.test_entry_ui_prompt5 import CONTRAST_JS

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "var/test_artifacts/phase3/ui-ux-checkpoint-1"

PHONE = {"width": 360, "height": 780}
MOBILE = {"width": 390, "height": 844}
TABLET = {"width": 820, "height": 1180}
DESKTOP = {"width": 1440, "height": 900}
WIDE = {"width": 1920, "height": 1080}
ALL_WIDTHS = (PHONE, MOBILE, TABLET, DESKTOP, WIDE)

OPS_PASSWORD = "__ui-checkpoint-synthetic__"  # noqa: S105
LOGO_RATIO = 537 / 240

OVERFLOW_JS = """
() => {
  const w = document.documentElement.clientWidth;
  const inScroller = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return true;
    }
    return false;
  };
  const offenders = [...document.querySelectorAll('body *')]
    .filter((el) => { const r = el.getBoundingClientRect();
      return r.width && (r.right > w + 1 || r.left < -1) && !inScroller(el); })
    .slice(0, 5).map((el) => el.tagName + '.' + el.className);
  return {overflow: document.documentElement.scrollWidth - w, offenders};
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_language(page, live_server, language: str) -> None:
    from django.conf import settings

    page.context.add_cookies(
        [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url}]
    )


def _contract(page, language: str, name: str) -> None:
    html = page.locator("html")
    expect(html).to_have_attribute("lang", language)
    expect(html).to_have_attribute("dir", "rtl" if language == "ar" else "ltr")
    expect(page.locator("h1")).to_have_count(1)
    assert page.locator("svg.asc-icon:not([aria-hidden=true])").count() == 0, name
    assert page.locator("body.asc-ui").count() == 1, name
    if language == "ar":
        for selector in ("h1", "body"):
            family = page.locator(selector).first.evaluate("el => getComputedStyle(el).fontFamily")
            assert family.startswith('"Thmanyah Sans"'), (name, selector, family)
    for logo in page.locator("img[src$='asc-logo.svg']").all():
        if logo.is_visible():
            box = logo.bounding_box()
            assert abs(box["width"] / box["height"] - LOGO_RATIO) < 0.02, (name, box)
    result = page.evaluate(OVERFLOW_JS)
    assert result["overflow"] <= 0, f"{name}: horizontal overflow {result}"


def _capture(page, name: str, language: str, viewport) -> None:
    page.set_viewport_size(viewport)
    page.evaluate("document.fonts.ready.then(() => true)")
    # A reload restores the scroll position; full-page captures start at the
    # top so sticky bars are not photographed mid-page.
    page.evaluate("window.scrollTo(0, 0)")
    _contract(page, language, name)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE_DIR / name), full_page=True)


def _in(page, live_server, language: str, url: str | None = None) -> None:
    _set_language(page, live_server, language)
    if url:
        page.goto(url)
    else:
        page.reload()
    page.wait_for_load_state("load")


def _login_participant(live_server, page, email: str) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/workspace/**")


@database_sync
def _ops_superuser(email: str = "ui.checkpoint.ops@example.test"):
    from apps.accounts.models import OperationalUser, OperationalUserStatus

    user = OperationalUser.objects.create_superuser(email=email, password=OPS_PASSWORD)
    user.status = OperationalUserStatus.ACTIVE
    user.display_name = "Synthetic Reviewer"
    user.save(update_fields=["status", "display_name"])
    return user


def _sign_in_operational(live_server, page, user) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", user.email_normalized)
    page.fill("#id_password", OPS_PASSWORD)
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/ops/registrations/**")


@pytest.fixture
@database_sync
def interest_topics(seeded_open_event):
    from apps.registrations.models import InterestTopic

    for code, label in (
        ("STARTUPS", "Startups and venture building"),
        ("AI", "Artificial intelligence"),
        ("ROBOTICS", "Robotics"),
        ("INVESTMENT", "Investment and funding"),
    ):
        InterestTopic.objects.get_or_create(
            event_edition=seeded_open_event, code=code, defaults={"label": label}
        )


@pytest.fixture
@database_sync
def review_world(db):
    """A synthetic event with registrations in several states, their review
    cases, and one case with checklist evidence and a note."""
    from django.utils import timezone

    from apps.core.models import Country
    from apps.events.models import EventEdition
    from apps.organizations.models import Organization, OrganizationType
    from apps.registrations.models import (
        RegistrationInternalStatus,
        RegistrationProfile,
        RegistrationPublicStatus,
    )
    from apps.reviews.models import ReviewCaseType
    from apps.reviews.services import open_review_case
    from apps.reviews.tests.conftest import make_registration

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    event = EventEdition.objects.create(
        code="ASCUIX26",
        name="African Startup Conference 2026",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    organization = Organization.objects.create(
        official_name="Synthetic Innovation Lab",
        normalized_name="synthetic innovation lab",
        organization_type=OrganizationType.OTHER,
    )
    people = (
        ("Amine", "Benali", RegistrationPublicStatus.UNDER_REVIEW, "REVIEW_IN_PROGRESS"),
        ("سارة", "بن يوسف", RegistrationPublicStatus.SUBMITTED, "PENDING_ASSIGNMENT"),
        (
            "Lina",
            "Haddad",
            RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
            "AWAITING_APPLICANT",
        ),
        ("Karim", "Mansouri", RegistrationPublicStatus.APPROVED, "QUALIFICATION_COMPLETE"),
    )
    cases = []
    for index, (given, family, public, internal) in enumerate(people, start=1):
        registration = make_registration(
            event=event, organization=organization, public_reference=f"ASC26-UI-{index:04d}"
        )
        registration.public_status = public
        registration.internal_status = getattr(RegistrationInternalStatus, internal)
        registration.save(update_fields=["public_status", "internal_status"])
        RegistrationProfile.objects.create(
            registration=registration,
            submitted_given_names=given,
            submitted_family_name=family,
            submitted_full_name=f"{given} {family}",
            nationality_code_id="DZ",
        )
        cases.append(
            open_review_case(
                registration=registration,
                case_type=ReviewCaseType.STANDARD,
                queue_code="GENERAL",
            )
        )
    return {"event": event, "cases": cases}


# ---------------------------------------------------------------------------
# Public and authentication shell
# ---------------------------------------------------------------------------


def test_public_and_sign_in_pages_evidence(live_server, page) -> None:
    for language in ("en", "fr", "ar"):
        _in(page, live_server, language, f"{live_server.url}/accounts/start/")
        _capture(page, f"auth-01-otp-request-{language}-desktop.png", language, DESKTOP)
        _capture(page, f"auth-01-otp-request-{language}-mobile.png", language, MOBILE)
    _capture(page, "auth-01-otp-request-ar-tablet.png", "ar", TABLET)
    _in(page, live_server, "en")
    _capture(page, "auth-01-otp-request-en-wide.png", "en", WIDE)

    # Validation error state (French, phone width): summary receives focus.
    _in(page, live_server, "fr")
    page.set_viewport_size(PHONE)
    page.locator("#main-content button[type=submit]").click()
    expect(page.locator("#error-summary")).to_be_focused()
    _capture(page, "auth-02-otp-request-error-fr-phone.png", "fr", PHONE)

    # Verification step; the email stays left-to-right inside Arabic text.
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        _in(page, live_server, "ar", f"{live_server.url}/accounts/start/")
        page.fill("#id_email", "synthetic.participant@example.test")
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/verify/**")
        expect(page.locator("#id_code")).to_have_attribute("dir", "ltr")
        email = page.locator(".asc-auth-head bdi[dir=ltr]")
        expect(email).to_have_text("synthetic.participant@example.test")
        _capture(page, "auth-03-otp-verify-ar-desktop.png", "ar", DESKTOP)
        page.fill("#id_code", "000000")
        page.locator("#main-content button[type=submit]").click()
        expect(page.locator("#error-summary")).to_be_visible()
        _capture(page, "auth-03-otp-verify-wrong-code-ar-mobile.png", "ar", MOBILE)

    _in(page, live_server, "en", f"{live_server.url}/accounts/ops/sign-in/")
    _capture(page, "auth-04-ops-sign-in-en-desktop.png", "en", DESKTOP)
    page.fill("#id_email", "nobody@example.test")
    page.fill("#id_password", "not-the-password")
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    expect(page.locator("#error-summary")).to_be_visible()
    _capture(page, "auth-04-ops-sign-in-error-en-mobile.png", "en", MOBILE)
    _in(page, live_server, "ar")
    _capture(page, "auth-04-ops-sign-in-ar-desktop.png", "ar", DESKTOP)


def test_public_shell_keeps_boost_and_keyboard_order(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    assert page.locator("#main-content").get_attribute("hx-boost") == "true"
    assert page.locator("header").get_attribute("hx-boost") is None
    # Brand panel is present from the lg breakpoint, carries no control.
    page.set_viewport_size(DESKTOP)
    expect(page.locator(".asc-auth-aside")).to_be_visible()
    assert (
        page.locator(".asc-auth-aside a, .asc-auth-aside button, .asc-auth-aside input").count()
        == 0
    )
    page.set_viewport_size(MOBILE)
    expect(page.locator(".asc-auth-aside")).to_be_hidden()
    # Focus is visible on the field and the primary action. Since UX-C2 the
    # ALTCHA widget's labelled checkbox sits between them in the tab order.
    page.locator("#id_email").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.type") == "checkbox"
    assert page.evaluate("!!document.activeElement.closest('altcha-widget')")
    # Since UXR-C1 (UXR-F02) the Privacy Notice and Terms link under the
    # form comes next, in reading order, before the primary action.
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('[data-legal-link]') !== null")
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.type")
    assert focused == "submit"
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == "solid"


# ---------------------------------------------------------------------------
# Registration wizard and participant workspace
# ---------------------------------------------------------------------------


def test_registration_wizard_evidence_in_three_languages(
    live_server,
    page,
    seeded_open_event,
    seeded_legal_notices,
    interest_topics,
    isolated_private_storage,
    tmp_path,
) -> None:
    from apps.documents.tests.factories import make_test_photo

    _login_participant(live_server, page, "ui-wizard@example.test")
    _in(page, live_server, "en")
    _capture(page, "wizard-00-workspace-empty-en-desktop.png", "en", DESKTOP)

    identity = f"{live_server.url}/register/identity/"
    _in(page, live_server, "en", identity)
    _capture(page, "wizard-01-identity-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-01-identity-ar-desktop.png", "ar", DESKTOP)
    _capture(page, "wizard-01-identity-ar-mobile.png", "ar", MOBILE)
    page.locator("#main-content button[type=submit]").click()
    expect(page.locator("#error-summary")).to_be_visible()
    _capture(page, "wizard-01-identity-errors-ar-phone.png", "ar", PHONE)
    _in(page, live_server, "fr", identity)
    page.check("#id_identity_path_1")
    _capture(page, "wizard-01-identity-passport-fr-tablet.png", "fr", TABLET)

    _in(page, live_server, "en", identity)
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-01-01")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    _capture(page, "wizard-01-identity-nin-filled-en-mobile.png", "en", MOBILE)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/contact/**")

    _capture(page, "wizard-02-contact-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-02-contact-ar-mobile.png", "ar", MOBILE)
    page.select_option("#id_mobile_country_code", "DZ")
    page.fill("#id_mobile_number", "0551234567")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/professional/**")

    _in(page, live_server, "en")
    _capture(page, "wizard-03-professional-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-03-professional-ar-desktop.png", "ar", DESKTOP)
    page.fill("#id_organization_name", "Startup Nation")
    select_choice(page, "id_organization_type", "COMPANY")
    page.check("input[name=operating_scope][value=NATIONAL]")
    page.fill("#id_job_title", "Founder")
    page.fill("#id_department", "Management")
    page.select_option("#id_sector", "TECH")
    page.select_option("#id_country_code", "DZ")
    page.fill("#id_organization_website", "https://example.com")
    page.fill("#id_professional_profile_url", "https://example.com/in/profile")
    page.fill("#id_biography", "A short synthetic professional biography.")
    photo_path = tmp_path / "synthetic-photo.png"
    photo_path.write_bytes(make_test_photo().read())
    page.set_input_files("#id_profile_photo", str(photo_path))
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/interests/**")

    _in(page, live_server, "fr")
    _capture(page, "wizard-04-interests-fr-desktop.png", "fr", DESKTOP)
    _in(page, live_server, "ar")
    page.locator("#main-content input[name=interest_topics]").nth(0).check()
    page.locator("#main-content input[name=interest_topics]").nth(1).check()
    page.fill("#id_objectives_text", "Meet investors and mentors")
    _capture(page, "wizard-04-interests-ar-mobile.png", "ar", MOBILE)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/review/**")

    for language, viewport, size in (
        ("en", DESKTOP, "desktop"),
        ("ar", DESKTOP, "desktop"),
        ("ar", MOBILE, "mobile"),
        ("fr", WIDE, "wide"),
    ):
        _in(page, live_server, language)
        _capture(page, f"wizard-05-review-{language}-{size}.png", language, viewport)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/notices/**")

    _in(page, live_server, "en")
    _capture(page, "wizard-06-notices-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-06-notices-ar-mobile.png", "ar", MOBILE)
    page.check("#id_accept_privacy_notice")
    page.check("#id_accept_terms")
    page.check("#id_accept_data_processing")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/confirmation/**")

    _in(page, live_server, "en")
    _capture(page, "wizard-07-confirmation-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-07-confirmation-ar-desktop.png", "ar", DESKTOP)
    _capture(page, "wizard-07-confirmation-ar-phone.png", "ar", PHONE)

    _in(page, live_server, "en", f"{live_server.url}/workspace/")
    _capture(page, "wizard-08-workspace-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "wizard-08-workspace-ar-mobile.png", "ar", MOBILE)


def test_wizard_stepper_semantics_and_back_navigation(live_server, page, seeded_open_event) -> None:
    _login_participant(live_server, page, "ui-stepper@example.test")
    page.goto(f"{live_server.url}/register/identity/")
    stepper = page.locator("nav.asc-stepper")
    expect(stepper).to_have_accessible_name(re.compile(r"Step 1 of 6"))
    expect(stepper.locator("li[aria-current=step]")).to_contain_text("Identity")
    # Nothing beyond the furthest step reached is linked.
    assert stepper.locator("a").count() == 0
    # The first step has no Back link; the form has exactly one submit.
    assert page.locator(".asc-form-actions a").count() == 0
    assert page.locator("#main-content button[type=submit]").count() == 1

    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")
    fill_date(page, "id_date_of_birth", "1990-01-01")
    page.select_option("#id_nationality_code", "DZ")
    page.select_option("#id_country_of_residence", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/register/contact/**")
    expect(stepper).to_have_accessible_name(re.compile(r"Step 2 of 6"))
    done = stepper.locator("li.asc-step-done a")
    expect(done).to_have_count(1)
    expect(done).to_contain_text("(completed)")
    # Back returns to the previous step without submitting anything.
    page.locator(".asc-form-actions a").click()
    page.wait_for_url("**/register/identity/**")
    expect(page.locator("#id_given_names")).to_have_value("Amine")
    # Editing an earlier step keeps the furthest step reachable.
    expect(page.locator("nav.asc-stepper li.asc-step-available a")).to_contain_text("Contact")


def test_wizard_has_no_overflow_at_any_supported_width(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "ui-widths@example.test")
    for language in ("en", "ar"):
        _in(page, live_server, language, f"{live_server.url}/register/identity/")
        page.check("#id_identity_path_1")
        for viewport in ALL_WIDTHS:
            page.set_viewport_size(viewport)
            result = page.evaluate(OVERFLOW_JS)
            assert result["overflow"] <= 0, (language, viewport, result)


def test_language_switch_keeps_values_on_the_redesigned_wizard(
    live_server, page, seeded_open_event
) -> None:
    """EN -> FR -> AR -> EN on the redesigned identity step keeps every
    safe field, and never the NIN (F5 regression on the new markup)."""
    _login_participant(live_server, page, "ui-lang@example.test")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "بن علي")
    fill_date(page, "id_date_of_birth", "1990-01-01")
    page.select_option("#id_nationality_code", "DZ")
    page.check("#id_identity_path_0")
    page.fill("#id_nin_value", "123456789012345678")
    for language in ("fr", "ar", "en"):
        switch_language(page, language)
        assert page.locator("html").get_attribute("lang") == language
        assert page.locator("#id_given_names").input_value() == "Amine"
        assert page.locator("#id_family_name").input_value() == "بن علي"
        assert read_date(page, "id_date_of_birth") == "1990-01-01"
        assert page.locator("#id_nationality_code").input_value() == "DZ"
        assert page.locator("#id_nin_value").input_value() == ""


# ---------------------------------------------------------------------------
# Operations: intake list/detail, review queue, review case detail
# ---------------------------------------------------------------------------


def test_operations_list_and_detail_evidence(live_server, page, review_world) -> None:
    user = _ops_superuser()
    _sign_in_operational(live_server, page, user)
    intake = f"{live_server.url}/ops/registrations/"
    _in(page, live_server, "en", intake)
    expect(page.locator(".asc-sectionnav a[aria-current=page]")).to_have_attribute(
        "data-section", "registrations"
    )
    _capture(page, "ops-01-intake-list-en-desktop.png", "en", DESKTOP)
    _capture(page, "ops-01-intake-list-en-wide.png", "en", WIDE)
    _in(page, live_server, "ar")
    _capture(page, "ops-01-intake-list-ar-desktop.png", "ar", DESKTOP)
    _capture(page, "ops-01-intake-list-ar-mobile.png", "ar", MOBILE)

    registration = database_call(lambda: review_world["cases"][0].registration)
    _in(page, live_server, "fr", f"{live_server.url}/ops/registrations/{registration.pk}/")
    _capture(page, "ops-02-intake-detail-fr-desktop.png", "fr", DESKTOP)
    _in(page, live_server, "ar")
    _capture(page, "ops-02-intake-detail-ar-mobile.png", "ar", MOBILE)

    queue = f"{live_server.url}/ops/reviews/queue/"
    _in(page, live_server, "en", queue)
    _capture(page, "ops-03-review-queue-en-desktop.png", "en", DESKTOP)
    _in(page, live_server, "fr")
    _capture(page, "ops-03-review-queue-fr-tablet.png", "fr", TABLET)
    _in(page, live_server, "ar")
    _capture(page, "ops-03-review-queue-ar-desktop.png", "ar", DESKTOP)
    _capture(page, "ops-03-review-queue-ar-mobile.png", "ar", MOBILE)
    _in(page, live_server, "en", f"{queue}?q=zz-no-match")
    expect(page.locator(".asc-empty")).to_be_visible()
    _capture(page, "ops-03-review-queue-empty-en-desktop.png", "en", DESKTOP)

    case = review_world["cases"][0]
    detail = f"{live_server.url}/ops/reviews/cases/{case.pk}/"
    _in(page, live_server, "en", detail)
    _capture(page, "ops-04-case-detail-en-desktop.png", "en", DESKTOP)
    _capture(page, "ops-04-case-detail-en-wide.png", "en", WIDE)
    _in(page, live_server, "ar")
    _capture(page, "ops-04-case-detail-ar-desktop.png", "ar", DESKTOP)
    _capture(page, "ops-04-case-detail-ar-tablet.png", "ar", TABLET)
    _in(page, live_server, "en")
    _capture(page, "ops-04-case-detail-en-mobile.png", "en", MOBILE)

    # Mobile section menu: collapsed by default, opens on its toggle.
    menu = page.locator("details.asc-sectionnav-menu")
    assert menu.evaluate("el => el.open") is False
    page.locator(".asc-sectionnav-toggle").click()
    assert menu.evaluate("el => el.open") is True
    page.screenshot(path=str(EVIDENCE_DIR / "ops-05-section-menu-open-en-mobile.png"))


def test_operations_pages_have_no_overflow_and_readable_chips(
    live_server, page, review_world
) -> None:
    user = _ops_superuser("ui.widths.ops@example.test")
    _sign_in_operational(live_server, page, user)
    case = review_world["cases"][0]
    urls = (
        f"{live_server.url}/ops/registrations/",
        f"{live_server.url}/ops/reviews/queue/",
        f"{live_server.url}/ops/reviews/cases/{case.pk}/",
    )
    for language in ("en", "ar"):
        for url in urls:
            _in(page, live_server, language, url)
            for viewport in ALL_WIDTHS:
                page.set_viewport_size(viewport)
                result = page.evaluate(OVERFLOW_JS)
                assert result["overflow"] <= 0, (language, url, viewport, result)
            for chip in page.locator(".asc-chip").all():
                ratio = chip.evaluate(CONTRAST_JS)
                assert ratio >= 4.5, (url, chip.inner_text(), ratio)


def test_touch_targets_in_the_slice_are_at_least_44px(live_server, page, review_world) -> None:
    user = _ops_superuser("ui.targets.ops@example.test")
    _sign_in_operational(live_server, page, user)
    case = review_world["cases"][0]
    page.set_viewport_size(MOBILE)
    selectors = ".btn, .asc-sectionnav-toggle, .asc-breadcrumbs a, .asc-summary-edit"
    for url in (
        f"{live_server.url}/ops/reviews/queue/",
        f"{live_server.url}/ops/reviews/cases/{case.pk}/",
    ):
        page.goto(url)
        for element in page.locator(selectors).all():
            if not element.is_visible():
                continue
            box = element.bounding_box()
            # Inline text links inside breadcrumbs are exempt from the size
            # rule (WCAG 2.5.8 inline exception); buttons and toggles are not.
            if element.evaluate("el => el.closest('.asc-breadcrumbs') !== null"):
                continue
            assert box["height"] >= 44 - 0.5, (url, element.inner_text(), box)


def test_status_is_never_colour_only(live_server, page, review_world) -> None:
    user = _ops_superuser("ui.chips.ops@example.test")
    _sign_in_operational(live_server, page, user)
    page.goto(f"{live_server.url}/ops/registrations/")
    chips = page.locator(".asc-chip")
    assert chips.count() >= 8
    for chip in chips.all():
        assert chip.inner_text().strip(), "every chip carries a text label"
        assert chip.locator("svg.asc-icon[aria-hidden=true]").count() == 1
