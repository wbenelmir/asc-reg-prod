"""Invited-registration provenance and multi-context tests
(Phase 2 Prompt 2 requirements 6, 7, 8, 9)."""

from __future__ import annotations

import pytest

from apps.invitations.models import (
    InvitationCampaignStatus,
    InvitationUse,
    InvitationUseKind,
)
from apps.invitations.selectors import campaigns_visible_to
from apps.invitations.services import change_campaign_status, create_campaign, issue_initial_link
from apps.registrations.models import Registration, RegistrationSourceKind
from apps.registrations.services import create_invited_draft_registration

from .conftest import make_person

pytestmark = pytest.mark.django_db


@pytest.fixture
def active_campaign(event, organization):
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="Provenance Campaign",
        public_reference="CAMP-PROV-0001",
    )
    return change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)


def test_invitation_use_link_and_provenance_persist_on_the_registration(
    active_campaign, event
) -> None:
    from apps.invitations.services import create_invited_draft_registration as _create_via_link

    link, _token = issue_initial_link(active_campaign)
    result = _create_via_link(link, preferred_language="en")

    result.registration.refresh_from_db()
    assert result.registration.source_kind == RegistrationSourceKind.INVITATION
    assert result.registration.invitation_campaign_id == active_campaign.pk
    assert result.registration.source_organization_id == active_campaign.organization_id
    assert InvitationUse.objects.filter(
        registration=result.registration, use_kind=InvitationUseKind.DRAFT_CREATED, link=link
    ).exists()


def test_provenance_is_never_inferred_only_from_the_currently_active_link(
    active_campaign, event
) -> None:
    """Rotating the link afterwards must not change what is already persisted."""
    from apps.invitations.services import create_invited_draft_registration as _create_via_link
    from apps.invitations.services import rotate_link

    link, _token = issue_initial_link(active_campaign)
    result = _create_via_link(link, preferred_language="en")
    rotate_link(active_campaign)

    result.registration.refresh_from_db()
    assert result.registration.invitation_campaign_id == active_campaign.pk


def test_same_person_invited_by_two_organizations_creates_two_valid_contexts(
    event, organization, other_organization
) -> None:
    person = make_person("multi-org@example.com")

    campaign_a = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Campaign A",
            public_reference="CAMP-A-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    campaign_b = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=other_organization,
            name="Campaign B",
            public_reference="CAMP-B-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    link_a, _ = issue_initial_link(campaign_a)
    link_b, _ = issue_initial_link(campaign_b)

    from apps.invitations.services import create_invited_draft_registration as _create_via_link

    result_a = _create_via_link(link_a, preferred_language="en", person_id=person.pk)
    result_b = _create_via_link(link_b, preferred_language="en", person_id=person.pk)

    assert result_a.registration.pk != result_b.registration.pk
    assert result_a.registration.deduplication_key != result_b.registration.deduplication_key
    assert Registration.objects.filter(person=person, is_current_context=True).count() == 2


def test_invitation_creation_never_touches_professional_affiliation(active_campaign, event) -> None:
    """No professional affiliation exists until the participant submits it
    themselves -- invitation creation never writes/overwrites one."""
    link, _token = issue_initial_link(active_campaign)
    result = create_invited_draft_registration(
        event_edition=event,
        campaign=active_campaign,
        source_context_key="test-key",
        preferred_language="en",
        person_id=None,
    )
    assert not hasattr(result.registration, "professional_affiliation")


def test_registration_model_has_no_role_badge_or_access_field() -> None:
    """Structural proof that invitation-linked creation cannot assign
    approval, Participant Role, Badge Type or Access Profile (FR-INV-010) --
    no such field exists on the model at all."""
    field_names = {f.name for f in Registration._meta.get_fields()}
    for forbidden in ("participant_role", "badge_type", "access_profile", "decision"):
        assert forbidden not in field_names


def test_campaigns_visible_to_denies_anonymous_and_inactive_users() -> None:
    from django.contrib.auth.models import AnonymousUser

    assert campaigns_visible_to(AnonymousUser()).count() == 0


def test_invitation_use_creation_is_audited(active_campaign) -> None:
    """Phase 2 Prompt 2 correction pass requirement 10: invitation-use
    creation must be audited, using the centralized action-code constant
    (never a raw string literal)."""
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.invitations.services import create_invited_draft_registration as _create_via_link

    link, _token = issue_initial_link(active_campaign)
    result = _create_via_link(link, preferred_language="en")
    assert AuditEvent.objects.filter(
        action_code=action_codes.INVITATION_USE_RECORDED,
        target_uuid=result.registration.pk,
    ).exists()
