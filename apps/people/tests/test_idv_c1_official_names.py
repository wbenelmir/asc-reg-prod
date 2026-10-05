"""IDV-C1, R-IDV-02: a case- or spacing-only name difference is corrected from the official record.

When the ministry (or, locally, the development simulation) confirms the
identity and a name differs from the official Latin form only by letter case
or spacing (`normalized_match`, A13-04), the official form becomes the
registration's current name:

* the profile names are set to the official form, inside the same
  transaction that applies the result;
* the change is RECORDED as a new identity revision
  (`OFFICIAL_NAME_NORMALIZATION`) whose fingerprint covers the new names, and
  the verification is bound to it, so freshness checks keep passing;
* the submitted originals stay in the immutable submission snapshot, and an
  `OFFICIAL_NAME_APPLIED` decision records which fields changed and from which
  source;
* downstream reads (snapshots, staff pages) use the current names.

A substantive difference changes nothing (manual review), an exact match needs
no correction, and the birth date is never altered (the presumed-date rule
keeps the entered date). Synthetic identities only.
"""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse

from apps.people.identity_contract import Comparison
from apps.people.models import (
    IdentityDecisionAction,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerificationAttempt,
    VerificationSource,
)
from apps.people.services import identity_verification as idv
from apps.people.tests.conftest import (
    SIM_MATCH_NIN,
    SIM_NAME_MISMATCH_NIN,
    SIM_PRESUMED_NIN,
    case_for,
    make_staff,
    process_all,
    staff_client,
    submit_case,
)
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db


class OfficialDouble:
    """A test double that reports itself official and answers with the
    synthetic simulation bodies. It is not the ministry service."""

    PROVIDER_CODE = "OFFICIAL_TEST_DOUBLE"
    IS_OFFICIAL = True

    def configuration_problems(self):
        return []

    def lookup(self, nin):
        from apps.people.nin_provider import LocalSimulationNinProvider

        return LocalSimulationNinProvider().lookup(nin)


def _names(registration) -> tuple[str, str, str]:
    profile = registration.profile
    profile.refresh_from_db()
    return (
        profile.submitted_given_names,
        profile.submitted_family_name,
        profile.submitted_full_name,
    )


def _initial_snapshot_names(registration) -> tuple[str, str]:
    initial = registration.submissions.get(submission_kind="INITIAL")
    return (
        initial.snapshot_json["identity"]["given_names"],
        initial.snapshot_json["identity"]["family_name"],
    )


def test_an_official_normalized_match_applies_the_official_name_form(
    idv_event, legal_versions
) -> None:
    from apps.registrations.services import build_registration_snapshot

    registration = submit_case(idv_event, legal_versions)  # "Amina Bentest" vs AMINA BENTEST
    checked = case_for(registration).current_revision

    assert process_all(provider=OfficialDouble()) == {IdentityStatus.API_VERIFIED: 1}

    case = case_for(registration)
    assert case.verification_source == VerificationSource.MINISTRY_API
    assert _names(registration) == ("AMINA", "BENTEST", "AMINA BENTEST")
    assert _initial_snapshot_names(registration) == ("Amina", "Bentest")  # originals kept
    revision = case.current_revision
    assert revision.source == IdentityRevisionSource.OFFICIAL_NAME_NORMALIZATION
    assert sorted(revision.changed_fields) == ["family_name", "given_names"]
    assert revision.number == checked.number + 1
    assert revision.identifier_id == checked.identifier_id
    assert idv._fingerprint_still_current(revision, registration.profile)  # revision-bound
    decision = case.decisions.get(action=IdentityDecisionAction.OFFICIAL_NAME_APPLIED)
    assert decision.reason_code == VerificationSource.MINISTRY_API
    assert sorted(decision.requested_items) == ["family_name", "given_names"]
    assert decision.revision_id == revision.pk
    # The attempt that produced it stays on the checked revision, with the
    # retained official facts.
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.revision_id == checked.pk
    assert attempt.comparison["given_names"] == Comparison.NORMALIZED_MATCH
    from apps.people.selectors.identity import latest_provider_attempt

    assert latest_provider_attempt(case) == attempt
    # Downstream reads use the current names.
    assert build_registration_snapshot(registration)["identity"]["given_names"] == "AMINA"


def test_the_presumed_date_rule_keeps_the_entered_date_while_names_are_set(
    idv_event, legal_versions
) -> None:
    entered = datetime.date(1985, 6, 15)  # the official date 01/01/1985 is presumed
    registration = submit_case(
        idv_event,
        legal_versions,
        nin=SIM_PRESUMED_NIN,
        given="Karim",
        family="Ouztest",
        birth=entered,
    )
    process_all(provider=OfficialDouble())
    assert case_for(registration).status == IdentityStatus.API_VERIFIED
    assert _names(registration)[:2] == ("KARIM", "OUZTEST")
    registration.profile.refresh_from_db()
    assert registration.profile.date_of_birth == entered


def test_a_substantive_name_difference_is_never_corrected(idv_event, legal_versions) -> None:
    registration = submit_case(
        idv_event, legal_versions, nin=SIM_NAME_MISMATCH_NIN, given="Samir", family="Bentest"
    )
    process_all(provider=OfficialDouble())
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == IdentityReasonCode.DATA_MISMATCH
    assert _names(registration) == ("Samir", "Bentest", "Samir Bentest")
    assert case.current_revision.source == IdentityRevisionSource.SUBMISSION
    assert not case.decisions.filter(action=IdentityDecisionAction.OFFICIAL_NAME_APPLIED).exists()


def test_an_exact_match_needs_no_correction(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions, given="AMINA", family="BENTEST")
    process_all(provider=OfficialDouble())
    case = case_for(registration)
    assert case.status == IdentityStatus.API_VERIFIED
    assert case.current_revision.source == IdentityRevisionSource.SUBMISSION
    assert case.revisions.count() == 1
    assert not case.decisions.exists()


def test_the_simulation_applies_the_same_rule_and_is_labelled_as_such(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    process_all()
    case = case_for(registration)
    assert case.verification_source == VerificationSource.SIMULATED_API
    decision = case.decisions.get(action=IdentityDecisionAction.OFFICIAL_NAME_APPLIED)
    assert decision.reason_code == VerificationSource.SIMULATED_API  # never shown as official
    assert _names(registration)[:2] == ("AMINA", "BENTEST")


def test_the_review_screen_shows_the_names_as_entered(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    process_all(provider=OfficialDouble())
    reviewer = make_staff(
        "idv-c1-names@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )
    case = case_for(registration)
    content = (
        staff_client(reviewer)
        .get(reverse("identity:case", kwargs={"pk": case.pk}) + "?status=ALL")
        .content.decode()
    )
    assert "data-idv-official-names" in content
    assert "AMINA" in content and "Bentest" in content  # current form and as entered
