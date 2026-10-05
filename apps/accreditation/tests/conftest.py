from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accreditation.models import AccessProfile, AccessRule, BadgeType, ParticipantRole
from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
)

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105


@pytest.fixture(autouse=True)
def _seed_countries():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="ACCTEST",
        name="Accreditation Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def other_event() -> EventEdition:
    return EventEdition.objects.create(
        code="ACCTEST2",
        name="Accreditation Test 2",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="EVENT_OPERATIONS",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Accreditation Org One",
        normalized_name="accreditation org one",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(
        official_name="Accreditation Org Two",
        normalized_name="accreditation org two",
        organization_type=OrganizationType.INSTITUTION,
    )


@pytest.fixture
def role(event) -> ParticipantRole:
    return ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")


@pytest.fixture
def other_role(event) -> ParticipantRole:
    return ParticipantRole.objects.create(event_edition=event, code="SPEAKER", name="Speaker")


@pytest.fixture
def badge_type(event) -> BadgeType:
    return BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")


@pytest.fixture
def visible_badge_type(event) -> BadgeType:
    return BadgeType.objects.create(
        event_edition=event, code="VIP", name="VIP", participant_visible=True
    )


@pytest.fixture
def access_profile(event) -> AccessProfile:
    return AccessProfile.objects.create(event_edition=event, code="EXHIBITOR", name="Exhibitor")


@pytest.fixture
def access_rule(event) -> AccessRule:
    return AccessRule.objects.create(event_edition=event, code="HALL_A", name="Hall A")


def make_registration(*, event, organization=None, person=None, public_reference=None):
    import uuid

    return Registration.objects.create(
        public_reference=public_reference or f"ACC-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=RegistrationPublicStatus.SUBMITTED,
        internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
        submitted_at=timezone.now(),
    )


@pytest.fixture
def registration(event, organization):
    return make_registration(event=event, organization=organization)


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
