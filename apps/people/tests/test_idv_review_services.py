"""Staff identity commands and the participant's resubmission (IDV-3).

Covers manual verification from evidence (and its refusals), the staff
exception, return for correction on the same registration, NIN correction
with duplicate checks and a new revision, final rejection, role and scope
boundaries, stale versions, pending and verified duplicates, the foreign path
and the separation from participation. Synthetic data only.
"""

from __future__ import annotations

import datetime

import pytest
from django.core import mail

from apps.audit.models import AuditEvent
from apps.documents.models import MalwareScanStatus, StoredObject
from apps.documents.tests.factories import make_test_photo
from apps.people.models import (
    IdentifierStatus,
    IdentityDecision,
    IdentityDecisionAction,
    IdentityJobStatus,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerificationJob,
    VerificationSource,
)
from apps.people.policies import IdentityPermissionDenied
from apps.people.services import identity_review as review
from apps.registrations.models import (
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSubmission,
    RegistrationSubmissionKind,
)
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

from .conftest import (
    SIM_MATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
    upload_national_id_card,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "idv-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manager(idv_event):
    return make_staff(
        "idv-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def not_found_case(idv_event, legal_versions):
    """An Algerian case in manual review (NOT_FOUND), with a clean card."""
    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    upload_national_id_card(registration)
    case = case_for(registration)
    assert case.reason_code == IdentityReasonCode.NOT_FOUND
    return case


def _card(case):
    return case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")


# ---------------------------------------------------------------------------
# Manual verification (IDV-06)
# ---------------------------------------------------------------------------


def test_manual_verification_after_not_found_records_source_actor_and_evidence(
    not_found_case, reviewer
) -> None:
    card = _card(not_found_case)
    case = review.verify_identity_manually(
        not_found_case.pk,
        actor=reviewer,
        expected_version=not_found_case.version,
        evidence_document_id=card.pk,
        reason_code="MINISTRY_RECORD_UNAVAILABLE",
        note="Card checked against the submitted data.",
    )
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_NATIONAL_ID_CARD
    assert case.decided_by == reviewer
    case.current_revision.identifier.refresh_from_db()
    assert case.current_revision.identifier.status == IdentifierStatus.VERIFIED
    decision = IdentityDecision.objects.get(verification=case)
    assert decision.action == IdentityDecisionAction.MANUAL_VERIFY
    assert decision.evidence_document_id == card.pk
    assert decision.note_encrypted == "Card checked against the submitted data."
    card.refresh_from_db()
    assert card.verified_by_user == reviewer
    # The unsuccessful API outcome is preserved beside the manual one.
    assert case.registration.verification_attempts.filter(outcome="NOT_FOUND").exists()
    # Identity verification never approves participation (IDV-10): the
    # registration only enters participation review (apps.reviews.intake).
    case.registration.refresh_from_db()
    assert case.registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert not case.registration.decisions.exists()


def test_manual_verification_needs_reviewable_evidence(not_found_case, reviewer) -> None:
    card = _card(not_found_case)
    for scan_status in (
        MalwareScanStatus.PENDING,
        MalwareScanStatus.REJECTED,
        MalwareScanStatus.FAILED,
    ):
        StoredObject.objects.filter(pk=card.stored_object_id).update(
            malware_scan_status=scan_status
        )
        with pytest.raises(review.EvidenceUnavailable):
            review.verify_identity_manually(
                not_found_case.pk,
                actor=reviewer,
                expected_version=not_found_case.version,
                evidence_document_id=card.pk,
                reason_code="DOCUMENT_MATCHES_SUBMISSION",
            )
    with pytest.raises(review.EvidenceUnavailable):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version,
            evidence_document_id=None,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )
    assert case_for(not_found_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_another_registrations_document_is_never_evidence(
    not_found_case, reviewer, idv_event, legal_versions
) -> None:
    other = submit_case(idv_event, legal_versions, nin="111111111111111110")
    other_card = upload_national_id_card(other)
    with pytest.raises(review.EvidenceUnavailable):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version,
            evidence_document_id=other_card.pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )


def test_an_unknown_reason_is_refused(not_found_case, reviewer) -> None:
    with pytest.raises(review.InvalidIdentityInput):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version,
            evidence_document_id=_card(not_found_case).pk,
            reason_code="BECAUSE",
        )


def test_a_pending_case_cannot_be_decided_manually(idv_event, legal_versions, reviewer) -> None:
    registration = submit_case(idv_event, legal_versions)
    upload_national_id_card(registration)
    case = case_for(registration)
    with pytest.raises(review.IdentityStateError):
        review.verify_identity_manually(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            evidence_document_id=_card(case).pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )


def test_a_stale_version_is_a_conflict_not_an_overwrite(not_found_case, reviewer) -> None:
    with pytest.raises(review.StaleIdentityVersion):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version - 1,
            evidence_document_id=_card(not_found_case).pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )


def test_a_foreign_passport_case_is_verified_from_the_identity_page(
    idv_event, legal_versions, reviewer
) -> None:
    registration = submit_case(idv_event, legal_versions, nationality="FR")
    case = case_for(registration)
    page = registration.documents.get(document_type="PASSPORT_IDENTITY_PAGE", status="ACTIVE")
    verified = review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=page.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert verified.verification_source == VerificationSource.MANUAL_PASSPORT
    assert not IdentityVerificationJob.objects.filter(verification=case).exists()


# ---------------------------------------------------------------------------
# Staff-assisted exception (A13-01)
# ---------------------------------------------------------------------------


def test_the_exception_needs_its_own_permission(not_found_case, reviewer) -> None:
    with pytest.raises(IdentityPermissionDenied):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version,
            evidence_document_id=_card(not_found_case).pk,
            reason_code="NO_USABLE_MINISTRY_RECORD",
            note="A documented explanation.",
            exception=True,
        )


def test_the_exception_needs_an_explanation(not_found_case, manager) -> None:
    with pytest.raises(review.InvalidIdentityInput):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=manager,
            expected_version=not_found_case.version,
            evidence_document_id=_card(not_found_case).pk,
            reason_code="NO_USABLE_MINISTRY_RECORD",
            note="short",
            exception=True,
        )


def test_the_exception_is_recorded_as_such(not_found_case, manager) -> None:
    case = review.verify_identity_manually(
        not_found_case.pk,
        actor=manager,
        expected_version=not_found_case.version,
        evidence_document_id=_card(not_found_case).pk,
        reason_code="NO_USABLE_MINISTRY_RECORD",
        note="Ministry record unavailable; card verified in person.",
        exception=True,
    )
    assert case.verification_source == VerificationSource.STAFF_EXCEPTION
    assert IdentityDecision.objects.get(verification=case).action == (
        IdentityDecisionAction.EXCEPTION_VERIFY
    )


def test_the_exception_is_never_available_on_the_passport_route(
    idv_event, legal_versions, manager
) -> None:
    registration = submit_case(idv_event, legal_versions, nationality="FR")
    case = case_for(registration)
    page = registration.documents.get(document_type="PASSPORT_IDENTITY_PAGE", status="ACTIVE")
    with pytest.raises(review.IdentityStateError):
        review.verify_identity_manually(
            case.pk,
            actor=manager,
            expected_version=case.version,
            evidence_document_id=page.pk,
            reason_code="NO_USABLE_MINISTRY_RECORD",
            note="A documented explanation.",
            exception=True,
        )


# ---------------------------------------------------------------------------
# Duplicates (IDV-07)
# ---------------------------------------------------------------------------


def test_a_pending_duplicate_of_another_person_is_caught_at_submission(
    idv_event, legal_versions
) -> None:
    first = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    second = submit_case(
        idv_event, legal_versions, nin=SIM_MATCH_NIN, given="Other", family="Person"
    )
    assert case_for(first).status == IdentityStatus.PENDING
    second_case = case_for(second)
    assert (second_case.status, second_case.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.DUPLICATE_IDENTIFIER,
    )
    assert not IdentityVerificationJob.objects.filter(verification=second_case).exists()
    # The first one's own check then refuses to verify while the claim is open.
    process_all()
    first_case = case_for(first)
    assert (first_case.status, first_case.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.DUPLICATE_IDENTIFIER,
    )
    first_case.current_revision.identifier.refresh_from_db()
    assert first_case.current_revision.identifier.status == IdentifierStatus.DECLARED


def test_manual_verification_cannot_override_a_cross_person_duplicate(
    idv_event, legal_versions, reviewer
) -> None:
    first = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    second = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN, given="Other", family="Person")
    upload_national_id_card(second)
    case = case_for(second)
    with pytest.raises(review.DuplicateIdentityConflict):
        review.verify_identity_manually(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            evidence_document_id=_card(case).pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )
    assert case_for(second).status == IdentityStatus.MANUAL_REVIEW
    assert case_for(first).status in (IdentityStatus.PENDING, IdentityStatus.MANUAL_REVIEW)


def test_a_finally_rejected_claim_no_longer_blocks_the_other_person(
    idv_event, legal_versions, reviewer, manager
) -> None:
    first = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    second = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN, given="Other", family="Person")
    second_case = case_for(second)
    review.reject_identity(
        second_case.pk,
        actor=manager,
        expected_version=second_case.version,
        reason_code="IDENTITY_OF_ANOTHER_PERSON",
        note="The card shows another person's NIN.",
        confirmed=True,
    )
    upload_national_id_card(first)
    first_case = case_for(first)
    verified = review.verify_identity_manually(
        first_case.pk,
        actor=reviewer,
        expected_version=first_case.version,
        evidence_document_id=_card(first_case).pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert verified.status == IdentityStatus.MANUALLY_VERIFIED


def test_the_same_person_on_another_event_is_not_a_duplicate(idv_event, legal_versions) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    other_event = make_event("IDVOTHER")
    person = resolve_or_create_participant_for_email("idv-same-person@example.test")
    submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN, person=person)
    process_all()
    second = submit_case(other_event, legal_versions, nin=SIM_MATCH_NIN, person=person)
    assert case_for(second).reason_code != IdentityReasonCode.DUPLICATE_IDENTIFIER
    process_all()
    assert case_for(second).status == IdentityStatus.API_VERIFIED


def test_a_verified_identifier_of_another_person_conflicts_on_every_event(
    idv_event, legal_versions
) -> None:
    submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    process_all()
    other = submit_case(
        make_event("IDVTWO"), legal_versions, nin=SIM_MATCH_NIN, given="X", family="Y"
    )
    assert case_for(other).reason_code == IdentityReasonCode.DUPLICATE_IDENTIFIER


# ---------------------------------------------------------------------------
# Return for correction and participant resubmission (IDV-09)
# ---------------------------------------------------------------------------


def _return(case, actor, items=("NIN_NUMBER", "NATIONAL_ID_CARD")):
    return review.return_identity_for_correction(
        case.pk, actor=actor, expected_version=case.version, items=list(items)
    )


def test_return_for_correction_uses_the_additional_information_status_and_notifies(
    not_found_case, reviewer
) -> None:
    mail.outbox.clear()
    case = _return(not_found_case, reviewer)
    assert case.status == IdentityStatus.RETURNED_FOR_CORRECTION
    registration = case.registration
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED
    assert registration.internal_status == RegistrationInternalStatus.AWAITING_APPLICANT
    from apps.communications.models import CommunicationMessage

    message = CommunicationMessage.objects.get(registration=registration)
    text = f"{message.rendered_subject} {message.rendered_body_encrypted}"
    assert "NOT_FOUND" not in text and UNKNOWN_NIN not in text  # safe, existing template


def test_return_items_must_belong_to_the_route(not_found_case, reviewer) -> None:
    with pytest.raises(review.InvalidIdentityInput):
        _return(not_found_case, reviewer, items=("PASSPORT_PAGE",))
    with pytest.raises(review.InvalidIdentityInput):
        _return(not_found_case, reviewer, items=())


def test_return_is_refused_while_an_information_request_is_active(not_found_case, reviewer) -> None:
    from apps.reviews.services import create_information_request

    create_information_request(
        registration=not_found_case.registration,
        purpose="CLARIFICATION",
        message_en="Synthetic question.",
        items=[{"kind": "CLARIFICATION", "field_code": "job_title"}],
        created_by=reviewer,
    )
    with pytest.raises(review.IdentityStateError):
        _return(not_found_case, reviewer)


def test_participant_resubmission_with_a_changed_nin_starts_a_new_check_on_the_same_registration(
    not_found_case, reviewer
) -> None:
    case = _return(not_found_case, reviewer)
    registration = case.registration
    submissions_before = RegistrationSubmission.objects.filter(registration=registration).count()
    updated = review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=case.version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        nin_value=SIM_MATCH_NIN,
    )
    assert updated.registration_id == registration.pk  # same registration, same account
    assert updated.status == IdentityStatus.PENDING
    assert updated.current_revision.number == 2
    assert updated.current_revision.source == IdentityRevisionSource.PARTICIPANT_RESUBMISSION
    assert updated.current_revision.changed_fields == ["nin"]
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    assert (
        RegistrationSubmission.objects.filter(
            registration=registration,
            submission_kind=RegistrationSubmissionKind.ADDITIONAL_INFORMATION_RESPONSE,
        ).count()
        == 1
    )
    assert RegistrationSubmission.objects.filter(registration=registration).count() == (
        submissions_before + 1
    )
    old_identifiers = registration.person.identity_identifiers.filter(
        status=IdentifierStatus.REPLACED
    )
    assert old_identifiers.count() == 1
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}
    # History is preserved: every revision and the return decision remain.
    # IDV-C1 (R-IDV-02): the verified case- and spacing-only name difference
    # adds the recorded official-name revision.
    assert list(updated.revisions.order_by("number").values_list("source", flat=True)) == [
        IdentityRevisionSource.SUBMISSION,
        IdentityRevisionSource.PARTICIPANT_RESUBMISSION,
        IdentityRevisionSource.OFFICIAL_NAME_NORMALIZATION,
    ]
    assert updated.decisions.filter(action=IdentityDecisionAction.RETURN_FOR_CORRECTION).exists()


def test_resubmitting_only_evidence_returns_to_manual_review(not_found_case, reviewer) -> None:
    case = _return(not_found_case, reviewer, items=("NATIONAL_ID_CARD",))
    registration = case.registration
    updated = review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=case.version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        nin_value=UNKNOWN_NIN,
        national_id_card_file=make_test_photo(width=320, height=320),
    )
    assert (updated.status, updated.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.PARTICIPANT_RESUBMITTED,
    )
    assert not IdentityVerificationJob.objects.filter(revision=updated.current_revision).exists()


def test_a_requested_document_must_be_supplied(not_found_case, reviewer) -> None:
    from apps.documents.models import Document, DocumentStatus

    Document.objects.filter(registration=not_found_case.registration).filter(
        document_type="NATIONAL_ID_CARD"
    ).update(status=DocumentStatus.REPLACED)
    case = _return(not_found_case, reviewer, items=("NATIONAL_ID_CARD",))
    with pytest.raises(review.EvidenceUnavailable):
        review.resubmit_identity_correction(
            case.registration,
            person=case.registration.person,
            expected_version=case.version,
            given_names="Amina",
            family_name="Bentest",
            date_of_birth=datetime.date(1990, 3, 7),
            nin_value=UNKNOWN_NIN,
        )
    assert case_for(case.registration).status == IdentityStatus.RETURNED_FOR_CORRECTION


def test_a_second_resubmission_is_refused_as_stale(not_found_case, reviewer) -> None:
    case = _return(not_found_case, reviewer, items=("NIN_NUMBER",))
    kwargs = dict(
        person=case.registration.person,
        expected_version=case.version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        nin_value=SIM_MATCH_NIN,
    )
    review.resubmit_identity_correction(case.registration, **kwargs)
    with pytest.raises(review.StaleIdentityVersion):
        review.resubmit_identity_correction(case.registration, **kwargs)


def test_only_the_owner_can_resubmit(not_found_case, reviewer) -> None:
    from apps.people.services import resolve_or_create_participant_for_email

    case = _return(not_found_case, reviewer, items=("NIN_NUMBER",))
    stranger = resolve_or_create_participant_for_email("idv-stranger@example.test")
    with pytest.raises(review.IdentityStateError):
        review.resubmit_identity_correction(
            case.registration,
            person=stranger,
            expected_version=case.version,
            given_names="Amina",
            family_name="Bentest",
            date_of_birth=datetime.date(1990, 3, 7),
            nin_value=SIM_MATCH_NIN,
        )


def test_a_foreign_participant_resubmits_passport_details(
    idv_event, legal_versions, reviewer
) -> None:
    registration = submit_case(idv_event, legal_versions, nationality="FR")
    case = case_for(registration)
    case = review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["PASSPORT_PAGE"]
    )
    updated = review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=case.version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        passport_number="Y7654321",
        passport_country_code_id="FR",
        passport_expires_at=datetime.date.today() + datetime.timedelta(days=500),
        passport_page_file=make_test_photo(width=400, height=300),
    )
    assert updated.status == IdentityStatus.MANUAL_REVIEW  # always manual, no ministry call
    assert "passport_number" in updated.current_revision.changed_fields
    assert not IdentityVerificationJob.objects.filter(verification=updated).exists()


# ---------------------------------------------------------------------------
# NIN correction and recheck (IDV-05)
# ---------------------------------------------------------------------------


def test_staff_nin_correction_needs_evidence_confirmation_and_a_free_number(
    not_found_case, reviewer, idv_event, legal_versions
) -> None:
    common = dict(actor=reviewer, expected_version=not_found_case.version, new_nin=SIM_MATCH_NIN)
    with pytest.raises(review.InvalidIdentityInput):  # not confirmed
        review.correct_nin_and_recheck(
            not_found_case.pk,
            reason_code="TYPING_ERROR_CONFIRMED",
            evidence_document_id=_card(not_found_case).pk,
            **common,
        )
    with pytest.raises(review.EvidenceUnavailable):
        review.correct_nin_and_recheck(
            not_found_case.pk, reason_code="TYPING_ERROR_CONFIRMED", confirmed=True, **common
        )
    with pytest.raises(review.InvalidIdentityInput):  # a recheck reason for a changed NIN
        review.correct_nin_and_recheck(
            not_found_case.pk,
            reason_code="RECHECK_AFTER_TECHNICAL_FAILURE",
            evidence_document_id=_card(not_found_case).pk,
            confirmed=True,
            **common,
        )
    submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN, given="Other", family="Holder")
    with pytest.raises(review.DuplicateIdentityConflict):
        review.correct_nin_and_recheck(
            not_found_case.pk,
            reason_code="TYPING_ERROR_CONFIRMED",
            evidence_document_id=_card(not_found_case).pk,
            confirmed=True,
            **common,
        )
    assert case_for(not_found_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_staff_nin_correction_creates_a_revision_and_a_new_check(not_found_case, reviewer) -> None:
    old_identifier = not_found_case.current_revision.identifier
    case = review.correct_nin_and_recheck(
        not_found_case.pk,
        actor=reviewer,
        expected_version=not_found_case.version,
        new_nin=SIM_MATCH_NIN,
        reason_code="TYPING_ERROR_CONFIRMED",
        evidence_document_id=_card(not_found_case).pk,
        confirmed=True,
    )
    assert case.status == IdentityStatus.PENDING  # never optimistically verified
    assert case.current_revision.source == IdentityRevisionSource.STAFF_NIN_CORRECTION
    old_identifier.refresh_from_db()
    assert old_identifier.status == IdentifierStatus.REPLACED
    assert case.current_revision.identifier.value_encrypted == SIM_MATCH_NIN
    job = IdentityVerificationJob.objects.get(revision=case.current_revision)
    assert job.status == IdentityJobStatus.PENDING
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}
    events = AuditEvent.objects.filter(action_code="IDV_NIN_CORRECTED")
    assert events.count() == 1
    assert SIM_MATCH_NIN not in str(events.get().after_summary)


def test_a_recheck_of_the_same_nin_needs_no_evidence(not_found_case, reviewer) -> None:
    case = review.correct_nin_and_recheck(
        not_found_case.pk,
        actor=reviewer,
        expected_version=not_found_case.version,
        new_nin=UNKNOWN_NIN,
        reason_code="RECHECK_AFTER_TECHNICAL_FAILURE",
    )
    assert case.current_revision.source == IdentityRevisionSource.STAFF_RECHECK
    assert case.current_revision.changed_fields == []
    assert case.status == IdentityStatus.PENDING


# ---------------------------------------------------------------------------
# Final rejection
# ---------------------------------------------------------------------------


def test_final_rejection_requires_permission_explanation_and_confirmation(
    not_found_case, reviewer, manager
) -> None:
    with pytest.raises(IdentityPermissionDenied):
        review.reject_identity(
            not_found_case.pk,
            actor=reviewer,
            expected_version=not_found_case.version,
            reason_code="NO_VALID_EVIDENCE",
            note="A sufficient explanation.",
            confirmed=True,
        )
    with pytest.raises(review.InvalidIdentityInput):
        review.reject_identity(
            not_found_case.pk,
            actor=manager,
            expected_version=not_found_case.version,
            reason_code="NO_VALID_EVIDENCE",
            note="short",
            confirmed=True,
        )
    with pytest.raises(review.InvalidIdentityInput):
        review.reject_identity(
            not_found_case.pk,
            actor=manager,
            expected_version=not_found_case.version,
            reason_code="NO_VALID_EVIDENCE",
            note="A sufficient explanation.",
            confirmed=False,
        )
    case = review.reject_identity(
        not_found_case.pk,
        actor=manager,
        expected_version=not_found_case.version,
        reason_code="NO_VALID_EVIDENCE",
        note="A sufficient explanation.",
        confirmed=True,
    )
    assert case.status == IdentityStatus.REJECTED
    case.registration.refresh_from_db()
    # Owner decision IDV-Q2 (supersedes "no decision is recorded here"): the
    # rejection records its own coded NOT_APPROVED participation decision.
    decision = case.registration.decisions.get()
    assert decision.outcome == "NOT_APPROVED"
    assert decision.internal_reason_code == "IDENTITY_REJECTED"


def test_rejecting_a_returned_case_withdraws_the_correction_request(
    not_found_case, reviewer, manager
) -> None:
    case = _return(not_found_case, reviewer, items=("NIN_NUMBER",))
    rejected = review.reject_identity(
        case.pk,
        actor=manager,
        expected_version=case.version,
        reason_code="DOCUMENT_NOT_GENUINE_SUSPECTED",
        note="The card shows clear signs of alteration.",
        confirmed=True,
    )
    rejected.registration.refresh_from_db()
    # Owner decision IDV-Q2 (supersedes UNDER_REVIEW): the correction request
    # is withdrawn and the registration is closed as NOT_APPROVED.
    assert rejected.registration.public_status == RegistrationPublicStatus.NOT_APPROVED


# ---------------------------------------------------------------------------
# Roles and scope (AS-13)
# ---------------------------------------------------------------------------


def test_read_only_intake_staff_cannot_act(not_found_case) -> None:
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME

    intake = make_staff(
        "idv-intake@example.test",
        REGISTRATION_INTAKE_GROUP_NAME,
        event=not_found_case.event_edition,
    )
    with pytest.raises(IdentityPermissionDenied):
        review.verify_identity_manually(
            not_found_case.pk,
            actor=intake,
            expected_version=not_found_case.version,
            evidence_document_id=_card(not_found_case).pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )
    with pytest.raises(IdentityPermissionDenied):
        _return(not_found_case, intake)


def test_staff_scoped_to_another_event_cannot_act(not_found_case) -> None:
    outsider = make_staff(
        "idv-outsider@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=make_event("IDVELSE")
    )
    for command, kwargs in (
        (
            review.verify_identity_manually,
            dict(
                evidence_document_id=_card(not_found_case).pk,
                reason_code="DOCUMENT_MATCHES_SUBMISSION",
            ),
        ),
        (review.return_identity_for_correction, dict(items=["NIN_NUMBER"])),
        (
            review.correct_nin_and_recheck,
            dict(new_nin=UNKNOWN_NIN, reason_code="RECHECK_AFTER_TECHNICAL_FAILURE"),
        ),
        (
            review.reject_identity,
            dict(reason_code="OTHER", note="A sufficient explanation.", confirmed=True),
        ),
    ):
        with pytest.raises(IdentityPermissionDenied):
            command(
                not_found_case.pk, actor=outsider, expected_version=not_found_case.version, **kwargs
            )
    assert case_for(not_found_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_the_role_mapping_is_exactly_the_documented_one() -> None:
    from django.contrib.auth.models import Group

    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME
    from apps.people.policies import IDENTITY_CODENAMES

    def identity_permissions(name):
        return set(
            Group.objects.get(name=name)
            .permissions.filter(content_type__app_label="people")
            .values_list("codename", flat=True)
        ) & set(IDENTITY_CODENAMES)

    assert identity_permissions(REGISTRATION_REVIEWERS_GROUP_NAME) == {
        "view_identityverification",
        "view_identity_evidence",
        "verify_identity_manually",
        "correct_identity_nin",
        "return_identity_for_correction",
    }
    assert identity_permissions(ACCREDITATION_MANAGERS_GROUP_NAME) == set(IDENTITY_CODENAMES)
    assert identity_permissions(REGISTRATION_INTAKE_GROUP_NAME) == set()
    holders = {
        group.name
        for group in Group.objects.filter(
            permissions__content_type__app_label="people",
            permissions__codename__in=IDENTITY_CODENAMES,
        )
    }
    assert holders == {REGISTRATION_REVIEWERS_GROUP_NAME, ACCREDITATION_MANAGERS_GROUP_NAME}


def test_a_broker_outage_never_fails_a_saved_return_or_resubmission(
    not_found_case, reviewer, django_capture_on_commit_callbacks
) -> None:
    """ASYNC-01: the participant email is queued after commit; a broker error
    is swallowed and the PENDING outbox event stays for the sweeper."""
    from unittest import mock

    from apps.communications import tasks as communication_tasks
    from apps.core.models import OutboxEvent, OutboxEventStatus

    with mock.patch.object(
        communication_tasks.dispatch_communication_outbox_event_task,
        "delay",
        side_effect=ConnectionError("synthetic broker outage"),
    ) as delay:
        with django_capture_on_commit_callbacks(execute=True):
            case = _return(not_found_case, reviewer, items=("NIN_NUMBER",))
        with django_capture_on_commit_callbacks(execute=True):
            review.resubmit_identity_correction(
                case.registration,
                person=case.registration.person,
                expected_version=case.version,
                given_names="Amina",
                family_name="Bentest",
                date_of_birth=datetime.date(1990, 3, 7),
                nin_value=SIM_MATCH_NIN,
            )
    assert delay.call_count == 2
    assert case_for(case.registration).status == IdentityStatus.PENDING
    assert (
        OutboxEvent.objects.filter(
            event_type="communications.message_queued", status=OutboxEventStatus.PENDING
        ).count()
        == 2
    )
