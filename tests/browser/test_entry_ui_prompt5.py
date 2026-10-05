"""Real-browser coverage for the Phase 3 Prompt 5 entry experience.

The verification screen and its result states are the design-checkpoint
screen. Each test asserts the behaviour a reviewer should be able to rely
on -- RTL/LTR layout, keyboard and scanner behaviour, accessible
announcements, contrast, target size, reflow, privacy of submitted values,
and every important state -- and saves review screenshots to
`var/test_artifacts/phase3/prompt5-ui/checkpoint/` as a side effect.

Screenshots are evidence only; they are not a claim of human approval.
All data is synthetic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call, operational_session_key

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT_DIR = ROOT / "var/test_artifacts/phase3/prompt5-ui/checkpoint"

DESKTOP = {"width": 1440, "height": 900}
TABLET = {"width": 1024, "height": 768}
MOBILE = {"width": 390, "height": 844}

SYNTHETIC_NAME_EN = "Synthetic Participant Nadia"
SYNTHETIC_NAME_AR = "مشاركة تجريبية نادية"
SYNTHETIC_NIN = "109990000000000042"

# WCAG 2.2 relative-luminance contrast, evaluated in the page.
CONTRAST_JS = """
(el) => {
  const parse = (c) => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  let node = el, bg = null;
  while (node) {
    const c = getComputedStyle(node).backgroundColor;
    if (c && !c.startsWith('rgba(0, 0, 0, 0)') && c !== 'transparent') { bg = c; break; }
    node = node.parentElement;
  }
  const fg = lum(parse(getComputedStyle(el).color));
  const back = lum(parse(bg || 'rgb(255,255,255)'));
  const [hi, lo] = fg > back ? [fg, back] : [back, fg];
  return (hi + 0.05) / (lo + 0.05);
}
"""


def _shot(page, name: str) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(CHECKPOINT_DIR / name), full_page=True)


def _open(page, live_server, entry_world, *, language="en", viewport=DESKTOP) -> None:
    from django.conf import settings

    page.set_viewport_size(viewport)
    user = entry_world["operator"]
    session_key = operational_session_key(user)
    page.goto(f"{live_server.url}/healthz")
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_key,
                "url": live_server.url,
            },
            {"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url},
            {
                "name": settings.ENTRY_DEVICE_COOKIE_NAME,
                "value": entry_world["secret"],
                "url": f"{live_server.url}/entry/",
            },
        ]
    )
    page.goto(f"{live_server.url}/entry/")
    page.locator("select[name=zone_id]").select_option(str(entry_world["layout"].main.pk))
    page.locator("form:has(input[name=action][value=start]) button[type=submit]").click()
    expect(page).to_have_url(re.compile(r"/entry/verify/$"))
    page.evaluate("document.fonts.ready.then(() => true)")


def _scan(page, value: str) -> None:
    field = page.locator("input[name=token]")
    field.fill(value)
    field.press("Enter")
    expect(page.locator("#entry-result")).to_be_visible()


def _assert_no_horizontal_scroll(page) -> None:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, f"horizontal overflow of {overflow}px"


def _assert_targets_at_least(page, selector: str, minimum: int = 44) -> None:
    for box in page.locator(selector).evaluate_all(
        "els => els.filter(e => e.offsetParent !== null).map(e => e.getBoundingClientRect().height)"
    ):
        assert box >= minimum, f"{selector} target height {box}px < {minimum}px"


# ---------------------------------------------------------------------------
# Idle verification screen (empty state) in both directions and sizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "viewport", "name"),
    [
        ("en", DESKTOP, "01-verify-idle-en-desktop.png"),
        ("ar", DESKTOP, "02-verify-idle-ar-desktop.png"),
        ("en", MOBILE, "03-verify-idle-en-mobile.png"),
        ("ar", MOBILE, "04-verify-idle-ar-mobile.png"),
    ],
)
def test_verify_screen_layout_direction_and_scanner_readiness(
    live_server, page, entry_world, language, viewport, name
):
    _open(page, live_server, entry_world, language=language, viewport=viewport)
    html = page.locator("html")
    expect(html).to_have_attribute("dir", "rtl" if language == "ar" else "ltr")
    expect(html).to_have_attribute("lang", language)
    expect(page.locator("h1")).to_have_count(1)
    expect(page.locator("header.asc-appbar")).to_be_visible()
    expect(page.locator("main#main-content")).to_be_visible()
    expect(page.locator("nav.asc-subnav a[aria-current=page]")).to_have_count(1)
    # Scanner-friendly: the QR field has focus and says so.
    expect(page.locator("input[name=token]")).to_be_focused()
    expect(page.locator("[data-scan-status]")).to_have_attribute("data-ready", "true")
    # Checkpoint identity and device scope are always visible.
    context = page.locator("section.asc-context")
    expect(context).to_contain_text("North Gate")
    expect(context).to_contain_text("North Gate tablet 1")
    expect(page.locator(".asc-chip-list li")).to_have_count(5)
    # Dynamic values are isolated, never whole components forced to LTR.
    context_values = context.locator(".asc-meta-value bdi")
    assert context_values.count() >= 3
    assert context.locator("bdi[dir=ltr].asc-code").inner_text() == "GA"
    assert context.evaluate("el => getComputedStyle(el).direction") == (
        "rtl" if language == "ar" else "ltr"
    )
    # Every scope chip is fully visible: wrapped, never clipped or scrolled
    # away (UI checkpoint correction 1).
    width = page.evaluate("document.documentElement.clientWidth")
    for box in page.locator(".asc-chip-list li").evaluate_all(
        "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.left, r.right]; })"
    ):
        assert box[0] >= 0 and box[1] <= width, f"chip clipped at {box} (viewport {width})"
    chip_list = page.locator(".asc-chip-list")
    assert chip_list.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    expect(page.locator("#entry-connection")).to_have_attribute("data-state", "online")
    # Icons are decorative; the logo keeps its proportions.
    assert page.locator("svg.asc-icon:not([aria-hidden=true])").count() == 0
    ratio = page.locator(".asc-brand img").evaluate(
        "img => img.getBoundingClientRect().width / img.getBoundingClientRect().height"
    )
    assert abs(ratio - 537 / 240) < 0.05
    if language == "ar":
        expect(page.locator("h1")).to_have_text("التحقق من الدخول")
        assert page.evaluate("document.fonts.check('16px \"Thmanyah Sans\"', 'تحقق')")
        family = page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily")
        assert family.startswith('"Thmanyah Sans"')
    else:
        family = page.locator("h1").evaluate("el => getComputedStyle(el).fontFamily")
        assert "Thmanyah" not in family
    _assert_targets_at_least(page, "main button, main .btn, .asc-appbar-btn, .asc-subnav a")
    if viewport is MOBILE:
        _assert_no_horizontal_scroll(page)
    _shot(page, name)


def test_scanner_keystrokes_return_focus_to_the_qr_field(live_server, page, entry_world):
    _open(page, live_server, entry_world)
    page.locator("body").click(position={"x": 5, "y": 400})
    page.evaluate("document.activeElement.blur()")
    expect(page.locator("[data-scan-status]")).to_have_attribute("data-ready", "false")
    page.keyboard.type("abc")
    expect(page.locator("input[name=token]")).to_be_focused()
    expect(page.locator("input[name=token]")).to_have_value("abc")


# ---------------------------------------------------------------------------
# Results: success, denial, manual review, technical error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "viewport", "name"),
    [
        ("en", DESKTOP, "05-result-verified-en-desktop.png"),
        ("ar", DESKTOP, "06-result-verified-ar-desktop.png"),
        ("en", MOBILE, "07-result-verified-en-mobile.png"),
        ("ar", MOBILE, "08-result-verified-ar-mobile.png"),
    ],
)
def test_verified_result_is_announced_and_admit_is_the_focused_action(
    live_server, page, entry_world, language, viewport, name
):
    _open(page, live_server, entry_world, language=language, viewport=viewport)
    _scan(page, entry_world["token_ar" if language == "ar" else "token_en"])
    result = page.locator("#entry-result")
    expect(result).to_have_class(re.compile(r"\basc-result-success\b"))
    expect(result.locator(".asc-result-icon use")).to_have_attribute(
        "href", re.compile(r"#i-check-circle$")
    )
    expect(page.locator("#entry-result p")).to_have_count(0)  # no "No reason" line
    heading = result.locator("h1").inner_text()
    expect(page.locator("#entry-announcer-polite")).to_have_text(re.compile(re.escape(heading)))
    admit = page.locator("button[type=submit].btn-success")
    expect(admit).to_be_focused()
    assert result.locator("h1").evaluate(CONTRAST_JS) >= 4.5
    assert admit.evaluate(CONTRAST_JS) >= 4.5
    name_text = SYNTHETIC_NAME_AR if language == "ar" else SYNTHETIC_NAME_EN
    expect(page.locator("#entry-participant")).to_contain_text(name_text)
    # The registration reference is an isolated LTR code, the name an
    # isolated run with its own direction (UI checkpoint correction 2).
    reference = page.locator("#entry-participant bdi[dir=ltr].asc-code").first
    assert reference.evaluate("el => getComputedStyle(el).direction") == "ltr"
    assert reference.evaluate("el => getComputedStyle(el).unicodeBidi") == "isolate"
    name_bdi = page.locator("#entry-participant .asc-dd-lead bdi")
    expected_direction = "rtl" if language == "ar" else "ltr"
    assert name_bdi.evaluate("el => getComputedStyle(el).direction") == expected_direction
    assert entry_world["token_en"] not in page.content()
    _assert_targets_at_least(page, "main button, main .btn")
    if viewport is MOBILE:
        _assert_no_horizontal_scroll(page)
    _shot(page, name)


@pytest.mark.parametrize(
    ("language", "viewport", "name"),
    [
        ("en", DESKTOP, "09-result-denied-en-desktop.png"),
        ("ar", MOBILE, "10-result-denied-ar-mobile.png"),
    ],
)
def test_invalid_credential_is_an_assertive_denial_without_details(
    live_server, page, entry_world, language, viewport, name
):
    _open(page, live_server, entry_world, language=language, viewport=viewport)
    _scan(page, "NOT-A-DIGITAL-ENTRY-PASS-0000")
    result = page.locator("#entry-result")
    expect(result).to_have_class(re.compile(r"\basc-result-danger\b"))
    expect(result).to_have_attribute("data-announce", "assertive")
    expect(page.locator("#entry-announcer-assertive")).not_to_be_empty()
    expect(page.locator("#entry-participant")).to_have_count(0)
    assert result.locator("h1").evaluate(CONTRAST_JS) >= 4.5
    assert "NOT-A-DIGITAL-ENTRY-PASS-0000" not in page.content()
    _shot(page, name)


def test_declared_identity_asks_for_a_manual_check(live_server, page, entry_world):
    _open(page, live_server, entry_world)
    page.get_by_role("tab", name=re.compile("NIN or passport")).click()
    page.locator("input[name=value]").fill(SYNTHETIC_NIN)
    page.locator("#pane-identity button[type=submit]").click()
    result = page.locator("#entry-result")
    expect(result).to_have_class(re.compile(r"\basc-result-warning\b"))
    expect(result.locator("h1")).to_have_text("Manual verification required")
    expect(page.locator("#entry-participant")).to_contain_text(SYNTHETIC_NAME_EN)
    assert SYNTHETIC_NIN not in page.content()
    assert result.locator("h1").evaluate(CONTRAST_JS) >= 4.5
    _shot(page, "11-result-manual-review-en-desktop.png")


def test_technical_error_is_distinct_and_tells_the_operator_what_to_do(
    live_server, page, entry_world, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic outage")

    monkeypatch.setattr("apps.badges.credentials.verify.verify_pass_token", fail)
    _open(page, live_server, entry_world)
    _scan(page, entry_world["token_en"])
    result = page.locator("#entry-result")
    expect(result).to_have_class(re.compile(r"\basc-result-neutral\b"))
    expect(result.locator(".asc-result-icon use")).to_have_attribute(
        "href", re.compile(r"#i-alert-octagon$")
    )
    expect(page.get_by_text("Verify again. Nothing was recorded for this attempt.")).to_be_visible()
    _shot(page, "12-result-technical-error-en-desktop.png")


def test_details_clear_after_inactivity_but_the_result_heading_stays(
    live_server, page, entry_world, settings
):
    settings.ENTRY_RESULT_CLEAR_SECONDS = 2
    _open(page, live_server, entry_world)
    _scan(page, entry_world["token_en"])
    expect(page.locator("#entry-participant")).to_have_count(0, timeout=6_000)
    expect(page.locator("[data-entry-decision]")).to_have_count(0)
    expect(page.locator("#entry-result h1")).to_have_text("Verified")
    expect(page.locator("#entry-next")).to_be_focused()
    assert SYNTHETIC_NAME_EN not in page.content()
    _shot(page, "19-result-details-cleared-en-desktop.png")
    page.keyboard.press("Escape")
    expect(page).to_have_url(re.compile(r"/entry/verify/$"))


# ---------------------------------------------------------------------------
# Validation, throttling, connection, session states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "viewport", "name"),
    [
        ("en", DESKTOP, "13-verify-validation-error-en-desktop.png"),
        ("ar", MOBILE, "14-verify-validation-error-ar-mobile.png"),
    ],
)
def test_validation_error_is_specific_linked_and_never_echoes_the_value(
    live_server, page, entry_world, language, viewport, name
):
    _open(page, live_server, entry_world, language=language, viewport=viewport)
    page.get_by_role("tab", name=re.compile("NIN|الوطني")).click()
    page.locator("select[name=method]").select_option("PASSPORT")
    page.locator("input[name=value]").fill("SYNTHPASS9981")
    page.locator("#pane-identity button[type=submit]").click()
    summary = page.locator("#error-summary")
    expect(summary).to_be_visible()
    expect(summary).to_be_focused()
    field = page.locator("input[name=country_code]")
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_attribute("aria-describedby", re.compile("id_country_code_error"))
    expect(page.locator("input[name=value]")).to_have_value("")
    assert "SYNTHPASS9981" not in page.content()
    # Label and message are separately isolated so parentheses and Latin
    # fragments cannot reorder in RTL (UI checkpoint correction 3).
    item = summary.locator("li").first
    expect(item.locator("a > bdi")).to_have_count(1)
    expect(item.locator(":scope > bdi")).to_have_count(1)
    label_bdi = item.locator("a > bdi")
    assert label_bdi.evaluate("el => getComputedStyle(el).unicodeBidi") == "isolate"
    if language == "ar":
        assert label_bdi.evaluate("el => getComputedStyle(el).direction") == "rtl"
        # The closing parenthesis of the label is the visually LEFT-most
        # glyph of an RTL run ending in ")": measure it.
        order_ok = label_bdi.evaluate(
            """el => {
              const text = el.textContent;
              const open = text.indexOf('('), close = text.lastIndexOf(')');
              if (open < 0 || close < 0) return true;
              const range = document.createRange();
              const node = el.firstChild;
              range.setStart(node, open); range.setEnd(node, open + 1);
              const o = range.getBoundingClientRect();
              range.setStart(node, close); range.setEnd(node, close + 1);
              const c = range.getBoundingClientRect();
              return o.left > c.left;  // RTL: "(" sits to the right of ")"
            }"""
        )
        assert order_ok, "parentheses rendered in the wrong order in RTL"
    if viewport is MOBILE:
        _assert_no_horizontal_scroll(page)
    _shot(page, name)


def test_repeated_invalid_scans_raise_a_signal_then_pause_qr(
    live_server, page, entry_world, settings
):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 2
    settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 2
    _open(page, live_server, entry_world)
    for _ in range(2):
        _scan(page, "NOT-A-DIGITAL-ENTRY-PASS-0000")
        page.locator("#entry-next").click()
    page.locator("input[name=token]").fill("NOT-A-DIGITAL-ENTRY-PASS-0000")
    page.locator("input[name=token]").press("Enter")
    notice = page.locator("#entry-throttled")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("Call your supervisor")
    assert (
        database_call(
            lambda: AuditEvent.objects.filter(action_code=action_codes.ENTRY_ANOMALY_SIGNAL).count()
        )
        == 1
    )
    assert (
        database_call(
            lambda: AuditEvent.objects.filter(
                action_code=action_codes.ENTRY_LOOKUP_THROTTLED
            ).count()
        )
        == 1
    )
    _shot(page, "15-verify-throttled-en-desktop.png")


def test_lost_connection_blocks_lookups_and_says_what_to_do(live_server, page, entry_world):
    _open(page, live_server, entry_world)
    page.context.set_offline(True)
    indicator = page.locator("#entry-connection")
    expect(indicator).to_have_attribute("data-state", "offline")
    expect(page.locator("#entry-offline-banner")).to_be_visible()
    expect(page.locator("[data-entry-submit]")).to_be_disabled()
    expect(page.locator("[data-scan-status]")).to_have_attribute("data-ready", "false")
    expect(page.locator("#entry-connection-announcer")).not_to_be_empty()
    _shot(page, "16-verify-offline-en-desktop.png")
    page.context.set_offline(False)
    expect(indicator).to_have_attribute("data-state", "online", timeout=10_000)
    expect(page.locator("[data-entry-submit]")).to_be_enabled()


def test_ended_session_is_detected_by_the_passive_status_check(
    live_server, page, entry_world, settings
):
    from django.utils import timezone

    from apps.entry.models import EntryOperatorSession

    settings.ENTRY_CONNECTION_POLL_SECONDS = 1
    _open(page, live_server, entry_world, language="ar")
    database_call(
        lambda: EntryOperatorSession.objects.filter(ended_at__isnull=True).update(
            ended_at=timezone.now(), end_reason="TEST"
        )
    )
    expect(page.locator("#entry-connection")).to_have_attribute(
        "data-state", "session-ended", timeout=10_000
    )
    expect(page.locator("#entry-session-banner")).to_be_visible()
    _shot(page, "17-verify-session-ended-ar-desktop.png")


def test_slow_gate_shows_a_degraded_online_notice(live_server, page, entry_world):
    from django.utils import timezone

    from apps.entry.models import VerificationSample

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
    _open(page, live_server, entry_world, viewport=TABLET)
    expect(page.locator("#entry-degraded-server")).to_be_visible()
    expect(page.locator("#entry-connection")).to_have_attribute(
        "data-state", "degraded", timeout=30_000
    )
    _shot(page, "18-verify-degraded-en-tablet.png")
