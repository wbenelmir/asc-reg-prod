"""Real-browser (Playwright) coverage for the Phase 2 Prompt 5 controlled-
export critical path: generate a scoped export, then download it.

Mirrors the established pattern in `test_invitations_i18n_rtl.py`: a real
rendered page in a real Chromium instance against `pytest-django`'s
`live_server`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import solve_staff_captcha

pytestmark = pytest.mark.django_db(transaction=True)


def test_export_workspace_requires_sign_in(live_server, page) -> None:
    page.goto(f"{live_server.url}/ops/exports/")
    page.wait_for_load_state("networkidle")
    assert "/ops/sign-in/" in page.url


def test_generate_and_download_an_export_in_a_real_browser(live_server, page) -> None:
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.exports.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        make_operational_user_with_membership,
        make_registration,
    )
    from apps.organizations.models import Organization, OrganizationType

    event = database_call(
        lambda: EventEdition.objects.create(
            code="BROWSEREXP",
            name="Browser Export Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
            status="REGISTRATION_OPEN",
        )
    )
    organization = database_call(
        lambda: Organization.objects.create(
            official_name="Browser Export Org",
            normalized_name="browser export org",
            organization_type=OrganizationType.OTHER,
        )
    )
    registration = database_call(lambda: make_registration(event=event, organization=organization))
    user = database_call(
        lambda: make_operational_user_with_membership(
            email="browser-export@example.com",
            group_name="Export Administrators",
            event_edition=event,
            organization=organization,
        )
    )

    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", user.email_normalized)
    page.fill("#id_password", TEST_OPERATIONAL_PASSWORD)
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/ops/exports/")
    page.wait_for_load_state("networkidle")
    page.locator("#id_event_edition_id").select_option(str(event.pk))
    # The organization is a permission-scoped choice now, never a typed id
    # (UI/UX Completion Gate F8).
    page.locator("#id_organization_id").select_option(str(organization.pk))
    page.locator("#id_purpose_code").select_option("OPERATIONAL_REPORTING")
    page.fill("#id_reason", "Browser end-to-end check")
    page.get_by_role("button", name="Generate export").click()

    # Recent exports render as a table on wide screens and as stacked records
    # on phones; only the displayed one is in the accessibility tree.
    download_link = page.get_by_role("link", name="Download")
    expect(download_link).to_have_count(1, timeout=10_000)
    content = page.content()
    assert registration.public_reference not in content  # the LIST view never shows raw rows
    download_href = download_link.get_attribute("href")

    # Follow the download link through the SAME authenticated browser
    # session (shares cookies with `page`) rather than triggering a real
    # OS-level download prompt, which is flaky under headless Chromium.
    response = page.request.get(f"{live_server.url}{download_href}")
    assert response.status == 200
    assert response.headers.get("content-type", "").startswith("text/csv")
    assert "attachment" in response.headers.get("content-disposition", "")
    body = response.text()
    assert registration.public_reference in body
    assert "public_reference" in body.splitlines()[0]


def test_communication_status_page_renders_rtl_in_arabic(live_server, page) -> None:
    """Phase 2 Prompt 6 §5.L: the operations communication-status page
    inherits `base.html`'s RTL switching -- confirm it actually applies to
    THIS page, not merely to the pages exercised by other browser tests."""
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.exports.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        make_operational_user_with_membership,
    )
    from apps.organizations.models import Organization, OrganizationType

    event = database_call(
        lambda: EventEdition.objects.create(
            code="BROWSERCOMMS",
            name="Browser Communications Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
            status="REGISTRATION_OPEN",
        )
    )
    organization = database_call(
        lambda: Organization.objects.create(
            official_name="Browser Communications Org",
            normalized_name="browser communications org",
            organization_type=OrganizationType.OTHER,
        )
    )
    user = database_call(
        lambda: make_operational_user_with_membership(
            email="browser-comms@example.com",
            group_name="Communication Operators",
            event_edition=event,
            organization=organization,
        )
    )

    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", user.email_normalized)
    page.fill("#id_password", TEST_OPERATIONAL_PASSWORD)
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/ops/communications/")
    page.wait_for_load_state("networkidle")
    assert page.locator("html").get_attribute("dir") == "ltr"

    page.locator("select[name=language]").select_option("ar")
    # The language form submits a same-path navigation.  `networkidle` can
    # resolve against the old document before that navigation starts, so wait
    # for the observable post-navigation state instead.
    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    assert "bootstrap.rtl.min.css" in page.content()
    assert page.get_by_role("heading", name="متابعة إرسال المراسلات").count() == 1
