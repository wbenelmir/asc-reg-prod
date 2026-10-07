"""The staff password setup link in a real browser.

The secret-bearing URL renders nothing and redirects to `/accounts/setup/`;
the forms of that page (the password form and the language switch) are
accepted by CSRF origin checking -- a `no-referrer` policy on those pages made
browsers send `Origin: null` and every post was refused. An invalid link
lands on the "not valid" page at the clean address. Synthetic account.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import switch_language

pytestmark = pytest.mark.django_db(transaction=True)

EMAIL = "browser-setup@example.test"
CHOSEN = "browser-setup-Chosen-Battery-7"


def _bootstrap_link() -> str:
    from apps.accounts import administration as admin

    _user, raw = database_call(
        lambda: admin.bootstrap_administrator(email=EMAIL, display_name="Browser Setup")
    )
    return raw


def test_a_setup_link_sets_the_password_in_a_real_browser(live_server, page):
    raw = _bootstrap_link()
    page.goto(f"{live_server.url}/accounts/setup/{raw}/")
    page.wait_for_load_state("load")
    assert page.url.endswith("/accounts/setup/")
    assert raw not in page.content()
    switch_language(page, "fr")  # a CSRF-protected post from this page
    assert page.evaluate("() => document.documentElement.lang") == "fr"
    page.fill("#id_new_password1", CHOSEN)
    page.fill("#id_new_password2", CHOSEN)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_url("**/accounts/ops/sign-in/**")
    assert "403" not in page.title()

    from apps.accounts.models import OperationalUser

    user = database_call(lambda: OperationalUser.objects.get(email_normalized=EMAIL))
    assert user.check_password(CHOSEN)


def test_an_invalid_link_shows_the_not_valid_page_at_the_clean_address(live_server, page):
    page.goto(f"{live_server.url}/accounts/setup/browser-invalid-0000000000/")
    page.wait_for_load_state("load")
    assert page.url.endswith("/accounts/setup/")
    expect(page.locator("main h1")).to_be_visible()
    switch_language(page, "ar")
    assert page.evaluate("() => document.documentElement.dir") == "rtl"
