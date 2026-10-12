"""UPD-06: complete organization management for the superadmin.

Covered: create (with the duplicate warning), view, edit, deactivate and
activate an organization (audited); a deactivated organization starts no new
campaign, delegation list or on-behalf registration while its existing
campaign keeps its registrations; campaign editing (versioned, audited, a
lower limit never removes registrations); disabling and replacing a link
never detaches earlier registrations; "View registrations" per organization,
campaign and link; the export preselected and limited to one campaign;
discoverable management for the superadmin only; ordinary staff keep their
restrictions on the server side. Synthetic data only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.audit.models import AuditEvent
from apps.invitations.models import (
    InvitationCampaign,
    InvitationLink,
    InvitationUse,
    InvitationUseKind,
)
from apps.invitations.services import (
    change_campaign_status,
    create_campaign,
    issue_initial_link,
    rotate_link,
)
from apps.organizations.models import Organization, OrganizationStatus
from apps.people.tests.conftest import TEST_OPERATIONAL_PASSWORD, staff_client
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    from apps.events.models import EventEdition

    # The only event open for registration in this test (current_event_edition).
    EventEdition.objects.filter(status="REGISTRATION_OPEN").update(status="REGISTRATION_CLOSED")
    return EventEdition.objects.create(
        code="UPD06",
        name="Organization test",
        timezone="UTC",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        ends_at=timezone.now() + datetime.timedelta(days=32),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def superadmin():
    return OperationalUser.objects.create_user(
        email="upd06-super@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=True,
    )


@pytest.fixture
def organization():
    return Organization.objects.create(
        official_name="Upd06 Agency", normalized_name="upd06 agency", organization_type="COMPANY"
    )


def _manager(email, organization, event):
    user = OperationalUser.objects.create_user(
        email=email, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name="Invitation Manager"),
        event_edition=event,
        organization=organization,
        granted_by=user,
    )
    return user


def _campaign(event, organization, *, capacity=None):
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="Upd06 campaign",
        public_reference=f"CAMP-{uuid.uuid4().hex[:10].upper()}",
        capacity=capacity,
    )
    return change_campaign_status(campaign, "ACTIVE")


def _registration_through(link, *, status="SUBMITTED"):
    registration = Registration.objects.create(
        public_reference=f"UPD06-{uuid.uuid4().hex[:8].upper()}",
        event_edition=link.campaign.event_edition,
        source_kind="INVITATION",
        source_context_key=f"invitation-campaign:{link.campaign_id}",
        source_organization=link.campaign.organization,
        invitation_campaign=link.campaign,
        public_status=status,
        submitted_at=timezone.now(),
    )
    InvitationUse.objects.create(
        campaign=link.campaign,
        link=link,
        registration=registration,
        use_kind=InvitationUseKind.DRAFT_CREATED,
        occurred_at=timezone.now(),
        operation_id=f"upd06:{registration.pk}",
    )
    return registration


def test_the_superadmin_creates_edits_and_deactivates_an_organization(superadmin, event):
    client = staff_client(superadmin)
    response = client.post(
        reverse("organizations:create"),
        {
            "official_name": "  New   Synthetic  Org ",
            "organization_type": "NGO",
            "country_code": "DZ",
        },
    )
    organization = Organization.objects.get(official_name="New Synthetic Org")
    assert response.status_code == 302
    assert response.url == reverse("organizations:detail", kwargs={"pk": organization.pk})
    page = client.get(response.url).content.decode()
    assert "New Synthetic Org" in page and "data-organization-create-campaign" in page

    client.post(
        reverse("organizations:edit", kwargs={"pk": organization.pk}),
        {
            "official_name": "Renamed Synthetic Org",
            "organization_type": "NGO",
            "country_code": "DZ",
        },
    )
    organization.refresh_from_db()
    assert organization.official_name == "Renamed Synthetic Org"

    client.post(
        reverse("organizations:status", kwargs={"pk": organization.pk}), {"status": "INACTIVE"}
    )
    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.INACTIVE
    client.post(
        reverse("organizations:status", kwargs={"pk": organization.pk}), {"status": "ACTIVE"}
    )
    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.ACTIVE
    codes = list(
        AuditEvent.objects.filter(target_uuid=organization.pk)
        .order_by("occurred_at")
        .values_list("action_code", flat=True)
    )
    assert codes[0] == "ORG_ORGANIZATION_CREATED"
    assert "ORG_ORGANIZATION_UPDATED" in codes
    assert codes.count("ORG_ORGANIZATION_STATUS_CHANGED") == 2


def test_a_possible_duplicate_needs_confirmation(superadmin, organization):
    client = staff_client(superadmin)
    data = {"official_name": "UPD06 agency", "organization_type": "COMPANY"}
    response = client.post(reverse("organizations:create"), data)
    assert response.status_code == 200
    assert "data-organization-duplicates" in response.content.decode()
    assert Organization.objects.filter(normalized_name="upd06 agency").count() == 1
    client.post(reverse("organizations:create"), {**data, "confirm_duplicate": "on"})
    assert Organization.objects.filter(normalized_name="upd06 agency").count() == 2


def test_an_inactive_organization_starts_nothing_new_and_keeps_its_registrations(
    superadmin, event, organization
):
    campaign = _campaign(event, organization)
    link, _token = issue_initial_link(campaign)
    registration = _registration_through(link)
    organization.status = OrganizationStatus.INACTIVE
    organization.save(update_fields=["status"])
    client = staff_client(superadmin)
    for url in (
        reverse("invitations:campaign-create", kwargs={"organization_id": organization.pk}),
        reverse("invitations:delegation-upload", kwargs={"organization_id": organization.pk}),
        reverse("invitations:on-behalf-create", kwargs={"organization_id": organization.pk}),
    ):
        response = client.get(url)
        assert response.status_code == 302, url
        assert response.url == reverse("invitations:workspace-dashboard")
    registration.refresh_from_db()
    assert registration.invitation_campaign_id == campaign.pk
    assert registration.source_organization_id == organization.pk


def test_campaign_edit_is_versioned_audited_and_never_removes_registrations(
    superadmin, event, organization
):
    campaign = _campaign(event, organization, capacity=5)
    link, _token = issue_initial_link(campaign)
    for _ in range(2):
        registration = _registration_through(link)
        InvitationUse.objects.create(
            campaign=campaign, link=link, registration=registration,
            use_kind=InvitationUseKind.SUBMITTED, occurred_at=timezone.now(),
            operation_id=f"upd06-sub:{registration.pk}",
        )  # fmt: skip
    client = staff_client(superadmin)
    url = reverse("invitations:campaign-edit", kwargs={"pk": campaign.pk})
    response = client.post(
        url,
        {"name": "Renamed campaign", "capacity": "1", "language": "fr",
         "expected_version": campaign.version},
        follow=True,
    )  # fmt: skip
    campaign.refresh_from_db()
    assert (campaign.name, campaign.capacity, campaign.language) == ("Renamed campaign", 1, "fr")
    assert "lower than the places already used" in response.content.decode()
    assert campaign.registrations.count() == 2
    assert AuditEvent.objects.filter(action_code="INV_CAMPAIGN_UPDATED", target_uuid=campaign.pk)
    # A stale form (the version it was rendered with) changes nothing.
    client.post(url, {"name": "Stale", "language": "en", "expected_version": 1})
    campaign.refresh_from_db()
    assert campaign.name == "Renamed campaign"


def test_disabling_or_replacing_a_link_keeps_earlier_registrations(superadmin, event, organization):
    campaign = _campaign(event, organization)
    first, _token = issue_initial_link(campaign)
    early = _registration_through(first)
    second, _token = rotate_link(campaign)
    late = _registration_through(second)
    client = staff_client(superadmin)
    client.post(reverse("invitations:campaign-revoke-link", kwargs={"pk": campaign.pk}))
    assert not InvitationLink.objects.filter(campaign=campaign, status="ACTIVE").exists()
    for registration in (early, late):
        registration.refresh_from_db()
        assert registration.invitation_campaign_id == campaign.pk
    page = client.get(reverse("invitations:campaign-detail", kwargs={"pk": campaign.pk}))
    content = page.content.decode()
    assert content.count('data-link-row="') == 2
    by_link = client.get(reverse("registrations:ops-intake-list"), {"link": str(first.pk)})
    assert [r.pk for r in by_link.context["registrations"]] == [early.pk]
    by_campaign = client.get(
        reverse("registrations:ops-intake-list"), {"campaign": str(campaign.pk)}
    )
    assert {r.pk for r in by_campaign.context["registrations"]} == {early.pk, late.pk}
    by_org = client.get(
        reverse("registrations:ops-intake-list"), {"organization": str(organization.pk)}
    )
    assert by_org.context["matching_total"] == 2


def test_a_withdrawn_submission_keeps_its_place_in_the_limit(event, organization):
    from apps.invitations.services import campaign_usage

    campaign = _campaign(event, organization, capacity=2)
    link, _token = issue_initial_link(campaign)
    registration = _registration_through(link, status="WITHDRAWN")
    InvitationUse.objects.create(
        campaign=campaign, link=link, registration=registration,
        use_kind=InvitationUseKind.SUBMITTED, occurred_at=timezone.now(),
        operation_id=f"upd06-w:{registration.pk}",
    )  # fmt: skip
    assert campaign_usage(campaign) == {"submitted": 1, "started": 1, "remaining": 1}


def test_the_export_can_be_limited_to_one_campaign(
    superadmin, event, organization, tmp_path, settings
):
    from apps.exports.models import ExportRequest

    settings.PRIVATE_STORAGE_ROOT = tmp_path
    campaign = _campaign(event, organization)
    link, _token = issue_initial_link(campaign)
    inside = _registration_through(link)
    Registration.objects.create(
        public_reference="UPD06-OUTSIDE",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="open",
        source_organization=organization,
        public_status="SUBMITTED",
        submitted_at=timezone.now(),
    )
    client = staff_client(superadmin)
    page = client.get(
        reverse("exports:workspace"),
        {
            "event": str(event.pk),
            "organization": str(organization.pk),
            "campaign": str(campaign.pk),
        },
    )
    assert page.context["form"].initial["campaign_id"] == str(campaign.pk)
    client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "organization_id": str(organization.pk),
            "campaign_id": str(campaign.pk),
            "purpose_code": "ORGANIZATION_DELEGATION_OVERSIGHT",
            "reason": "Synthetic campaign oversight",
        },
    )
    export = ExportRequest.objects.get()
    assert export.invitation_campaign_id == campaign.pk
    assert export.target_registration_ids == [str(inside.pk)]


def test_management_is_discoverable_for_the_superadmin_only(superadmin, event, organization):
    page = staff_client(superadmin).get(reverse("invitations:workspace-dashboard")).content.decode()
    assert "data-organization-management" in page
    manager = _manager("upd06-manager@example.test", organization, event)
    client = staff_client(manager)
    assert (
        "data-organization-management"
        not in client.get(reverse("invitations:workspace-dashboard")).content.decode()
    )
    # Server-side, not only hidden buttons.
    assert client.get(reverse("organizations:list")).status_code == 403
    assert client.get(reverse("organizations:create")).status_code == 403
    assert (
        client.post(
            reverse("organizations:status", kwargs={"pk": organization.pk}), {"status": "INACTIVE"}
        ).status_code
        == 403
    )
    organization.refresh_from_db()
    assert organization.status == OrganizationStatus.ACTIVE


def test_a_manager_of_another_organization_cannot_edit_or_disable_this_campaign(
    event, organization
):
    other = Organization.objects.create(
        official_name="Other Org", normalized_name="other org", organization_type="COMPANY"
    )
    campaign = _campaign(event, organization)
    issue_initial_link(campaign)
    outsider = staff_client(_manager("upd06-outsider@example.test", other, event))
    assert (
        outsider.get(reverse("invitations:campaign-edit", kwargs={"pk": campaign.pk})).status_code
        == 404
    )
    assert (
        outsider.post(
            reverse("invitations:campaign-revoke-link", kwargs={"pk": campaign.pk})
        ).status_code
        == 404
    )
    assert InvitationLink.objects.filter(campaign=campaign, status="ACTIVE").exists()


def test_the_organization_list_paginates_and_counts(superadmin, event):
    Organization.objects.bulk_create(
        Organization(official_name=f"Bulk Org {index:03d}", normalized_name=f"bulk org {index:03d}",
                     organization_type="COMPANY")
        for index in range(60)
    )  # fmt: skip
    client = staff_client(superadmin)
    response = client.get(reverse("organizations:list"))
    assert response.context["page"].paginator.count == 60
    assert len(response.context["page"].object_list) == 50
    filtered = client.get(reverse("organizations:list"), {"q": "Org 05"})
    assert filtered.context["page"].paginator.count == 10
    assert InvitationCampaign.objects.count() == 0
