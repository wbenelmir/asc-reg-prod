"""Review correction R-04: an organization's subordinate data respects the
reader's own scopes.

One organization takes part in two events. A staff member granted
organization access (a custom group holding the view permissions) for event
A only must see event A's campaigns, delegation lists and registration
counts on the organization pages, never event B's; the superadmin sees both.
Synthetic data only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.invitations.models import DelegationBatch
from apps.invitations.services import create_campaign
from apps.organizations.models import Organization
from apps.people.tests.conftest import TEST_OPERATIONAL_PASSWORD, staff_client
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db

VIEW_PERMISSIONS = (
    ("organizations", "view_organization"),
    ("invitations", "view_invitationcampaign"),
    ("invitations", "view_delegationbatch"),
    ("registrations", "view_registration"),
)


def _event(code, *, open_=False):
    from apps.events.models import EventEdition

    if open_:
        EventEdition.objects.filter(status="REGISTRATION_OPEN").update(status="REGISTRATION_CLOSED")
    return EventEdition.objects.create(
        code=code,
        name=f"Scope test {code}",
        timezone="UTC",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        ends_at=timezone.now() + datetime.timedelta(days=32),
        status="REGISTRATION_OPEN" if open_ else "REGISTRATION_CLOSED",
    )


def _user(email, *, superuser=False):
    return OperationalUser.objects.create_user(
        email=email,
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=superuser,
    )


def _registrations(campaign, count, prefix):
    for index in range(count):
        Registration.objects.create(
            public_reference=f"{prefix}-{index}",
            event_edition=campaign.event_edition,
            source_kind="INVITATION",
            source_context_key=f"invitation-campaign:{campaign.pk}",
            source_organization=campaign.organization,
            invitation_campaign=campaign,
            public_status="SUBMITTED",
            submitted_at=timezone.now(),
        )


@pytest.fixture
def shared():
    event_a = _event("R04A", open_=True)
    event_b = _event("R04B")
    organization = Organization.objects.create(
        official_name="R04 Shared Agency", normalized_name="r04 shared agency",
        organization_type="COMPANY",
    )  # fmt: skip
    for event, name, count in ((event_a, "Campaign in A", 2), (event_b, "Campaign in B", 3)):
        campaign = create_campaign(
            event_edition=event,
            organization=organization,
            name=name,
            public_reference=f"R04-{uuid.uuid4().hex[:8].upper()}",
        )
        _registrations(campaign, count, name.split()[-1])
    from apps.documents.models import StoredObject

    creator = _user("r04-creator@example.test")
    batches = {}
    for event, label in ((event_a, "A"), (event_b, "B")):
        stored = StoredObject.objects.create(
            storage_key=f"synthetic/{uuid.uuid4().hex}", bucket_class="RESTRICTED",
            content_type="text/csv", size_bytes=10, sha256="0" * 64,
        )  # fmt: skip
        batches[label] = DelegationBatch.objects.create(
            organization=organization,
            event_edition=event,
            stored_object=stored,
            created_by=creator,
            idempotency_key=f"r04-{label}-{uuid.uuid4().hex}",
        ).pk
    group = Group.objects.create(name="R04 Organization Viewers")
    for app_label, codename in VIEW_PERMISSIONS:
        group.permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    delegated = _user("r04-delegated@example.test")
    ScopedGroupMembership.objects.create(
        user=delegated, group=group, event_edition=event_a, organization=organization,
        granted_by=delegated,
    )  # fmt: skip
    return {"organization": organization, "delegated": delegated, "batches": batches}


def test_a_delegated_reader_sees_only_the_event_of_the_scope(shared):
    client = staff_client(shared["delegated"])
    organization = shared["organization"]
    response = client.get(reverse("organizations:detail", kwargs={"pk": organization.pk}))
    assert response.status_code == 200
    assert [c.name for c in response.context["campaigns"]] == ["Campaign in A"]
    assert response.context["campaigns"][0].registration_count == 2
    assert response.context["counts"]["total"] == 2
    assert [b.pk for b in response.context["batches"]] == [shared["batches"]["A"]]
    html = response.content.decode()
    assert "Campaign in B" not in html and str(shared["batches"]["B"]) not in html
    assert "data-organization-scope-note" in html

    listing = client.get(reverse("organizations:list"))
    row = next(o for o in listing.context["page"].object_list if o.pk == organization.pk)
    assert (row.campaign_count, row.registration_count) == (1, 2)


def test_the_superadmin_sees_every_event(shared):
    client = staff_client(_user("r04-super@example.test", superuser=True))
    organization = shared["organization"]
    response = client.get(reverse("organizations:detail", kwargs={"pk": organization.pk}))
    assert sorted(c.name for c in response.context["campaigns"]) == [
        "Campaign in A",
        "Campaign in B",
    ]
    assert response.context["counts"]["total"] == 5
    assert len(response.context["batches"]) == 2
    listing = client.get(reverse("organizations:list"))
    row = next(o for o in listing.context["page"].object_list if o.pk == organization.pk)
    assert (row.campaign_count, row.registration_count) == (2, 5)
