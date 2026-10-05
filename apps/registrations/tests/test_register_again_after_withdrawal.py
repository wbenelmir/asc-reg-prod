"""Registration correction package (2026-10-04), item 6: register again after
a withdrawal.

A withdrawn registration stayed visible but offered no way to register again
(and its still-current context blocked a new one). Now the participant can
create a NEW, separate registration from the same account, by the origin-aware
policy of IDV-Q-C1:

* public: while the public channel is open;
* invitation: through the same invitation, re-validated under lock (and its
  capacity at submission); otherwise a new invitation is needed, never the
  public channel;
* on behalf or delegation, or cancelled by the registration team: contact them.

The withdrawn registration is never reopened: it stays WITHDRAWN with its
audit, consent and acceptance history; only its context is released. Nothing
is carried over to the new draft. Synthetic data only.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.documents.models import Document
from apps.people.services import resolve_or_create_participant_for_email
from apps.people.tests.conftest import make_event, participant_client
from apps.privacy.models import AcceptanceRecord, ConsentRecord
from apps.registrations.models import (
    AccommodationRequest,
    Registration,
    RegistrationProfile,
    RegistrationSubmission,
)
from apps.registrations.services import (
    RegistrationNotRestartable,
    get_or_create_active_draft,
    save_accommodation_request,
    submit_full_registration,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db


@pytest.fixture
def event(db):
    from apps.core.models import Country, Sector

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    return make_event(f"WD{uuid.uuid4().hex[:6].upper()}")


@pytest.fixture
def legal(db):
    from apps.privacy.selectors import effective_published_version

    versions = tuple(
        effective_published_version(code, "en") for code in ("PRIVACY_NOTICE", "TERMS")
    )
    if None in versions:
        pytest.skip("the published notices were flushed by an earlier transactional test")
    return versions


def start_registration_after_withdrawal(**kwargs):
    """Imported on use, so a run against the baseline fails per test."""
    from apps.registrations.services import start_registration_after_withdrawal as start

    return start(**kwargs)


def _person():
    return resolve_or_create_participant_for_email(f"wd-{uuid.uuid4().hex[:8]}@example.test")


def _submit(draft, legal, **extra):
    privacy, terms = legal
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="wd",
        idempotency_key=f"wd-{draft.pk}",
        **extra,
    )
    return Registration.objects.get(pk=draft.pk)


def _campaign(event, *, capacity=None):
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        issue_initial_link,
    )
    from apps.organizations.models import Organization, OrganizationType

    organization = Organization.objects.create(
        official_name=f"WD Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"wd org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="WD campaign",
        public_reference=f"CMP-{uuid.uuid4().hex[:8].upper()}",
        capacity=capacity,
    )
    campaign = change_campaign_status(campaign, "ACTIVE")
    link, _token = issue_initial_link(campaign)
    return campaign, link


def _submitted(event, legal, origin="OPEN", link=None, person=None):
    person = person or _person()
    if origin == "OPEN":
        draft = get_or_create_active_draft(person=person, event_edition=event)
    elif origin == "INVITATION":
        from apps.invitations.services import create_invited_draft_registration

        draft = create_invited_draft_registration(
            link, preferred_language="en", person_id=person.pk
        ).registration
    else:
        from apps.accounts.models import OperationalUser
        from apps.invitations.services import claim_on_behalf_registration, create_on_behalf_draft
        from apps.organizations.models import Organization, OrganizationType
        from apps.people.models import ContactPoint

        organization = Organization.objects.create(
            official_name=f"WD OB {uuid.uuid4().hex[:6]}",
            normalized_name=f"wd ob {uuid.uuid4().hex[:6]}",
            organization_type=OrganizationType.MINISTRY,
        )
        staff = OperationalUser.objects.create_user(
            email=f"wd-staff-{uuid.uuid4().hex[:6]}@example.test", password=None
        )
        email = ContactPoint.objects.filter(person=person).first().value_encrypted
        draft, token = create_on_behalf_draft(
            event_edition=event,
            source_organization=organization,
            created_by=staff,
            intended_email=email,
        )
        draft = claim_on_behalf_registration(
            raw_claim_token=token, verified_email=email, person=person
        )
    walk_draft_through_every_step(draft)
    return _submit(draft, legal)


def _withdraw(registration):
    client = participant_client(registration.person)
    response = client.post(
        reverse("reviews:my-registration-withdraw", kwargs={"pk": registration.pk}),
        {"expected_version": registration.version},
    )
    assert response.status_code == 302
    withdrawn = Registration.objects.get(pk=registration.pk)
    assert withdrawn.public_status == "WITHDRAWN"
    return withdrawn


def _workspace_card(registration) -> str:
    html = participant_client(registration.person).get(reverse("registrations:workspace")).text
    start = html.index(registration.public_reference)
    return html[start : html.index("</li>", start)]


def _register_again(registration):
    return participant_client(registration.person).post(
        reverse("registrations:register-again", kwargs={"pk": registration.pk})
    )


def _new_drafts(registration):
    return Registration.objects.filter(
        person=registration.person,
        event_edition=registration.event_edition,
        public_status="DRAFT",
        is_current_context=True,
    ).exclude(pk=registration.pk)


def _history(registration) -> tuple:
    return (
        sorted(
            RegistrationSubmission.objects.filter(registration=registration).values_list(
                "pk", "snapshot_hash"
            )
        ),
        sorted(
            AcceptanceRecord.objects.filter(registration=registration).values_list(
                "pk", "legal_document_version_id"
            )
        ),
        sorted(
            ConsentRecord.objects.filter(person=registration.person).values_list("pk", "action")
        ),
        AuditEvent.objects.filter(target_uuid=registration.pk).count(),
    )


# ---------------------------------------------------------------------------
# Public origin
# ---------------------------------------------------------------------------


def test_a_withdrawn_public_registration_registers_again_as_a_new_one(event, legal) -> None:
    withdrawn = _withdraw(_submitted(event, legal))
    card = _workspace_card(withdrawn)
    assert "data-withdrawn-next-step" in card and 'data-register-again="OPEN"' in card
    history = _history(withdrawn)

    response = _register_again(withdrawn)
    assert response.status_code == 302
    draft = _new_drafts(withdrawn).get()
    assert response.url == reverse("registrations:step-identity")
    assert draft.source_kind == "OPEN" and draft.pk != withdrawn.pk

    old = Registration.objects.get(pk=withdrawn.pk)
    assert old.public_status == "WITHDRAWN" and old.withdrawn_at == withdrawn.withdrawn_at
    assert old.is_current_context is False  # released, never reopened
    assert _history(old)[:3] == history[:3]  # submissions, acceptances and consents kept
    assert AuditEvent.objects.filter(
        target_uuid=old.pk, action_code="REG_RESTARTED_AFTER_WITHDRAWAL"
    ).exists()
    # Nothing is carried over to the new registration.
    assert not AcceptanceRecord.objects.filter(registration=draft).exists()
    assert not Document.objects.filter(registration=draft).exists()
    assert not AccommodationRequest.objects.filter(registration=draft).exists()
    assert not RegistrationProfile.objects.filter(registration=draft).exists()
    # The withdrawn registration stays visible and readable.
    card = _workspace_card(old)
    assert "Your new registration for this event is listed on this page." in card
    assert (
        participant_client(old.person)
        .get(reverse("registrations:confirmation", kwargs={"reference": old.public_reference}))
        .status_code
        == 200
    )


def test_repeated_requests_converge_on_one_new_registration(event, legal) -> None:
    withdrawn = _withdraw(_submitted(event, legal))
    first = _register_again(withdrawn)
    second = _register_again(withdrawn)
    assert first.status_code == second.status_code == 302
    assert _new_drafts(withdrawn).count() == 1
    again = start_registration_after_withdrawal(
        registration=Registration.objects.get(pk=withdrawn.pk), person=withdrawn.person
    )
    assert again.pk == _new_drafts(withdrawn).get().pk


def test_the_new_registration_needs_its_own_fresh_consents_and_requirements(event, legal) -> None:
    from apps.registrations.services import IncompleteRegistrationError

    registration = get_or_create_active_draft(person=_person(), event_edition=event)
    walk_draft_through_every_step(registration)
    save_accommodation_request(
        registration=registration, answer="YES", categories=["CAPTIONING"], note="Synthetic."
    )
    registration = _submit(registration, legal, sensitive_data_consent_granted=True)
    old_consents = list(
        ConsentRecord.objects.filter(person=registration.person).values_list("pk", "purpose__code")
    )
    withdrawn = _withdraw(registration)
    _register_again(withdrawn)
    draft = _new_drafts(withdrawn).get()
    with pytest.raises(IncompleteRegistrationError) as incomplete:
        _submit(draft, legal)  # an empty draft: every requirement applies again
    assert incomplete.value.step == "identity"
    walk_draft_through_every_step(draft)
    _submit(draft, legal)
    assert AcceptanceRecord.objects.filter(registration=draft).count() == 2
    new_consents = ConsentRecord.objects.filter(person=draft.person).exclude(
        pk__in=[pk for pk, _code in old_consents]
    )
    # A fresh processing consent; no sensitive consent without new support data.
    assert list(new_consents.values_list("purpose__code", flat=True)) == [
        "PERSONAL_DATA_PROCESSING"
    ]
    assert not AccommodationRequest.objects.filter(registration=draft).exists()
    assert ConsentRecord.objects.filter(pk__in=[pk for pk, _ in old_consents]).count() == len(
        old_consents
    )


def test_no_public_re_registration_while_the_public_channel_is_closed(event, legal) -> None:
    from apps.events.models import EventEdition

    withdrawn = _withdraw(_submitted(event, legal))
    EventEdition.objects.filter(pk=event.pk).update(public_registration_mode="INVITATION_ONLY")
    card = _workspace_card(withdrawn)
    assert "data-register-again=" not in card
    assert "Registration is closed at the moment" in card
    assert _register_again(withdrawn).status_code == 403
    assert not _new_drafts(withdrawn).exists()
    assert Registration.objects.get(pk=withdrawn.pk).is_current_context  # nothing released


# ---------------------------------------------------------------------------
# Invitation origin
# ---------------------------------------------------------------------------


def test_a_withdrawn_invitation_registration_registers_again_through_it(event, legal) -> None:
    from apps.invitations.models import InvitationUse

    campaign, link = _campaign(event)
    withdrawn = _withdraw(_submitted(event, legal, "INVITATION", link))
    assert 'data-register-again="INVITATION"' in _workspace_card(withdrawn)
    assert _register_again(withdrawn).status_code == 302
    draft = _new_drafts(withdrawn).get()
    assert draft.source_kind == "INVITATION" and draft.invitation_campaign_id == campaign.pk
    assert InvitationUse.objects.filter(registration=draft, use_kind="DRAFT_CREATED").exists()


@pytest.mark.parametrize("problem", ["full", "revoked", "campaign_closed"])
def test_an_unusable_invitation_asks_for_a_new_one_never_the_public_channel(
    event, legal, problem
) -> None:
    from apps.invitations.models import InvitationCampaign, InvitationLink

    campaign, link = _campaign(event, capacity=1)
    withdrawn = _withdraw(_submitted(event, legal, "INVITATION", link))
    if problem == "full":
        # Existing rule, kept: a withdrawn submission still counts toward the
        # campaign's capacity, so its only place stays used.
        pass
    elif problem == "revoked":
        InvitationLink.objects.filter(pk=link.pk).update(status="REVOKED")
    else:
        InvitationCampaign.objects.filter(pk=campaign.pk).update(status="CLOSED")
    card = _workspace_card(withdrawn)
    assert 'data-register-again-next="NEW_INVITATION_NEEDED"' in card
    assert "data-register-again=" not in card
    response = _register_again(withdrawn)
    assert response.status_code == 302 and response.url == reverse("registrations:workspace")
    assert not Registration.objects.filter(
        person=withdrawn.person, event_edition=event, public_status="DRAFT"
    ).exists()
    assert Registration.objects.get(pk=withdrawn.pk).is_current_context


def test_invitation_capacity_is_checked_again_at_the_new_submission(event, legal) -> None:
    from apps.invitations.services import CampaignCapacityExceededError

    _campaign_, link = _campaign(event, capacity=2)
    withdrawn = _withdraw(_submitted(event, legal, "INVITATION", link))
    _register_again(withdrawn)
    draft = _new_drafts(withdrawn).get()
    walk_draft_through_every_step(draft)
    _submitted(event, legal, "INVITATION", link)  # the last place goes to someone else
    with pytest.raises(CampaignCapacityExceededError):
        _submit(draft, legal)


def test_opening_the_invitation_again_while_signed_in_starts_a_new_registration(
    event, legal
) -> None:
    _campaign_, link = _campaign(event)
    withdrawn = _withdraw(_submitted(event, legal, "INVITATION", link))
    client = participant_client(withdrawn.person)
    session = client.session
    session["invitation_link_id"] = str(link.pk)
    session.save()
    response = client.post(reverse("registrations:start-pending-invitation"))
    assert response.status_code == 302
    draft = _new_drafts(withdrawn).get()
    assert draft.source_kind == "INVITATION"
    assert Registration.objects.get(pk=withdrawn.pk).public_status == "WITHDRAWN"
    assert AuditEvent.objects.filter(
        target_uuid=withdrawn.pk, action_code="REG_RESTARTED_AFTER_WITHDRAWAL"
    ).exists()


def test_a_pending_invitation_on_a_wizard_url_resumes_instead_of_failing(event, legal) -> None:
    """Before: a wizard URL with a pending invitation always created a second
    context of that campaign, which the deduplication constraint refuses."""
    _campaign_, link = _campaign(event)
    person = _person()
    from apps.invitations.services import create_invited_draft_registration

    draft = create_invited_draft_registration(
        link, preferred_language="en", person_id=person.pk
    ).registration
    client = participant_client(person)
    session = client.session
    session["invitation_link_id"] = str(link.pk)
    session.save()
    response = client.get(reverse("registrations:step-identity"))
    assert response.status_code == 200
    assert list(Registration.objects.filter(person=person)) == [draft]  # resumed, no second one


# ---------------------------------------------------------------------------
# Staff-created origin, cancellation, guards
# ---------------------------------------------------------------------------


def test_an_on_behalf_registration_points_to_the_organization(event, legal) -> None:
    withdrawn = _withdraw(_submitted(event, legal, "ON_BEHALF"))
    card = _workspace_card(withdrawn)
    assert 'data-register-again-next="CONTACT_TEAM"' in card and "data-register-again=" not in card
    response = _register_again(withdrawn)
    assert response.status_code == 302 and response.url == reverse("registrations:workspace")
    assert not _new_drafts(withdrawn).exists()
    with pytest.raises(RegistrationNotRestartable):
        start_registration_after_withdrawal(registration=withdrawn, person=withdrawn.person)


def test_a_registration_cancelled_by_the_team_offers_no_self_service(event, legal) -> None:
    from apps.reviews.services import cancel_registration_operationally

    registration = _submitted(event, legal)
    cancelled = cancel_registration_operationally(
        registration=registration,
        expected_version=registration.version,
        reason="Synthetic cancellation",
        actor=None,
    )
    card = _workspace_card(cancelled)
    assert "data-cancelled-next-step" in card and "data-register-again=" not in card
    assert _register_again(cancelled).status_code == 302
    assert not _new_drafts(cancelled).exists()


def test_only_the_owner_can_register_again(event, legal) -> None:
    withdrawn = _withdraw(_submitted(event, legal))
    stranger = _person()
    with pytest.raises(RegistrationNotRestartable):
        start_registration_after_withdrawal(registration=withdrawn, person=stranger)
    response = participant_client(stranger).post(
        reverse("registrations:register-again", kwargs={"pk": withdrawn.pk})
    )
    assert response.status_code == 404
    assert not _new_drafts(withdrawn).exists()


def test_a_released_withdrawn_registration_cannot_be_reopened_by_staff(event, legal) -> None:
    from apps.reviews.services import reopen_registration

    withdrawn = _withdraw(_submitted(event, legal))
    _register_again(withdrawn)
    released = Registration.objects.get(pk=withdrawn.pk)
    with pytest.raises(ValueError, match="replaced by a newer registration"):
        reopen_registration(
            registration=released,
            expected_version=released.version,
            reason="Synthetic reopen",
            actor=None,
        )
    assert Registration.objects.get(pk=withdrawn.pk).public_status == "WITHDRAWN"


def test_a_withdrawn_registration_that_was_not_replaced_can_still_be_reopened(event, legal) -> None:
    from apps.reviews.services import reopen_registration

    withdrawn = _withdraw(_submitted(event, legal))
    reopen_registration(
        registration=withdrawn,
        expected_version=withdrawn.version,
        reason="Synthetic reopen",
        actor=None,
    )
    reopened = Registration.objects.get(pk=withdrawn.pk)
    assert reopened.public_status == "UNDER_REVIEW"
    with pytest.raises(RegistrationNotRestartable):
        start_registration_after_withdrawal(registration=reopened, person=reopened.person)


def test_another_origin_of_the_same_event_is_not_a_conflict(event, legal) -> None:
    """The deduplication rule is per origin context: a current invitation
    registration does not block a new public one, and is left untouched."""
    _campaign_, link = _campaign(event)
    person = _person()
    invited = _submitted(event, legal, "INVITATION", link, person=person)
    withdrawn = _withdraw(_submitted(event, legal, person=person))
    assert _register_again(withdrawn).status_code == 302
    assert _new_drafts(withdrawn).get().source_kind == "OPEN"
    assert Registration.objects.get(pk=invited.pk).public_status == "SUBMITTED"


def test_a_final_identity_rejection_cannot_be_withdrawn_by_a_direct_request(event, legal) -> None:
    """IDV-Q2 keeps a final identity rejection terminal: the workspace offers no
    withdrawal, and a forged request changes nothing (lifecycle review)."""
    from apps.people.services import identity_review as review
    from apps.people.tests.conftest import case_for, make_staff, process_all
    from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

    manager = make_staff(
        f"wd-m-{uuid.uuid4().hex[:6]}@example.test", ACCREDITATION_MANAGERS_GROUP_NAME
    )
    registration = _submitted(event, legal)
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
    rejected = Registration.objects.get(pk=registration.pk)
    assert rejected.public_status == "NOT_APPROVED"
    response = participant_client(rejected.person).post(
        reverse("reviews:my-registration-withdraw", kwargs={"pk": rejected.pk}),
        {"expected_version": rejected.version},
    )
    assert response.status_code == 302
    assert Registration.objects.get(pk=rejected.pk).public_status == "NOT_APPROVED"
    card = _workspace_card(rejected)
    assert "data-identity-rejected" in card and "data-withdrawn-next-step" not in card
