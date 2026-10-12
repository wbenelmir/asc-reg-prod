"""UPD-02: "Save corrections and verify identity" in the identity review.

A reviewer corrects the first name, last name, date of birth and (Algerian
NIN route) the NIN, and verifies in one action, without returning the case
to the participant. Covered: truthful provenance (API path only for a
verifying recheck of exactly the corrected values; manual otherwise, with
the lookup attempt kept), no Ministry call for foreign participants, the
presume rule, validation without partial writes, duplicates, permissions,
stale versions and stale worker results, the audit (field names only), and
participation review entered exactly once. Synthetic identities only.
"""

from __future__ import annotations

import datetime
import json

import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.people.models import (
    IdentityDecision,
    IdentityDecisionAction,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerificationAttempt,
    VerificationSource,
)
from apps.people.nin_provider import LocalSimulationNinProvider, LookupOutcome
from apps.people.services import identity_review as review
from apps.registrations.models import RegistrationProfile, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME
from apps.reviews.models import ReviewCase

from .conftest import (
    SIM_MATCH_NIN,
    SIM_NAME_MISMATCH_NIN,
    SIM_PRESUMED_NIN,
    UNKNOWN_NIN,
    case_for,
    make_staff,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)

pytestmark = pytest.mark.django_db

OTHER_UNKNOWN_NIN = "123456789012345679"


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "upd02-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def nin_case(idv_event, legal_versions):
    """An Algerian case in manual review: the NIN was not found."""
    registration = submit_case(
        idv_event, legal_versions, nin=UNKNOWN_NIN, given="Amina", family="Bentest"
    )
    process_all()
    upload_national_id_card(registration)
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


@pytest.fixture
def passport_case(idv_event, legal_versions):
    registration = submit_case(
        idv_event, legal_versions, nationality="FR", given="Lucie", family="Martin"
    )
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


def _evidence(case, kind="NATIONAL_ID_CARD"):
    return case.registration.documents.get(document_type=kind, status="ACTIVE")


def _correct(case, actor, *, provider=None, **values):
    data = {
        "given_names": "Amina",
        "family_name": "Bentest",
        "date_of_birth": datetime.date(1990, 3, 7),
        "nin": UNKNOWN_NIN if case.route == "NIN" else "",
        "reason_code": "DATA_CORRECTED_FROM_DOCUMENT",
        **values,
    }
    kind = "NATIONAL_ID_CARD" if case.route == "NIN" else "PASSPORT_IDENTITY_PAGE"
    return review.correct_and_verify_identity(
        case.pk,
        actor=actor,
        expected_version=case.version,
        evidence_document_id=_evidence(case, kind).pk,
        provider=provider,
        **data,
    )


def _open_standard_cases(registration):
    return ReviewCase.objects.filter(
        registration=registration, case_type="STANDARD", status__in=("QUEUED", "ASSIGNED")
    )


class SpyProvider(LocalSimulationNinProvider):
    calls: list = []

    def lookup(self, nin):
        SpyProvider.calls.append(nin)
        return super().lookup(nin)


def test_names_and_birth_date_corrected_on_the_passport_route_without_any_lookup(
    passport_case, reviewer
):
    SpyProvider.calls = []
    case = _correct(
        passport_case,
        reviewer,
        provider=SpyProvider(),
        given_names="Lucie Anne",
        family_name="Martin",
        date_of_birth=datetime.date(1991, 4, 2),
    )
    assert SpyProvider.calls == []  # never a Ministry call for a foreign participant
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_PASSPORT
    profile = RegistrationProfile.objects.get(registration=case.registration)
    assert (profile.submitted_given_names, profile.date_of_birth) == (
        "Lucie Anne",
        datetime.date(1991, 4, 2),
    )
    assert case.current_revision.source == IdentityRevisionSource.STAFF_IDENTITY_CORRECTION
    decision = IdentityDecision.objects.get(
        verification=case, action=IdentityDecisionAction.CORRECT_AND_VERIFY
    )
    assert sorted(decision.requested_items) == ["date_of_birth", "given_names"]
    changes = json.loads(decision.changes_encrypted)
    assert changes["given_names"] == {"before": "Lucie", "after": "Lucie Anne"}
    # The audit names the fields and the provenance, never a value.
    event = AuditEvent.objects.filter(action_code="IDV_CORRECTED_AND_VERIFIED").get()
    summary = json.dumps(event.after_summary)
    assert "given_names" in summary and "Lucie" not in summary and "1991" not in summary
    case.registration.refresh_from_db()
    assert case.registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert _open_standard_cases(case.registration).count() == 1
    assert not case.registration.decisions.exists()  # never an attendance approval


def test_a_changed_nin_that_the_ministry_confirms_takes_the_api_path(nin_case, reviewer):
    case = _correct(nin_case, reviewer, nin=SIM_MATCH_NIN)
    assert case.status == IdentityStatus.API_VERIFIED
    # The test provider is the development simulation, recorded as such.
    assert case.verification_source == VerificationSource.SIMULATED_API
    lookup = IdentityVerificationAttempt.objects.filter(
        registration=case.registration, revision=case.current_revision, method="EXTERNAL_SERVICE"
    ).get()
    assert lookup.outcome == "FOUND" and lookup.reason_code == ""
    assert lookup.performed_by_user == reviewer
    assert not IdentityVerificationAttempt.objects.filter(
        revision=case.current_revision, method="MANUAL_DOCUMENT"
    ).exists()
    assert _open_standard_cases(case.registration).count() == 1


def test_a_changed_nin_not_found_still_allows_the_manual_resolution(nin_case, reviewer):
    case = _correct(nin_case, reviewer, nin=OTHER_UNKNOWN_NIN)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_NATIONAL_ID_CARD
    attempts = IdentityVerificationAttempt.objects.filter(revision=case.current_revision)
    assert set(attempts.values_list("method", "outcome")) == {
        ("EXTERNAL_SERVICE", "NOT_FOUND"),
        ("MANUAL_DOCUMENT", "MANUAL"),
    }
    case.registration.refresh_from_db()
    assert case.registration.public_status == RegistrationPublicStatus.UNDER_REVIEW


def test_an_unavailable_service_never_forces_a_participant_correction(nin_case, reviewer):
    class Down(LocalSimulationNinProvider):
        def lookup(self, nin):
            return LookupOutcome("UNAVAILABLE", detail="synthetic_outage", retryable=True)

    case = _correct(nin_case, reviewer, nin=OTHER_UNKNOWN_NIN, provider=Down())
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert IdentityVerificationAttempt.objects.filter(
        revision=case.current_revision, outcome="UNAVAILABLE"
    ).exists()


def test_a_conflicting_official_record_is_never_called_api_verified(nin_case, reviewer):
    # The service answers with other names for this NIN: a manual resolution.
    case = _correct(nin_case, reviewer, nin=SIM_NAME_MISMATCH_NIN)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.verification_source == VerificationSource.MANUAL_NATIONAL_ID_CARD
    lookup = IdentityVerificationAttempt.objects.get(
        revision=case.current_revision, method="EXTERNAL_SERVICE"
    )
    assert lookup.reason_code == "DATA_MISMATCH"


def test_a_presumed_ministry_date_never_replaces_the_reviewer_date(nin_case, reviewer):
    case = _correct(
        nin_case,
        reviewer,
        nin=SIM_PRESUMED_NIN,
        given_names="Karim",
        family_name="Ouztest",
        date_of_birth=datetime.date(1985, 6, 15),
    )
    profile = RegistrationProfile.objects.get(registration=case.registration)
    assert profile.date_of_birth == datetime.date(1985, 6, 15)
    lookup = IdentityVerificationAttempt.objects.get(
        revision=case.current_revision, method="EXTERNAL_SERVICE"
    )
    assert lookup.comparison["birth_date"] == "skipped_presumed"
    assert lookup.official_birth_date == ""  # no official date is invented or kept


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"nin": "12345"}, "nin_format"),
        ({"given_names": "أمينة"}, "names"),
        ({"date_of_birth": datetime.date.today()}, "birth_date"),
    ],
)
def test_invalid_values_change_nothing(nin_case, reviewer, values, code):
    before = (nin_case.version, RegistrationProfile.objects.get(registration=nin_case.registration))
    with pytest.raises(review.InvalidIdentityInput) as raised:
        _correct(nin_case, reviewer, **values)
    assert raised.value.code == code
    case = case_for(nin_case.registration)
    assert (case.version, case.status) == (before[0], IdentityStatus.MANUAL_REVIEW)
    profile = RegistrationProfile.objects.get(registration=nin_case.registration)
    assert profile.submitted_given_names == before[1].submitted_given_names
    assert not IdentityDecision.objects.filter(verification=case, action="CORRECT_AND_VERIFY")


def test_a_nin_of_another_person_is_refused_before_any_write(
    nin_case, reviewer, idv_event, legal_versions
):
    other = submit_case(idv_event, legal_versions, nin=OTHER_UNKNOWN_NIN)
    process_all()
    assert case_for(other).status == IdentityStatus.MANUAL_REVIEW
    with pytest.raises(review.DuplicateIdentityConflict):
        _correct(nin_case, reviewer, nin=OTHER_UNKNOWN_NIN)
    assert case_for(nin_case.registration).status == IdentityStatus.MANUAL_REVIEW


def test_changing_the_nin_needs_the_nin_correction_permission(nin_case, idv_event):
    group = Group.objects.create(name="UPD02 verify only")
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label="people",
            codename__in=("view_identityverification", "verify_identity_manually"),
        )
    )
    limited = make_staff("upd02-limited@example.test", "UPD02 verify only", event=idv_event)
    from apps.people.policies import IdentityPermissionDenied

    with pytest.raises(IdentityPermissionDenied):
        _correct(nin_case, limited, nin=OTHER_UNKNOWN_NIN)
    case = _correct(nin_case, limited, given_names="Amina Sara")  # the NIN unchanged
    assert case.status == IdentityStatus.MANUALLY_VERIFIED


def test_a_stale_page_is_a_conflict_and_saves_nothing(nin_case, reviewer):
    stale_version = nin_case.version
    review.return_identity_for_correction(
        nin_case.pk, actor=reviewer, expected_version=nin_case.version, items=["NIN_NUMBER"]
    )
    nin_case.version = stale_version
    with pytest.raises((review.StaleIdentityVersion, review.IdentityStateError)):
        _correct(nin_case, reviewer, given_names="Amina Sara")
    profile = RegistrationProfile.objects.get(registration=nin_case.registration)
    assert profile.submitted_given_names == "Amina"


def test_verification_enters_participation_review_exactly_once(nin_case, reviewer):
    case = _correct(nin_case, reviewer)
    with pytest.raises(review.IdentityStateError):
        _correct(case, reviewer)  # already verified: not repeatable
    assert _open_standard_cases(case.registration).count() == 1
    assert ReviewCase.objects.filter(registration=case.registration).count() == 1


def test_a_late_worker_result_never_overwrites_the_reviewer_decision(
    idv_event, legal_versions, reviewer
):
    """A check of the OLD identity was running when the reviewer corrected
    and verified: its result is recorded as not applied."""
    from apps.people.services import identity_verification as worker

    registration = submit_case(idv_event, legal_versions, nin=SIM_NAME_MISMATCH_NIN)
    process_all()
    upload_national_id_card(registration)
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    # A recheck is queued (case PENDING), then the worker claims it ...
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
            from apps.people.models import IdentityVerification

            # ... the case goes to manual review again (a data mismatch would)
            # and the reviewer resolves it while the old answer is in flight.
            IdentityVerification.objects.filter(pk=case.pk).update(status="MANUAL_REVIEW")
            fresh = case_for(registration)
            _correct(
                fresh,
                reviewer,
                given_names="Samir",
                family_name="Bentesti",
                date_of_birth=datetime.date(1992, 6, 15),
                nin=OTHER_UNKNOWN_NIN,
            )
            return super().lookup(nin)

    assert worker.process_identity_job(job.pk, provider=ReviewerActsDuringLookup()) == "stale"
    final = case_for(registration)
    assert final.status == IdentityStatus.MANUALLY_VERIFIED
    assert final.current_revision.source == IdentityRevisionSource.STAFF_IDENTITY_CORRECTION


def test_the_review_page_offers_the_action_and_posting_it_works(nin_case, reviewer):
    client = staff_client(reviewer)
    page = client.get(reverse("identity:case", kwargs={"pk": nin_case.pk})).content.decode()
    assert 'data-idv-form="correct-verify"' in page
    assert "Save corrections and verify identity" in page
    response = client.post(
        reverse("identity:correct-and-verify", kwargs={"pk": nin_case.pk}),
        {
            "expected_version": nin_case.version,
            "given_names": "Amina",
            "family_name": "Bentest",
            "date_of_birth": "1990-03-07",
            "nin": OTHER_UNKNOWN_NIN,
            "evidence_document": str(_evidence(nin_case).pk),
            "reason_code": "TYPING_ERROR_CORRECTED",
            "advance": "stay",
        },
    )
    assert response.status_code == 302
    case = case_for(nin_case.registration)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED
    assert case.current_revision.identifier.value_encrypted == OTHER_UNKNOWN_NIN


def test_the_manager_role_can_also_correct_and_verify(nin_case, idv_event):
    manager = make_staff(
        "upd02-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )
    assert _correct(nin_case, manager).status == IdentityStatus.MANUALLY_VERIFIED
