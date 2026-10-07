from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accounts.tests.sign_in import staff_sign_in
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
        code="REVTEST",
        name="Review Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def other_event() -> EventEdition:
    return EventEdition.objects.create(
        code="REVTEST2",
        name="Review Test 2",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="EVENT_OPERATIONS",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Review Org One",
        normalized_name="review org one",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(
        official_name="Review Org Two",
        normalized_name="review org two",
        organization_type=OrganizationType.INSTITUTION,
    )


def make_registration(*, event, organization=None, person=None, public_reference=None):
    import uuid

    return Registration.objects.create(
        public_reference=public_reference or f"REV-{uuid.uuid4().hex[:10].upper()}",
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

    staff_sign_in(client, email, TEST_OPERATIONAL_PASSWORD)
