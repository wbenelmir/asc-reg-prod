"""Scoped authorization policy and selector tests (Schema §12.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.utils import timezone

from apps.accounts.models import (
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
    ScopedGroupMembershipStatus,
)
from apps.accounts.policies import effective_scoped_memberships, has_scoped_permission
from apps.accounts.selectors import registrations_visible_to
from apps.events.models import EventEdition
from apps.organizations.models import Organization
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db

PERMISSION_CODENAME = "registrations.view_registration"


@pytest.fixture
def group() -> Group:
    return Group.objects.create(name="Test Intake")


@pytest.fixture
def permission() -> Permission:
    return Permission.objects.get(
        content_type__app_label="registrations", codename="view_registration"
    )


@pytest.fixture
def event_a() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="EVA", name="Event A", timezone="UTC", starts_at=now, ends_at=now
    )


@pytest.fixture
def event_b() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="EVB", name="Event B", timezone="UTC", starts_at=now, ends_at=now
    )


def _make_user(email: str, *, status: str = OperationalUserStatus.ACTIVE) -> OperationalUser:
    return OperationalUser.objects.create_user(email=email, password=None, status=status)


def test_user_without_group_permission_is_denied(event_a: EventEdition) -> None:
    user = _make_user("no-perm@example.com")
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is False


def test_user_with_permission_but_no_membership_is_denied(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    user = _make_user("perm-no-membership@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is False


def test_active_membership_in_scope_is_allowed(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    granter = _make_user("granter@example.com")
    user = _make_user("allowed@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        granted_by=granter,
        status=ScopedGroupMembershipStatus.ACTIVE,
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is True


def test_membership_cannot_cross_into_a_different_event(
    group: Group, permission: Permission, event_a: EventEdition, event_b: EventEdition
) -> None:
    granter = _make_user("granter2@example.com")
    user = _make_user("scoped-to-a@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    ScopedGroupMembership.objects.create(
        user=user, group=group, event_edition=event_a, granted_by=granter
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is True
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_b.pk) is False


def test_membership_cannot_cross_into_a_different_organization(
    group: Group, permission: Permission
) -> None:
    granter = _make_user("granter3@example.com")
    user = _make_user("scoped-to-org@example.com")
    org_a = Organization.objects.create(official_name="Org A", organization_type="COMPANY")
    org_b = Organization.objects.create(official_name="Org B", organization_type="COMPANY")
    group.permissions.add(permission)
    user.groups.add(group)
    ScopedGroupMembership.objects.create(
        user=user, group=group, organization=org_a, granted_by=granter
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, organization_id=org_a.pk) is True
    assert has_scoped_permission(user, PERMISSION_CODENAME, organization_id=org_b.pk) is False


def test_expired_membership_is_denied(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    granter = _make_user("granter4@example.com")
    user = _make_user("expired@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    now = timezone.now()
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        granted_by=granter,
        active_from=now - timedelta(days=10),
        active_until=now - timedelta(days=1),
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is False


def test_membership_is_expired_at_its_exact_end_instant(
    group: Group, permission: Permission, event_a: EventEdition, monkeypatch
) -> None:
    granter = _make_user("boundary-granter@example.com")
    user = _make_user("boundary-expired@example.com")
    group.permissions.add(permission)
    boundary = timezone.now()
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        granted_by=granter,
        active_from=boundary - timedelta(days=1),
        active_until=boundary,
    )
    monkeypatch.setattr("apps.accounts.policies.timezone.now", lambda: boundary)

    assert not effective_scoped_memberships(user, event_edition_id=event_a.pk).exists()


def test_future_membership_is_denied(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    granter = _make_user("granter5@example.com")
    user = _make_user("future@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    now = timezone.now()
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        granted_by=granter,
        active_from=now + timedelta(days=1),
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is False


def test_suspended_membership_is_denied(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    granter = _make_user("granter6@example.com")
    user = _make_user("suspended@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        granted_by=granter,
        status=ScopedGroupMembershipStatus.SUSPENDED,
    )
    assert has_scoped_permission(user, PERMISSION_CODENAME, event_edition_id=event_a.pk) is False


def test_superuser_is_explicitly_allowed_regardless_of_membership(event_a: EventEdition) -> None:
    superuser = OperationalUser.objects.create_superuser(email="root@example.com", password=None)
    assert (
        has_scoped_permission(superuser, PERMISSION_CODENAME, event_edition_id=event_a.pk) is True
    )


def test_anonymous_like_user_is_denied() -> None:
    from django.contrib.auth.models import AnonymousUser

    assert has_scoped_permission(AnonymousUser(), PERMISSION_CODENAME) is False


def test_selector_returns_only_registrations_in_scope(
    group: Group, permission: Permission, event_a: EventEdition, event_b: EventEdition
) -> None:
    granter = _make_user("granter7@example.com")
    user = _make_user("selector-user@example.com")
    group.permissions.add(permission)
    user.groups.add(group)
    ScopedGroupMembership.objects.create(
        user=user, group=group, event_edition=event_a, granted_by=granter
    )
    visible_registration = Registration.objects.create(
        public_reference="EVA-R-000001",
        event_edition=event_a,
        source_kind="OPEN",
        source_context_key="ctx-a",
    )
    Registration.objects.create(
        public_reference="EVB-R-000001",
        event_edition=event_b,
        source_kind="OPEN",
        source_context_key="ctx-b",
    )

    visible = registrations_visible_to(user)
    assert list(visible) == [visible_registration]


def test_organization_only_membership_never_grants_all_registrations(
    group: Group, permission: Permission, event_a: EventEdition
) -> None:
    granter = _make_user("org-selector-granter@example.com")
    user = _make_user("org-selector@example.com")
    organization_a = Organization.objects.create(
        official_name="Scoped Organization", organization_type="COMPANY"
    )
    organization_b = Organization.objects.create(
        official_name="Other Organization", organization_type="COMPANY"
    )
    group.permissions.add(permission)
    ScopedGroupMembership.objects.create(
        user=user, group=group, organization=organization_a, granted_by=granter
    )
    visible_registration = Registration.objects.create(
        public_reference="EVA-R-ORG-001",
        event_edition=event_a,
        source_kind="OPEN",
        source_context_key="org-a",
        source_organization=organization_a,
    )
    Registration.objects.create(
        public_reference="EVA-R-ORG-002",
        event_edition=event_a,
        source_kind="OPEN",
        source_context_key="org-b",
        source_organization=organization_b,
    )

    assert list(registrations_visible_to(user)) == [visible_registration]


def test_event_and_organization_scopes_are_kept_on_the_same_membership(
    group: Group, permission: Permission, event_a: EventEdition, event_b: EventEdition
) -> None:
    granter = _make_user("combined-selector-granter@example.com")
    user = _make_user("combined-selector@example.com")
    organization = Organization.objects.create(
        official_name="Combined Scope Organization", organization_type="COMPANY"
    )
    group.permissions.add(permission)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_a,
        organization=organization,
        granted_by=granter,
    )
    visible_registration = Registration.objects.create(
        public_reference="EVA-R-COMBINED-001",
        event_edition=event_a,
        source_kind="OPEN",
        source_context_key="combined-visible",
        source_organization=organization,
    )
    Registration.objects.create(
        public_reference="EVB-R-COMBINED-001",
        event_edition=event_b,
        source_kind="OPEN",
        source_context_key="wrong-event",
        source_organization=organization,
    )

    assert list(registrations_visible_to(user)) == [visible_registration]


def test_selector_returns_empty_queryset_for_unauthorized_user_never_raises() -> None:
    from django.contrib.auth.models import AnonymousUser

    result = registrations_visible_to(AnonymousUser())
    assert list(result) == []
