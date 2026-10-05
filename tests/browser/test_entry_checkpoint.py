"""Real-browser coverage for the online entry checkpoint (Phase 3 Prompt 4).

Proves the two behaviours that only a real browser can show: participant
details are cleared after short operator inactivity while the result
heading stays until acknowledged, and the checkpoint renders right-to-left
with translated text in Arabic. Captures review screenshots into
`var/screenshots/phase3_prompt4/named/`. All data is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_fixture, operational_session_key

pytestmark = pytest.mark.django_db(transaction=True)

SCREENSHOT_DIR = Path(__file__).resolve().parents[2] / "var/screenshots/phase3_prompt4/named"


def _save(page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)


@pytest.fixture
@database_fixture
def checkpoint_world():
    from apps.core.crypto.signing import (
        InMemorySigningKeyProvider,
        set_signing_key_provider_for_testing,
    )
    from apps.entry.tests import factories

    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)
    factories.seed_reference_values()
    event = factories.make_event("ENTBROWSER")
    layout = factories.VenueLayout(event)
    setup = factories.AccreditationSetup(event, layout)
    staff = factories.make_user("staff@example.test")
    factories.publish_key(provider=provider, actor=staff)
    person = factories.make_person("Synthetic Browser Participant")
    registration = factories.make_registration(event=event, person=person)
    factories.assign(registration=registration, setup=setup, actor=staff)
    credential = factories.issue_active_pass(registration=registration, actor=staff)
    admin = factories.make_user(
        "da@example.test", group_name="Entry Device Administrators", event=event
    )
    operator = factories.make_user(
        "gate.operator@example.test",
        group_name="Entry Operators",
        event=event,
        gate=layout.gate_a,
    )
    _device, secret = factories.enroll_device(event=event, layout=layout, admin=admin)
    yield {
        "operator": operator,
        "secret": secret,
        "token": factories.token_for(credential),
        "layout": layout,
    }
    set_signing_key_provider_for_testing(None)


def _open_checkpoint(page, live_server, world) -> None:
    from django.conf import settings

    user = world["operator"]
    session_key = operational_session_key(user)

    page.goto(f"{live_server.url}/healthz")
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_key,
                "url": live_server.url,
            },
            {
                "name": settings.ENTRY_DEVICE_COOKIE_NAME,
                "value": world["secret"],
                "url": f"{live_server.url}/entry/",
            },
        ]
    )
    page.goto(f"{live_server.url}/entry/")
    page.locator("select[name=zone_id]").select_option(str(world["layout"].main.pk))
    page.get_by_role("button", name=re.compile("Start checkpoint session")).click()
    expect(page).to_have_url(re.compile(r"/entry/verify/$"))


def test_participant_details_clear_after_inactivity(live_server, page, settings, checkpoint_world):
    settings.ENTRY_RESULT_CLEAR_SECONDS = 2
    _open_checkpoint(page, live_server, checkpoint_world)
    expect(page.locator("input[name=token]")).to_be_focused()
    page.locator("input[name=token]").fill(checkpoint_world["token"])
    page.keyboard.press("Enter")

    expect(page.locator("#entry-result h1")).to_contain_text("Verified")
    # An allowed result carries no reason line at all.
    expect(page.locator("#entry-result p")).to_have_count(0)
    expect(page.locator("#entry-participant")).to_contain_text("Synthetic Browser Participant")
    expect(page.get_by_role("button", name="Admit", exact=True)).to_be_visible()
    _save(page, "01-entry-result-verified.png")

    # No operator activity: the details and the decision controls go away,
    # the result heading stays until acknowledged.
    expect(page.locator("#entry-participant")).to_have_count(0, timeout=6_000)
    expect(page.get_by_role("button", name="Admit", exact=True)).to_have_count(0)
    expect(page.locator("#entry-result h1")).to_contain_text("Verified")
    expect(page.get_by_text("Participant details cleared after inactivity")).to_be_visible()
    assert "Synthetic Browser Participant" not in page.content()
    assert checkpoint_world["token"] not in page.content()
    _save(page, "02-entry-result-cleared.png")


def test_checkpoint_renders_right_to_left_in_arabic(live_server, page, checkpoint_world):
    _open_checkpoint(page, live_server, checkpoint_world)
    page.locator("select[name=language]").select_option("ar")
    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    expect(page.locator("html")).to_have_attribute("lang", "ar")
    expect(page.locator("h1")).to_have_text("التحقق من الدخول")
    _save(page, "03-entry-verify-arabic-rtl.png")

    page.locator("input[name=token]").fill(checkpoint_world["token"])
    page.keyboard.press("Enter")
    expect(page.locator("#entry-result h1")).to_contain_text("تم التحقق")
    expect(page.get_by_role("button", name="السماح بالدخول", exact=True)).to_be_visible()
    _save(page, "04-entry-result-arabic-rtl.png")
    page.get_by_role("button", name="السماح بالدخول", exact=True).click()
    expect(page.get_by_text("تم تسجيل الدخول: مسموح.").first).to_be_visible()
