"""Shared fixtures for the Digital Entry Pass suite.

Signing keys used here are generated in memory by
`InMemorySigningKeyProvider` and discarded when the process exits. No
private key is ever written to disk, to a fixture file, to the database, or
to a review archive -- including test keys.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accreditation.models import (
    AccessProfile,
    AccessProfileAssignment,
    AssignmentStatus,
    BadgeType,
    BadgeTypeAssignment,
    ParticipantRole,
    ParticipantRoleAssignment,
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

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105


@pytest.fixture(autouse=True)
def _seed_reference_values(request):
    """Seed shared reference rows, but only for tests that use the database.

    Much of this suite pins the QR wire format and needs no database at all;
    forcing one on those tests would slow the suite and hide the fact that
    the canonical layer is pure.
    """
    if "db" not in request.fixturenames and "transactional_db" not in request.fixturenames:
        return
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def signing_provider():
    """An ephemeral in-memory provider, reset after every test."""
    provider = InMemorySigningKeyProvider(key_ids=("v1", "v2"), current="v1")
    set_signing_key_provider_for_testing(provider)
    yield provider
    set_signing_key_provider_for_testing(None)


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


@pytest.fixture
def event() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="BDGTEST",
        name="Badge Test Edition",
        timezone="UTC",
        starts_at=now - _days(1),
        ends_at=now + _days(30),
        status="EVENT_OPERATIONS",
    )


@pytest.fixture
def other_event() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="BDGTEST2",
        name="Badge Test Edition Two",
        timezone="UTC",
        starts_at=now - _days(1),
        ends_at=now + _days(30),
        status="EVENT_OPERATIONS",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Badge Org One",
        normalized_name="badge org one",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(
        official_name="Badge Org Two",
        normalized_name="badge org two",
        organization_type=OrganizationType.INSTITUTION,
    )


@pytest.fixture
def person() -> Person:
    return Person.objects.create(status=PersonStatus.ACTIVE)


@pytest.fixture
def role(event) -> ParticipantRole:
    return ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")


@pytest.fixture
def badge_type(event) -> BadgeType:
    return BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")


@pytest.fixture
def access_profile(event) -> AccessProfile:
    return AccessProfile.objects.create(event_edition=event, code="EXHIBITOR", name="Exhibitor")


def make_registration(
    *, event, organization=None, person=None, public_status=RegistrationPublicStatus.APPROVED
):
    registration = Registration.objects.create(
        public_reference=f"BDG-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=public_status,
        internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
        submitted_at=timezone.now(),
    )
    if person is not None:
        # Owner decision IDV-Q1: an approved context is eligible only with a
        # verified identity, so every factory registration gets one.
        from apps.people.tests.identity_fixtures import make_verified_identity_case

        make_verified_identity_case(registration)
    return registration


def grant_required_assignments(*, registration, event, actor, role, badge_type, access_profile):
    """Give a registration the three CURRENT assignments eligibility needs.

    Mirrors the real Phase 2 assignment shape rather than bypassing it, so
    these tests exercise the same invariants the accreditation app enforces.
    """
    now = timezone.now()
    common = {
        "event_edition": event,
        "organization": registration.source_organization,
        "status": AssignmentStatus.CURRENT,
        "effective_from": now,
        "created_by": actor,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    badge_assignment = BadgeTypeAssignment.objects.create(
        registration=registration, badge_type=badge_type, **common
    )
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )
    return badge_assignment


def make_operational_user_with_membership(
    *, email: str, group_name: str, event_edition=None, organization=None
):
    from django.contrib.auth.models import Group

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    user = OperationalUser.objects.create_user(
        email=email, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    group = Group.objects.get(name=group_name)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_edition,
        organization=organization,
        granted_by=user,
    )
    return user


def sign_in_operational(client, email: str) -> None:
    from django.urls import reverse

    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": email, "password": TEST_OPERATIONAL_PASSWORD},
    )


def publish_and_activate_key(*, provider, actor, key_id: str = "v1"):
    """Publish the provider's PUBLIC key and promote it to ACTIVE."""
    from apps.badges.services import promote_verification_key, publish_verification_key

    key = publish_verification_key(
        key_id=key_id,
        public_key_pem=provider.public_key_pem(key_id).decode("ascii"),
        actor=actor,
    )
    return promote_verification_key(key=key, actor=actor)


@pytest.fixture
def pass_admin(db, event, organization):
    return make_operational_user_with_membership(
        email="pass.admin@example.test",
        group_name="Pass Administrators",
        event_edition=event,
        organization=organization,
    )


@pytest.fixture
def key_custodian(db):
    return make_operational_user_with_membership(
        email="key.custodian@example.test",
        group_name="Credential Key Custodians",
    )


@pytest.fixture
def eligible_registration(
    db, event, organization, person, role, badge_type, access_profile, pass_admin
):
    registration = make_registration(event=event, organization=organization, person=person)
    grant_required_assignments(
        registration=registration,
        event=event,
        actor=pass_admin,
        role=role,
        badge_type=badge_type,
        access_profile=access_profile,
    )
    return registration


@pytest.fixture
def active_key(db, signing_provider, key_custodian):
    return publish_and_activate_key(provider=signing_provider, actor=key_custodian)


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
# ---------------------------------------------------------------------------


@pytest.fixture
def stock_admin(db, event):
    """Event-scoped only, deliberately with no organization: physical badge
    stock belongs to the event, not to any one organization (ADR-0020;
    neither `StockLocation` nor `PrintBatch` carries an organization field).
    """
    return make_operational_user_with_membership(
        email="stock.admin@example.test",
        group_name="Badge Stock Administrators",
        event_edition=event,
    )


@pytest.fixture
def stock_issuer(db, event, organization):
    """Issuance IS organization-scopable (it flows through the Registration
    Context's own organization), so this fixture keeps that dimension --
    unlike `stock_admin`, above."""
    return make_operational_user_with_membership(
        email="stock.issuer@example.test",
        group_name="Badge Stock Issuers",
        event_edition=event,
        organization=organization,
    )


@pytest.fixture
def stock_location(db, event, stock_admin):
    from apps.badges.services import create_stock_location, new_operation_id

    return create_stock_location(
        event_edition=event,
        code="CENTRAL",
        name="Central Store",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=new_operation_id(),
    )


@pytest.fixture
def other_location(db, event, stock_admin):
    from apps.badges.services import create_stock_location, new_operation_id

    return create_stock_location(
        event_edition=event,
        code="GATE-A",
        name="Gate A Checkpoint",
        location_type="CHECKPOINT",
        actor=stock_admin,
        operation_id=new_operation_id(),
    )


@pytest.fixture
def current_badge_assignment(eligible_registration):
    return BadgeTypeAssignment.objects.get(
        registration=eligible_registration, status=AssignmentStatus.CURRENT
    )


@pytest.fixture
def foreign_badge_type(db, other_event):
    """A Badge Type belonging to a DIFFERENT event edition.

    The bait for every cross-event test (Prompt 3 correction §2): every
    stock command must refuse it, whether it arrives through a forged POST
    or a direct service call.
    """
    return BadgeType.objects.create(event_edition=other_event, code="FOREIGN", name="Foreign")


@pytest.fixture
def foreign_location(db, other_event, stock_admin):
    """A stock location belonging to a DIFFERENT event edition."""
    from apps.badges.models import StockLocation

    return StockLocation.objects.create(
        event_edition=other_event,
        code="FOREIGN-LOC",
        name="Foreign Location",
        location_type="CENTRAL",
    )
