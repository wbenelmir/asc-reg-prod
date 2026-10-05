"""Final visual evidence for Phase 3 Prompt 5 (entry, participant pass, badge
operations, device administration, observability) in English, French and
Arabic, at desktop and mobile widths, plus important operational states.

Every capture also asserts the shared layout contract: language and
direction, exactly one h1, decorative-only icons, no horizontal overflow on
mobile, and the Arabic font on Arabic pages. Screenshots go to
`var/test_artifacts/phase3/prompt5-ui/final/`; they are evidence only, not a
claim of human approval. All data is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.test_badge_stock import _build_stock_scenario
from tests.browser.test_badge_stock import _sign_in_operational as _sign_in_ops
from tests.browser.test_digital_entry_pass import _build_active_pass, _sign_in_participant
from tests.browser.test_entry_ui_prompt5 import DESKTOP, MOBILE, TABLET, _open, _scan

pytestmark = pytest.mark.django_db(transaction=True)

FINAL_DIR = Path(__file__).resolve().parents[2] / "var/test_artifacts/phase3/prompt5-ui/final"
LANGUAGES = ("en", "fr", "ar")


def _set_language(page, live_server, language: str) -> None:
    from django.conf import settings

    page.context.add_cookies(
        [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url}]
    )


def _evidence(page, name: str, language: str, viewport) -> None:
    page.set_viewport_size(viewport)
    page.evaluate("document.fonts.ready.then(() => true)")
    html = page.locator("html")
    expect(html).to_have_attribute("lang", language)
    expect(html).to_have_attribute("dir", "rtl" if language == "ar" else "ltr")
    expect(page.locator("h1")).to_have_count(1)
    assert page.locator("svg.asc-icon:not([aria-hidden=true])").count() == 0
    assert page.locator("body.asc-ui").count() == 1, "page is not on the Prompt 5 design system"
    if language == "ar":
        family = page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily")
        assert family.startswith('"Thmanyah Sans"'), family
    if viewport is MOBILE:
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        offenders = page.evaluate(
            """() => {
              const w = document.documentElement.clientWidth;
              const inScroller = (el) => {
                for (let p = el.parentElement; p; p = p.parentElement) {
                  const o = getComputedStyle(p).overflowX;
                  if (o === 'auto' || o === 'scroll') return true;
                }
                return false;
              };
              return [...document.querySelectorAll('body *')]
                .filter((el) => { const r = el.getBoundingClientRect();
                  return r.width && (r.right > w + 1 || r.left < -1) && !inScroller(el); })
                .slice(0, 5).map((el) => el.tagName + '.' + el.className);
            }"""
        )
        assert overflow <= 0, f"{name}: horizontal overflow {overflow}px from {offenders}"
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(FINAL_DIR / name), full_page=True)


def _reload_in(page, live_server, language: str, viewport=DESKTOP) -> None:
    _set_language(page, live_server, language)
    page.set_viewport_size(viewport)
    page.reload()


# ---------------------------------------------------------------------------
# Entry & Security
# ---------------------------------------------------------------------------


def test_entry_verification_screens_in_three_languages(live_server, page, entry_world):
    _open(page, live_server, entry_world, language="en")
    for language in LANGUAGES:
        for viewport, size in ((DESKTOP, "desktop"), (MOBILE, "mobile")):
            _reload_in(page, live_server, language, viewport)
            expect(page.locator("input[name=token]")).to_be_visible()
            _evidence(page, f"entry-01-verify-idle-{language}-{size}.png", language, viewport)
    for language in LANGUAGES:
        for viewport, size in ((DESKTOP, "desktop"), (MOBILE, "mobile")):
            _set_language(page, live_server, language)
            page.set_viewport_size(viewport)
            page.goto(f"{live_server.url}/entry/verify/")
            _scan(page, entry_world["token_ar" if language == "ar" else "token_en"])
            expect(page.locator("#entry-result")).to_have_class(re.compile("asc-result-success"))
            _evidence(page, f"entry-02-result-verified-{language}-{size}.png", language, viewport)


def test_entry_result_and_error_states(live_server, page, entry_world, settings, monkeypatch):
    _open(page, live_server, entry_world, language="ar")
    _scan(page, "NOT-A-DIGITAL-ENTRY-PASS-0000")
    _evidence(page, "entry-03-result-denied-ar-desktop.png", "ar", DESKTOP)

    _set_language(page, live_server, "fr")
    page.goto(f"{live_server.url}/entry/verify/")
    page.locator("#tab-identity").click()
    page.locator("select[name=method]").select_option("PASSPORT")
    page.locator("input[name=value]").fill("SYNTHPASS77")
    page.locator("#pane-identity button[type=submit]").click()
    expect(page.locator("#error-summary")).to_be_visible()
    _evidence(page, "entry-04-verify-validation-error-fr-mobile.png", "fr", MOBILE)

    _set_language(page, live_server, "en")
    page.set_viewport_size(DESKTOP)
    page.goto(f"{live_server.url}/entry/verify/")
    page.locator("#tab-identity").click()
    page.locator("input[name=value]").fill("109990000000000042")
    page.locator("#pane-identity button[type=submit]").click()
    expect(page.locator("#entry-result")).to_have_class(re.compile("asc-result-warning"))
    _evidence(page, "entry-05-result-manual-review-en-desktop.png", "en", DESKTOP)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr("apps.badges.credentials.verify.verify_pass_token", fail)
    _set_language(page, live_server, "fr")
    page.goto(f"{live_server.url}/entry/verify/")
    _scan(page, entry_world["token_en"])
    expect(page.locator("#entry-result")).to_have_class(re.compile("asc-result-neutral"))
    _evidence(page, "entry-06-result-technical-error-fr-desktop.png", "fr", DESKTOP)


def test_entry_candidates_monitor_and_limits(live_server, page, entry_world, settings):
    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
    settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 2
    _open(page, live_server, entry_world, language="en")
    for _ in range(2):
        _scan(page, "NOT-A-DIGITAL-ENTRY-PASS-0000")
        page.locator("#entry-next").click()
    page.locator("input[name=token]").fill("NOT-A-DIGITAL-ENTRY-PASS-0000")
    page.locator("input[name=token]").press("Enter")
    expect(page.locator("#entry-throttled")).to_be_visible()
    _evidence(page, "entry-07-verify-throttled-en-desktop.png", "en", DESKTOP)

    page.goto(f"{live_server.url}/entry/verify/")
    page.locator("#tab-manual").click()
    page.locator("input[name=query]").fill("Synthetic")
    page.locator("#pane-manual button[type=submit]").click()
    expect(page.locator(".asc-candidate").first).to_be_visible()
    _evidence(page, "entry-08-candidates-en-desktop.png", "en", DESKTOP)
    _reload_in(page, live_server, "ar", MOBILE)
    page.goto(f"{live_server.url}/entry/verify/")
    page.locator("#tab-manual").click()
    page.locator("input[name=query]").fill("Synthetic")
    page.locator("#pane-manual button[type=submit]").click()
    _evidence(page, "entry-09-candidates-ar-mobile.png", "ar", MOBILE)

    for language in ("en", "ar"):
        _set_language(page, live_server, language)
        page.set_viewport_size(DESKTOP)
        page.goto(f"{live_server.url}/entry/monitor/")
        expect(page.locator("#anomalies-heading")).to_be_visible()
        _evidence(page, f"entry-10-monitor-{language}-desktop.png", language, DESKTOP)


def test_entry_connection_session_and_degraded_states(live_server, page, entry_world, settings):
    from django.utils import timezone

    from apps.entry.models import EntryOperatorSession, VerificationSample

    layout = entry_world["layout"]
    for _ in range(6):
        database_call(
            lambda: VerificationSample.objects.create(
                occurred_at=timezone.now(),
                event_edition=entry_world["event"],
                gate=layout.gate_a,
                zone=layout.main,
                device=entry_world["device"],
                method="QR",
                result="ALLOWED",
                latency_ms=2600,
            )
        )
    settings.ENTRY_CONNECTION_POLL_SECONDS = 1
    _open(page, live_server, entry_world, language="fr", viewport=TABLET)
    expect(page.locator("#entry-degraded-server")).to_be_visible()
    _evidence(page, "entry-11-verify-degraded-fr-tablet.png", "fr", TABLET)
    database_call(lambda: VerificationSample.objects.all().delete())

    _reload_in(page, live_server, "en")
    page.context.set_offline(True)
    expect(page.locator("#entry-connection")).to_have_attribute("data-state", "offline")
    _evidence(page, "entry-12-verify-offline-en-desktop.png", "en", DESKTOP)
    page.context.set_offline(False)

    _reload_in(page, live_server, "ar")
    database_call(
        lambda: EntryOperatorSession.objects.filter(ended_at__isnull=True).update(
            ended_at=timezone.now(), end_reason="TEST"
        )
    )
    expect(page.locator("#entry-connection")).to_have_attribute(
        "data-state", "session-ended", timeout=10_000
    )
    _evidence(page, "entry-13-verify-session-ended-ar-desktop.png", "ar", DESKTOP)

    # The next request shows the set-up screen again (expired-session state).
    page.goto(f"{live_server.url}/entry/verify/")
    expect(page).to_have_url(re.compile(r"/entry/$"))
    _evidence(page, "entry-14-checkpoint-setup-after-session-end-ar-desktop.png", "ar", DESKTOP)
    _set_language(page, live_server, "en")
    page.set_viewport_size(MOBILE)
    page.reload()
    _evidence(page, "entry-15-checkpoint-setup-en-mobile.png", "en", MOBILE)


def test_entry_unauthorized_device_state(live_server, page, entry_world):
    _open(page, live_server, entry_world, language="fr")
    page.context.clear_cookies(name="asc_entry_device")
    page.goto(f"{live_server.url}/entry/")
    expect(page.locator("h1")).to_be_visible()
    _evidence(page, "entry-16-device-not-enrolled-fr-desktop.png", "fr", DESKTOP)


# ---------------------------------------------------------------------------
# Participant workspace
# ---------------------------------------------------------------------------


def test_participant_pass_in_three_languages(live_server, page):
    person, _credential = _build_active_pass()
    _sign_in_participant(page, live_server, person)
    for language in LANGUAGES:
        for viewport, size in ((DESKTOP, "desktop"), (MOBILE, "mobile")):
            _set_language(page, live_server, language)
            page.set_viewport_size(viewport)
            page.goto(f"{live_server.url}/my-passes/")
            expect(page.locator("img[data-pass-qr]")).to_be_visible()
            # The QR is never mirrored, even in RTL; its caption is not
            # forced LTR (it keeps the page direction).
            qr_parent = page.locator("img[data-pass-qr]").evaluate(
                "img => getComputedStyle(img.parentElement).direction"
            )
            assert qr_parent == "ltr"
            caption = page.locator(".asc-pass-qr > p").evaluate(
                "el => getComputedStyle(el).direction"
            )
            assert caption == ("rtl" if language == "ar" else "ltr")
            _evidence(page, f"pass-01-active-{language}-{size}.png", language, viewport)


def test_participant_pass_empty_state(live_server, page):
    from apps.people.models import Person, PersonStatus

    _build_active_pass()
    lonely = database_call(
        lambda: Person.objects.create(status=PersonStatus.ACTIVE, display_name="Synthetic No Pass")
    )
    _sign_in_participant(page, live_server, lonely)
    for language in ("en", "ar"):
        _set_language(page, live_server, language)
        page.goto(f"{live_server.url}/my-passes/")
        expect(page.locator(".asc-empty")).to_be_visible()
        _evidence(page, f"pass-02-empty-{language}-desktop.png", language, DESKTOP)


# ---------------------------------------------------------------------------
# Badge operations and device administration
# ---------------------------------------------------------------------------


def test_badge_operations_screens(live_server, page):
    scenario = _build_stock_scenario()
    _sign_in_ops(page, live_server, scenario["admin"])
    stock_url = f"{live_server.url}/ops/badges/stock/{scenario['event'].pk}/"
    for language in LANGUAGES:
        _set_language(page, live_server, language)
        page.set_viewport_size(DESKTOP)
        page.goto(stock_url)
        expect(page.locator(".asc-kpi").first).to_be_visible()
        _evidence(page, f"stock-01-dashboard-{language}-desktop.png", language, DESKTOP)
    _set_language(page, live_server, "fr")
    page.goto(stock_url)
    _evidence(page, "stock-02-dashboard-fr-mobile.png", "fr", MOBILE)

    _set_language(page, live_server, "en")
    page.goto(f"{live_server.url}/ops/badges/stock/batches/{scenario['batch'].pk}/")
    _evidence(page, "stock-03-batch-detail-en-desktop.png", "en", DESKTOP)

    issuance_url = (
        f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/"
    )
    for language in ("en", "ar"):
        _set_language(page, live_server, language)
        page.set_viewport_size(DESKTOP)
        page.goto(issuance_url)
        _evidence(page, f"stock-04-issuance-{language}-desktop.png", language, DESKTOP)
    _set_language(page, live_server, "en")
    page.goto(issuance_url)
    page.get_by_role("button", name=re.compile("^Issue", re.I)).click()
    expect(page.locator("[data-issuance-status]").first).to_contain_text("Issued")
    _evidence(page, "stock-05-issuance-issued-en-desktop.png", "en", DESKTOP)
    _set_language(page, live_server, "ar")
    page.reload()
    _evidence(page, "stock-06-issuance-issued-ar-mobile.png", "ar", MOBILE)


def test_credential_and_key_screens(live_server, page):
    from django.contrib.auth.models import Group

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    _person, credential = _build_active_pass()
    admin = database_call(
        lambda: OperationalUser.objects.create_user(
            email="final-evidence-pass@example.test",
            password="__test_password__",  # noqa: S106
            status=OperationalUserStatus.ACTIVE,
            display_name="Synthetic pass administrator",
        )
    )
    for group in ("Pass Administrators", "Credential Key Custodians"):
        database_call(
            lambda group=group: ScopedGroupMembership.objects.create(
                user=admin,
                group=Group.objects.get(name=group),
                event_edition=credential.registration.event_edition
                if group == "Pass Administrators"
                else None,
                granted_by=admin,
            )
        )
    _sign_in_ops(page, live_server, admin)
    for language in ("en", "ar"):
        _set_language(page, live_server, language)
        page.set_viewport_size(DESKTOP)
        page.goto(
            f"{live_server.url}/ops/badges/registrations/{credential.registration_id}/credential/"
        )
        expect(page.locator("[data-pass-status]").first).to_be_visible()
        _evidence(page, f"pass-03-operations-credential-{language}-desktop.png", language, DESKTOP)
    _set_language(page, live_server, "fr")
    page.goto(f"{live_server.url}/ops/badges/verification-keys/")
    _evidence(page, "pass-04-verification-keys-fr-desktop.png", "fr", DESKTOP)


def test_device_administration_and_observability(live_server, page, entry_world, settings):
    from apps.entry.models import EntryDevice

    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
    _open(page, live_server, entry_world, language="en")
    _scan(page, entry_world["token_en"])
    page.locator("#entry-next").click()
    _scan(page, "NOT-A-DIGITAL-ENTRY-PASS-0000")

    from apps.accounts.models import OperationalUser

    admin = database_call(
        lambda: OperationalUser.objects.get(email_normalized="device.admin@example.test")
    )
    page.context.clear_cookies()
    _sign_in_ops(page, live_server, admin)
    event = entry_world["event"]
    device = database_call(lambda: EntryDevice.objects.get(event_edition=event))
    for language in LANGUAGES:
        _set_language(page, live_server, language)
        page.set_viewport_size(DESKTOP)
        page.goto(f"{live_server.url}/ops/entry/events/{event.pk}/observability/")
        expect(page.locator('[data-metric="p95"]')).to_be_visible()
        _evidence(page, f"ops-01-observability-{language}-desktop.png", language, DESKTOP)
    _set_language(page, live_server, "fr")
    page.goto(f"{live_server.url}/ops/entry/events/{event.pk}/observability/")
    _evidence(page, "ops-02-observability-fr-mobile.png", "fr", MOBILE)

    _set_language(page, live_server, "en")
    page.goto(f"{live_server.url}/ops/entry/events/{event.pk}/devices/")
    _evidence(page, "ops-03-device-list-en-desktop.png", "en", DESKTOP)
    _set_language(page, live_server, "ar")
    page.goto(f"{live_server.url}/ops/entry/devices/{device.public_id}/")
    _evidence(page, "ops-04-device-detail-ar-desktop.png", "ar", DESKTOP)
