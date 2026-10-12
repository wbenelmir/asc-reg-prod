"""UPD-03: the note to the participant on an identity correction request.

Covered: the submission language shown to staff independently of the staff
interface language (recorded, derived for older registrations, or unknown
with an explicit choice), the note stored with its own request (repeated
cycles keep the sequence), shown to the participant escaped with its line
breaks, and sent once per request in the participant's language through the
existing outbox (retries and double submissions never duplicate it).
Synthetic identities only.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import translation

from apps.communications.models import CommunicationMessage
from apps.communications.tasks import deliver_message
from apps.people.models import IdentityDecision, IdentityDecisionAction, IdentityStatus
from apps.people.services import identity_review as review
from apps.registrations import language as registration_language
from apps.registrations.models import Registration, RegistrationSubmission
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

from .conftest import (
    UNKNOWN_NIN,
    case_for,
    make_staff,
    participant_client,
    process_all,
    staff_client,
    submit_case,
)

pytestmark = pytest.mark.django_db

NOTE = "Bonjour,\nveuillez vérifier votre NIN.\n<script>alert(1)</script>"


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "upd03-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


def _case(idv_event, legal_versions, *, language="fr"):
    with translation.override(language):
        registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


def _messages(registration):
    return CommunicationMessage.objects.filter(
        registration=registration,
        template_version__template__code="IDENTITY_CORRECTION_REQUEST",
    )


def test_a_new_submission_records_its_language(idv_event, legal_versions):
    case = _case(idv_event, legal_versions, language="ar")
    registration = Registration.objects.get(pk=case.registration_id)
    assert registration.submission_language == "ar"
    language = registration_language.submission_language_for(registration)
    assert (language.code, language.basis) == ("ar", registration_language.RECORDED)


def test_the_note_is_sent_in_the_participant_language_whatever_the_staff_language(
    idv_event, legal_versions, reviewer
):
    case = _case(idv_event, legal_versions, language="fr")
    with translation.override("en"):  # the reviewer works in English
        review.return_identity_for_correction(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            items=["NIN_NUMBER"],
            participant_note=NOTE,
        )
    decision = IdentityDecision.objects.get(
        verification=case, action=IdentityDecisionAction.RETURN_FOR_CORRECTION
    )
    assert decision.participant_note_encrypted == NOTE.replace("\r\n", "\n")
    assert (decision.notification_language, decision.notification_language_basis) == (
        "fr",
        "RECORDED",
    )
    message = _messages(case.registration).get()
    assert message.language == "fr"
    mail.outbox.clear()
    deliver_message(str(message.pk))
    body = mail.outbox[-1].body
    assert "Message de l'équipe d'inscription :" in body  # the participant's language
    assert "veuillez vérifier votre NIN.\n" in body  # line breaks kept (plain text)
    assert "Veuillez corriger" in mail.outbox[-1].subject


def test_the_participant_sees_the_note_escaped_with_its_line_breaks(
    idv_event, legal_versions, reviewer
):
    case = _case(idv_event, legal_versions)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"],
        participant_note=NOTE,
    )  # fmt: skip
    registration = Registration.objects.get(pk=case.registration_id)
    page = participant_client(registration.person).get(
        reverse("identity:participant-correction", kwargs={"pk": registration.pk})
    )
    content = page.content.decode()
    assert "data-idv-participant-note" in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "<script>alert(1)</script>" not in content
    assert "asc-preline" in content


def test_repeated_cycles_keep_each_note_with_its_own_request(idv_event, legal_versions, reviewer):
    case = _case(idv_event, legal_versions)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"],
        participant_note="Premier message",
    )  # fmt: skip
    registration = Registration.objects.get(pk=case.registration_id)
    review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=case_for(registration).version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=registration.profile.date_of_birth,
        nin_value="123456789012345670",
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["BIRTH_DATE"],
        participant_note="Deuxième message",
    )  # fmt: skip
    notes = list(
        IdentityDecision.objects.filter(
            verification=case, action=IdentityDecisionAction.RETURN_FOR_CORRECTION
        )
        .order_by("created_at")
        .values_list("participant_note_encrypted", "requested_items")
    )
    assert notes == [("Premier message", ["NIN_NUMBER"]), ("Deuxième message", ["BIRTH_DATE"])]
    assert _messages(registration).count() == 2
    assert review.open_correction_request(registration).participant_note == "Deuxième message"


def test_retries_and_double_submissions_never_send_twice(idv_event, legal_versions, reviewer):
    case = _case(idv_event, legal_versions)
    client = staff_client(reviewer)
    data = {
        "expected_version": case.version,
        "items": ["NIN_NUMBER"],
        "participant_note": "Une seule fois",
        "advance": "stay",
    }
    url = reverse("identity:return", kwargs={"pk": case.pk})
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 409  # the same rendered form again
    registration = Registration.objects.get(pk=case.registration_id)
    assert _messages(registration).count() == 1
    decision = IdentityDecision.objects.get(verification=case, action="RETURN_FOR_CORRECTION")
    # A replayed notification (an outbox retry) converges on the same message.
    review._notify(
        registration,
        purpose_code="IDENTITY_CORRECTION_REQUEST",
        idempotency_key=f"identity-correction-request:{decision.pk}",
        language="fr",
        extra_context={
            "participant_note_section": review.participant_note_section("Une seule fois", "fr")
        },
    )
    assert _messages(registration).count() == 1


def test_an_older_registration_uses_the_language_of_its_accepted_notice(
    idv_event, legal_versions, reviewer
):
    case = _case(idv_event, legal_versions)
    registration = Registration.objects.get(pk=case.registration_id)
    Registration.objects.filter(pk=registration.pk).update(submission_language="")
    submission = RegistrationSubmission.objects.get(registration=registration)
    snapshot = submission.snapshot_json
    snapshot["legal_acceptance"]["privacy_notice"]["language"] = "ar"
    RegistrationSubmission.objects.filter(pk=submission.pk).update(snapshot_json=snapshot)
    registration.refresh_from_db()
    language = registration_language.submission_language_for(registration)
    assert (language.code, language.basis) == ("ar", registration_language.ACCEPTED_NOTICE)
    page = staff_client(reviewer).get(reverse("identity:case", kwargs={"pk": case.pk}))
    assert 'data-idv-submission-language="ar"' in page.content.decode()


def test_an_unknown_language_is_shown_as_unknown_and_chosen_explicitly(
    idv_event, legal_versions, reviewer
):
    case = _case(idv_event, legal_versions)
    registration = Registration.objects.get(pk=case.registration_id)
    Registration.objects.filter(pk=registration.pk).update(
        submission_language="", preferred_language="en"
    )
    submission = RegistrationSubmission.objects.get(registration=registration)
    snapshot = submission.snapshot_json
    snapshot["legal_acceptance"] = {}
    RegistrationSubmission.objects.filter(pk=submission.pk).update(snapshot_json=snapshot)
    client = staff_client(reviewer)
    page = client.get(reverse("identity:case", kwargs={"pk": case.pk})).content.decode()
    assert 'data-idv-submission-language="unknown"' in page
    assert 'name="notification_language"' in page
    # Without a choice the form is refused; nothing is sent.
    url = reverse("identity:return", kwargs={"pk": case.pk})
    base = {"expected_version": case.version, "items": ["NIN_NUMBER"], "advance": "stay"}
    assert client.post(url, base).status_code == 400
    assert not _messages(registration).exists()
    assert client.post(url, {**base, "notification_language": "ar"}).status_code == 302
    decision = IdentityDecision.objects.get(verification=case, action="RETURN_FOR_CORRECTION")
    assert (decision.notification_language, decision.notification_language_basis) == (
        "ar",
        "STAFF_SELECTED",
    )
    assert _messages(registration).get().language == "ar"


def test_a_note_with_control_characters_or_too_long_is_refused(idv_event, legal_versions, reviewer):
    case = _case(idv_event, legal_versions)
    for note, code in (
        ("bad\x07bell", "participant_note_invalid"),
        ("x" * 2001, "participant_note_too_long"),
    ):
        with pytest.raises(review.InvalidIdentityInput) as raised:
            review.return_identity_for_correction(
                case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"],
                participant_note=note,
            )  # fmt: skip
        assert raised.value.code == code
    assert case_for(case.registration).status == IdentityStatus.MANUAL_REVIEW
