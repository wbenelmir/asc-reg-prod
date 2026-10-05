"""InvitationLink create/rotate/revoke and generic invalid-link tests
(Phase 2 Prompt 2 requirements 2, 3, 4)."""

from __future__ import annotations

import pytest

from apps.invitations.models import InvitationCampaignStatus, InvitationLinkStatus
from apps.invitations.services import (
    InvitationLinkUnavailable,
    NoActiveLinkError,
    change_campaign_status,
    create_campaign,
    create_invited_draft_registration,
    issue_initial_link,
    resolve_invitation_link,
    revoke_link,
    rotate_link,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def active_campaign(event, organization):
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="Active Campaign",
        public_reference="CAMP-ACTIVE-0001",
    )
    return change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)


def test_issue_initial_link_creates_active_link(active_campaign) -> None:
    link, raw_token = issue_initial_link(active_campaign)
    assert link.status == InvitationLinkStatus.ACTIVE
    assert len(raw_token) > 30


def test_resolve_invitation_link_succeeds_for_a_valid_token(active_campaign) -> None:
    _link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)
    assert resolved.campaign_id == active_campaign.pk


def test_resolve_invitation_link_rejects_an_unknown_token(active_campaign) -> None:
    issue_initial_link(active_campaign)
    with pytest.raises(InvitationLinkUnavailable):
        resolve_invitation_link("this-token-was-never-issued")


def test_resolve_invitation_link_rejects_a_suspended_campaign(active_campaign) -> None:
    _link, raw_token = issue_initial_link(active_campaign)
    change_campaign_status(active_campaign, InvitationCampaignStatus.SUSPENDED)
    with pytest.raises(InvitationLinkUnavailable):
        resolve_invitation_link(raw_token)


def test_rotate_link_disables_old_link_and_new_one_works(active_campaign) -> None:
    old_link, old_token = issue_initial_link(active_campaign)
    new_link, new_token = rotate_link(active_campaign)

    old_link.refresh_from_db()
    assert old_link.status == InvitationLinkStatus.ROTATED
    with pytest.raises(InvitationLinkUnavailable):
        resolve_invitation_link(old_token)

    resolved = resolve_invitation_link(new_token)
    assert resolved.pk == new_link.pk
    assert new_link.rotated_from_id == old_link.pk


def test_rotation_preserves_the_same_stable_campaign(active_campaign, event) -> None:
    old_link, _old_token = issue_initial_link(active_campaign)
    result = create_invited_draft_registration(old_link, preferred_language="en")
    _new_link, _new_token = rotate_link(active_campaign)

    result.registration.refresh_from_db()
    assert result.registration.invitation_campaign_id == active_campaign.pk


def test_rotate_link_requires_an_active_link(active_campaign) -> None:
    with pytest.raises(NoActiveLinkError):
        rotate_link(active_campaign)


def test_revoke_link_makes_it_unusable(active_campaign) -> None:
    _link, raw_token = issue_initial_link(active_campaign)
    revoke_link(active_campaign)
    with pytest.raises(InvitationLinkUnavailable):
        resolve_invitation_link(raw_token)


def test_only_one_active_link_per_campaign_at_the_database_level(active_campaign) -> None:
    from django.db import IntegrityError, transaction

    from apps.invitations.models import InvitationLink

    issue_initial_link(active_campaign)
    with pytest.raises(IntegrityError), transaction.atomic():
        InvitationLink.objects.create(campaign=active_campaign, token_hash="b" * 64)


# ---------------------------------------------------------------------------
# Atomic re-validation at consumption time (Phase 2 Prompt 2 correction
# pass): the link was VALID when first resolved (e.g. when the participant
# opened it and started OTP), but something happened to it or its campaign
# before `create_invited_draft_registration` -- the only point that
# actually mutates the database -- was reached. Every one of these MUST
# raise the exact same `InvitationLinkUnavailable` a never-valid token
# raises, and MUST NOT create a Registration or any `InvitationUse` row.
# ---------------------------------------------------------------------------


def _registration_and_use_counts(campaign) -> tuple[int, int]:
    from apps.invitations.models import InvitationUse
    from apps.registrations.models import Registration

    return (
        Registration.objects.filter(invitation_campaign=campaign).count(),
        InvitationUse.objects.filter(campaign=campaign).count(),
    )


def test_consumption_rejects_a_link_revoked_after_initial_resolution(active_campaign) -> None:
    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    revoke_link(active_campaign)

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_consumption_rejects_a_link_rotated_after_initial_resolution(active_campaign) -> None:
    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    rotate_link(active_campaign)

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_consumption_rejects_a_link_expired_after_initial_resolution(active_campaign) -> None:
    from datetime import timedelta

    from django.utils import timezone

    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    link.expires_at = timezone.now() - timedelta(seconds=1)
    link.save(update_fields=["expires_at"])

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_consumption_rejects_a_link_whose_campaign_was_suspended_after_initial_resolution(
    active_campaign,
) -> None:
    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    change_campaign_status(active_campaign, InvitationCampaignStatus.SUSPENDED)

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_consumption_rejects_a_link_whose_campaign_closed_after_initial_resolution(
    active_campaign,
) -> None:
    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    change_campaign_status(active_campaign, InvitationCampaignStatus.CLOSED)

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_consumption_rejects_a_link_whose_campaign_expired_after_initial_resolution(
    active_campaign,
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    link, raw_token = issue_initial_link(active_campaign)
    resolved = resolve_invitation_link(raw_token)  # valid at this point
    active_campaign.valid_until = timezone.now() - timedelta(seconds=1)
    active_campaign.save(update_fields=["valid_until"])

    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")
    assert _registration_and_use_counts(active_campaign) == (0, 0)


def test_create_invited_draft_registration_has_no_caller_suppliable_event_edition() -> None:
    """Structural proof that a caller cannot combine a link from one event
    with a Registration for a different event (Phase 2 Prompt 2 correction
    pass): `event_edition` is not an accepted parameter at all -- it is
    derived exclusively from the link's own (locked) campaign."""
    import inspect

    signature = inspect.signature(create_invited_draft_registration)
    assert "event_edition" not in signature.parameters


def test_registration_event_always_matches_the_links_own_campaign_event(
    organization, event, other_event
) -> None:
    """Even if a caller mistakenly believes a different event applies, the
    created Registration's event always matches the LINK's campaign."""
    campaign_on_event = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Event-Bound Campaign",
            public_reference="CAMP-EVENTBOUND-0001",
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    link, _token = issue_initial_link(campaign_on_event)
    result = create_invited_draft_registration(link, preferred_language="en")
    assert result.registration.event_edition_id == event.pk
    assert result.registration.event_edition_id != other_event.pk
