"""Real-browser (Playwright) i18n/RTL and Organization Workspace scope
coverage for Phase 2 Prompt 2 (requirements 27, 28).

Mirrors the established pattern in `test_i18n_rtl_accessibility.py`: a real
rendered page in a real Chromium instance against `pytest-django`'s
`live_server`.
"""

from __future__ import annotations

import pytest

from tests.browser.database import database_call
from tests.browser.helpers import switch_language

pytestmark = pytest.mark.django_db(transaction=True)


def test_invitation_unavailable_is_english_by_default(live_server, page) -> None:
    page.goto(f"{live_server.url}/invite/never-issued-token/")
    assert page.locator("html").get_attribute("lang") == "en"
    assert page.locator("html").get_attribute("dir") == "ltr"
    assert "This invitation link is not available" in page.content()


def test_invitation_unavailable_renders_in_french(live_server, page) -> None:
    page.goto(f"{live_server.url}/invite/never-issued-token/")
    switch_language(page, "fr")
    assert page.locator("html").get_attribute("lang") == "fr"
    assert page.locator("html").get_attribute("dir") == "ltr"
    assert "Ce lien d'invitation n'est pas disponible" in page.content()


def test_invitation_unavailable_renders_rtl_in_arabic(live_server, page) -> None:
    page.goto(f"{live_server.url}/invite/never-issued-token/")
    switch_language(page, "ar")
    assert page.locator("html").get_attribute("lang") == "ar"
    assert page.locator("html").get_attribute("dir") == "rtl"
    assert "bootstrap.rtl.min.css" in page.content()
    # The generic-unavailable message itself must also be translated, not
    # merely the page chrome around it.
    assert "غير متاح" in page.content()


def test_workspace_dashboard_shows_only_scoped_campaigns_in_a_real_browser(
    live_server, page
) -> None:
    """Real-browser confirmation of the same scope-isolation the unit-level
    view test already proves (Gate G3, TRD WORK-003)."""
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.invitations.models import InvitationCampaignStatus
    from apps.invitations.services import change_campaign_status, create_campaign
    from apps.invitations.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        make_operational_user_with_membership,
    )
    from apps.organizations.models import Organization, OrganizationType

    event = database_call(
        lambda: EventEdition.objects.create(
            code="BROWSERINV",
            name="Browser Invitation Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
            status="REGISTRATION_OPEN",
        )
    )
    mine = database_call(
        lambda: Organization.objects.create(
            official_name="Mine Org",
            normalized_name="mine org",
            organization_type=OrganizationType.OTHER,
        )
    )
    theirs = database_call(
        lambda: Organization.objects.create(
            official_name="Theirs Org",
            normalized_name="theirs org",
            organization_type=OrganizationType.OTHER,
        )
    )
    visible = database_call(
        lambda: change_campaign_status(
            create_campaign(
                event_edition=event,
                organization=mine,
                name="Visible Browser Campaign",
                public_reference="CAMP-BROWSER-VIS-0001",
            ),
            InvitationCampaignStatus.ACTIVE,
        )
    )
    hidden = database_call(
        lambda: change_campaign_status(
            create_campaign(
                event_edition=event,
                organization=theirs,
                name="Hidden Browser Campaign",
                public_reference="CAMP-BROWSER-HID-0001",
            ),
            InvitationCampaignStatus.ACTIVE,
        )
    )
    user = database_call(
        lambda: make_operational_user_with_membership(
            email="browser-workspace@example.com",
            group_name="Invitation Manager",
            event_edition=event,
            organization=mine,
        )
    )

    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", user.email_normalized)
    page.fill("#id_password", TEST_OPERATIONAL_PASSWORD)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/organizations/workspace/")
    page.wait_for_load_state("networkidle")
    content = page.content()
    assert visible.public_reference in content
    assert hidden.public_reference not in content
