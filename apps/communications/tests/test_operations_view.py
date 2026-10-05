from __future__ import annotations

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.communications.models import CommunicationChannel, MessageTemplate, MessageTemplateVersion
from apps.communications.services import queue_communication
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db(transaction=True)
PASSWORD = "__test_password__"  # noqa: S105


def _message(*, event, organization, reference, key):
    registration = Registration.objects.create(
        public_reference=reference,
        event_edition=event,
        source_kind="OPEN",
        source_context_key=f"open:{key}",
        source_organization=organization,
    )
    return queue_communication(
        purpose_code="OPS_TEST",
        event_edition=event,
        registration=registration,
        person=None,
        language="en",
        destination=f"{key}@example.com",
        context={"public_reference": reference},
        idempotency_key=key,
    )


def test_operations_message_view_is_scope_filtered_and_redacts_recipient():
    event = EventEdition.objects.create(
        code="COMMOPS",
        name="Communication Operations",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
    )
    allowed_org = Organization.objects.create(
        official_name="Allowed Org",
        normalized_name="allowed org",
        organization_type=OrganizationType.OTHER,
    )
    other_org = Organization.objects.create(
        official_name="Other Org",
        normalized_name="other org",
        organization_type=OrganizationType.OTHER,
    )
    template = MessageTemplate.objects.create(
        code="OPS_TEST", channel=CommunicationChannel.EMAIL, purpose_code="OPS_TEST"
    )
    MessageTemplateVersion.objects.create(
        template=template,
        language="en",
        version_label="v1",
        subject="Status {{public_reference}}",
        body="Status {{public_reference}}",
        allowed_variables=["public_reference"],
        status="PUBLISHED",
        effective_from=timezone.now(),
        content_hash="a" * 64,
    )
    _message(event=event, organization=allowed_org, reference="VISIBLE-REF", key="visible")
    _message(event=event, organization=other_org, reference="HIDDEN-REF", key="hidden")

    user = OperationalUser.objects.create_user(
        email="communication-operator@example.com",
        password=PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name="Communication Operators"),
        event_edition=event,
        organization=allowed_org,
        granted_by=user,
    )
    client = Client()
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": user.email_normalized, "password": PASSWORD},
    )
    response = client.get(reverse("communications:operations-messages"))
    content = response.content.decode()
    assert response.status_code == 200
    assert "VISIBLE-REF" in content
    assert "HIDDEN-REF" not in content
    assert "visible@example.com" not in content
