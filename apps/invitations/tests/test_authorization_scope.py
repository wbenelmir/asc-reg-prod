"""Authorization and scope-isolation tests (Phase 2 Prompt 2 requirements
24-27; reuses the Phase 1 `ScopedGroupMembership`/`has_scoped_permission`
model exactly)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser, Group
from django.utils import timezone

from apps.accounts.models import (
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
    ScopedGroupMembershipStatus,
)
from apps.invitations.models import InvitationCampaignStatus
from apps.invitations.selectors import campaigns_visible_to
from apps.invitations.services import change_campaign_status, create_campaign, issue_initial_link

pytestmark = pytest.mark.django_db


def _user(email: str, *, status=OperationalUserStatus.ACTIVE) -> OperationalUser:
    return OperationalUser.objects.create_user(email=email, password=None, status=status)


@pytest.fixture
def campaign(event, organization):
    return change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Scope Campaign",
            public_reference="CAMP-SCOPE-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )


def _membership(user, group_name, **kwargs):
    group = Group.objects.get(name=group_name)
    return ScopedGroupMembership.objects.create(user=user, group=group, granted_by=user, **kwargs)


def test_user_with_no_membership_sees_no_campaigns(campaign) -> None:
    user = _user("no-membership@example.com")
    assert campaigns_visible_to(user).count() == 0


def test_membership_scoped_to_the_wrong_organization_sees_nothing(
    campaign, event, other_organization
) -> None:
    user = _user("wrong-org@example.com")
    _membership(user, "Invitation Manager", event_edition=event, organization=other_organization)
    assert campaigns_visible_to(user).count() == 0


def test_membership_scoped_to_the_wrong_event_sees_nothing(
    campaign, organization, other_event
) -> None:
    user = _user("wrong-event@example.com")
    _membership(user, "Invitation Manager", event_edition=other_event, organization=organization)
    assert campaigns_visible_to(user).count() == 0


def test_correctly_scoped_membership_sees_the_campaign(campaign, event, organization) -> None:
    user = _user("right-scope@example.com")
    _membership(user, "Invitation Manager", event_edition=event, organization=organization)
    assert campaigns_visible_to(user).filter(pk=campaign.pk).exists()


def test_inactive_membership_sees_nothing(campaign, event, organization) -> None:
    user = _user("inactive-membership@example.com")
    _membership(
        user,
        "Invitation Manager",
        event_edition=event,
        organization=organization,
        status=ScopedGroupMembershipStatus.SUSPENDED,
    )
    assert campaigns_visible_to(user).count() == 0


def test_future_membership_sees_nothing_yet(campaign, event, organization) -> None:
    user = _user("future-membership@example.com")
    _membership(
        user,
        "Invitation Manager",
        event_edition=event,
        organization=organization,
        active_from=timezone.now() + timedelta(days=1),
    )
    assert campaigns_visible_to(user).count() == 0


def test_expired_membership_sees_nothing(campaign, event, organization) -> None:
    user = _user("expired-membership@example.com")
    _membership(
        user,
        "Invitation Manager",
        event_edition=event,
        organization=organization,
        active_until=timezone.now() - timedelta(days=1),
    )
    assert campaigns_visible_to(user).count() == 0


def test_permission_from_one_group_never_combines_with_scope_from_another(
    campaign, event, organization
) -> None:
    """Holding `view_invitationcampaign` through an UNSCOPED group must never
    combine with a differently-scoped, unrelated membership to leak scope."""
    user = _user("split-permission@example.com")
    unscoped_group = Group.objects.create(name="Unrelated Unscoped Group")
    from django.contrib.auth.models import Permission

    permission = Permission.objects.get(
        content_type__app_label="invitations", codename="view_invitationcampaign"
    )
    unscoped_group.permissions.add(permission)
    user.groups.add(unscoped_group)

    other_group = Group.objects.create(name="Scoped But Different Group")
    ScopedGroupMembership.objects.create(
        user=user,
        group=other_group,
        event_edition=event,
        organization=organization,
        granted_by=user,
    )
    assert campaigns_visible_to(user).count() == 0


def test_suspended_operational_account_sees_nothing(campaign, event, organization) -> None:
    user = _user("suspended-account@example.com", status=OperationalUserStatus.SUSPENDED)
    _membership(user, "Invitation Manager", event_edition=event, organization=organization)
    assert campaigns_visible_to(user).count() == 0


def test_anonymous_user_sees_nothing(campaign) -> None:
    assert campaigns_visible_to(AnonymousUser()).count() == 0


def test_superuser_sees_every_campaign(campaign) -> None:
    superuser = OperationalUser.objects.create_superuser(
        email="super@example.com",
        password="__irrelevant_not_used_for_auth__",  # noqa: S106
    )
    assert campaigns_visible_to(superuser).filter(pk=campaign.pk).exists()


def test_link_possession_alone_grants_no_workspace_visibility(
    campaign, event, organization
) -> None:
    """Resolving/using a valid invitation link must never itself grant
    Organization Workspace access -- only an explicit ScopedGroupMembership
    does (TRD WORK-003, Gate G3)."""
    _link, _raw_token = issue_initial_link(campaign)
    user = _user("link-holder@example.com")  # No ScopedGroupMembership at all.
    assert campaigns_visible_to(user).count() == 0


# ---------------------------------------------------------------------------
# Least-privilege default operational permissions (Phase 2 Prompt 2
# correction pass requirement 11): the BASIC "Organization User" group
# must never carry a sensitive write permission by default. Each sensitive
# write capability requires its OWN separate, explicitly-granted group.
# ---------------------------------------------------------------------------


def test_organization_user_group_has_no_write_permissions() -> None:
    from django.contrib.auth.models import Group

    group = Group.objects.get(name="Organization User")
    codenames = set(group.permissions.values_list("codename", flat=True))
    forbidden = {
        "add_delegationbatch",
        "add_invitationcampaign",
        "change_invitationcampaign",
        "add_invitationlink",
        "change_invitationlink",
        "register_on_behalf",
    }
    assert codenames.isdisjoint(forbidden), (
        f"unexpected write permission(s): {codenames & forbidden}"
    )


def test_organization_user_group_has_only_view_permissions() -> None:
    from django.contrib.auth.models import Group

    group = Group.objects.get(name="Organization User")
    codenames = set(group.permissions.values_list("codename", flat=True))
    assert codenames, "Organization User should have at least the baseline view permissions"
    assert all(codename.startswith("view_") for codename in codenames)


def test_invitation_manager_group_does_not_include_delegation_or_on_behalf_writes() -> None:
    from django.contrib.auth.models import Group

    group = Group.objects.get(name="Invitation Manager")
    codenames = set(group.permissions.values_list("codename", flat=True))
    assert "add_delegationbatch" not in codenames
    assert "register_on_behalf" not in codenames


def test_delegation_write_requires_the_dedicated_delegation_coordinator_group() -> None:
    from django.contrib.auth.models import Group

    coordinator_group = Group.objects.get(name="Delegation Coordinator")
    assert "add_delegationbatch" in set(
        coordinator_group.permissions.values_list("codename", flat=True)
    )
    for other_name in ("Organization User", "Invitation Manager", "On-Behalf Registrar"):
        other_group = Group.objects.get(name=other_name)
        assert "add_delegationbatch" not in set(
            other_group.permissions.values_list("codename", flat=True)
        )


def test_on_behalf_write_requires_the_dedicated_registrar_group() -> None:
    from django.contrib.auth.models import Group

    registrar_group = Group.objects.get(name="On-Behalf Registrar")
    assert "register_on_behalf" in set(
        registrar_group.permissions.values_list("codename", flat=True)
    )
    for other_name in ("Organization User", "Invitation Manager", "Delegation Coordinator"):
        other_group = Group.objects.get(name=other_name)
        assert "register_on_behalf" not in set(
            other_group.permissions.values_list("codename", flat=True)
        )


def test_organization_user_cannot_upload_a_delegation_batch(event, organization) -> None:
    """End-to-end proof, not just a permission-table check: the default
    Organization User role cannot actually perform the sensitive action."""
    from apps.accounts.policies import has_scoped_permission
    from apps.invitations.services import DelegationHeaderError  # noqa: F401 - documents intent

    user = _user("basic-org-user@example.com")
    _membership(user, "Organization User", event_edition=event, organization=organization)
    assert (
        has_scoped_permission(
            user, "invitations.add_delegationbatch", organization_id=organization.pk
        )
        is False
    )


def test_organization_user_cannot_register_on_behalf(event, organization) -> None:
    from apps.accounts.policies import has_scoped_permission

    user = _user("basic-org-user-2@example.com")
    _membership(user, "Organization User", event_edition=event, organization=organization)
    assert (
        has_scoped_permission(
            user, "registrations.register_on_behalf", organization_id=organization.pk
        )
        is False
    )
