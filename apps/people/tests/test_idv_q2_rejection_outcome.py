"""Owner decision IDV-Q2 (2026-10-02): a final identity rejection has a safe, visible outcome.

* In the rejection's own transaction the registration becomes NOT_APPROVED
  through a coded participation decision (`IDENTITY_REJECTED` internally,
  `IDENTITY_NOT_VERIFIED` for the participant) that supersedes the current
  one, an earlier approval included; open review work ends; the context is
  released so the participant can register again from the same account.
* One notification is queued, idempotently, with the dedicated template: it
  says the registration was rejected and how to register again, and holds no
  reason, ministry data, identifier, evidence or staff note.
* The participant's workspace shows the same message and a "Register again"
  action that opens a new draft on the same account; the staff screens show
  the same outcome.
* A return for correction stays the remediable path on the same
  registration; a rejected registration cannot be reopened.

Synthetic data only; the mail is captured by the test outbox, never sent.
Imports of the new API are inside the tests (before/after evidence).
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.people.models import IdentityStatus
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_staff,
    participant_client,
    process_all,
    staff_client,
    submit_case,
)
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

NOTE = "The card shows clear signs of alteration (synthetic staff note)."


@pytest.fixture
def manager(idv_event):
    return make_staff("q2-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


@pytest.fixture
def reviewer(idv_event):
    return make_staff("q2-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


@pytest.fixture
def manual_case(idv_event, legal_versions):
    registration = submit_case(
        idv_event, legal_versions, nin=UNKNOWN_NIN, email="q2-participant@example.test"
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


def _reject(case, manager, **kwargs):
    case.refresh_from_db()
    return review.reject_identity(
        case.pk,
        actor=manager,
        expected_version=case.version,
        reason_code=kwargs.get("reason_code", "DOCUMENT_NOT_GENUINE_SUSPECTED"),
        note=kwargs.get("note", NOTE),
        confirmed=True,
    )


def _rejection_messages(registration):
    return CommunicationMessage.objects.filter(
        registration=registration, template_version__template__code="IDENTITY_REJECTION"
    )


def test_a_final_rejection_records_a_not_approved_participation_decision(
    manual_case, manager
) -> None:
    from apps.reviews.services import (
        IDENTITY_REJECTION_INTERNAL_REASON,
        IDENTITY_REJECTION_PARTICIPANT_REASON,
    )

    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    assert rejected.status == IdentityStatus.REJECTED
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert registration.internal_status == "CLOSED"
    assert registration.is_current_context is False  # released: a new registration may start
    decision = registration.decisions.get(is_current=True)
    assert decision.outcome == "NOT_APPROVED"
    assert decision.internal_reason_code == IDENTITY_REJECTION_INTERNAL_REASON
    assert decision.participant_reason_code == IDENTITY_REJECTION_PARTICIPANT_REASON
    assert decision.decided_by == manager
    assert decision.internal_note_encrypted == ""  # the staff note stays in the identity history
    # Two histories: the identity decision keeps the reason and the note.
    identity_decision = rejected.decisions.get(action="REJECT")
    assert identity_decision.note_encrypted == NOTE
    assert identity_decision.reason_code == "DOCUMENT_NOT_GENUINE_SUSPECTED"
    assert AuditEvent.objects.filter(
        action_code="REV_DECISION_RECORDED",
        target_uuid=registration.pk,
        after_summary__origin="identity_rejection",
    ).exists()


def test_the_participant_is_notified_once_without_any_reason_or_identity_data(
    manual_case, manager
) -> None:
    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    messages = list(_rejection_messages(registration))
    assert len(messages) == 1
    message = messages[0]
    body = message.rendered_body_encrypted
    subject = message.rendered_subject
    assert "rejected" in subject
    assert registration.public_reference in body
    assert "register again" in body
    assert "same account" in body
    for secret in (UNKNOWN_NIN, NOTE, "DOCUMENT_NOT_GENUINE_SUSPECTED", "alteration", "ministry"):
        assert secret not in body
        assert secret not in subject
    # No second, generic decision e-mail for the same outcome.
    assert not CommunicationMessage.objects.filter(
        registration=registration, template_version__template__code="DECISION_STATUS"
    ).exists()


def test_the_notification_is_idempotent_for_the_rejection(manual_case, manager) -> None:
    """A retried or double-submitted rejection is a visible conflict and never
    queues a second message; the outcome service itself reuses the key."""
    from apps.reviews.services import record_identity_rejection_outcome

    rejected = _reject(manual_case, manager)
    with pytest.raises(review.StaleIdentityVersion):
        review.reject_identity(
            rejected.pk,
            actor=manager,
            expected_version=rejected.version - 1,
            reason_code="OTHER",
            note=NOTE,
            confirmed=True,
        )
    registration = Registration.objects.get(pk=rejected.registration_id)
    decision = rejected.decisions.get(action="REJECT")
    # Same identity decision, queued again (for example by a retried job): one message.
    from apps.communications.purposes import CommunicationPurpose
    from apps.reviews.services import _queue_registration_communication

    _queue_registration_communication(
        registration=registration,
        purpose_code=CommunicationPurpose.IDENTITY_REJECTION,
        context={
            "public_reference": registration.public_reference,
            "event_name": registration.event_edition.display_name(registration.preferred_language),
        },
        idempotency_key=f"identity-rejection:{decision.pk}",
    )
    assert _rejection_messages(registration).count() == 1
    # The outcome refuses to run twice on a closed registration.
    from apps.reviews.services import InvalidStateTransitionError

    with pytest.raises(InvalidStateTransitionError):
        record_identity_rejection_outcome(
            registration=registration, decided_by=manager, identity_decision_id=decision.pk
        )


def test_a_rejection_of_a_returned_case_closes_the_correction_request(
    manual_case, manager, reviewer
) -> None:
    returned = review.return_identity_for_correction(
        manual_case.pk, actor=reviewer, expected_version=manual_case.version, items=["NIN_NUMBER"]
    )
    registration = Registration.objects.get(pk=returned.registration_id)
    assert registration.public_status == RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED
    _reject(returned, manager)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert review.open_correction_request(registration) is None


def test_a_rejection_supersedes_an_earlier_approval(idv_event, legal_versions, manager) -> None:
    from apps.people.models import IdentityVerification
    from apps.people.tests.conftest import upload_national_id_card
    from apps.people.tests.identity_fixtures import assign_approval_prerequisites
    from apps.reviews.services import record_approved_decision

    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    case = case_for(registration)
    card = upload_national_id_card(registration)
    review.verify_identity_manually(
        case.pk,
        actor=manager,
        expected_version=case.version,
        evidence_document_id=card.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assign_approval_prerequisites(registration, manager)
    registration.refresh_from_db()
    approval = record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=manager,
        attendance_category="FOLLOWING_TWO_DAYS",
    )
    # The identity later goes back to manual review (as a linked correction would do).
    IdentityVerification.objects.filter(pk=case.pk).update(
        status=IdentityStatus.MANUAL_REVIEW,
        verification_source="",
        reason_code="IDENTITY_DATA_CHANGED",
    )
    _reject(case, manager)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    current = registration.decisions.get(is_current=True)
    assert current.supersedes_id == approval.pk
    approval.refresh_from_db()
    assert approval.is_current is False  # kept as history, never deleted


def test_a_return_for_correction_stays_remediable_on_the_same_registration(
    manual_case, reviewer
) -> None:
    returned = review.return_identity_for_correction(
        manual_case.pk, actor=reviewer, expected_version=manual_case.version, items=["NIN_NUMBER"]
    )
    registration = Registration.objects.get(pk=returned.registration_id)
    assert registration.is_current_context is True
    assert not registration.decisions.exists()
    assert not _rejection_messages(registration).exists()


def test_a_rejected_registration_cannot_be_reopened(manual_case, manager) -> None:
    from apps.reviews.services import reopen_registration

    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    with pytest.raises(ValueError, match="finally rejected"):
        reopen_registration(
            registration=registration,
            expected_version=registration.version,
            reason="Try again.",
            actor=manager,
        )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED


def test_a_closed_registration_is_still_refused(manual_case, manager) -> None:
    """R-IDV-06 is unchanged: no rejection of a withdrawn registration."""
    from apps.reviews.services import withdraw_registration

    registration = Registration.objects.get(pk=manual_case.registration_id)
    withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )
    with pytest.raises(review.IdentityStateError):
        _reject(manual_case, manager)
    assert not _rejection_messages(registration).exists()


# ---------------------------------------------------------------------------
# Participant: the message, and registering again from the same account
# ---------------------------------------------------------------------------


def test_the_workspace_tells_the_participant_and_offers_a_new_registration(
    manual_case, manager
) -> None:
    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    client = participant_client(registration.person)
    page = client.get(reverse("registrations:workspace"))
    assert page.status_code == 200
    assert "data-identity-rejected" in page.text
    assert "This registration was rejected" in page.text
    assert "register again from this account" in page.text
    assert "data-register-again" in page.text
    for secret in (UNKNOWN_NIN, NOTE, "DOCUMENT_NOT_GENUINE_SUSPECTED"):
        assert secret not in page.text


def test_register_again_opens_a_new_draft_on_the_same_account(manual_case, manager) -> None:
    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    person = registration.person
    client = participant_client(person)
    url = reverse("registrations:register-again", kwargs={"pk": registration.pk})
    response = client.post(url)
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-identity")
    drafts = Registration.objects.filter(
        person=person, event_edition=registration.event_edition, is_current_context=True
    )
    assert drafts.count() == 1
    draft = drafts.get()
    assert draft.pk != registration.pk
    assert draft.public_status == RegistrationPublicStatus.DRAFT
    assert draft.public_reference != registration.public_reference
    # A double click converges on the same draft; the rejected one is unchanged.
    client.post(url)
    assert drafts.count() == 1
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert (
        AuditEvent.objects.filter(
            action_code="IDV_REGISTRATION_RESTARTED", target_uuid=registration.pk
        ).count()
        == 1
    )
    # The identity step now serves the new draft.
    page = client.get(reverse("registrations:step-identity"))
    assert page.status_code == 200
    assert draft.public_reference in page.text


def test_register_again_is_refused_for_anything_but_an_identity_rejection(
    manual_case, manager
) -> None:
    from apps.reviews.services import record_not_approved_decision

    registration = Registration.objects.get(pk=manual_case.registration_id)
    record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="INCOMPLETE_INFORMATION",
        decided_by=manager,
    )
    client = participant_client(registration.person)
    client.post(reverse("registrations:register-again", kwargs={"pk": registration.pk}))
    assert not Registration.objects.filter(
        person=registration.person, public_status=RegistrationPublicStatus.DRAFT
    ).exists()


def test_register_again_belongs_to_the_owner_only(manual_case, manager) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    rejected = _reject(manual_case, manager)
    stranger = resolve_or_create_participant_for_email("q2-stranger@example.test")
    client = participant_client(stranger)
    response = client.post(
        reverse("registrations:register-again", kwargs={"pk": rejected.registration_id})
    )
    assert response.status_code == 404
    assert not Registration.objects.filter(person=stranger).exists()


def test_register_again_waits_while_the_public_channel_is_closed(manual_case, manager) -> None:
    from apps.events.models import EventEdition

    rejected = _reject(manual_case, manager)
    registration = Registration.objects.get(pk=rejected.registration_id)
    EventEdition.objects.filter(pk=registration.event_edition_id).update(
        public_registration_mode="CLOSED"
    )
    client = participant_client(registration.person)
    page = client.get(reverse("registrations:workspace"))
    assert 'data-register-again="' not in page.text  # no button (IDV-Q-C1 markup)
    assert 'data-register-again-next="EVENT_CLOSED"' in page.text
    assert "Registration is closed at the moment" in page.text
    response = client.post(reverse("registrations:register-again", kwargs={"pk": registration.pk}))
    assert response.status_code == 403
    assert not Registration.objects.filter(
        person=registration.person, public_status=RegistrationPublicStatus.DRAFT
    ).exists()


# ---------------------------------------------------------------------------
# Staff screens agree
# ---------------------------------------------------------------------------


def test_the_staff_screens_show_the_same_outcome(manual_case, manager) -> None:
    from apps.reviews.services import open_review_case

    registration = Registration.objects.get(pk=manual_case.registration_id)
    review_case = open_review_case(
        registration=registration, case_type="STANDARD", queue_code="GENERAL"
    )
    _reject(manual_case, manager)
    client = staff_client(manager)
    identity_page = client.get(reverse("identity:case", kwargs={"pk": manual_case.pk}))
    assert "data-idv-rejection-outcome" in identity_page.text
    review_page = client.get(reverse("reviews:case-detail", kwargs={"pk": review_case.pk}))
    assert 'data-decision-origin="identity"' in review_page.text
