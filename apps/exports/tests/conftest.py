from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accounts.tests.sign_in import staff_sign_in
from apps.core.models import Country
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


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="EXPTEST",
        name="Exports Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def other_event() -> EventEdition:
    return EventEdition.objects.create(
        code="EXPTEST2",
        name="Exports Test 2",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="EVENT_OPERATIONS",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Export Org One",
        normalized_name="export org one",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(
        official_name="Export Org Two",
        normalized_name="export org two",
        organization_type=OrganizationType.INSTITUTION,
    )


def make_registration(*, event, organization=None, public_reference=None, status=None):
    return Registration.objects.create(
        public_reference=public_reference or f"EXP-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=status or RegistrationPublicStatus.APPROVED,
        internal_status=RegistrationInternalStatus.CLOSED,
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
