"""Campaign lifecycle and illegal-transition tests (AF-ORG-06)."""

from __future__ import annotations

import pytest

from apps.invitations.models import InvitationCampaign, InvitationCampaignStatus
from apps.invitations.services import (
    IllegalCampaignTransitionError,
    change_campaign_status,
    create_campaign,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign(event, organization) -> InvitationCampaign:
    return create_campaign(
        event_edition=event,
        organization=organization,
        name="Test Campaign",
        public_reference="CAMP-TEST-0001",
    )


def test_campaign_starts_in_draft_status(campaign: InvitationCampaign) -> None:
    assert campaign.status == InvitationCampaignStatus.DRAFT


def test_draft_can_be_activated(campaign: InvitationCampaign) -> None:
    activated = change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
    assert activated.status == InvitationCampaignStatus.ACTIVE


def test_active_can_be_suspended_and_resumed(campaign: InvitationCampaign) -> None:
    change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
    suspended = change_campaign_status(campaign, InvitationCampaignStatus.SUSPENDED)
    assert suspended.status == InvitationCampaignStatus.SUSPENDED
    resumed = change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
    assert resumed.status == InvitationCampaignStatus.ACTIVE


def test_active_can_be_closed(campaign: InvitationCampaign) -> None:
    change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
    closed = change_campaign_status(campaign, InvitationCampaignStatus.CLOSED)
    assert closed.status == InvitationCampaignStatus.CLOSED
    assert closed.closed_at is not None


def test_closed_campaign_has_no_legal_transitions(campaign: InvitationCampaign) -> None:
    change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
    change_campaign_status(campaign, InvitationCampaignStatus.CLOSED)
    with pytest.raises(IllegalCampaignTransitionError):
        change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)


def test_draft_cannot_be_suspended_directly(campaign: InvitationCampaign) -> None:
    with pytest.raises(IllegalCampaignTransitionError):
        change_campaign_status(campaign, InvitationCampaignStatus.SUSPENDED)


def test_campaign_capacity_must_be_nonnegative(event, organization) -> None:
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        InvitationCampaign.objects.create(
            public_reference="CAMP-BAD-0001",
            event_edition=event,
            organization=organization,
            name="Bad capacity",
            capacity=-1,
        )
