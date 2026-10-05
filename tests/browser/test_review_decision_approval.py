"""Real-browser (Playwright) coverage for the Phase 2 Prompt 8 operational
APPROVED-decision journey (closing P7-H-01): sign in as a correctly scoped
reviewer, open a review case that already satisfies Prompt 4's assignment
eligibility, and record an APPROVED decision through the real case-detail
control.

Mirrors the established pattern in `test_exports_workflow.py`: a real
rendered page in a real Chromium instance against `pytest-django`'s
`live_server`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call

pytestmark = pytest.mark.django_db(transaction=True)


def test_reviewer_completes_the_approval_journey_through_the_real_ui(live_server, page) -> None:
    from django.utils import timezone

    from apps.accreditation.models import AccessProfile, BadgeType, ParticipantRole
    from apps.accreditation.services import assign
    from apps.events.models import EventEdition
    from apps.organizations.models import Organization, OrganizationType
    from apps.reviews.models import ReviewCaseType
    from apps.reviews.services import open_review_case
    from apps.reviews.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        make_operational_user_with_membership,
        make_registration,
    )

    event = database_call(
        lambda: EventEdition.objects.create(
            code="BROWSERAPPROVE",
            name="Browser Approve Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
            status="REGISTRATION_OPEN",
        )
    )
    organization = database_call(
        lambda: Organization.objects.create(
            official_name="Browser Approve Org",
            normalized_name="browser approve org",
            organization_type=OrganizationType.OTHER,
        )
    )
    registration = database_call(lambda: make_registration(event=event, organization=organization))
    case = database_call(
        lambda: open_review_case(
            registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
        )
    )
    manager = database_call(
        lambda: make_operational_user_with_membership(
            email="browser-approve@example.com",
            group_name="Accreditation Managers",
            event_edition=event,
            organization=organization,
        )
    )

    # Satisfy Prompt 4's approval-eligibility boundary BEFORE the journey --
    # this test proves the UI control, not the eligibility rule itself
    # (already covered at the service layer).
    role = database_call(
        lambda: ParticipantRole.objects.create(
            event_edition=event, code="BROWSER-ROLE", name="Delegate"
        )
    )
    badge = database_call(
        lambda: BadgeType.objects.create(event_edition=event, code="BROWSER-BADGE", name="Standard")
    )
    profile = database_call(
        lambda: AccessProfile.objects.create(
            event_edition=event, code="BROWSER-PROFILE", name="Standard access"
        )
    )
    database_call(
        lambda: assign(
            kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=manager
        )
    )
    database_call(
        lambda: assign(
            kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=manager
        )
    )
    database_call(
        lambda: assign(
            kind="ACCESS_PROFILE", registration=registration, reference_obj=profile, actor=manager
        )
    )
    # Owner decision IDV-Q1: approval also requires a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    database_call(lambda: make_verified_identity_case(registration))

    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", manager.email_normalized)
    page.fill("#id_password", TEST_OPERATIONAL_PASSWORD)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/ops/reviews/cases/{case.pk}/")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Record Approved decision").click()

    # `#main-content` is htmx-boosted (`hx-boost="true"`, base.html), so the
    # post-redirect page swaps in asynchronously -- an immediate `page.content()`
    # check can race the swap (the same gotcha fixed in
    # `test_exports_workflow.py`). Playwright's auto-retrying `expect(...)`
    # waits the swap out instead of racing it.
    decision_history = page.locator("table[aria-label='Decision history']")
    expect(decision_history).to_contain_text("Approved", timeout=10_000)
    # The case summary is a design-system definition list (UI/UX gate CP1);
    # it is located by its stable data hook rather than Bootstrap's `dl.row`.
    expect(page.locator("[data-case-summary]")).to_contain_text("Approved")

    database_call(lambda: registration.refresh_from_db())
    from apps.registrations.models import RegistrationPublicStatus

    assert registration.public_status == RegistrationPublicStatus.APPROVED


def test_approve_control_renders_rtl_in_arabic(live_server, page) -> None:
    """Phase 2 Prompt 8 §4.12: the approve control's own label must be
    reachable and correctly RTL-mirrored in Arabic, not only in English."""
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.organizations.models import Organization, OrganizationType
    from apps.reviews.models import ReviewCaseType
    from apps.reviews.services import open_review_case
    from apps.reviews.tests.conftest import (
        TEST_OPERATIONAL_PASSWORD,
        make_operational_user_with_membership,
        make_registration,
    )

    event = database_call(
        lambda: EventEdition.objects.create(
            code="BROWSERAPPROVEAR",
            name="Browser Approve Arabic Test",
            timezone="UTC",
            starts_at=timezone.now(),
            ends_at=timezone.now(),
            status="REGISTRATION_OPEN",
        )
    )
    organization = database_call(
        lambda: Organization.objects.create(
            official_name="Browser Approve Arabic Org",
            normalized_name="browser approve arabic org",
            organization_type=OrganizationType.OTHER,
        )
    )
    registration = database_call(lambda: make_registration(event=event, organization=organization))
    case = database_call(
        lambda: open_review_case(
            registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
        )
    )
    manager = database_call(
        lambda: make_operational_user_with_membership(
            email="browser-approve-ar@example.com",
            group_name="Accreditation Managers",
            event_edition=event,
            organization=organization,
        )
    )

    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", manager.email_normalized)
    page.fill("#id_password", TEST_OPERATIONAL_PASSWORD)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")

    page.goto(f"{live_server.url}/ops/reviews/cases/{case.pk}/")
    page.wait_for_load_state("networkidle")
    page.locator("select[name=language]").select_option("ar")

    # Wait for the language POST/redirect to replace the document; checking
    # `networkidle` immediately can race the navigation and inspect the old
    # English document.
    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    expect(page.get_by_role("button", name="تسجيل قرار الموافقة")).to_have_count(1)
