"""Owner decision IDV-Q1 (2026-10-02): APPROVED requires a current, valid, verified identity.

Enforced in the decision service (`record_approved_decision`), under the
Registration lock with the identity case locked too, not only in the UI:

* pending, manual review, returned and rejected identities are refused;
* a verified case whose identifier is no longer current (replaced, or only
  declared) is refused, and so is an expired document;
* a registration without an identity case (submitted before identity
  verification existed) is refused explicitly (`NO_IDENTITY_CASE`);
* a development-simulation result is accepted only where the simulation is
  allowed (local and test), never elsewhere;
* a refusal writes nothing except a DENIED audit event with the coded reason.

Identity and participation keep separate histories: the approval records
only the identity status and source in its audit event. Imports of the new
API are inside the tests so that the same file can run against the tree
before this change (before/after evidence). Synthetic data only.
"""

from __future__ import annotations

import datetime

import pytest

from apps.audit.models import AuditEvent
from apps.people.models import IdentityStatus, VerificationSource
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    SIM_MATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(idv_event):
    return make_staff("q1-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


@pytest.fixture
def reviewer(idv_event):
    return make_staff("q1-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


def _approve(registration, manager):
    from apps.reviews.services import record_approved_decision

    registration.refresh_from_db()
    return record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=manager,
        attendance_category="FOLLOWING_TWO_DAYS",
    )


def _assert_refused(registration, manager, code):
    """Refused before any write, with a coded DENIED audit event."""
    from apps.reviews.services import ApprovalRequiresVerifiedIdentityError

    registration.refresh_from_db()
    version, status = registration.version, registration.public_status
    decisions = registration.decisions.count()
    with pytest.raises(ApprovalRequiresVerifiedIdentityError) as refusal:
        _approve(registration, manager)
    assert refusal.value.code == code
    registration.refresh_from_db()
    assert registration.public_status == status != RegistrationPublicStatus.APPROVED
    assert registration.version == version
    assert registration.decisions.count() == decisions
    assert AuditEvent.objects.filter(
        action_code="REV_APPROVAL_BLOCKED_IDENTITY",
        target_uuid=registration.pk,
        result="DENIED",
        reason_code=code,
    ).exists()


def _submitted(idv_event, legal_versions, manager, **kwargs):
    registration = submit_case(idv_event, legal_versions, **kwargs)
    assign_approval_prerequisites(registration, manager)
    return registration


def _verify_manually(registration, reviewer):
    case = case_for(registration)
    card = upload_national_id_card(registration)
    return review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=card.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )


# ---------------------------------------------------------------------------
# Refusals through the real identity lifecycle
# ---------------------------------------------------------------------------


def test_a_pending_identity_blocks_approval(idv_event, legal_versions, manager) -> None:
    registration = _submitted(idv_event, legal_versions, manager)
    assert case_for(registration).status == IdentityStatus.PENDING
    _assert_refused(registration, manager, "IDENTITY_NOT_VERIFIED")


def test_an_identity_in_manual_review_blocks_approval(idv_event, legal_versions, manager) -> None:
    registration = _submitted(idv_event, legal_versions, manager, nin=UNKNOWN_NIN)
    process_all()
    assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW
    _assert_refused(registration, manager, "IDENTITY_NOT_VERIFIED")


def test_a_foreign_participant_needs_the_passport_review_first(
    idv_event, legal_versions, manager, reviewer
) -> None:
    from apps.documents.services import active_passport_identity_page

    registration = _submitted(idv_event, legal_versions, manager, nationality="FR")
    _assert_refused(registration, manager, "IDENTITY_NOT_VERIFIED")
    case = case_for(registration)
    review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=active_passport_identity_page(registration).pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert _approve(registration, manager).outcome == "APPROVED"


def test_a_legacy_registration_without_identity_case_is_refused_explicitly(
    idv_event, manager
) -> None:
    """A registration submitted before identity verification existed has no
    identity case. It is never assumed verified (backfill is IDV-Q4)."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.tests.conftest import make_registration

    registration = make_registration(
        event=idv_event, person=resolve_or_create_participant_for_email("q1-legacy@example.test")
    )
    assign_approval_prerequisites(registration, manager)
    _assert_refused(registration, manager, "NO_IDENTITY_CASE")


# ---------------------------------------------------------------------------
# Accepted verifications, and the simulation rule
# ---------------------------------------------------------------------------


def test_a_manual_verification_allows_approval_and_records_its_source(
    idv_event, legal_versions, manager, reviewer
) -> None:
    registration = _submitted(idv_event, legal_versions, manager, nin=UNKNOWN_NIN)
    process_all()
    _verify_manually(registration, reviewer)
    decision = _approve(registration, manager)
    assert decision.outcome == "APPROVED"
    event = AuditEvent.objects.get(
        action_code="REV_DECISION_RECORDED", target_uuid=registration.pk, result="SUCCESS"
    )
    assert event.after_summary["identity_status"] == IdentityStatus.MANUALLY_VERIFIED
    assert event.after_summary["identity_source"] == VerificationSource.MANUAL_NATIONAL_ID_CARD


def test_an_official_verification_allows_approval(idv_event, legal_versions, manager) -> None:
    from apps.people.tests.test_idv_c1_official_names import OfficialDouble

    registration = _submitted(idv_event, legal_versions, manager)
    process_all(provider=OfficialDouble())
    assert case_for(registration).verification_source == VerificationSource.MINISTRY_API
    assert _approve(registration, manager).outcome == "APPROVED"


def test_a_simulated_verification_is_never_accepted_outside_local_and_test(
    idv_event, legal_versions, manager, settings
) -> None:
    registration = _submitted(idv_event, legal_versions, manager, nin=SIM_MATCH_NIN)
    process_all()
    assert case_for(registration).verification_source == VerificationSource.SIMULATED_API
    settings.IDENTITY_ALLOW_SIMULATED_PROVIDER = False
    _assert_refused(registration, manager, "SIMULATION_NOT_ACCEPTED")
    settings.IDENTITY_ALLOW_SIMULATED_PROVIDER = True
    _approve(registration, manager)
    event = AuditEvent.objects.get(
        action_code="REV_DECISION_RECORDED", target_uuid=registration.pk, result="SUCCESS"
    )
    # Labelled as the simulation, never as official evidence.
    assert event.after_summary["identity_source"] == VerificationSource.SIMULATED_API


# ---------------------------------------------------------------------------
# Every non-cleared state, on directly built cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "source", "identifier_status", "expires_in_days", "code"),
    [
        ("PENDING", "", "DECLARED", 900, "IDENTITY_NOT_VERIFIED"),
        ("MANUAL_REVIEW", "", "DECLARED", 900, "IDENTITY_NOT_VERIFIED"),
        ("RETURNED_FOR_CORRECTION", "", "DECLARED", 900, "IDENTITY_NOT_VERIFIED"),
        ("REJECTED", "", "DECLARED", 900, "IDENTITY_REJECTED"),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "REPLACED", 900, "IDENTIFIER_NOT_CURRENT"),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "DECLARED", 900, "IDENTIFIER_NOT_CURRENT"),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "REVOKED", 900, "IDENTIFIER_NOT_CURRENT"),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "VERIFIED", 0, "DOCUMENT_EXPIRED"),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "VERIFIED", -5, "DOCUMENT_EXPIRED"),
    ],
)
def test_every_non_cleared_identity_state_is_refused(
    idv_event, manager, status, source, identifier_status, expires_in_days, code
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.tests.conftest import make_registration

    person = resolve_or_create_participant_for_email(f"q1-{status}-{code}@example.test".lower())
    registration = make_registration(event=idv_event, person=person)
    assign_approval_prerequisites(registration, manager)
    make_verified_identity_case(
        registration,
        status=status,
        source=source,
        identifier_status=identifier_status,
        expires_at=datetime.date.today() + datetime.timedelta(days=expires_in_days),
    )
    _assert_refused(registration, manager, code)


def test_a_cleared_directly_built_case_is_approved(idv_event, manager) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.tests.conftest import make_registration

    registration = make_registration(
        event=idv_event, person=resolve_or_create_participant_for_email("q1-ok@example.test")
    )
    assign_approval_prerequisites(registration, manager)
    make_verified_identity_case(registration)
    assert _approve(registration, manager).outcome == "APPROVED"


# ---------------------------------------------------------------------------
# An identity changed elsewhere, after the decider loaded the case
# ---------------------------------------------------------------------------


def test_an_identity_invalidated_by_a_correction_elsewhere_blocks_approval(
    idv_event, legal_versions, manager, reviewer
) -> None:
    """The same person registered for two events; the first identity is
    verified, then the NIN is corrected in the second registration. The first
    case is rebound to manual review (R-IDV-01) and can no longer be approved
    from the page the decider still has open."""
    registration = _submitted(idv_event, legal_versions, manager, nin=UNKNOWN_NIN)
    process_all()
    _verify_manually(registration, reviewer)
    registration.refresh_from_db()
    loaded_version = registration.version

    second_event = make_event("IDVQ1B")
    sibling = submit_case(second_event, legal_versions, nin=UNKNOWN_NIN, person=registration.person)
    process_all()
    sibling_case = case_for(sibling)
    card = upload_national_id_card(sibling)
    review.correct_nin_and_recheck(
        sibling_case.pk,
        actor=manager,
        expected_version=sibling_case.version,
        new_nin="123456789012345679",
        reason_code="TYPING_ERROR_CONFIRMED",
        evidence_document_id=card.pk,
        confirmed=True,
    )
    assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW

    from apps.reviews.services import (
        ApprovalRequiresVerifiedIdentityError,
        record_approved_decision,
    )

    with pytest.raises(ApprovalRequiresVerifiedIdentityError) as refusal:
        record_approved_decision(
            registration=registration,
            expected_version=loaded_version,
            decided_by=manager,
            attendance_category="FOLLOWING_TWO_DAYS",
        )
    assert refusal.value.code == "IDENTITY_NOT_VERIFIED"
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED


# ---------------------------------------------------------------------------
# The staff screen
# ---------------------------------------------------------------------------


def test_the_case_page_explains_and_the_approve_action_refuses(
    idv_event, legal_versions, manager
) -> None:
    from django.urls import reverse

    from apps.reviews.services import open_review_case

    registration = _submitted(idv_event, legal_versions, manager)
    review_case = open_review_case(
        registration=registration, case_type="STANDARD", queue_code="GENERAL"
    )
    client = staff_client(manager)
    page = client.get(reverse("reviews:case-detail", kwargs={"pk": review_case.pk}))
    assert page.status_code == 200
    assert 'data-identity-clearance="IDENTITY_NOT_VERIFIED"' in page.text
    registration.refresh_from_db()
    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": review_case.pk}),
        {"expected_version": registration.version},
        follow=True,
    )
    assert "The identity is not verified yet" in response.text
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
