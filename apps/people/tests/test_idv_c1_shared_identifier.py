"""IDV-C1, R-IDV-01: one person's shared identifier keeps one lifecycle across their cases.

Identifiers belong to the Person and are event-independent, so the same
person's registrations in different events share one NIN identifier. When
that identifier is replaced in one registration (a staff NIN correction or a
participant resubmission), every other current case of the person that uses
it is rebound to the replacement in the same transaction and its version
changes, so a stale confirmation is refused. An open or verified sibling goes
to manual review (`IDENTITY_DATA_CHANGED`); a returned one stays returned. A
replaced, revoked or expired identifier is never made VERIFIED again, and the
history (revisions, attempts, decisions) is kept.

Synthetic identities only (`UNKNOWN_NIN` and the corrected NIN are NOT_FOUND
in the development simulation).
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.db import transaction

from apps.people.models import (
    IdentifierStatus,
    IdentityDecisionAction,
    IdentityIdentifier,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerificationAttempt,
)
from apps.people.services import identity_review as review
from apps.people.services import identity_verification as idv
from apps.people.tests.conftest import (
    SIM_MATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
    upload_national_id_card,
)
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

CORRECTED_NIN = "123456789012345670"


@pytest.fixture
def reviewer(idv_reference_data):
    # Broad scope: the same person's registrations are in two events.
    return make_staff("idv-c1-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


def _person():
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email(f"idv-c1-{uuid.uuid4().hex[:10]}@example.test")


def _card(registration):
    return registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")


def _two_cases(legal_versions, *, nin=UNKNOWN_NIN, second_given="Amina"):
    person = _person()
    first = submit_case(make_event("IDVC1A"), legal_versions, nin=nin, person=person)
    second = submit_case(
        make_event("IDVC1B"), legal_versions, nin=nin, person=person, given=second_given
    )
    process_all()
    for registration in (first, second):
        upload_national_id_card(registration)
    return first, second


def _correct(registration, reviewer, new_nin=CORRECTED_NIN):
    case = case_for(registration)
    return review.correct_nin_and_recheck(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        new_nin=new_nin,
        reason_code="TYPING_ERROR_CONFIRMED",
        evidence_document_id=_card(registration).pk,
        confirmed=True,
    )


def _verify(case, reviewer, registration):
    return review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=_card(registration).pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )


def _status_of(identifier_id) -> str:
    return IdentityIdentifier.objects.get(pk=identifier_id).status


def test_a_stale_confirmation_after_a_correction_elsewhere_is_refused(
    legal_versions, reviewer
) -> None:
    first, second = _two_cases(legal_versions)
    stale_first = case_for(first)
    old_id = stale_first.current_revision.identifier_id
    assert case_for(second).current_revision.identifier_id == old_id  # one shared identifier
    _correct(second, reviewer)
    assert _status_of(old_id) == IdentifierStatus.REPLACED

    with pytest.raises((review.StaleIdentityVersion, review.IdentityStateError)):
        _verify(stale_first, reviewer, first)

    assert _status_of(old_id) == IdentifierStatus.REPLACED  # never resurrected
    assert case_for(first).status == IdentityStatus.MANUAL_REVIEW


def test_the_other_case_is_rebound_to_the_replacement_and_sent_to_review(
    legal_versions, reviewer
) -> None:
    first, second = _two_cases(legal_versions)
    before = case_for(first)
    _correct(second, reviewer)
    after = case_for(first)
    replacement = case_for(second).current_revision.identifier
    assert after.version > before.version
    assert after.current_revision.identifier_id == replacement.pk
    assert after.current_revision.source == IdentityRevisionSource.LINKED_IDENTITY_CHANGE
    assert after.current_revision.changed_fields == ["nin"]
    assert after.status == IdentityStatus.MANUAL_REVIEW
    assert after.reason_code == IdentityReasonCode.IDENTITY_DATA_CHANGED
    decision = after.decisions.order_by("-created_at").first()
    assert decision.action == IdentityDecisionAction.LINKED_IDENTITY_CHANGE
    assert decision.revision_id == after.current_revision_id
    # History is kept: the earlier revision and its provider attempt remain.
    assert after.revisions.filter(pk=before.current_revision_id).exists()
    assert IdentityVerificationAttempt.objects.filter(revision=before.current_revision).exists()


def test_refreshing_the_stale_case_cannot_confirm_the_replaced_nin(
    legal_versions, reviewer
) -> None:
    first, second = _two_cases(legal_versions)
    old_id = case_for(first).current_revision.identifier_id
    _correct(second, reviewer)

    refreshed = case_for(first)  # the reviewer reloads the page
    _verify(refreshed, reviewer, first)  # a fresh decision about the CURRENT identifier

    verified = case_for(first)
    assert verified.status == IdentityStatus.MANUALLY_VERIFIED
    assert verified.current_revision.identifier_id != old_id
    assert _status_of(old_id) == IdentifierStatus.REPLACED
    assert _status_of(verified.current_revision.identifier_id) == IdentifierStatus.VERIFIED


def test_a_verified_case_is_reopened_when_its_identifier_is_replaced(
    legal_versions, reviewer
) -> None:
    # The first registration is verified by the (simulated) service; the
    # second one of the same person has different names and goes to review.
    first, second = _two_cases(legal_versions, nin=SIM_MATCH_NIN, second_given="Samira")
    verified = case_for(first)
    assert verified.status == IdentityStatus.API_VERIFIED
    assert case_for(second).status == IdentityStatus.MANUAL_REVIEW
    old_id = verified.current_revision.identifier_id

    _correct(second, reviewer)

    reopened = case_for(first)
    assert reopened.status == IdentityStatus.MANUAL_REVIEW
    assert reopened.reason_code == IdentityReasonCode.IDENTITY_DATA_CHANGED
    assert reopened.verification_source == ""  # no misleading verification is kept
    assert reopened.decided_at is None
    assert _status_of(old_id) == IdentifierStatus.REPLACED
    # The provider attempt of the earlier verification stays as history.
    assert IdentityVerificationAttempt.objects.filter(
        registration=first, applied=True, is_official_provider=False
    ).exists()


def test_a_returned_case_stays_returned_but_a_stale_participant_form_is_refused(
    legal_versions, reviewer
) -> None:
    first, second = _two_cases(legal_versions)
    case = case_for(first)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    stale = case_for(first)
    _correct(second, reviewer)

    rebound = case_for(first)
    assert rebound.status == IdentityStatus.RETURNED_FOR_CORRECTION
    assert rebound.version > stale.version
    assert rebound.current_revision.identifier_id == (
        case_for(second).current_revision.identifier_id
    )
    resubmission = {
        "given_names": "Amina",
        "family_name": "Bentest",
        "date_of_birth": datetime.date(1990, 3, 7),
    }
    with pytest.raises(review.StaleIdentityVersion):
        review.resubmit_identity_correction(
            first,
            person=first.person,
            expected_version=stale.version,
            nin_value=UNKNOWN_NIN,
            **resubmission,
        )
    # The refreshed form shows the corrected NIN; resubmitting it is accepted.
    review.resubmit_identity_correction(
        first,
        person=first.person,
        expected_version=rebound.version,
        nin_value=CORRECTED_NIN,
        **resubmission,
    )
    assert case_for(first).current_revision.identifier_id == rebound.current_revision.identifier_id
    assert _status_of(stale.current_revision.identifier_id) == IdentifierStatus.REPLACED


def test_a_participant_correction_rebinds_the_persons_other_case(legal_versions, reviewer) -> None:
    first, second = _two_cases(legal_versions)
    old_id = case_for(first).current_revision.identifier_id
    case = case_for(second)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    review.resubmit_identity_correction(
        second,
        person=second.person,
        expected_version=case_for(second).version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        nin_value=CORRECTED_NIN,
    )
    other = case_for(first)
    assert other.current_revision.identifier_id == case_for(second).current_revision.identifier_id
    assert other.status == IdentityStatus.MANUAL_REVIEW
    assert other.reason_code == IdentityReasonCode.IDENTITY_DATA_CHANGED
    decision = other.decisions.order_by("-created_at").first()
    assert decision.action == IdentityDecisionAction.LINKED_IDENTITY_CHANGE
    assert decision.actor_person_id == second.person_id
    assert _status_of(old_id) == IdentifierStatus.REPLACED


@pytest.mark.parametrize(
    "status", [IdentifierStatus.REPLACED, IdentifierStatus.REVOKED, IdentifierStatus.EXPIRED]
)
def test_the_final_update_never_promotes_an_ineligible_identifier(
    idv_event, legal_versions, status
) -> None:
    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    case = case_for(registration)
    identifier = case.current_revision.identifier  # cached: still DECLARED in memory
    IdentityIdentifier.objects.filter(pk=identifier.pk).update(status=status)

    with transaction.atomic():
        reason = idv.verify_identifier_or_conflict(case, identifier)

    assert reason == IdentityReasonCode.IDENTITY_DATA_CHANGED
    assert _status_of(identifier.pk) == status
