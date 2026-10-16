"""IDV-FIRST: identity verification from the first manual review, without a document.

A reviewer verifies a case in manual review immediately ("Verify identity"
or "Save corrections and verify identity"), without returning it to the
participant or asking for an upload. Without a document, a reason saying so
and an internal justification are required, and the basis is recorded as a
manual reviewer decision (`MANUAL_REVIEWER_DECISION`), never as a document or
a Ministry verification. Covered: the first review with no document and no
earlier correction request, the required justification, existing documents,
NIN rechecks with truthful provenance and the presume rule, duplicates,
foreign participants (never a Ministry call), the NIN exemption route (still
documentary), permissions, scope, closed registrations, stale versions, late
worker results, the audit and history, and participation review entered
exactly once without any approval or correction request. Synthetic data only.
"""

from __future__ import annotations

import datetime
import json
import re

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.documents.models import Document, DocumentStatus
from apps.people.models import (
    IdentifierStatus,
    IdentityDecision,
    IdentityDecisionAction,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerificationAttempt,
    IdentityVerificationMethod,
    VerificationSource,
)
from apps.people.nin_provider import LocalSimulationNinProvider
from apps.people.policies import IdentityPermissionDenied
from apps.people.services import identity_review as review
from apps.registrations.models import RegistrationProfile, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME
from apps.reviews.models import InformationRequest, ReviewCase

from .conftest import (
    SIM_MATCH_NIN,
    SIM_NAME_MISMATCH_NIN,
    SIM_PRESUMED_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)

pytestmark = pytest.mark.django_db

OTHER_UNKNOWN_NIN = "123456789012345679"
JUSTIFICATION = "Identity confirmed by phone with the delegation head."
VERIFY_REASON = "REVIEWER_CONFIRMED_WITHOUT_DOCUMENT"
CORRECT_REASON = "CORRECTED_WITHOUT_DOCUMENT"
IDENTITY_EVIDENCE_TYPES = ("NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE")


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "first-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def first_case(idv_event, legal_versions):
    """An Algerian case at its first manual review (NIN not found): no
    document was ever uploaded and no correction was ever requested."""
    registration = submit_case(
        idv_event, legal_versions, nin=UNKNOWN_NIN, given="Amina", family="Bentest"
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert not registration.documents.filter(document_type__in=IDENTITY_EVIDENCE_TYPES).exists()
    assert not case.decisions.filter(action=IdentityDecisionAction.RETURN_FOR_CORRECTION)
    return case


@pytest.fixture
def passport_case(idv_event, legal_versions):
    """A foreign participant in manual review whose passport page is gone."""
    registration = submit_case(
        idv_event, legal_versions, nationality="FR", given="Lucie", family="Martin"
    )
    Document.objects.filter(registration=registration).update(status=DocumentStatus.REPLACED)
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


class SpyProvider(LocalSimulationNinProvider):
    calls: list = []

    def lookup(self, nin):
        SpyProvider.calls.append(nin)
        return super().lookup(nin)


def _verify(case, actor, **overrides):
    kwargs = {
        "actor": actor,
        "expected_version": case.version,
        "evidence_document_id": None,
        "reason_code": VERIFY_REASON,
        "note": JUSTIFICATION,
        **overrides,
    }
    return review.verify_identity_manually(case.pk, **kwargs)


def _correct(case, actor, **overrides):
    kwargs = {
        "actor": actor,
        "expected_version": case.version,
        "given_names": "Amina",
        "family_name": "Bentest",
        "date_of_birth": datetime.date(1990, 3, 7),
        "nin": UNKNOWN_NIN if case.route == "NIN" else "",
        "evidence_document_id": None,
        "reason_code": CORRECT_REASON,
        "note": JUSTIFICATION,
        **overrides,
    }
    return review.correct_and_verify_identity(case.pk, **kwargs)


def _open_standard_cases(registration):
    return ReviewCase.objects.filter(
        registration=registration, case_type="STANDARD", status__in=("QUEUED", "ASSIGNED")
    )


def _assert_entered_participation_review_only(registration):
    """Exactly one participation case, no approval, no correction request."""
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert ReviewCase.objects.filter(registration=registration).count() == 1
    assert _open_standard_cases(registration).count() == 1
    assert not registration.decisions.exists()  # never an attendance approval
    assert not InformationRequest.objects.filter(registration=registration).exists()
    assert not IdentityDecision.objects.filter(
        verification__registration=registration,
        action=IdentityDecisionAction.RETURN_FOR_CORRECTION,
    ).exists()
    assert not CommunicationMessage.objects.filter(
        registration=registration,
        template_version__template__code="IDENTITY_CORRECTION_REQUEST",
    ).exists()


# ---------------------------------------------------------------------------
# "Verify identity" without a document
# ---------------------------------------------------------------------------


def test_first_review_verifies_without_a_document_as_a_manual_decision(first_case, reviewer):
    documents_before = set(
        Document.objects.filter(registration=first_case.registration).values_list("pk", "status")
    )
    case = _verify(first_case, reviewer)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    assert case.decided_by == reviewer and case.decided_at is not None
    case.current_revision.identifier.refresh_from_db()
    assert case.current_revision.identifier.status == IdentifierStatus.VERIFIED
    # No document is invented, and the decision carries the justification.
    assert (
        set(Document.objects.filter(registration=case.registration).values_list("pk", "status"))
        == documents_before
    )
    assert not Document.objects.filter(
        registration=case.registration, verified_by_user__isnull=False
    ).exists()
    decision = IdentityDecision.objects.get(verification=case)
    assert decision.action == IdentityDecisionAction.MANUAL_VERIFY
    assert decision.evidence_document_id is None
    assert decision.reason_code == VERIFY_REASON
    assert decision.note_encrypted == JUSTIFICATION
    assert decision.actor_user == reviewer and decision.created_at is not None
    manual = IdentityVerificationAttempt.objects.get(
        registration=case.registration, outcome="MANUAL"
    )
    assert manual.method == IdentityVerificationMethod.MANUAL_DECISION
    assert manual.evidence_document_id is None and manual.performed_by_user == reviewer
    # The earlier Ministry result stays as it was: never relabelled verified.
    lookup = IdentityVerificationAttempt.objects.get(
        registration=case.registration, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    )
    assert lookup.outcome == "NOT_FOUND"
    # The audit records the actor and the basis, never the justification.
    event = AuditEvent.objects.filter(action_code="IDV_VERIFIED_MANUALLY").get()
    assert event.actor_user_id == reviewer.pk
    assert event.after_summary["source"] == VerificationSource.MANUAL_REVIEWER_DECISION
    assert event.after_summary["document"] is False
    assert "delegation" not in json.dumps(event.after_summary)
    _assert_entered_participation_review_only(case.registration)


def test_the_manual_decision_clears_the_identity_for_a_later_approval(first_case, reviewer):
    from apps.people.selectors.clearance import accepted_sources, evaluate_clearance

    case = _verify(first_case, reviewer)
    assert VerificationSource.MANUAL_REVIEWER_DECISION in accepted_sources()
    case.refresh_from_db()
    assert evaluate_clearance(case).cleared


@pytest.mark.parametrize("note", ["", "   ", "Too short"])
def test_a_justification_is_required_without_a_document(first_case, reviewer, note):
    with pytest.raises(review.InvalidIdentityInput) as raised:
        _verify(first_case, reviewer, note=note)
    assert raised.value.code == "justification_required"
    with pytest.raises(review.InvalidIdentityInput):
        _correct(first_case, reviewer, note=note, given_names="Amina Sara")
    case = case_for(first_case.registration)
    assert (case.status, case.version) == (IdentityStatus.MANUAL_REVIEW, first_case.version)
    assert not case.decisions.exists()
    assert not ReviewCase.objects.filter(registration=case.registration).exists()


def test_a_document_reason_never_stands_without_a_document(first_case, reviewer):
    with pytest.raises(review.EvidenceUnavailable):
        _verify(first_case, reviewer, reason_code="DOCUMENT_MATCHES_SUBMISSION")
    with pytest.raises(review.EvidenceUnavailable):
        _correct(first_case, reviewer, reason_code="DATA_CORRECTED_FROM_DOCUMENT")
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_the_staff_exception_still_needs_its_document(first_case, idv_event):
    manager = make_staff(
        "first-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )
    with pytest.raises((review.EvidenceUnavailable, review.InvalidIdentityInput)):
        review.verify_identity_manually(
            first_case.pk,
            actor=manager,
            expected_version=first_case.version,
            evidence_document_id=None,
            reason_code="NO_USABLE_MINISTRY_RECORD",
            note=JUSTIFICATION,
            exception=True,
        )
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW


# ---------------------------------------------------------------------------
# Existing documents
# ---------------------------------------------------------------------------


def test_an_existing_document_stays_selectable_and_keeps_its_source(first_case, reviewer):
    card = upload_national_id_card(first_case.registration)
    with pytest.raises(review.InvalidIdentityInput) as raised:
        # A "without document" reason never describes a reviewed document.
        _verify(first_case, reviewer, evidence_document_id=card.pk)
    assert raised.value.code == "reason_without_document"
    case = _verify(
        first_case,
        reviewer,
        evidence_document_id=card.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
        note="",
    )
    assert case.verification_source == VerificationSource.MANUAL_NATIONAL_ID_CARD
    assert IdentityDecision.objects.get(verification=case).evidence_document_id == card.pk
    card.refresh_from_db()
    assert card.verified_by_user == reviewer
    _assert_entered_participation_review_only(case.registration)


def test_with_a_document_on_file_the_reviewer_may_still_decide_without_it(first_case, reviewer):
    card = upload_national_id_card(first_case.registration)
    case = _verify(first_case, reviewer)
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    card.refresh_from_db()
    assert card.verified_by_user is None  # not claimed as reviewed


# ---------------------------------------------------------------------------
# "Save corrections and verify identity" without a document
# ---------------------------------------------------------------------------


def test_corrections_without_a_document_are_saved_and_verified(first_case, reviewer):
    case = _correct(
        first_case,
        reviewer,
        given_names="Amina Sara",
        family_name="Ben Test",
        date_of_birth=datetime.date(1990, 3, 8),
    )
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    assert case.current_revision.source == IdentityRevisionSource.STAFF_IDENTITY_CORRECTION
    profile = RegistrationProfile.objects.get(registration=case.registration)
    assert (profile.submitted_given_names, profile.submitted_family_name) == (
        "Amina Sara",
        "Ben Test",
    )
    assert profile.date_of_birth == datetime.date(1990, 3, 8)
    decision = IdentityDecision.objects.get(
        verification=case, action=IdentityDecisionAction.CORRECT_AND_VERIFY
    )
    assert decision.evidence_document_id is None and decision.note_encrypted == JUSTIFICATION
    assert sorted(decision.requested_items) == ["date_of_birth", "family_name", "given_names"]
    assert json.loads(decision.changes_encrypted)["given_names"] == {
        "before": "Amina",
        "after": "Amina Sara",
    }
    # An unchanged NIN is not looked up again.
    assert not IdentityVerificationAttempt.objects.filter(
        revision=case.current_revision, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    ).exists()
    assert IdentityVerificationAttempt.objects.get(revision=case.current_revision).method == (
        IdentityVerificationMethod.MANUAL_DECISION
    )
    event = AuditEvent.objects.filter(action_code="IDV_CORRECTED_AND_VERIFIED").get()
    summary = json.dumps(event.after_summary)
    assert event.after_summary["document"] is False
    assert "Amina" not in summary and "delegation" not in summary
    _assert_entered_participation_review_only(case.registration)


def test_a_changed_nin_confirmed_by_the_ministry_still_takes_the_api_path(first_case, reviewer):
    case = _correct(first_case, reviewer, nin=SIM_MATCH_NIN)
    assert case.status == IdentityStatus.API_VERIFIED
    assert case.verification_source == VerificationSource.SIMULATED_API  # truthful provenance
    lookup = IdentityVerificationAttempt.objects.get(
        revision=case.current_revision, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    )
    assert lookup.outcome == "FOUND" and lookup.performed_by_user == reviewer
    assert not IdentityVerificationAttempt.objects.filter(
        revision=case.current_revision, outcome="MANUAL"
    ).exists()
    _assert_entered_participation_review_only(case.registration)


@pytest.mark.parametrize(
    ("nin", "reason"), [(OTHER_UNKNOWN_NIN, "NOT_FOUND"), (SIM_NAME_MISMATCH_NIN, "DATA_MISMATCH")]
)
def test_a_changed_nin_without_a_ministry_match_is_a_manual_decision(
    first_case, reviewer, nin, reason
):
    case = _correct(first_case, reviewer, nin=nin)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    attempts = IdentityVerificationAttempt.objects.filter(revision=case.current_revision)
    assert set(attempts.values_list("method", "reason_code")) == {
        (IdentityVerificationMethod.EXTERNAL_SERVICE, reason),
        (IdentityVerificationMethod.MANUAL_DECISION, ""),
    }
    assert case.current_revision.identifier.value_encrypted == nin


def test_a_presumed_ministry_date_never_replaces_the_reviewer_date(first_case, reviewer):
    case = _correct(
        first_case,
        reviewer,
        nin=SIM_PRESUMED_NIN,
        given_names="Karim",
        family_name="Ouztest",
        date_of_birth=datetime.date(1985, 6, 15),
    )
    profile = RegistrationProfile.objects.get(registration=case.registration)
    assert profile.date_of_birth == datetime.date(1985, 6, 15)
    lookup = IdentityVerificationAttempt.objects.get(
        revision=case.current_revision, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    )
    assert lookup.comparison["birth_date"] == "skipped_presumed"
    assert lookup.official_birth_date == ""


def test_a_nin_of_another_person_is_refused_before_any_write(
    first_case, reviewer, idv_event, legal_versions
):
    other = submit_case(idv_event, legal_versions, nin=OTHER_UNKNOWN_NIN)
    process_all()
    assert case_for(other).status == IdentityStatus.MANUAL_REVIEW
    with pytest.raises(review.DuplicateIdentityConflict):
        _correct(first_case, reviewer, nin=OTHER_UNKNOWN_NIN)
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.current_revision.identifier.value_encrypted == UNKNOWN_NIN


def test_changing_the_nin_still_needs_the_nin_correction_permission(first_case, idv_event):
    group = Group.objects.create(name="IDV-FIRST verify only")
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label="people",
            codename__in=("view_identityverification", "verify_identity_manually"),
        )
    )
    limited = make_staff("first-limited@example.test", "IDV-FIRST verify only", event=idv_event)
    with pytest.raises(IdentityPermissionDenied):
        _correct(first_case, limited, nin=OTHER_UNKNOWN_NIN)
    assert _correct(first_case, limited, given_names="Amina Sara").status == (
        IdentityStatus.MANUALLY_VERIFIED
    )


# ---------------------------------------------------------------------------
# Foreign participants and the NIN exemption route
# ---------------------------------------------------------------------------


def test_a_foreign_participant_is_verified_without_a_document_and_never_looked_up(
    passport_case, reviewer
):
    SpyProvider.calls = []
    case = _correct(
        passport_case,
        reviewer,
        given_names="Lucie Anne",
        family_name="Martin",
        date_of_birth=datetime.date(1991, 4, 2),
        provider=SpyProvider(),
    )
    assert SpyProvider.calls == []
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    assert not IdentityVerificationAttempt.objects.filter(
        registration=case.registration, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    ).exists()
    with pytest.raises(review.InvalidIdentityInput):  # a NIN never applies to this route
        _correct(case_for(case.registration), reviewer, nin=UNKNOWN_NIN)
    _assert_entered_participation_review_only(case.registration)


def test_a_foreign_participant_can_be_verified_directly(passport_case, reviewer):
    case = _verify(passport_case, reviewer)
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    _assert_entered_participation_review_only(case.registration)


def test_the_nin_exemption_route_still_needs_its_declared_document(first_case, reviewer):
    from apps.people.models import IdentityVerification

    # Owner decision IDV-Q3: that route is verified from the document only.
    IdentityVerification.objects.filter(pk=first_case.pk).update(route="NIN_EXEMPTION")
    case = case_for(first_case.registration)
    assert not review.verification_without_document_allowed(case)
    with pytest.raises(review.EvidenceUnavailable):
        _verify(case, reviewer)
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW


# ---------------------------------------------------------------------------
# Permissions, scope, closed registrations, versions, late results
# ---------------------------------------------------------------------------


def test_staff_without_the_permission_or_out_of_scope_cannot_verify(first_case, idv_event):
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME

    viewer_group = Group.objects.create(name="IDV-FIRST view only")
    viewer_group.permissions.add(
        Permission.objects.get(
            content_type__app_label="people", codename="view_identityverification"
        )
    )
    users = [
        make_staff("first-viewer@example.test", "IDV-FIRST view only", event=idv_event),
        make_staff("first-intake@example.test", REGISTRATION_INTAKE_GROUP_NAME, event=idv_event),
        make_staff(
            "first-outsider@example.test",
            ACCREDITATION_MANAGERS_GROUP_NAME,
            event=make_event("IDVFIRSTELSE"),
        ),
    ]
    for user in users:
        with pytest.raises(IdentityPermissionDenied):
            _verify(first_case, user)
        with pytest.raises(IdentityPermissionDenied):
            _correct(first_case, user, given_names="Amina Sara")
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert not ReviewCase.objects.filter(registration=case.registration).exists()


def test_a_withdrawn_registration_is_never_verified(first_case, reviewer):
    from apps.reviews.services import withdraw_registration

    registration = first_case.registration
    registration.refresh_from_db()
    withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )
    case = case_for(registration)
    for command in (_verify, _correct):
        with pytest.raises((review.IdentityStateError, review.StaleIdentityVersion)):
            command(first_case, reviewer)
        with pytest.raises(review.IdentityStateError):
            command(case, reviewer)
    assert case_for(registration).status != IdentityStatus.MANUALLY_VERIFIED
    assert not _open_standard_cases(registration).exists()


def test_a_stale_page_is_a_conflict_and_saves_nothing(first_case, reviewer):
    stale = first_case.version
    review.return_identity_for_correction(
        first_case.pk, actor=reviewer, expected_version=stale, items=["NIN_NUMBER"]
    )
    first_case.version = stale
    for command in (_verify, _correct):
        with pytest.raises((review.StaleIdentityVersion, review.IdentityStateError)):
            command(first_case, reviewer)
    assert case_for(first_case.registration).status == IdentityStatus.RETURNED_FOR_CORRECTION


def test_participation_review_is_entered_exactly_once(first_case, reviewer):
    case = _verify(first_case, reviewer)
    for command in (_verify, _correct):
        with pytest.raises(review.IdentityStateError):
            command(case, reviewer)  # already verified: not repeatable
    _assert_entered_participation_review_only(case.registration)


def test_a_late_worker_result_never_overwrites_the_manual_decision(
    idv_event, legal_versions, reviewer
):
    from apps.people.models import IdentityVerification
    from apps.people.services import identity_verification as worker

    registration = submit_case(idv_event, legal_versions, nin=SIM_NAME_MISMATCH_NIN)
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    case = review.correct_nin_and_recheck(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        new_nin=SIM_NAME_MISMATCH_NIN,
        reason_code="RECHECK_AFTER_TECHNICAL_FAILURE",
    )
    job = case.jobs.filter(status="PENDING").get()

    class ReviewerActsDuringLookup(LocalSimulationNinProvider):
        def lookup(self, nin):
            IdentityVerification.objects.filter(pk=case.pk).update(status="MANUAL_REVIEW")
            _verify(case_for(registration), reviewer)
            return super().lookup(nin)

    assert worker.process_identity_job(job.pk, provider=ReviewerActsDuringLookup()) == "stale"
    final = case_for(registration)
    assert final.status == IdentityStatus.MANUALLY_VERIFIED
    assert final.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    assert _open_standard_cases(registration).count() == 1


# ---------------------------------------------------------------------------
# The review page and its forms
# ---------------------------------------------------------------------------


def _page(client, case):
    return client.get(reverse("identity:case", kwargs={"pk": case.pk})).content.decode()


def test_the_first_review_page_offers_both_verify_actions_and_the_return(first_case, reviewer):
    page = _page(staff_client(reviewer), first_case)
    assert 'data-idv-form="verify"' in page
    assert 'data-idv-form="correct-verify"' in page
    assert 'data-idv-form="return"' in page  # still there when input is needed
    assert "data-idv-without-document" in page
    assert "data-idv-no-evidence" not in page
    # "No document" and its reason are preselected; nothing is submitted by default.
    assert re.search(r'<input[^>]*value="none"[^>]*checked', page)
    assert re.search(rf'<option value="{VERIFY_REASON}"[^>]*selected', page)
    assert re.search(rf'<option value="{CORRECT_REASON}"[^>]*selected', page)
    assert 'name="given_names"' in page and 'name="date_of_birth' in page
    assert 'name="nin"' in page


def test_with_a_document_on_file_the_page_lists_it_beside_the_manual_choice(first_case, reviewer):
    card = upload_national_id_card(first_case.registration)
    page = _page(staff_client(reviewer), first_case)
    assert 'value="none"' in page
    # The single document is preselected, as before; the manual choice is not.
    assert re.search(rf'<input[^>]*value="{card.pk}"[^>]*checked', page)
    assert not re.search(r'<input[^>]*value="none"[^>]*checked', page)
    assert not re.search(rf'<option value="{VERIFY_REASON}"[^>]*selected', page)
    assert "data-idv-without-document" not in page


def test_posting_verify_without_a_document_needs_the_justification(first_case, reviewer):
    client = staff_client(reviewer)
    url = reverse("identity:verify", kwargs={"pk": first_case.pk})
    data = {
        "expected_version": first_case.version,
        "evidence_document": "none",
        "reason_code": VERIFY_REASON,
        "advance": "stay",
    }
    response = client.post(url, {**data, "note": ""})
    assert response.status_code == 400
    assert "write the justification" in response.content.decode()
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW
    response = client.post(url, {**data, "note": JUSTIFICATION})
    assert response.status_code == 302
    case = case_for(first_case.registration)
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    _assert_entered_participation_review_only(case.registration)


def test_posting_correct_and_verify_without_a_document_works(first_case, reviewer):
    response = staff_client(reviewer).post(
        reverse("identity:correct-and-verify", kwargs={"pk": first_case.pk}),
        {
            "expected_version": first_case.version,
            "given_names": "Amina",
            "family_name": "Bentest",
            "date_of_birth": "1990-03-07",
            "nin": OTHER_UNKNOWN_NIN,
            "evidence_document": "none",
            "reason_code": CORRECT_REASON,
            "note": JUSTIFICATION,
            "advance": "stay",
        },
    )
    assert response.status_code == 302
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_REVIEWER_DECISION
    assert case.current_revision.identifier.value_encrypted == OTHER_UNKNOWN_NIN


def test_a_withdrawal_after_the_page_opened_refuses_the_post(first_case, reviewer):
    from apps.reviews.services import withdraw_registration

    version = first_case.version
    registration = first_case.registration
    registration.refresh_from_db()
    withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )
    response = staff_client(reviewer).post(
        reverse("identity:verify", kwargs={"pk": first_case.pk}),
        {
            "expected_version": version,
            "evidence_document": "none",
            "reason_code": VERIFY_REASON,
            "note": JUSTIFICATION,
            "advance": "stay",
        },
    )
    assert response.status_code == 409
    assert "identity processing has stopped" in response.content.decode()
    assert case_for(registration).status != IdentityStatus.MANUALLY_VERIFIED


# ---------------------------------------------------------------------------
# The full NIN is never sent to a reviewer who may not see it
# ---------------------------------------------------------------------------


def _staff_with(idv_event, email, *codenames):
    group = Group.objects.create(name=f"IDV-FIRST {email}")
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label="people",
            codename__in=("view_identityverification", *codenames),
        )
    )
    return make_staff(email, group.name, event=idv_event)


@pytest.fixture
def masked_reviewers(idv_event):
    """Reviewers who may verify but not see the full NIN: one without and one
    WITH the NIN correction permission (changing needs both)."""
    return [
        _staff_with(idv_event, "first-masked@example.test", "verify_identity_manually"),
        _staff_with(
            idv_event,
            "first-masked-corrector@example.test",
            "verify_identity_manually",
            "correct_identity_nin",
        ),
    ]


def _correct_post(case, **values):
    return {
        "expected_version": case.version,
        "given_names": "Amina Sara",
        "family_name": "Bentest",
        "date_of_birth": "1990-03-07",
        "evidence_document": "none",
        "reason_code": CORRECT_REASON,
        "note": JUSTIFICATION,
        "advance": "stay",
        **values,
    }


def _correct_url(case):
    return reverse("identity:correct-and-verify", kwargs={"pk": case.pk})


def test_the_page_and_its_error_responses_never_contain_a_hidden_nin(first_case, masked_reviewers):
    identifier = first_case.current_revision.identifier
    for user in masked_reviewers:
        client = staff_client(user)
        page = _page(client, first_case)
        assert UNKNOWN_NIN not in page
        assert identifier.masked_value in page
        assert 'name="nin"' not in page and "data-idv-nin-kept" in page
        assert 'data-idv-form="correct-verify"' in page  # the action itself stays
        # A validation error re-renders the page: still no NIN anywhere.
        response = client.post(
            _correct_url(first_case), _correct_post(first_case, given_names="أمينة")
        )
        assert response.status_code == 400
        assert UNKNOWN_NIN not in response.content.decode()
        response = client.post(_correct_url(first_case), _correct_post(first_case, note=""))
        assert response.status_code == 400
        assert UNKNOWN_NIN not in response.content.decode()
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_a_masked_reviewer_corrects_with_the_nin_kept_on_the_server(first_case, masked_reviewers):
    identifier_pk = first_case.current_revision.identifier_id
    response = staff_client(masked_reviewers[0]).post(
        _correct_url(first_case), _correct_post(first_case)
    )
    assert response.status_code == 302
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.current_revision.identifier_id == identifier_pk  # the same, stored NIN
    assert case.current_revision.identifier.value_encrypted == UNKNOWN_NIN
    decision = IdentityDecision.objects.get(
        verification=case, action=IdentityDecisionAction.CORRECT_AND_VERIFY
    )
    assert decision.requested_items == ["given_names"]
    _assert_entered_participation_review_only(case.registration)


def test_a_page_rendered_before_the_fix_may_still_post_the_unchanged_nin(
    first_case, masked_reviewers
):
    response = staff_client(masked_reviewers[0]).post(
        _correct_url(first_case), _correct_post(first_case, nin=UNKNOWN_NIN)
    )
    assert response.status_code == 302
    assert case_for(first_case.registration).status == IdentityStatus.MANUALLY_VERIFIED


def test_a_tampered_nin_from_a_masked_reviewer_is_refused(first_case, masked_reviewers):
    for user in masked_reviewers:
        response = staff_client(user).post(
            _correct_url(first_case), _correct_post(first_case, nin=OTHER_UNKNOWN_NIN)
        )
        assert response.status_code == 403
        assert OTHER_UNKNOWN_NIN not in response.content.decode()
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.current_revision.identifier.value_encrypted == UNKNOWN_NIN
    profile = RegistrationProfile.objects.get(registration=case.registration)
    assert profile.submitted_given_names == "Amina"  # nothing partial
    assert not IdentityVerificationAttempt.objects.filter(
        registration=case.registration, performed_by_user__in=masked_reviewers
    ).exists()  # no Ministry lookup for a refused change
    assert AuditEvent.objects.filter(action_code="IDV_ACTION_REFUSED").count() == 2
    # The service refuses it as well, whatever the caller.
    with pytest.raises(IdentityPermissionDenied):
        _correct(first_case, masked_reviewers[1], nin=OTHER_UNKNOWN_NIN)


def test_an_authorized_reviewer_sees_and_edits_the_nin(first_case, reviewer):
    client = staff_client(reviewer)
    page = _page(client, first_case)
    assert re.search(rf'<input[^>]*name="nin"[^>]*value="{UNKNOWN_NIN}"', page) or re.search(
        rf'<input[^>]*value="{UNKNOWN_NIN}"[^>]*name="nin"', page
    )
    assert "data-idv-nin-kept" not in page
    response = client.post(
        _correct_url(first_case), _correct_post(first_case, nin=OTHER_UNKNOWN_NIN)
    )
    assert response.status_code == 302
    case = case_for(first_case.registration)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.current_revision.identifier.value_encrypted == OTHER_UNKNOWN_NIN
    # The Ministry lookup of the corrected NIN still ran first.
    assert IdentityVerificationAttempt.objects.filter(
        revision=case.current_revision, method=IdentityVerificationMethod.EXTERNAL_SERVICE
    ).exists()


def test_an_authorized_reviewer_must_still_send_the_nin_field(first_case, reviewer):
    data = _correct_post(first_case)  # no "nin" key: the field is required for an editor
    response = staff_client(reviewer).post(_correct_url(first_case), data)
    assert response.status_code == 400
    assert case_for(first_case.registration).status == IdentityStatus.MANUAL_REVIEW
