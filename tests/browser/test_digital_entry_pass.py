"""Real-browser coverage for the Digital Entry Pass participant surface.

Phase 3 Prompt 2. Mirrors the established pattern in
`test_review_decision_approval.py`: a real rendered page in a real Chromium
instance against `pytest-django`'s `live_server`.

Assertions use Playwright's auto-retrying `expect()` rather than reading
`page.content()` immediately after a click, because `#main-content` carries
`hx-boost="true"` and an immediate read races the async swap.
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import expect

from tests.browser.database import (
    database_call,
    database_sync,
    operational_session_key,
    participant_session_key,
)

pytestmark = pytest.mark.django_db(transaction=True)


@database_sync
def _build_active_pass():
    """Create one eligible registration with an ACTIVE credential."""
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
        activate_pass,
        generate_pass,
        new_operation_id,
        promote_verification_key,
        publish_verification_key,
    )
    from apps.core.crypto.signing import (
        InMemorySigningKeyProvider,
        set_signing_key_provider_for_testing,
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

    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    now = timezone.now()
    event = EventEdition.objects.create(
        code="BROWSERPASS",
        name="Browser Pass Test",
        timezone="UTC",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
        status="EVENT_OPERATIONS",
    )
    organization = Organization.objects.create(
        official_name="Browser Pass Org",
        normalized_name="browser pass org",
        organization_type=OrganizationType.OTHER,
    )
    actor = OperationalUser.objects.create_user(
        email="browser-pass@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=actor,
        group=Group.objects.get(name="Pass Administrators"),
        event_edition=event,
        organization=organization,
        granted_by=actor,
    )
    custodian = OperationalUser.objects.create_user(
        email="browser-custodian@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )

    person = Person.objects.create(status=PersonStatus.ACTIVE)
    registration = Registration.objects.create(
        public_reference=f"BRW-{uuid.uuid4().hex[:10].upper()}",
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

    role = ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")
    badge_type = BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")
    access_profile = AccessProfile.objects.create(
        event_edition=event, code="EXHIBITOR", name="Exhibitor"
    )
    common = {
        "event_edition": event,
        "organization": organization,
        "status": AssignmentStatus.CURRENT,
        "effective_from": now,
        "created_by": actor,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    BadgeTypeAssignment.objects.create(registration=registration, badge_type=badge_type, **common)
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )

    key = publish_verification_key(
        key_id="v1",
        public_key_pem=provider.public_key_pem("v1").decode("ascii"),
        actor=custodian,
    )
    promote_verification_key(key=key, actor=custodian)

    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return person, credential


def _sign_in_participant(page, live_server, person) -> None:
    """Install a valid participant session cookie in the real browser."""
    from django.conf import settings

    session_key = participant_session_key(person)

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


def _assert_qr_actually_paints(page) -> None:
    """Wait for the QR image to finish loading, then assert it really rendered.

    `naturalWidth` is 0 both for a broken image and for one that simply has
    not loaded yet, so the wait is required -- without it this assertion is a
    race that passes or fails depending on machine speed.
    """
    page.wait_for_function(
        "() => { const i = document.querySelector('img[data-pass-qr]');"
        " return i && i.complete && i.naturalWidth > 0; }",
        timeout=10_000,
    )
    width = page.locator("img[data-pass-qr]").evaluate("node => node.naturalWidth")
    assert width > 0, "the QR image must load, not 404"


def _sign_in_operational(page, live_server, user) -> None:
    """Install a valid operational session cookie in the real browser.

    Driving the sign-in form here would couple this test to that form's
    markup and to the MFA flow, neither of which is what it is checking.
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


def test_a_participant_sees_their_active_pass_in_english(live_server, page) -> None:
    person, credential = _build_active_pass()
    _sign_in_participant(page, live_server, person)

    page.goto(f"{live_server.url}/my-passes/")
    expect(page.locator("h1")).to_contain_text("My entry passes")
    expect(page.locator("html")).to_have_attribute("dir", "ltr", timeout=10_000)

    # A real rendered image, not text pretending to be a QR code.
    qr = page.locator("img[data-pass-qr]")
    expect(qr).to_have_count(1)
    expect(qr).to_be_visible()
    expect(qr).to_have_attribute("alt", re.compile("QR code", re.I), timeout=10_000)

    # It must actually paint: a broken image has zero natural width.
    _assert_qr_actually_paints(page)

    # The safe fallback reference is shown alongside it.
    expect(page.get_by_text("ASC-", exact=False).first).to_be_visible()


def test_the_pass_renders_right_to_left_in_arabic(live_server, page) -> None:
    person, credential = _build_active_pass()
    _sign_in_participant(page, live_server, person)

    # Switch language through the real switcher, exactly as a participant
    # would -- the language route is POST-only, so a GET would silently
    # leave the page in English.
    page.goto(f"{live_server.url}/my-passes/")
    page.locator("select[name=language]").select_option("ar")

    expect(page.locator("html")).to_have_attribute("dir", "rtl", timeout=10_000)
    expect(page.locator("html")).to_have_attribute("lang", "ar", timeout=10_000)
    # The fallback reference stays left-to-right inside the RTL page.
    expect(page.locator("bdi").first).to_be_visible()

    # The QR renders in Arabic too, and is never mirrored: its container is
    # explicitly ltr, because a mirrored symbol would not scan.
    qr = page.locator("img[data-pass-qr]")
    expect(qr).to_have_count(1)
    expect(qr).to_be_visible()
    _assert_qr_actually_paints(page)
    container_direction = qr.evaluate("node => getComputedStyle(node.parentElement).direction")
    assert container_direction == "ltr", "the QR must never be mirrored in RTL"
    # The participant status text must be translated too, not just the
    # chrome -- an eagerly-evaluated module-level message map would leave
    # English here, so this assertion is load-bearing.
    expect(page.locator("[data-pass-status]").first).not_to_contain_text(
        "Your entry pass is active", timeout=10_000
    )


def test_the_print_view_renders_for_the_owner(live_server, page) -> None:
    person, credential = _build_active_pass()
    # refresh_from_db() cleared the related cache, so the series is a lazy
    # read; resolve it on the database worker, never on Playwright's loop.
    series_public_id = database_call(lambda: credential.series.public_id)
    _sign_in_participant(page, live_server, person)

    page.goto(f"{live_server.url}/my-passes/{series_public_id}/print/")
    expect(page.locator(".pass-print-sheet")).to_be_visible(timeout=10_000)

    qr = page.locator("img[data-pass-qr]")
    expect(qr).to_have_count(1)
    expect(qr).to_be_visible()
    _assert_qr_actually_paints(page)


def test_a_non_active_pass_shows_no_qr_to_the_participant(live_server, page) -> None:
    from apps.badges.models import DigitalEntryPassStatus

    person, credential = _build_active_pass()
    credential.status = DigitalEntryPassStatus.SUSPENDED
    database_call(lambda: credential.save(update_fields=["status"]))

    _sign_in_participant(page, live_server, person)
    page.goto(f"{live_server.url}/my-passes/")

    expect(page.locator("h1")).to_contain_text("My entry passes")
    expect(page.locator("img[data-pass-qr]")).to_have_count(0)


@pytest.mark.parametrize(
    ("status", "expected_text"),
    [
        ("INACTIVE", "not yet active"),
        ("SUSPENDED", "temporarily on hold"),
        ("REVOKED", "no longer valid"),
        ("EXPIRED", "validity period has ended"),
    ],
)
def test_no_non_active_state_renders_a_qr(live_server, page, status, expected_text) -> None:
    """Every non-active state shows safe guidance and no QR graphic at all."""
    person, credential = _build_active_pass()
    credential.status = status
    database_call(lambda: credential.save(update_fields=["status"]))

    _sign_in_participant(page, live_server, person)
    page.goto(f"{live_server.url}/my-passes/")

    expect(page.locator("h1")).to_contain_text("My entry passes")
    expect(page.get_by_text(expected_text, exact=False).first).to_be_visible(timeout=10_000)
    expect(page.locator("img[data-pass-qr]")).to_have_count(0)
    # A revoked or expired pass must never read as "you never had one".
    expect(page.get_by_text("do not have an entry pass", exact=False)).to_have_count(0)


def test_the_verification_key_page_shows_lifecycle_controls(live_server, page) -> None:
    """Retirement and emergency revocation are reachable from the real UI."""
    from django.contrib.auth.models import Group

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserStatus,
        ScopedGroupMembership,
    )

    _person, _credential = _build_active_pass()

    custodian = database_call(
        lambda: OperationalUser.objects.create_user(
            email="browser-keys@example.com",
            password="__test_password__",  # noqa: S106
            status=OperationalUserStatus.ACTIVE,
        )
    )
    database_call(
        lambda: ScopedGroupMembership.objects.create(
            user=custodian,
            group=Group.objects.get(name="Credential Key Custodians"),
            granted_by=custodian,
        )
    )

    _sign_in_operational(page, live_server, custodian)

    page.goto(f"{live_server.url}/ops/badges/verification-keys/")
    expect(page.locator("h1")).to_contain_text("Verification keys", timeout=10_000)
    expect(page.get_by_role("button", name=re.compile("Retire key", re.I))).to_be_visible()
    # Reasons are controlled selects now, not hidden fixed values.
    expect(page.locator("select[name=reason_code]")).to_have_count(2)
    expect(
        page.locator("label", has_text=re.compile("Emergency revocation reason", re.I))
    ).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Revoke key", re.I))).to_be_visible()
    # Public material only -- never a private key on screen.
    assert "PRIVATE KEY" not in page.content().upper()


def test_an_active_pass_past_its_window_shows_no_qr(live_server, page) -> None:
    """Fail closed on time in the real UI, with no sweep having run."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.badges.models import DigitalEntryPass, DigitalEntryPassStatus

    person, credential = _build_active_pass()
    past = timezone.now() - timedelta(days=2)
    database_call(
        lambda: DigitalEntryPass.objects.filter(pk=credential.pk).update(
            valid_from=past - timedelta(days=1), valid_until=past
        )
    )

    _sign_in_participant(page, live_server, person)
    page.goto(f"{live_server.url}/my-passes/")

    expect(page.locator("h1")).to_contain_text("My entry passes")
    expect(page.get_by_text("validity period has ended", exact=False).first).to_be_visible(
        timeout=10_000
    )
    expect(page.locator("img[data-pass-qr]")).to_have_count(0)

    # The stored row is untouched: a GET must not mutate state.
    database_call(lambda: credential.refresh_from_db())
    assert credential.status == DigitalEntryPassStatus.ACTIVE
