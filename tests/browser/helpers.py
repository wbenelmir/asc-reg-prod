"""Shared browser-test synchronisation helpers (UI/UX Completion Gate F5).

`switch_language` replaces the pattern

    page.select_option("select[name=language]", code)
    page.wait_for_load_state("networkidle")

which raced the language switch. The switcher submits a same-URL
POST-redirect navigation. `wait_for_load_state` could resolve while the NEW
document was still being parsed, blocked on a script-blocking stylesheet or
parser-blocking script before the inline restore script at the end of
`base.html` had run, so a test could read `lang="ar"` together with a field
that was still empty. This was reproduced deterministically by delaying
`bootstrap.rtl.min.css`, `app.css` or `htmx.min.js` (see
`test_language_switch_waits_for_the_new_document_under_injected_latency`).

Waiting for the language navigation's own `load` event guarantees the new
document has finished parsing and has run every script, including the
restore, before the test inspects it. It is a synchronisation fix, not a
retry.
"""

from __future__ import annotations


def switch_language(page, code: str) -> None:
    """Change the interface language through the real switcher and wait
    until the resulting document is fully loaded."""
    with page.expect_navigation(wait_until="load"):
        page.select_option("select[name=language]", code)


def fill_date(page, field_id: str, iso_date: str) -> None:
    """Type an ISO date into the Day / Month / Year parts of a date field
    (UX-1, S-08). `field_id` is the field's id, for example
    `id_date_of_birth`; the parts are `<id>_day`, `<id>_month`, `<id>_year`."""
    year, month, day = iso_date.split("-")
    page.fill(f"#{field_id}_day", day)
    page.fill(f"#{field_id}_month", month)
    page.fill(f"#{field_id}_year", year)


def read_date(page, field_id: str) -> str:
    """The date typed in the Day / Month / Year parts, as ISO, or "" when empty."""
    day = page.locator(f"#{field_id}_day").input_value()
    month = page.locator(f"#{field_id}_month").input_value()
    year = page.locator(f"#{field_id}_year").input_value()
    if not (day or month or year):
        return ""
    return f"{year}-{int(month):02d}-{int(day):02d}"


def no_js_page_at_otp_verify(browser, live_server_url: str, email: str):
    """A page with JavaScript switched OFF, already at the OTP verify step.

    The start page runs the automatic proof-of-work check (UX-4, M01), which
    needs JavaScript, so a no-JavaScript visitor cannot request a code; that
    refusal is tested on its own. To keep covering every LATER page without
    JavaScript, the code is requested in a normal context, and its session
    cookie is handed to a fresh context with JavaScript disabled. Returns
    `(context, page)`; the caller closes the context. Call it inside the
    caller's `override_settings(OTP_GENERATOR_BACKEND=...)` block.
    """
    scripted = browser.new_context()
    page = scripted.new_page()
    page.goto(f"{live_server_url}/accounts/start/")
    page.fill("#id_email", email)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/verify/**")
    state = scripted.storage_state()
    scripted.close()
    context = browser.new_context(java_script_enabled=False, storage_state=state)
    page = context.new_page()
    page.goto(f"{live_server_url}/accounts/verify/")
    return context, page


def select_choice(page, field_id: str, value: str) -> None:
    """Choose `value` for a select that may be upgraded to the search-and-select
    control (UX-1, S-07). The upgraded control keeps the native select, renamed
    `<id>_native`, as the submitted value; this sets it and fires `change`, which
    the control mirrors. A select that stays native is set the ordinary way."""
    page.wait_for_load_state("load")
    upgraded = page.locator(f"#{field_id}_native").count() == 1
    if upgraded:
        page.evaluate(
            """([id, value]) => { const select = document.getElementById(id + '_native');
                   select.value = value;
                   select.dispatchEvent(new Event('change', {bubbles: true})); }""",
            [field_id, value],
        )
    else:
        page.select_option(f"#{field_id}", value)
