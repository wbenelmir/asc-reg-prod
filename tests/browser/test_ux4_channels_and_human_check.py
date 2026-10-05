"""Real-browser checks for UX-4 (M01 human check, M24 registration channels).

Chromium against the real `live_server`: the vendored ALTCHA widget solving
its session-bound challenge (UX-C2) in EN/FR/AR, a submission made before it
finishes, an expired challenge replaced without losing the email, the
no-JavaScript explanation and assisted contact, a tampered payload, a
solution replayed from another browser session, a strict local-only CSP, the
operator's channel screen, and a stale participant tab after closure.
Synthetic data only. UX-4 screenshots go to
`var/test_artifacts/phase4/ux_4/screenshots/`; the UX-C2 human-check evidence
goes to `var/test_artifacts/phase4/ux_c2/`, so neither overwrites the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.test import override_settings

from tests.browser.database import database_call
from tests.browser.helpers import fill_date, switch_language
from tests.browser.test_ui_ux_checkpoint1 import _ops_superuser, _sign_in_operational

pytestmark = pytest.mark.django_db(transaction=True)

SHOTS = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "ux_4"
SHOTS = SHOTS / "screenshots"
EVIDENCE_C2 = SHOTS.parents[1] / "ux_c2"
SUBMIT = "#main-content button[type=submit]"
OTP_SETTINGS = {"OTP_GENERATOR_BACKEND": "apps.accounts.otp.DeterministicTestOtpGenerator"}


def _shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def _shot_c2(page, name: str) -> None:
    folder = EVIDENCE_C2 / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _challenge_count() -> int:
    from apps.accounts.models import AuthenticationChallenge

    return database_call(lambda: AuthenticationChallenge.objects.count())


def _login(live_server, page, email: str) -> None:
    from apps.accounts.otp import DeterministicTestOtpGenerator

    with override_settings(**OTP_SETTINGS):
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", email)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator(SUBMIT).click()
        page.wait_for_url("**/workspace/**")


def _set_mode(event, mode: str) -> None:
    from apps.events.models import EventEdition

    database_call(
        lambda: EventEdition.objects.filter(pk=event.pk).update(public_registration_mode=mode)
    )


# ---------------------------------------------------------------------------
# M01: the self-hosted ALTCHA check (UX-C2, owner decision UX-D01 option M)
# ---------------------------------------------------------------------------

_SOLVED = "form[data-human-check-state=solved]"
_PAYLOAD = "altcha-widget input[name=human_check]"


@pytest.mark.parametrize(
    ("language", "label"), [("en", "Verified"), ("fr", "Vérifié"), ("ar", "تم التحقق")]
)
def test_the_widget_solves_by_itself_and_the_code_is_sent(
    live_server, page, language, label
) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    if language != "en":
        switch_language(page, language)
    page.wait_for_selector(_SOLVED, timeout=20000)
    assert page.locator(_PAYLOAD).input_value() != ""
    widget_label = page.locator("altcha-widget label")
    assert label in widget_label.inner_text()
    # The widget's own checkbox is a real, labelled, keyboard-focusable control.
    checkbox = page.locator("altcha-widget input[type=checkbox]")
    assert checkbox.evaluate("el => el.labels.length") == 1
    page.focus("#id_email")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.type") == "checkbox"
    _shot_c2(page, f"human-check-done-{language}")
    with override_settings(**OTP_SETTINGS):
        page.fill("#id_email", f"ux4-browser-{language}@example.com")
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
    assert _challenge_count() == 1


# The challenge request is held until the test releases it, so the click
# certainly happens before the check ends.
_HOLD_CHALLENGE = """(() => {
  const original = window.fetch.bind(window);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  window.__releaseChallenge = () => release();
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (String(url).includes("/accounts/start/human-check/")) {
      return gate.then(() => original(input, init));
    }
    return original(input, init);
  };
})();"""


def test_a_submission_made_before_the_check_ends_waits_and_is_sent_once(live_server, page) -> None:
    page.add_init_script(_HOLD_CHALLENGE)
    page.goto(f"{live_server.url}/accounts/start/")
    page.fill("#id_email", "ux4-early@example.com")
    with override_settings(**OTP_SETTINGS):
        page.locator(SUBMIT).click()
        page.wait_for_timeout(500)
        assert "/accounts/start/" in page.url  # held, not sent unsolved
        assert _challenge_count() == 0
        page.evaluate("window.__releaseChallenge()")
        page.wait_for_url("**/verify/**", timeout=20000)
    assert _challenge_count() == 1


def test_an_expired_challenge_is_replaced_without_losing_the_email(live_server, page) -> None:
    with override_settings(HUMAN_CHECK_TTL_SECONDS=4, **OTP_SETTINGS):
        page.goto(f"{live_server.url}/accounts/start/")
        page.wait_for_selector(_SOLVED, timeout=20000)
        first = page.locator(_PAYLOAD).input_value()
        page.fill("#id_email", "ux4-expiry@example.com")
        page.wait_for_function(
            "(first) => {"
            " const f = document.querySelector('altcha-widget input[name=human_check]');"
            " const form = f && f.form;"
            " return f && f.value && f.value !== first"
            " && form.getAttribute('data-human-check-state') === 'solved'; }",
            arg=first,
            timeout=20000,
        )
        assert page.input_value("#id_email") == "ux4-expiry@example.com"
        _shot_c2(page, "human-check-refreshed-after-expiry-en")
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
    assert _challenge_count() == 1


def test_without_javascript_the_page_explains_and_offers_assisted_contact(
    live_server, browser
) -> None:
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/accounts/start/")
        notice = page.locator("[data-human-check-noscript]")
        assert notice.is_visible()
        assert "assisted registration" in notice.inner_text()
        assert notice.locator("a[href^='mailto:']").count() == 1
        assert notice.locator("a[href^='tel:']").count() == 1
        page.fill("#id_email", "ux4-nojs@example.com")
        page.locator(SUBMIT).click()
        page.wait_for_load_state("load")
        assert "/accounts/start/" in page.url
        assert page.locator("#error-summary").is_visible()
        assert "automatic security check" in page.locator("#error-summary").inner_text()
        assert page.input_value("#id_email") == "ux4-nojs@example.com"  # kept
        assert _challenge_count() == 0
        _shot_c2(page, "human-check-no-javascript-en")
    finally:
        context.close()


def test_a_tampered_payload_is_refused_and_a_fresh_check_then_works(live_server, page) -> None:
    page.goto(f"{live_server.url}/accounts/start/")
    page.wait_for_selector(_SOLVED, timeout=20000)
    page.evaluate(
        """() => { const field = document.querySelector('altcha-widget input[name=human_check]');
                   const data = JSON.parse(atob(field.value));
                   data.challenge.parameters.cost = 1;
                   field.value = btoa(JSON.stringify(data)); }"""
    )
    page.fill("#id_email", "ux4-tamper@example.com")
    page.locator(SUBMIT).click()
    page.wait_for_selector("#error-summary")
    assert _challenge_count() == 0
    assert page.input_value("#id_email") == "ux4-tamper@example.com"
    page.wait_for_selector(_SOLVED, timeout=20000)
    with override_settings(**OTP_SETTINGS):
        page.locator(SUBMIT).click()
        page.wait_for_url("**/verify/**")
    assert _challenge_count() == 1


def test_a_solution_from_another_browser_session_is_refused(live_server, browser) -> None:
    first, second = browser.new_context(), browser.new_context()
    try:
        solver = first.new_page()
        solver.goto(f"{live_server.url}/accounts/start/")
        solver.wait_for_selector(_SOLVED, timeout=20000)
        stolen = solver.locator(_PAYLOAD).input_value()
        thief = second.new_page()
        thief.goto(f"{live_server.url}/accounts/start/")
        thief.wait_for_selector(_SOLVED, timeout=20000)
        thief.evaluate(
            "(value) => { document.querySelector('altcha-widget input[name=human_check]')"
            ".value = value; }",
            stolen,
        )
        thief.fill("#id_email", "ux4-cross-session@example.com")
        thief.locator(SUBMIT).click()
        thief.wait_for_selector("#error-summary")
        assert _challenge_count() == 0
    finally:
        first.close()
        second.close()


# The strictest policy the widget must live with once P4-4 configures a CSP:
# local scripts, styles, workers and fetches only; no eval, no inline code, no
# remote origin. Existing inline scripts of the base layout are counted
# separately: they predate UX-C2 and are P4-4 work.
_STRICT_CSP = (
    "default-src 'none'; script-src 'self'; worker-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
_COLLECT_VIOLATIONS = """(() => {
  window.__cspViolations = [];
  document.addEventListener("securitypolicyviolation", (event) => {
    window.__cspViolations.push({
      directive: event.effectiveDirective, blocked: event.blockedURI,
      source: event.sourceFile, sample: event.sample });
  });
})();"""


def test_the_widget_works_under_a_strict_local_only_csp(live_server, browser) -> None:
    context = browser.new_context()
    page = context.new_page()
    page.add_init_script(_COLLECT_VIOLATIONS)

    def with_csp(route):
        if route.request.method != "GET":
            route.continue_()  # the form POST itself goes to the server untouched
            return
        response = route.fetch()
        headers = dict(response.headers)
        headers["content-security-policy"] = _STRICT_CSP
        route.fulfill(response=response, headers=headers)

    try:
        page.route("**/accounts/start/", with_csp)
        page.goto(f"{live_server.url}/accounts/start/")
        page.wait_for_selector(_SOLVED, timeout=20000)
        violations = page.evaluate("window.__cspViolations")
        altcha_related = [
            v
            for v in violations
            if any(
                marker in f"{v['blocked']} {v['source']} {v['sample']}"
                for marker in ("altcha", "pbkdf2", "human-check", "blob:", "eval")
            )
        ]
        assert altcha_related == [], altcha_related
        with override_settings(**OTP_SETTINGS):
            page.fill("#id_email", "ux4-csp@example.com")
            page.locator(SUBMIT).click()
            page.wait_for_url("**/verify/**")
        assert _challenge_count() == 1
        EVIDENCE_C2.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_C2 / "csp-violations-outside-altcha.json").write_text(
            json.dumps(sorted({f"{v['directive']} {v['blocked']}" for v in violations}), indent=1),
            encoding="utf-8",
        )
    finally:
        context.close()


# ---------------------------------------------------------------------------
# M24: registration channels
# ---------------------------------------------------------------------------


def test_an_operator_restricts_registration_and_a_stale_tab_is_refused(
    live_server, browser, seeded_open_event, seeded_legal_notices
) -> None:
    from apps.core.models import Country

    database_call(lambda: Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"}))
    participant = browser.new_context()
    tab = participant.new_page()
    operator_context = browser.new_context()
    operator = operator_context.new_page()
    try:
        _login(live_server, tab, "ux4-stale@example.com")
        tab.goto(f"{live_server.url}/register/identity/")
        tab.fill("#id_given_names", "Amine")
        tab.fill("#id_family_name", "Benali")
        fill_date(tab, "id_date_of_birth", "1990-03-07")

        admin = _ops_superuser("ux4.ops@example.test")
        _sign_in_operational(live_server, operator, admin)
        operator.goto(f"{live_server.url}/ops/events/registration-channels/")
        operator.locator("a[data-record-action]").first.click()
        operator.wait_for_url("**/registration-channels/**")
        _shot(operator, "channels-detail-open-en")
        operator.check("input[name=mode][value=INVITATION_ONLY]")
        operator.fill("#id_reason", "Public quota reached (synthetic).")
        operator.locator("#main-content form.asc-wizard-form button[type=submit]").click()
        operator.locator("#asc-confirm-dialog [data-confirm-accept]").click()
        operator.wait_for_selector('[data-channel-state="INVITATION_ONLY"]')
        _shot(operator, "channels-detail-invitation-only-en")
        switch_language(operator, "ar")
        assert operator.locator("html").get_attribute("dir") == "rtl"
        _shot(operator, "channels-detail-invitation-only-ar")

        # The participant's tab was opened before the change.
        tab.locator(SUBMIT).click(force=True)
        tab.wait_for_selector('[data-registration-closed="public"]')
        assert "/register/identity/" in tab.url
        _shot(tab, "registration-closed-public-en")
        switch_language(tab, "ar")
        _shot(tab, "registration-closed-public-ar")
        tab.goto(f"{live_server.url}/workspace/")
        assert tab.locator("[data-draft-closed]").count() == 1
    finally:
        participant.close()
        operator_context.close()


def test_invitation_only_suppresses_the_open_registration_fallback(
    live_server, page, seeded_open_event
) -> None:
    _set_mode(seeded_open_event, "INVITATION_ONLY")
    page.goto(f"{live_server.url}/invite/not-a-real-token/")
    assert page.locator("text=You can still register directly.").count() == 0
    assert page.locator("a[href='/accounts/start/'].btn").count() == 0
    _shot(page, "invitation-fallback-suppressed-en")


def test_closed_mode_keeps_sign_in_and_the_workspace(live_server, page, seeded_open_event) -> None:
    _set_mode(seeded_open_event, "CLOSED")
    _login(live_server, page, "ux4-closed-signin@example.com")
    assert "/workspace/" in page.url
    assert page.locator("text=Public registration is closed").count() >= 1
    page.goto(f"{live_server.url}/register/identity/")
    assert page.locator('[data-registration-closed="all"]').count() == 1
    _shot(page, "registration-closed-all-en")
