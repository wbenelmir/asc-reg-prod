"""Real-browser coverage for generic physical badge stock (Phase 3 Prompt 3).

Mirrors the established pattern in `test_digital_entry_pass.py`: a real
rendered page in a real Chromium instance against `pytest-django`'s
`live_server`. Also captures the functional screenshots requested for local
review into `var/screenshots/phase3_prompt3/named/` -- English and Arabic
RTL surfaces, since this prompt exposes real UI.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call, database_sync, operational_session_key

pytestmark = pytest.mark.django_db(transaction=True)

SCREENSHOT_DIR = Path(__file__).resolve().parents[2] / "var/screenshots/phase3_prompt3/named"


def _save(page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_DIR / name), full_page=True)


def _sign_in_operational(page, live_server, user) -> None:
    """Install a valid operational session cookie in the real browser.

    Copied from `test_digital_entry_pass.py` rather than imported: driving
    the sign-in form here would couple this test to that form's markup and
    to the MFA flow, neither of which is what any of these tests check.
    """
    from django.conf import settings

    session_key = operational_session_key(user)

    page.goto(f"{live_server.url}/healthz")
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_key,
                "url": live_server.url,
            }
        ]
    )


@database_sync
def _build_stock_scenario():
    """One event, two stock locations with real received stock, and one
    eligible registration with a current Badge Type assignment."""
    from datetime import timedelta

    from django.contrib.auth.models import Group
    from django.utils import timezone

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserStatus,
        ScopedGroupMembership,
    )
    from apps.accreditation.models import (
        AccessProfile,
        AccessProfileAssignment,
        AssignmentStatus,
        BadgeType,
        BadgeTypeAssignment,
        ParticipantRole,
        ParticipantRoleAssignment,
    )
    from apps.badges.services import (
        change_batch_status,
        create_print_batch,
        create_stock_location,
        new_operation_id,
        receive_print_batch,
    )
    from apps.core.models import Country, Sector
    from apps.events.models import EventEdition
    from apps.organizations.models import Organization, OrganizationType
    from apps.people.models import Person, PersonStatus
    from apps.registrations.models import (
        Registration,
        RegistrationInternalStatus,
        RegistrationPublicStatus,
        RegistrationSourceKind,
    )

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    now = timezone.now()
    event = EventEdition.objects.create(
        code="BROWSERSTOCK",
        name="Browser Stock Test",
        timezone="UTC",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
        status="EVENT_OPERATIONS",
    )
    organization = Organization.objects.create(
        official_name="Browser Stock Org",
        normalized_name="browser stock org",
        organization_type=OrganizationType.OTHER,
    )
    admin = OperationalUser.objects.create_user(
        email="browser-stock-admin@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    # Badge Stock Administrators is event-scoped only (ADR-0020 §6).
    ScopedGroupMembership.objects.create(
        user=admin,
        group=Group.objects.get(name="Badge Stock Administrators"),
        event_edition=event,
        granted_by=admin,
    )
    ScopedGroupMembership.objects.create(
        user=admin,
        group=Group.objects.get(name="Badge Stock Issuers"),
        event_edition=event,
        organization=organization,
        granted_by=admin,
    )

    badge_type = BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")
    central = create_stock_location(
        event_edition=event,
        code="CENTRAL",
        name="Central Store",
        name_fr="Magasin central",
        name_ar="المخزن المركزي",
        location_type="CENTRAL",
        actor=admin,
        operation_id=new_operation_id(),
    )
    create_stock_location(
        event_edition=event,
        code="GATE-A",
        name="Gate A Checkpoint",
        name_fr="Poste de contrôle porte A",
        name_ar="نقطة تفتيش البوابة أ",
        location_type="CHECKPOINT",
        actor=admin,
        operation_id=new_operation_id(),
    )

    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=50,
        artwork_version="v1",
        actor=admin,
        operation_id=new_operation_id(),
        destination_location=central,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="READY",
        actor=admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="IN_PRODUCTION",
        actor=admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = receive_print_batch(
        batch=batch,
        produced_quantity=50,
        accepted_quantity=45,
        damaged_quantity=5,
        actor=admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )

    role = ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")
    access_profile = AccessProfile.objects.create(
        event_edition=event, code="EXHIBITOR", name="Exhibitor"
    )
    person = Person.objects.create(status=PersonStatus.ACTIVE)
    registration = Registration.objects.create(
        public_reference=f"BRWSTK-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=RegistrationPublicStatus.APPROVED,
        internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
        submitted_at=now,
    )
    # Owner decision IDV-Q1: an approved context needs a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)
    common = {
        "event_edition": event,
        "organization": organization,
        "status": AssignmentStatus.CURRENT,
        "effective_from": now,
        "created_by": admin,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    BadgeTypeAssignment.objects.create(registration=registration, badge_type=badge_type, **common)
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )

    return {
        "event": event,
        "admin": admin,
        "badge_type": badge_type,
        "central": central,
        "registration": registration,
        "batch": batch,
    }


def test_the_stock_dashboard_renders_locations_batches_and_balances(live_server, page) -> None:
    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/stock/{scenario['event'].pk}/")
    expect(page.locator("h1")).to_contain_text("Badge stock")
    expect(page.locator("html")).to_have_attribute("dir", "ltr", timeout=10_000)

    # The real accounting result: 45 accepted, none issued yet.
    expect(
        page.locator("td[data-balance-location][data-balance-badge-type]").first
    ).to_contain_text("45")
    expect(page.get_by_text("Received", exact=False).first).to_be_visible()
    _save(page, "01-stock-dashboard-english.png")


def test_allocating_stock_reduces_available_without_moving_on_hand(live_server, page) -> None:
    """The allocation workflow end to end in the real UI (correction §1).

    On-hand stays at 45; allocated becomes 10; available becomes 35. The
    ledger is untouched, because reserving stock moves nothing.
    """
    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/stock/{scenario['event'].pk}/")
    on_hand = page.locator("td[data-balance-location][data-balance-badge-type]").first
    available = page.locator("td[data-available-location][data-available-badge-type]").first
    expect(on_hand).to_contain_text("45")
    expect(available).to_contain_text("45")

    # Scoped to the allocation form itself: the transfer form on the same
    # row also has a `quantity` field, and a page-wide `.first` picks that
    # one instead, silently submitting the allocation with no quantity.
    allocation_form = page.locator('form[action*="/allocate/"]').first
    allocation_form.locator('input[name="quantity"]').fill("10")
    allocation_form.get_by_role("button", name=re.compile("^Allocate$", re.I)).click()

    expect(page.locator("td[data-available-location]").first).to_contain_text("35", timeout=10_000)
    expect(page.locator("td[data-balance-location]").first).to_contain_text("45")
    expect(page.locator("td[data-reserved-location]").first).to_contain_text("10")
    expect(page.locator("tr[data-allocation]")).to_have_count(1)
    _save(page, "07-stock-dashboard-allocation-english.png")

    from apps.badges.models import BadgeStockLedgerEntry
    from apps.badges.services import current_balance, reserved_balance

    # One RECEIVE row only: allocation posted nothing to the ledger.
    assert database_call(lambda: BadgeStockLedgerEntry.objects.count()) == 1
    assert (
        database_call(
            lambda: current_balance(
                event_edition=scenario["event"],
                badge_type=scenario["badge_type"],
                location=scenario["central"],
            )
        )
        == 45
    )
    assert (
        database_call(
            lambda: reserved_balance(
                event_edition=scenario["event"],
                badge_type=scenario["badge_type"],
                location=scenario["central"],
            )
        )
        == 10
    )


def test_the_stock_dashboard_renders_right_to_left_in_arabic(live_server, page) -> None:
    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/stock/{scenario['event'].pk}/")
    page.locator("select[name=language]").select_option("ar")

    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    expect(page.locator("html")).to_have_attribute("lang", "ar", timeout=10_000)
    # The stock dashboard heading is genuinely translated, not left in English.
    expect(page.locator("h1")).to_have_text(re.compile(r"مخزون"))
    _save(page, "02-stock-dashboard-arabic-rtl.png")


def test_the_print_batch_detail_page_shows_the_receipt_split(live_server, page) -> None:
    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/stock/batches/{scenario['batch'].pk}/")
    expect(page.locator("h1")).to_contain_text("Print batch")
    expect(page.get_by_text("45", exact=True).first).to_be_visible()
    expect(page.get_by_text("5", exact=True).first).to_be_visible()
    _save(page, "03-print-batch-detail-english.png")


def test_issuing_a_badge_shows_it_as_the_current_issuance(live_server, page) -> None:
    from apps.accreditation.models import AssignmentStatus, BadgeTypeAssignment

    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/")
    expect(page.locator("h1")).to_contain_text("Physical badge")
    expect(page.get_by_role("button", name=re.compile("^Issue", re.I))).to_be_visible()
    _save(page, "04-registration-badge-issuance-before-english.png")

    page.get_by_role("button", name=re.compile("^Issue", re.I)).click()
    expect(page.locator("[data-issuance-status]").first).to_contain_text(
        "Issued", ignore_case=True, timeout=10_000
    )
    expect(page.get_by_role("button", name=re.compile("Replace badge", re.I))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Record return", re.I))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Report lost", re.I))).to_be_visible()
    _save(page, "05-registration-badge-issuance-issued-english.png")

    assignment = database_call(
        lambda: BadgeTypeAssignment.objects.get(
            registration=scenario["registration"], status=AssignmentStatus.CURRENT
        )
    )
    from apps.badges.selectors import current_issuance_for_assignment

    assert database_call(lambda: current_issuance_for_assignment(assignment)) is not None


def test_issuing_a_badge_can_draw_from_a_visible_allocation(live_server, page) -> None:
    from apps.badges.models import BadgeStockOperation, StockAllocationPurpose
    from apps.badges.services import allocate_stock, new_operation_id

    scenario = _build_stock_scenario()
    allocation = database_call(
        lambda: allocate_stock(
            event_edition=scenario["event"],
            badge_type=scenario["badge_type"],
            location=scenario["central"],
            quantity=4,
            purpose_code=StockAllocationPurpose.CHECKPOINT_OPENING,
            actor=scenario["admin"],
            operation_id=new_operation_id(),
        )
    )
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/")
    allocation_select = page.locator('select[name="allocation_id"]')
    expect(allocation_select.locator(f'option[value="{allocation.pk}"]')).to_have_count(1)
    allocation_select.select_option(str(allocation.pk))
    page.get_by_role("button", name=re.compile("^Issue", re.I)).click()

    expect(page.locator("[data-issuance-status]").first).to_contain_text(
        "Issued", ignore_case=True, timeout=10_000
    )
    database_call(lambda: allocation.refresh_from_db())
    assert allocation.consumed_quantity == 1
    assert (
        database_call(lambda: BadgeStockOperation.objects.get(operation_type="ISSUE")).allocation_id
        == allocation.pk
    )


def test_the_badge_issuance_page_renders_right_to_left_in_arabic(live_server, page) -> None:
    scenario = _build_stock_scenario()
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/")
    page.locator("select[name=language]").select_option("ar")

    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    expect(page.locator("html")).to_have_attribute("lang", "ar", timeout=10_000)
    expect(page.locator("h1")).to_have_text(re.compile(r"الشارة"))
    _save(page, "06-registration-badge-issuance-arabic-rtl.png")


def test_the_issue_form_only_ever_offers_the_assignments_own_badge_type(live_server, page) -> None:
    """No automatic substitution, made visible in the real UI: the issue
    form offers exactly one Badge Type option, the assignment's own -- there
    is no client-side path to request any other type at all."""
    from apps.accreditation.models import BadgeType
    from apps.badges.services import create_print_batch, new_operation_id

    scenario = _build_stock_scenario()
    wrong_type = database_call(
        lambda: BadgeType.objects.create(
            event_edition=scenario["event"], code="WRONGTYPE", name="Wrong"
        )
    )
    database_call(
        lambda: create_print_batch(
            event_edition=scenario["event"],
            badge_type=wrong_type,
            planned_quantity=10,
            actor=scenario["admin"],
            operation_id=new_operation_id(),
        )
    )
    _sign_in_operational(page, live_server, scenario["admin"])

    page.goto(f"{live_server.url}/ops/badges/registrations/{scenario['registration'].pk}/badge/")
    hidden_badge_type = page.locator('input[name="badge_type_id"]')
    expect(hidden_badge_type).to_have_value(str(scenario["badge_type"].pk))
    assert hidden_badge_type.get_attribute("value") != str(wrong_type.pk)
