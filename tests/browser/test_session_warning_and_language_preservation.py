"""Real-browser evidence for the session-warning flow (Prompt 5 correction
pass §4) and the language-switch value-preservation fix (Prompt 5
correction pass §5).

Uses short, deterministic, `override_settings`-configured intervals rather
than waiting out the real production defaults -- these tests prove the
actual client-side timer reveals the warning at the right moment, with the
right limiting reason, not a per-request server-computed flag.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts.otp import DeterministicTestOtpGenerator
from tests.browser.database import database_call, database_sync
from tests.browser.helpers import fill_date, read_date, switch_language

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


@database_sync
def _make_operational_user(email: str):
    from apps.accounts.models import OperationalUser, OperationalUserStatus

    user = OperationalUser.objects.create_superuser(
        email=email,
        password="__test_password__",  # noqa: S106
    )
    user.status = OperationalUserStatus.ACTIVE
    user.save(update_fields=["status"])
    return user


def _sign_in_operational(live_server, page, email: str) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", email)
    page.fill("#id_password", "__test_password__")
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/ops/registrations/**")


# ---------------------------------------------------------------------------
# Participant session-warning flow.
# ---------------------------------------------------------------------------


def test_participant_inactivity_warning_becomes_visible_with_the_correct_reason(
    live_server, page
) -> None:
    with override_settings(
        PARTICIPANT_SESSION_INACTIVITY_SECONDS=6,
        PARTICIPANT_SESSION_ABSOLUTE_SECONDS=999,
        PARTICIPANT_SESSION_WARNING_SECONDS=3,
    ):
        _login_participant(live_server, page, "warn-inactivity@example.com")
        # Deadline in ~6s, warning window 3s before it -- becomes visible
        # around t=3s. The banner starts `d-none`; wait for it to gain the
        # revealed `d-flex` class via the real client-side timer, not a
        # fresh server response.
        page.wait_for_selector("#participant-session-warning.d-flex", timeout=9000)
        text = page.locator("#participant-session-warning [data-role='warning-text']").inner_text()
        assert text == "Your session is about to end due to inactivity."
        # The extend action is offered for an inactivity-limited warning.
        assert page.locator("#participant-session-extend-form").is_visible()


def test_participant_absolute_warning_never_claims_inactivity_and_hides_the_extend_action(
    live_server, page
) -> None:
    with override_settings(
        PARTICIPANT_SESSION_ABSOLUTE_SECONDS=6,
        PARTICIPANT_SESSION_INACTIVITY_SECONDS=999,
        PARTICIPANT_SESSION_WARNING_SECONDS=3,
    ):
        _login_participant(live_server, page, "warn-absolute@example.com")
        page.wait_for_selector("#participant-session-warning.d-flex", timeout=9000)
        text = page.locator("#participant-session-warning [data-role='warning-text']").inner_text()
        assert "inactivity" not in text
        assert text == (
            "Your session will end soon. Please save your work; signing in again will be required."
        )
        # Extending inactivity cannot help when the ABSOLUTE deadline is
        # the limiting one -- the action must not be offered at all
        # (Prompt 5 correction pass §4).
        assert not page.locator("#participant-session-extend-form").is_visible()


def test_participant_stay_signed_in_extends_the_session_and_keeps_it_alive(
    live_server, page
) -> None:
    with override_settings(
        PARTICIPANT_SESSION_INACTIVITY_SECONDS=6,
        PARTICIPANT_SESSION_ABSOLUTE_SECONDS=999,
        PARTICIPANT_SESSION_WARNING_SECONDS=3,
    ):
        _login_participant(live_server, page, "warn-extend@example.com")
        page.wait_for_selector("#participant-session-warning.d-flex", timeout=9000)
        page.locator("#participant-session-extend-form button[type=submit]").click()
        page.wait_for_load_state("networkidle")
        # The session must still be authenticated afterward (not expired).
        response = page.goto(f"{live_server.url}/workspace/")
        assert response.status < 400
        assert "workspace" in page.url


# ---------------------------------------------------------------------------
# Operational session-warning flow -- independent state from the
# participant one (Prompt 5 correction pass §2/§4).
# ---------------------------------------------------------------------------


def test_operational_inactivity_warning_becomes_visible_with_the_correct_reason(
    live_server, page
) -> None:
    _make_operational_user("warn-ops@example.com")
    with override_settings(
        OPERATIONAL_SESSION_INACTIVITY_SECONDS=6,
        OPERATIONAL_SESSION_ABSOLUTE_SECONDS=999,
        OPERATIONAL_SESSION_WARNING_SECONDS=3,
    ):
        _sign_in_operational(live_server, page, "warn-ops@example.com")
        page.wait_for_selector("#operational-session-warning.d-flex", timeout=9000)
        text = page.locator("#operational-session-warning [data-role='warning-text']").inner_text()
        assert text == "Your operational session is about to end due to inactivity."


# ---------------------------------------------------------------------------
# Language-switch form-value preservation (Prompt 5 correction pass §5):
# `this.form.submit()` never dispatches a `submit` event, so the snapshot
# listener was silently never invoked -- fixed with `requestSubmit()`.
# ---------------------------------------------------------------------------


def test_language_switch_preserves_safe_field_values_across_en_fr_ar(
    live_server, page, seeded_open_event
) -> None:
    from apps.registrations.models import Registration

    _login_participant(live_server, page, "lang-preserve@example.com")
    page.goto(f"{live_server.url}/register/identity/")

    page.fill("#id_given_names", "Amine")
    page.fill("#id_family_name", "Benali")

    drafts_before = database_call(
        lambda: Registration.objects.filter(event_edition=seeded_open_event).count()
    )

    switch_language(page, "fr")
    assert page.locator("html").get_attribute("lang") == "fr"
    assert page.locator("#id_given_names").input_value() == "Amine"
    assert page.locator("#id_family_name").input_value() == "Benali"

    switch_language(page, "ar")
    assert page.locator("html").get_attribute("lang") == "ar"
    assert page.locator("#id_given_names").input_value() == "Amine"
    assert page.locator("#id_family_name").input_value() == "Benali"

    # No Draft was duplicated and nothing was submitted by switching language.
    drafts_after = database_call(
        lambda: Registration.objects.filter(event_edition=seeded_open_event).count()
    )
    assert drafts_after == drafts_before


@pytest.mark.parametrize("delayed_asset", ["bootstrap.rtl.min.css", "htmx.min.js"])
def test_language_switch_waits_for_the_new_document_under_injected_latency(
    live_server, page, seeded_open_event, delayed_asset: str
) -> None:
    """UI/UX Completion Gate F5 regression. The intermittent Prompt 6 failure
    (`lang="ar"` but an empty `#id_given_names`) is reproduced on demand by
    slowing one resource the Arabic document must load before its inline
    restore script can run: a script-blocking stylesheet or a parser-blocking
    script. The old `wait_for_load_state("networkidle")` synchronisation
    returned while that document was still parsing; `switch_language` waits
    for the navigation's own `load` event, so the restored value is present
    however slow the resource is."""
    import time

    _login_participant(live_server, page, f"lang-latency-{delayed_asset}@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")
    switch_language(page, "fr")
    assert page.locator("#id_given_names").input_value() == "Amine"

    def _delay(route) -> None:
        time.sleep(1.2)
        route.continue_()

    page.route(f"**/{delayed_asset}*", _delay)
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("lang") == "ar"
    assert page.locator("#id_given_names").input_value() == "Amine"


def test_language_switch_never_preserves_sensitive_fields(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "lang-sensitive@example.com")
    page.goto(f"{live_server.url}/register/identity/")

    page.check("#id_identity_path_0")  # NIN
    page.fill("#id_nin_value", "123456789012345678")

    switch_language(page, "fr")

    # The NIN value must never be restored by the client-side snapshot --
    # structurally excluded (Prompt 5 correction pass §5).
    assert page.locator("#id_nin_value").input_value() == ""


# ---------------------------------------------------------------------------
# Prompt 5 correction pass §1 follow-up: language-preservation must use a
# strict allowlist -- a form is eligible ONLY when explicitly marked
# `data-language-preserve="safe-fields"`, and within it only fields
# explicitly marked `data-language-preserve-field` are ever captured.
# ---------------------------------------------------------------------------

_SNAPSHOT_KEY = "asc2026_form_snapshot_v1"


def _install_snapshot_capture(page) -> None:
    """Mirror every write to the real snapshot key into a second,
    test-only key that `restore()` never touches -- lets a test inspect
    exactly what was serialized even after the real page reload has
    already consumed (and deleted) the real snapshot."""
    page.add_init_script(
        f"""
        (function () {{
          var orig = Storage.prototype.setItem;
          Storage.prototype.setItem = function (key, value) {{
            if (key === '{_SNAPSHOT_KEY}') {{
              orig.call(sessionStorage, '__test_captured_snapshot__', value);
            }}
            return orig.call(this, key, value);
          }};
        }})();
        """
    )


def test_operational_sign_in_password_is_never_captured_by_language_switch(
    live_server, page
) -> None:
    _make_operational_user("lang-ops-password@example.com")
    _install_snapshot_capture(page)
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", "lang-ops-password@example.com")
    page.fill("#id_password", "a-synthetic-test-password-1")

    switch_language(page, "fr")

    # The operational sign-in form is never marked eligible -- no snapshot
    # is ever taken on this page, so nothing is ever written under either
    # key, and the synthetic password appears nowhere in sessionStorage.
    real = page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')")
    captured = page.evaluate("() => sessionStorage.getItem('__test_captured_snapshot__')")
    assert real is None
    assert captured is None
    assert page.locator("#id_password").input_value() == ""


def test_otp_verification_code_is_never_captured_by_language_switch(live_server, page) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        _install_snapshot_capture(page)
        page.goto(f"{live_server.url}/accounts/start/")
        page.fill("#id_email", "lang-otp-code@example.com")
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/verify/**")
        page.fill("#id_code", "135790")

        switch_language(page, "fr")

        real = page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')")
        captured = page.evaluate("() => sessionStorage.getItem('__test_captured_snapshot__')")
        assert real is None
        assert captured is None


def test_otp_request_page_creates_no_snapshot_on_language_switch(live_server, page) -> None:
    _install_snapshot_capture(page)
    page.goto(f"{live_server.url}/accounts/start/")
    page.fill("#id_email", "lang-otp-request@example.com")

    switch_language(page, "fr")

    real = page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')")
    captured = page.evaluate("() => sessionStorage.getItem('__test_captured_snapshot__')")
    assert real is None
    assert captured is None


def test_language_switch_never_preserves_passport_fields(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "lang-passport@example.com")
    page.goto(f"{live_server.url}/register/identity/")

    page.check("#id_identity_path_1")  # PASSPORT
    page.fill("#id_passport_number", "X1234567")
    page.select_option("#id_passport_country_code", label="France")
    fill_date(page, "id_passport_expires_at", "2030-01-01")

    switch_language(page, "fr")

    assert page.locator("#id_passport_number").input_value() == ""
    assert read_date(page, "id_passport_expires_at") == ""


def test_language_switch_never_preserves_mobile_number(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "lang-mobile@example.com")
    _install_snapshot_capture(page)
    page.goto(f"{live_server.url}/register/contact/")
    page.fill("#id_mobile_number", "0555123456")

    switch_language(page, "fr")

    # The contact step form marks no fields safe at all -- no snapshot is
    # ever taken on this page.
    real = page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')")
    captured = page.evaluate("() => sessionStorage.getItem('__test_captured_snapshot__')")
    assert real is None
    assert captured is None


def test_language_switch_never_serializes_an_unmarked_field_added_to_an_eligible_form(
    live_server, page, seeded_open_event
) -> None:
    """A hypothetical future field added to an ELIGIBLE wizard form without
    the per-field opt-in marker must never be captured -- eligibility is
    per-field, never inferred from being inside an eligible form."""
    _login_participant(live_server, page, "lang-unmarked-field@example.com")
    _install_snapshot_capture(page)
    page.goto(f"{live_server.url}/register/identity/")
    page.evaluate(
        """() => {
            var form = document.querySelector('form[data-language-preserve="safe-fields"]');
            var input = document.createElement('input');
            input.type = 'text';
            input.name = 'future_unmarked_field';
            input.value = 'should-never-be-preserved';
            form.appendChild(input);
        }"""
    )

    switch_language(page, "fr")

    captured = page.evaluate("() => sessionStorage.getItem('__test_captured_snapshot__')")
    assert captured is not None
    assert "future_unmarked_field" not in captured
    assert "should-never-be-preserved" not in captured


def test_language_switch_snapshot_is_not_replayed_after_a_second_page_load(
    live_server, page, seeded_open_event
) -> None:
    """A snapshot is consumed at most once -- after `restore()` deletes it,
    reloading the same page again must not resurrect the earlier values
    (no replay)."""
    _login_participant(live_server, page, "lang-no-replay@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.fill("#id_given_names", "Amine")

    switch_language(page, "fr")
    assert page.locator("#id_given_names").input_value() == "Amine"

    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#id_given_names").input_value() == ""


def test_language_switch_discards_a_stale_snapshot(live_server, page, seeded_open_event) -> None:
    _login_participant(live_server, page, "lang-stale@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.evaluate(
        f"""() => {{
            sessionStorage.setItem('{_SNAPSHOT_KEY}', JSON.stringify({{
                path: '/register/identity/',
                timestamp: Date.now() - 10 * 60 * 1000,
                data: {{given_names: 'StaleName'}},
            }}));
        }}"""
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#id_given_names").input_value() == ""
    assert page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')") is None


def test_language_switch_discards_a_path_mismatched_snapshot(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "lang-wrong-path@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.evaluate(
        f"""() => {{
            sessionStorage.setItem('{_SNAPSHOT_KEY}', JSON.stringify({{
                path: '/register/contact/',
                timestamp: Date.now(),
                data: {{given_names: 'WrongPathName'}},
            }}));
        }}"""
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#id_given_names").input_value() == ""
    assert page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')") is None


def test_language_switch_discards_a_malformed_snapshot(
    live_server, page, seeded_open_event
) -> None:
    _login_participant(live_server, page, "lang-malformed@example.com")
    page.goto(f"{live_server.url}/register/identity/")
    page.evaluate(f"() => sessionStorage.setItem('{_SNAPSHOT_KEY}', 'not-json-at-all')")
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("#id_given_names").input_value() == ""
    assert page.evaluate(f"() => sessionStorage.getItem('{_SNAPSHOT_KEY}')") is None
