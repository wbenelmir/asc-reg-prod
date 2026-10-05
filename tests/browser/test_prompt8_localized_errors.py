"""Real-browser evidence for Phase 3 Prompt 8 (P8-01, with P8-02).

Three operational failures, rendered in a real Chromium instance against
`live_server` with synthetic data only, now show a translated, safe message
instead of the service's raw English exception text:

1. French -- a physical badge refused for a withdrawn Registration Context
   (P8-02's refusal, localized by P8-01);
2. Arabic RTL -- a physical badge refused for lack of stock at the chosen
   location;
3. Arabic RTL -- a device registration refused because its expiry outlives
   the event edition.

Screenshots are review evidence only, written to their own folder so they
never overwrite earlier approved evidence:
`var/test_artifacts/phase3/prompt8-corrections/`.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import switch_language
from tests.browser.test_badge_stock import _build_stock_scenario, _sign_in_operational

pytestmark = pytest.mark.django_db(transaction=True)

SCREENSHOT_DIR = (
    Path(__file__).resolve().parents[2] / "var/test_artifacts/phase3/prompt8-corrections"
)


def _save(page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)


def _notice(page):
    return page.locator(".asc-notice-danger").first


def test_french_badge_refusal_for_a_withdrawn_context(live_server, page) -> None:
    from apps.reviews.services import withdraw_registration

    scenario = _build_stock_scenario()
    registration = scenario["registration"]
    database_call(
        lambda: withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration.version,
        )
    )
    _sign_in_operational(page, live_server, scenario["admin"])
    page.goto(f"{live_server.url}/ops/badges/registrations/{registration.pk}/badge/")
    switch_language(page, "fr")
    expect(page.locator("html")).to_have_attribute("lang", "fr")
    page.locator("form").filter(has=page.locator("#issue-location")).locator(
        "button[type=submit]"
    ).click()

    expect(_notice(page)).to_contain_text(
        "Cette inscription n’est plus approuvée : aucun badge physique ne peut lui être "
        "remis ni remplacé.",
        timeout=10_000,
    )
    body = page.content()
    assert "no physical badge may be issued" not in body
    assert "no longer approved" not in body
    _save(page, "01-fr-badge-refused-withdrawn-context.png")


def test_arabic_badge_refusal_for_insufficient_stock(live_server, page) -> None:
    from apps.badges.models import StockLocation

    scenario = _build_stock_scenario()
    empty = database_call(
        lambda: StockLocation.objects.get(event_edition=scenario["event"], code="GATE-A")
    )
    _sign_in_operational(page, live_server, scenario["admin"])
    page.goto(f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/")
    switch_language(page, "ar")
    expect(page.locator("html")).to_have_attribute("dir", "rtl")
    page.locator("#issue-location").select_option(str(empty.pk))
    page.locator("form").filter(has=page.locator("#issue-location")).locator(
        "button[type=submit]"
    ).click()

    expect(_notice(page)).to_contain_text(
        "لا يوجد مخزون متاح كافٍ من نوع الشارة هذا في هذا الموقع.", timeout=10_000
    )
    expect(page.locator("html")).to_have_attribute("dir", "rtl")
    assert "There is no stock of this Badge Type" not in page.content()
    _save(page, "02-ar-badge-refused-insufficient-stock.png")


def test_arabic_device_registration_refused_after_the_event(live_server, page, entry_world):
    from apps.entry.tests import factories

    event = entry_world["event"]
    layout = entry_world["layout"]
    admin = database_call(
        lambda: factories.make_user(
            "prompt8.device.admin@example.test",
            group_name="Entry Device Administrators",
            event=event,
        )
    )
    _sign_in_operational(page, live_server, admin)
    page.goto(f"{live_server.url}/ops/entry/events/{event.pk}/devices/")
    switch_language(page, "ar")
    expect(page.locator("html")).to_have_attribute("dir", "rtl")

    too_late = (event.ends_at + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    page.locator("input[name=public_name]").fill("North Gate tablet 9")
    page.locator("input[name=expires_at]").fill(too_late)
    page.locator("select[name=gate_id]").select_option(str(layout.gate_a.pk))
    page.locator(f"input[name=zone_ids][value='{layout.main.pk}']").check()
    page.locator("input[name=verification_methods][value=QR]").check()
    page.get_by_role("button", name=re.compile("تسجيل الجهاز")).click()

    expect(_notice(page)).to_contain_text(
        "لا يمكن أن يبقى الجهاز مسجّلًا بعد انتهاء دورة الحدث الخاصة به.", timeout=10_000
    )
    assert "cannot outlive its event edition" not in page.content()
    _save(page, "03-ar-device-registration-refused-after-event.png")
