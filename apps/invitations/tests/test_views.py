"""Organization Workspace view-level scope isolation and query-count tests
(Phase 2 Prompt 2 requirements 27, 29)."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.invitations.models import InvitationCampaignStatus
from apps.invitations.services import change_campaign_status, create_campaign

from .conftest import make_operational_user_with_membership, sign_in_operational

pytestmark = pytest.mark.django_db


def test_workspace_dashboard_requires_sign_in() -> None:
    client = Client()
    response = client.get(reverse("invitations:workspace-dashboard"))
    assert response.status_code == 302


def test_workspace_dashboard_only_shows_scoped_campaigns(
    event, organization, other_organization
) -> None:
    visible_campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Visible Campaign",
            public_reference="CAMP-VIEW-VIS-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    hidden_campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=other_organization,
            name="Hidden Campaign",
            public_reference="CAMP-VIEW-HID-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    user = make_operational_user_with_membership(
        email="workspace-scope@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(reverse("invitations:workspace-dashboard"))
    assert response.status_code == 200
    content = response.content.decode()
    assert visible_campaign.public_reference in content
    assert hidden_campaign.public_reference not in content


def test_campaign_detail_returns_404_for_an_out_of_scope_campaign(
    event, organization, other_organization
) -> None:
    hidden_campaign = create_campaign(
        event_edition=event,
        organization=other_organization,
        name="Not Yours",
        public_reference="CAMP-VIEW-404-0001",
    )
    user = make_operational_user_with_membership(
        email="workspace-404@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(reverse("invitations:campaign-detail", kwargs={"pk": hidden_campaign.pk}))
    assert response.status_code == 404


def test_workspace_dashboard_query_count_is_bounded(
    event, organization, django_assert_max_num_queries
) -> None:
    for index in range(10):
        change_campaign_status(
            create_campaign(
                event_edition=event,
                organization=organization,
                name=f"Bulk Campaign {index}",
                public_reference=f"CAMP-BULK-{index:04d}",
            ),
            InvitationCampaignStatus.ACTIVE,
        )
    user = make_operational_user_with_membership(
        email="query-count@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    # A fixed, small upper bound regardless of how many campaigns exist --
    # proves the dashboard does not issue one query per row (N+1).
    with django_assert_max_num_queries(20):
        response = client.get(reverse("invitations:workspace-dashboard"))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Authorization-aware UI (Phase 2 Prompt 2 correction pass requirement 9):
# a control the user cannot use is never rendered, even though the
# server-side permission check remains the real enforcement either way.
# ---------------------------------------------------------------------------


def test_view_only_user_never_sees_campaign_change_controls(event, organization) -> None:
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Controls Campaign",
            public_reference="CAMP-CTRL-VIEW-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    viewer = make_operational_user_with_membership(
        email="view-only-campaign@example.com",
        group_name="Organization User",  # view_invitationcampaign only
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.get(reverse("invitations:campaign-detail", kwargs={"pk": campaign.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    # `{% url %}` renders the resolved PATH, never the url-pattern NAME --
    # assert on the actual rendered form actions.
    assert reverse("invitations:campaign-issue-link", kwargs={"pk": campaign.pk}) not in content
    assert reverse("invitations:campaign-rotate-link", kwargs={"pk": campaign.pk}) not in content
    assert reverse("invitations:campaign-change-status", kwargs={"pk": campaign.pk}) not in content


def test_change_capable_user_sees_campaign_change_controls(event, organization) -> None:
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Controls Campaign 2",
            public_reference="CAMP-CTRL-CHANGE-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    manager = make_operational_user_with_membership(
        email="change-capable-campaign@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.get(reverse("invitations:campaign-detail", kwargs={"pk": campaign.pk}))
    assert response.status_code == 200
    content = response.content.decode()
    assert reverse("invitations:campaign-issue-link", kwargs={"pk": campaign.pk}) in content
    assert reverse("invitations:campaign-change-status", kwargs={"pk": campaign.pk}) in content


def test_view_only_user_never_sees_the_delegation_apply_control(event, organization) -> None:
    from apps.invitations.services import upload_delegation_batch

    coordinator = make_operational_user_with_membership(
        email="delegation-coordinator-ctrl@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=organization,
    )
    batch = upload_delegation_batch(
        organization=organization,
        event_edition=event,
        csv_bytes=b"email,given_names,family_name\napply-ctrl@example.com,A,B\n",
        uploaded_filename="delegation.csv",
        created_by=coordinator,
        idempotency_key="delegation-ctrl-view-only",
    )
    viewer = make_operational_user_with_membership(
        email="view-only-delegation@example.com",
        group_name="Organization User",  # view_delegationbatch only
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.get(reverse("invitations:delegation-detail", kwargs={"pk": batch.pk}))
    assert response.status_code == 200
    assert (
        reverse("invitations:delegation-apply", kwargs={"pk": batch.pk})
        not in response.content.decode()
    )


def test_delegation_coordinator_sees_the_delegation_apply_control(event, organization) -> None:
    from apps.invitations.services import upload_delegation_batch

    coordinator = make_operational_user_with_membership(
        email="delegation-coordinator-ctrl2@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=organization,
    )
    batch = upload_delegation_batch(
        organization=organization,
        event_edition=event,
        csv_bytes=b"email,given_names,family_name\napply-ctrl2@example.com,A,B\n",
        uploaded_filename="delegation.csv",
        created_by=coordinator,
        idempotency_key="delegation-ctrl-change-capable",
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.get(reverse("invitations:delegation-detail", kwargs={"pk": batch.pk}))
    assert response.status_code == 200
    assert (
        reverse("invitations:delegation-apply", kwargs={"pk": batch.pk})
        in response.content.decode()
    )


def test_server_side_permission_still_enforced_even_if_a_control_were_hidden(
    event, organization
) -> None:
    """Direct POST to the apply endpoint by a view-only user is denied by
    the decorator regardless of what the rendered template shows."""
    from apps.invitations.services import upload_delegation_batch

    coordinator = make_operational_user_with_membership(
        email="delegation-coordinator-enforce@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=organization,
    )
    batch = upload_delegation_batch(
        organization=organization,
        event_edition=event,
        csv_bytes=b"email,given_names,family_name\nenforce@example.com,A,B\n",
        uploaded_filename="delegation.csv",
        created_by=coordinator,
        idempotency_key="delegation-enforce-server-side",
    )
    viewer = make_operational_user_with_membership(
        email="view-only-enforce@example.com",
        group_name="Organization User",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.post(reverse("invitations:delegation-apply", kwargs={"pk": batch.pk}))
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    batch.refresh_from_db()
    assert batch.status != "APPLIED"


# ---------------------------------------------------------------------------
# Action-specific object scope on every mutation (Phase 2 Prompt 2 V2
# correction pass requirement 2): a user with WRITE scope for organization A
# and only VIEW scope for organization B must never be able to mutate an
# object belonging to B, even though `operational_permission_required`
# alone would let them past the decorator (it only proves the permission is
# held through SOME membership, never that it applies to THIS object).
# ---------------------------------------------------------------------------


def _add_membership(user, group_name: str, *, event_edition=None, organization=None):
    from django.contrib.auth.models import Group

    from apps.accounts.models import ScopedGroupMembership

    group = Group.objects.get(name=group_name)
    return ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_edition,
        organization=organization,
        granted_by=user,
    )


def _mismatched_scope_user(
    email: str, *, event, write_group: str, organization, other_organization
):
    """A user with `write_group` scoped to `organization`, and ONLY the
    view-only "Organization User" group scoped to `other_organization`."""
    user = make_operational_user_with_membership(
        email=email, group_name=write_group, event_edition=event, organization=organization
    )
    _add_membership(user, "Organization User", event_edition=event, organization=other_organization)
    return user


def test_campaign_change_status_denies_write_scope_for_a_different_organization(
    event, organization, other_organization
) -> None:
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=other_organization,
            name="Not Yours",
            public_reference="CAMP-SCOPE2-STATUS-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    user = _mismatched_scope_user(
        "mismatch-status@example.com",
        event=event,
        write_group="Invitation Manager",
        organization=organization,
        other_organization=other_organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(
        reverse("invitations:campaign-change-status", kwargs={"pk": campaign.pk}),
        {"new_status": "SUSPENDED"},
    )
    assert response.status_code == 404
    campaign.refresh_from_db()
    assert campaign.status == InvitationCampaignStatus.ACTIVE


def test_campaign_issue_link_denies_write_scope_for_a_different_organization(
    event, organization, other_organization
) -> None:
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=other_organization,
            name="Not Yours",
            public_reference="CAMP-SCOPE2-ISSUE-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    user = _mismatched_scope_user(
        "mismatch-issue@example.com",
        event=event,
        write_group="Invitation Manager",
        organization=organization,
        other_organization=other_organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(reverse("invitations:campaign-issue-link", kwargs={"pk": campaign.pk}))
    assert response.status_code == 404
    assert campaign.links.count() == 0


def test_campaign_rotate_link_denies_write_scope_for_a_different_organization(
    event, organization, other_organization
) -> None:
    from apps.invitations.services import issue_initial_link

    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=other_organization,
            name="Not Yours",
            public_reference="CAMP-SCOPE2-ROTATE-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    original_link, _raw = issue_initial_link(campaign)
    user = _mismatched_scope_user(
        "mismatch-rotate@example.com",
        event=event,
        write_group="Invitation Manager",
        organization=organization,
        other_organization=other_organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(reverse("invitations:campaign-rotate-link", kwargs={"pk": campaign.pk}))
    assert response.status_code == 404
    original_link.refresh_from_db()
    assert original_link.status == "ACTIVE"
    assert campaign.links.count() == 1


def test_delegation_apply_denies_write_scope_for_a_different_organization(
    event, organization, other_organization
) -> None:
    from apps.invitations.services import upload_delegation_batch

    coordinator = make_operational_user_with_membership(
        email="coordinator-owner@example.com",
        group_name="Delegation Coordinator",
        event_edition=event,
        organization=other_organization,
    )
    batch = upload_delegation_batch(
        organization=other_organization,
        event_edition=event,
        csv_bytes=b"email,given_names,family_name\nscope2-apply@example.com,A,B\n",
        uploaded_filename="delegation.csv",
        created_by=coordinator,
        idempotency_key="delegation-scope2-mismatch",
    )
    user = _mismatched_scope_user(
        "mismatch-apply@example.com",
        event=event,
        write_group="Delegation Coordinator",
        organization=organization,
        other_organization=other_organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(reverse("invitations:delegation-apply", kwargs={"pk": batch.pk}))
    assert response.status_code == 404
    batch.refresh_from_db()
    assert batch.status == "VALIDATED"
    assert batch.applied_row_count == 0


# ---------------------------------------------------------------------------
# Event scope preserved in organization actions (Phase 2 Prompt 2 V2
# correction pass requirement 3): a membership scoped to a PAST event alone
# must never authorize acting on an organization for the CURRENT event.
# ---------------------------------------------------------------------------


def _past_event():
    from django.utils import timezone as tz

    from apps.events.models import EventEdition, EventEditionStatus

    return EventEdition.objects.create(
        code="PASTSCOPE",
        name="Past Event",
        timezone="UTC",
        starts_at=tz.now(),
        ends_at=tz.now(),
        status=EventEditionStatus.ARCHIVED,
    )


def _ensure_single_open_event(*, keep):
    """`events.0002_seed_current_event_edition` permanently seeds an
    "ASC2026" event with `REGISTRATION_OPEN` in the migrated test database
    (visible inside every non-transactional test's savepoint); a test that
    ALSO creates its own open `event` fixture would otherwise leave TWO
    simultaneously-open editions, and `current_event_edition()` fails
    closed with `MultipleOpenEventEditions` rather than silently picking
    one. Tests that exercise the "current event" resolution path close
    every OTHER open edition first so `keep` is unambiguously the one."""
    from apps.events.models import EventEdition, EventEditionStatus

    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).exclude(
        pk=keep.pk
    ).update(status=EventEditionStatus.REGISTRATION_CLOSED)


def test_campaign_create_denies_a_membership_scoped_only_to_a_past_event(
    event, organization
) -> None:
    _ensure_single_open_event(keep=event)
    past_event = _past_event()
    user = make_operational_user_with_membership(
        email="past-event-campaign@example.com",
        group_name="Invitation Manager",
        event_edition=past_event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(
        reverse("invitations:campaign-create", kwargs={"organization_id": organization.pk})
    )
    assert response.status_code == 404


def test_delegation_upload_denies_a_membership_scoped_only_to_a_past_event(
    event, organization
) -> None:
    _ensure_single_open_event(keep=event)
    past_event = _past_event()
    user = make_operational_user_with_membership(
        email="past-event-delegation@example.com",
        group_name="Delegation Coordinator",
        event_edition=past_event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(
        reverse("invitations:delegation-upload", kwargs={"organization_id": organization.pk})
    )
    assert response.status_code == 404


def test_on_behalf_create_denies_a_membership_scoped_only_to_a_past_event(
    event, organization
) -> None:
    _ensure_single_open_event(keep=event)
    past_event = _past_event()
    user = make_operational_user_with_membership(
        email="past-event-onbehalf@example.com",
        group_name="On-Behalf Registrar",
        event_edition=past_event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(
        reverse("invitations:on-behalf-create", kwargs={"organization_id": organization.pk})
    )
    assert response.status_code == 404


def test_organization_search_excludes_an_organization_visible_only_for_a_past_event(
    event,
) -> None:
    from apps.organizations.models import Organization, OrganizationType

    _ensure_single_open_event(keep=event)
    past_event = _past_event()
    visible_org = Organization.objects.create(
        official_name="Search Scope Target Visible",
        normalized_name="search scope target visible",
        organization_type=OrganizationType.OTHER,
    )
    wrong_event_org = Organization.objects.create(
        official_name="Search Scope Target PastEvent",
        normalized_name="search scope target pastevent",
        organization_type=OrganizationType.OTHER,
    )
    user = make_operational_user_with_membership(
        email="search-past-event@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=visible_org,
    )
    _add_membership(
        user, "Invitation Manager", event_edition=past_event, organization=wrong_event_org
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.get(reverse("invitations:organization-search"), {"q": "Search Scope Target"})
    assert response.status_code == 200
    content = response.content.decode()
    assert visible_org.official_name in content
    assert wrong_event_org.official_name not in content


def test_organization_search_hides_action_controls_granted_only_for_a_past_event(
    event, organization
) -> None:
    """Search visibility and each action control share the current-event scope.

    A current-event Invitation Manager may find the organization, but
    delegation/on-behalf memberships for an archived event must not make
    those current-event controls visible.
    """
    _ensure_single_open_event(keep=event)
    past_event = _past_event()
    user = make_operational_user_with_membership(
        email="search-action-past-event@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=organization,
    )
    _add_membership(
        user, "Delegation Coordinator", event_edition=past_event, organization=organization
    )
    _add_membership(
        user, "On-Behalf Registrar", event_edition=past_event, organization=organization
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)

    response = client.get(reverse("invitations:organization-search"), {"q": "Acme"})

    assert response.status_code == 200
    content = response.content.decode()
    assert organization.official_name in content
    assert (
        reverse("invitations:delegation-upload", kwargs={"organization_id": organization.pk})
        not in content
    )
    assert (
        reverse("invitations:on-behalf-create", kwargs={"organization_id": organization.pk})
        not in content
    )


# ---------------------------------------------------------------------------
# Organization search execution (Phase 2 Prompt 2 V2 correction pass
# requirement 4): scope and every filter apply before ordering/slicing, so
# the view never raises "Cannot filter a query once a slice has been
# taken", and only correctly-scoped, bounded results are ever returned.
# ---------------------------------------------------------------------------


def test_organization_search_view_is_scoped_bounded_and_never_raises_on_slicing(
    event, organization, other_organization
) -> None:
    from apps.organizations.models import Organization, OrganizationType

    _ensure_single_open_event(keep=event)
    no_membership_org = Organization.objects.create(
        official_name="Search Slicing Target Stranger",
        normalized_name="search slicing target stranger",
        organization_type=OrganizationType.OTHER,
    )
    visible_org = Organization.objects.create(
        official_name="Search Slicing Target Visible",
        normalized_name="search slicing target visible",
        organization_type=OrganizationType.OTHER,
    )
    user = make_operational_user_with_membership(
        email="search-slicing@example.com",
        group_name="Invitation Manager",
        event_edition=event,
        organization=visible_org,
    )
    client = Client()
    sign_in_operational(client, user.email_normalized)
    # This is the exact call shape that previously raised
    # "Cannot filter a query once a slice has been taken" when the view
    # filtered `search_organizations(query)`'s ALREADY-SLICED result.
    response = client.get(
        reverse("invitations:organization-search"), {"q": "Search Slicing Target"}
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert visible_org.official_name in content
    assert no_membership_org.official_name not in content
    assert other_organization.official_name not in content
