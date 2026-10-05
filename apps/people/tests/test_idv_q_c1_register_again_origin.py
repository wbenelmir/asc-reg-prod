"""IDV-Q-C1, finding 3: "register again" follows the rejected registration's origin.

After a final identity rejection the participant must get an accurate,
usable next step from the same account:

* an open (public) registration: register again while public registration
  is open, otherwise a "closed" message;
* an invitation registration: register again through the SAME invitation,
  re-validated under lock exactly as when it was first opened (active link,
  not expired, campaign valid, campaign capacity not used up, event not
  closed); otherwise no button, and an explicit message that a new invitation
  from the inviting organization is needed. The public channel is never used
  as a way around the invitation, and capacity is still enforced at
  submission;
* a registration created by staff (on behalf, delegation): contact the
  registration team.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_staff,
    participant_client,
    process_all,
    submit_case,
)
from apps.registrations.models import Registration
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(idv_event):
    return make_staff("q-c1-ra-m@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


def _reject(registration, manager):
    process_all()
    case = case_for(registration)
    review.reject_identity(
        case.pk,
        actor=manager,
        expected_version=case.version,
        reason_code="NO_VALID_EVIDENCE",
        note="No valid evidence was provided (synthetic).",
        confirmed=True,
    )
    return Registration.objects.get(pk=registration.pk)


def _campaign(event, *, capacity=None):
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        issue_initial_link,
    )
    from apps.organizations.models import Organization, OrganizationType

    organization = Organization.objects.create(
        official_name=f"Inviting Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"inviting org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="Synthetic campaign",
        public_reference=f"CMP-{uuid.uuid4().hex[:8].upper()}",
        capacity=capacity,
    )
    campaign = change_campaign_status(campaign, "ACTIVE")
    link, _token = issue_initial_link(campaign)
    return campaign, link


def _invited_submission(event, legal_versions, link, email):
    from apps.invitations.services import create_invited_draft_registration
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.services import save_identity_step, submit_full_registration
    from apps.registrations.tests.factories import walk_draft_through_every_step

    person = resolve_or_create_participant_for_email(email)
    draft = create_invited_draft_registration(
        link, preferred_language="en", person_id=person.pk
    ).registration
    walk_draft_through_every_step(draft, nin_value=UNKNOWN_NIN)
    save_identity_step(
        registration=draft,
        given_names="Amina",
        family_name="Invitest",
        date_of_birth=datetime.date(1990, 3, 7),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value=UNKNOWN_NIN,
    )
    privacy, terms = legal_versions
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="q-c1-ra",
        idempotency_key=f"q-c1-ra-{draft.pk}",
    )
    return Registration.objects.get(pk=draft.pk)


def _set_mode(event, mode):
    from apps.events.models import EventEdition

    EventEdition.objects.filter(pk=event.pk).update(public_registration_mode=mode)


def _workspace(registration):
    return participant_client(registration.person).get(reverse("registrations:workspace")).text


def _register_again(registration):
    client = participant_client(registration.person)
    return client.post(reverse("registrations:register-again", kwargs={"pk": registration.pk}))


def _new_drafts(registration):
    return Registration.objects.filter(
        person=registration.person,
        event_edition=registration.event_edition,
        public_status="DRAFT",
        is_current_context=True,
    )


# ---------------------------------------------------------------------------
# Open registrations (preserved)
# ---------------------------------------------------------------------------


def test_an_open_registration_registers_again_while_public_registration_is_open(
    idv_event, legal_versions, manager
) -> None:
    registration = _reject(
        submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN, email="q-c1-ra-open@example.test"),
        manager,
    )
    assert "data-register-again" in _workspace(registration)  # the button is offered
    assert _register_again(registration).status_code == 302
    assert _new_drafts(registration).get().source_kind == "OPEN"


def test_an_open_registration_waits_while_public_registration_is_closed(
    idv_event, legal_versions, manager
) -> None:
    registration = _reject(
        submit_case(
            idv_event, legal_versions, nin=UNKNOWN_NIN, email="q-c1-ra-closed@example.test"
        ),
        manager,
    )
    _set_mode(idv_event, "INVITATION_ONLY")
    page = _workspace(registration)
    assert "<button" not in page.split("data-identity-rejected", 1)[1].split("</li>", 1)[0]
    assert "Registration is closed at the moment" in page
    assert _register_again(registration).status_code in (302, 403)
    assert not _new_drafts(registration).exists()


# ---------------------------------------------------------------------------
# Invitation registrations
# ---------------------------------------------------------------------------


def test_an_invitation_registration_registers_again_through_its_own_invitation(
    idv_event, legal_versions, manager
) -> None:
    from apps.invitations.models import InvitationUse

    campaign, link = _campaign(idv_event)
    registration = _reject(
        _invited_submission(idv_event, legal_versions, link, "q-c1-ra-inv@example.test"), manager
    )
    assert 'data-register-again="INVITATION"' in _workspace(registration)
    response = _register_again(registration)
    assert response.status_code == 302
    draft = _new_drafts(registration).get()
    assert draft.source_kind == "INVITATION"
    assert draft.invitation_campaign_id == campaign.pk
    assert draft.source_organization_id == campaign.organization_id
    assert InvitationUse.objects.filter(
        registration=draft, link=link, use_kind="DRAFT_CREATED"
    ).exists()
    # A double click converges on the same draft.
    _register_again(registration)
    assert _new_drafts(registration).count() == 1


def test_a_valid_invitation_works_while_public_registration_is_closed(
    idv_event, legal_versions, manager
) -> None:
    _campaign_obj, link = _campaign(idv_event)
    registration = _reject(
        _invited_submission(idv_event, legal_versions, link, "q-c1-ra-inv-only@example.test"),
        manager,
    )
    _set_mode(idv_event, "INVITATION_ONLY")
    assert 'data-register-again="INVITATION"' in _workspace(registration)
    assert _register_again(registration).status_code == 302
    assert _new_drafts(registration).get().source_kind == "INVITATION"


@pytest.mark.parametrize("invalidation", ["revoked", "expired", "campaign_closed", "full"])
def test_an_unusable_invitation_asks_for_a_new_one_and_offers_no_button(
    idv_event, legal_versions, manager, invalidation
) -> None:
    from apps.invitations.models import InvitationCampaign, InvitationLink
    from apps.invitations.services import revoke_link

    campaign, link = _campaign(idv_event, capacity=1 if invalidation == "full" else None)
    registration = _reject(
        _invited_submission(
            idv_event, legal_versions, link, f"q-c1-ra-{invalidation}@example.test"
        ),
        manager,
    )
    if invalidation == "revoked":
        revoke_link(campaign)
    elif invalidation == "expired":
        InvitationLink.objects.filter(pk=link.pk).update(
            expires_at=timezone.now() - datetime.timedelta(minutes=1)
        )
    elif invalidation == "campaign_closed":
        InvitationCampaign.objects.filter(pk=campaign.pk).update(status="CLOSED")
    # "full": the only place was used by the rejected submission itself.
    page = _workspace(registration)
    assert "data-register-again=" not in page
    assert 'data-register-again-next="NEW_INVITATION_NEEDED"' in page
    assert "a new invitation" in page
    _register_again(registration)
    assert not _new_drafts(registration).exists()  # never an open draft instead


def test_a_closed_event_offers_no_new_registration(idv_event, legal_versions, manager) -> None:
    _campaign_obj, link = _campaign(idv_event)
    registration = _reject(
        _invited_submission(idv_event, legal_versions, link, "q-c1-ra-ev-closed@example.test"),
        manager,
    )
    _set_mode(idv_event, "CLOSED")
    page = _workspace(registration)
    assert "data-register-again=" not in page
    _register_again(registration)
    assert not _new_drafts(registration).exists()


def test_capacity_is_still_enforced_at_submission(idv_event, legal_versions, manager) -> None:
    """Registering again creates a draft only; the campaign's capacity is
    checked again at the new submission, never bypassed."""
    from apps.invitations.services import CampaignCapacityExceededError
    from apps.registrations.services import submit_full_registration
    from apps.registrations.tests.factories import walk_draft_through_every_step

    campaign, link = _campaign(idv_event, capacity=2)
    registration = _reject(
        _invited_submission(idv_event, legal_versions, link, "q-c1-ra-cap@example.test"), manager
    )
    assert _register_again(registration).status_code == 302
    draft = _new_drafts(registration).get()
    # Another invitee takes the last place meanwhile.
    _invited_submission(idv_event, legal_versions, link, "q-c1-ra-cap-other@example.test")
    walk_draft_through_every_step(draft, nin_value=UNKNOWN_NIN)
    privacy, terms = legal_versions
    with pytest.raises(CampaignCapacityExceededError):
        submit_full_registration(
            registration=draft,
            privacy_notice_version=privacy,
            terms_version=terms,
            data_processing_consent_granted=True,
            session_reference="q-c1-ra",
            idempotency_key=f"q-c1-ra-{draft.pk}",
        )


# ---------------------------------------------------------------------------
# The identity-rejection message (copy in EN, FR and AR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_rejection_message_states_the_next_step_for_every_origin(language) -> None:
    """The seeded copy points to the workspace, which shows the next step for
    the registration's own origin, and says that an invitation may need to be
    renewed by the inviting organization. It never promises a button that may
    not exist. The owner approved this exact wording for real delivery on
    2026-10-02 (COMM-IDV-01). The label `v1-draft` is kept: it is the label the
    earlier seeds share, not a status."""
    from apps.communications.services import resolve_template_version

    version = resolve_template_version(purpose_code="IDENTITY_REJECTION", language=language)
    assert version is not None and version.language == language
    assert (version.version_label, version.status) == ("v1-draft", "PUBLISHED")
    assert set(version.allowed_variables) == {"public_reference", "event_name"}
    marker = {
        "en": ("participant workspace", "new invitation"),
        "fr": ("espace participant", "nouvelle invitation"),
        "ar": ("مساحة المشارك", "دعوة جديدة"),
    }[language]
    for text in marker:
        assert text in version.body
    for promise in ("choose to register again", "choisissez de vous inscrire", "واختر التسجيل"):
        assert promise not in version.body


# ---------------------------------------------------------------------------
# A new invitation, issued by the organization, used from the same account
# ---------------------------------------------------------------------------


def test_a_new_invitation_opened_while_signed_in_starts_a_registration(
    idv_event, legal_versions, manager
) -> None:
    """The step the message promises really works: the participant whose
    invitation can no longer be used receives a new one, opens it while signed
    in, and registers with it from the workspace, although a rejected
    registration is already listed (and may be the session's active one)."""
    from apps.invitations.services import issue_initial_link, revoke_link

    campaign, link = _campaign(idv_event)
    registration = _reject(
        _invited_submission(idv_event, legal_versions, link, "q-c1-ra-new-inv@example.test"),
        manager,
    )
    revoke_link(campaign)
    _new_link, token = issue_initial_link(campaign)  # issued by the organization
    client = participant_client(registration.person)
    client.post(reverse("registrations:continue", kwargs={"pk": registration.pk}))
    client.get(reverse("invitations:invitation-start", kwargs={"token": token}))
    page = client.get(reverse("registrations:workspace")).text
    assert "data-pending-invitation" in page
    response = client.post(reverse("registrations:start-pending-invitation"))
    assert response.status_code == 302
    draft = _new_drafts(registration).get()
    assert draft.source_kind == "INVITATION"
    assert draft.invitation_campaign_id == campaign.pk
    # Without a pending invitation the action is the generic unavailable page.
    assert client.post(reverse("registrations:start-pending-invitation")).status_code != 302
