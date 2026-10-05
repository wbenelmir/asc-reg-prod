"""Temporary external-security operational-account tests (Phase 2 Prompt 4 §7)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import (
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accounts.services import (
    ExternalSecurityAccountError,
    activate_external_security_account,
    change_external_security_account_scope,
    create_external_security_account,
    deactivate_external_security_account,
)
from apps.core.models import Country
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_countries():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="EXTSEC",
        name="External Security Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Security Vendor",
        normalized_name="security vendor",
        organization_type=OrganizationType.OTHER,
    )


@pytest.fixture
def creator():
    from apps.accounts.models import OperationalUser

    return OperationalUser.objects.create_user(
        email="creator@example.com", password=None, status=OperationalUserStatus.ACTIVE
    )


def test_creation_requires_an_expiry(creator):
    with pytest.raises(ExternalSecurityAccountError):
        create_external_security_account(
            email="no-expiry@example.com",
            display_name="No Expiry",
            expires_at=None,
            created_by=creator,
        )


def test_creation_rejects_a_past_expiry(creator):
    with pytest.raises(ExternalSecurityAccountError):
        create_external_security_account(
            email="already-expired@example.com",
            display_name="Already expired",
            expires_at=timezone.now() - timedelta(seconds=1),
            created_by=creator,
        )


def test_creation_produces_a_scoped_zero_permission_account(event, organization, creator):
    expires_at = timezone.now() + timedelta(days=3)
    user = create_external_security_account(
        email="guard@example.com",
        display_name="Guard",
        expires_at=expires_at,
        created_by=creator,
        event_edition=event,
        organization=organization,
    )
    assert user.account_type == OperationalUserAccountType.EXTERNAL_SECURITY
    assert user.active_until == expires_at
    assert user.is_active is True
    membership = ScopedGroupMembership.objects.get(user=user)
    assert membership.group.name == "External Security (Temporary)"
    assert membership.event_edition_id == event.pk
    assert membership.organization_id == organization.pk
    # Deny-by-default: the temporary group carries NO permission.
    assert membership.group.permissions.count() == 0


def test_account_denied_server_side_once_expired(event, organization, creator):
    user = create_external_security_account(
        email="expiring@example.com",
        display_name="Expiring",
        expires_at=timezone.now() + timedelta(seconds=1),
        created_by=creator,
        event_edition=event,
        organization=organization,
    )
    assert user.is_active is True
    user.active_until = timezone.now() - timedelta(seconds=1)
    user.save(update_fields=["active_until"])
    user.refresh_from_db()
    assert user.is_active is False


def test_account_denied_before_its_active_from_window(creator):
    from apps.accounts.models import OperationalUser

    user = OperationalUser.objects.create_user(
        email="not-yet-active@example.com",
        password=None,
        status=OperationalUserStatus.ACTIVE,
        active_from=timezone.now() + timedelta(days=1),
    )
    assert user.is_active is False


def test_scope_change_closes_prior_membership_and_opens_a_new_one(event, organization, creator):
    from apps.organizations.models import Organization as OrgModel

    other_org = OrgModel.objects.create(
        official_name="Other Venue",
        normalized_name="other venue",
        organization_type=OrganizationType.OTHER,
    )
    user = create_external_security_account(
        email="rescope@example.com",
        display_name="Rescope",
        expires_at=timezone.now() + timedelta(days=3),
        created_by=creator,
        event_edition=event,
        organization=organization,
    )
    change_external_security_account_scope(
        user=user, actor=creator, event_edition=event, organization=other_org
    )
    memberships = ScopedGroupMembership.objects.filter(user=user).order_by("created_at")
    assert memberships.count() == 2
    assert memberships[0].active_until is not None  # closed
    assert memberships[1].organization_id == other_org.pk
    assert memberships[1].active_until is not None or memberships[1].active_until is None


def test_scope_change_rejects_a_non_external_security_account(creator):
    from apps.accounts.models import OperationalUser

    internal_user = OperationalUser.objects.create_user(
        email="internal@example.com", password=None, status=OperationalUserStatus.ACTIVE
    )
    with pytest.raises(ExternalSecurityAccountError):
        change_external_security_account_scope(user=internal_user, actor=creator)


def test_deactivation_requires_a_reason(event, organization, creator):
    user = create_external_security_account(
        email="deactivate-no-reason@example.com",
        display_name="D",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=creator,
    )
    with pytest.raises(ExternalSecurityAccountError):
        deactivate_external_security_account(user=user, actor=creator, reason="")


def test_deactivation_suspends_the_account(event, organization, creator):
    user = create_external_security_account(
        email="deactivate@example.com",
        display_name="D",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=creator,
    )
    deactivate_external_security_account(user=user, actor=creator, reason="No longer needed.")
    user.refresh_from_db()
    assert user.status == OperationalUserStatus.SUSPENDED
    assert user.is_active is False
    assert not ScopedGroupMembership.objects.filter(
        user=user, active_until__gt=timezone.now()
    ).exists()


def test_activation_reopens_a_bounded_scope(event, organization, creator):
    user = create_external_security_account(
        email="reactivate@example.com",
        display_name="Reactivate",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=creator,
        event_edition=event,
        organization=organization,
    )
    deactivate_external_security_account(user=user, actor=creator, reason="Shift ended.")
    new_expiry = timezone.now() + timedelta(days=2)
    activate_external_security_account(
        user=user,
        actor=creator,
        expires_at=new_expiry,
        event_edition=event,
        organization=organization,
    )
    user.refresh_from_db()
    assert user.status == OperationalUserStatus.ACTIVE
    assert user.active_until == new_expiry
    assert user.is_active is True


def test_creation_and_lifecycle_events_are_audited(event, organization, creator):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    user = create_external_security_account(
        email="audited@example.com",
        display_name="Audited",
        expires_at=timezone.now() + timedelta(days=1),
        created_by=creator,
        event_edition=event,
        organization=organization,
    )
    assert AuditEvent.objects.filter(
        action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_CREATED, target_uuid=user.pk
    ).exists()
    deactivate_external_security_account(user=user, actor=creator, reason="Done.")
    assert AuditEvent.objects.filter(
        action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_DEACTIVATED, target_uuid=user.pk
    ).exists()
