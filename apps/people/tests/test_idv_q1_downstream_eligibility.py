"""Owner decision IDV-Q1 (2026-10-02): downstream eligibility follows the identity.

An earlier approval never makes an identity-rejected or invalidated
registration eligible. The shared definition of an active approved context
(`apps.registrations.selectors`) -- used by pass, physical-badge, online
entry and offline-package code -- and the accreditation eligibility boundary
both require a cleared identity, read from the database. The participation
decision itself is not rewritten when the identity changes: the two
histories stay separate. Synthetic data only; imports of the new API are
inside the tests (before/after evidence).
"""

from __future__ import annotations

import datetime

import pytest

from apps.people.models import IdentityStatus
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
    upload_national_id_card,
)
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(idv_event):
    return make_staff("q1d-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


def _eligible_everywhere(registration) -> dict:
    """Every downstream reading of eligibility, side by side."""
    from apps.accreditation.services import evaluate_eligibility
    from apps.entry.services.offline_evaluation import approved_at
    from apps.registrations.selectors import active_approved_context_q, is_active_approved_context

    fresh = Registration.objects.get(pk=registration.pk)
    return {
        "in_memory": is_active_approved_context(fresh),
        "queryset": Registration.objects.filter(active_approved_context_q(), pk=fresh.pk).exists(),
        "offline": approved_at(fresh, datetime.datetime.now(datetime.UTC)),
        "accreditation": evaluate_eligibility(fresh),
    }


def _approved_with_verified_identity(idv_event, legal_versions, manager):
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
    record_approved_decision(
        registration=registration, expected_version=registration.version, decided_by=manager
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.APPROVED
    return registration


def test_an_approved_registration_with_a_verified_identity_is_eligible(
    idv_event, legal_versions, manager
) -> None:
    registration = _approved_with_verified_identity(idv_event, legal_versions, manager)
    assert _eligible_everywhere(registration) == {
        "in_memory": True,
        "queryset": True,
        "offline": True,
        "accreditation": {"eligible": True, "reason": "ELIGIBLE"},
    }


def test_an_identity_invalidated_after_approval_ends_eligibility_everywhere(
    idv_event, legal_versions, manager
) -> None:
    """The NIN is corrected in the same person's other registration: the
    approved case is rebound to manual review. The approval stays recorded,
    but nothing downstream treats the registration as eligible any more."""
    registration = _approved_with_verified_identity(idv_event, legal_versions, manager)
    sibling = submit_case(
        make_event("IDVQ1D"), legal_versions, nin=UNKNOWN_NIN, person=registration.person
    )
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
    registration.refresh_from_db()
    assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW
    assert registration.public_status == RegistrationPublicStatus.APPROVED  # history kept
    assert registration.decisions.get(is_current=True).outcome == "APPROVED"
    assert _eligible_everywhere(registration) == {
        "in_memory": False,
        "queryset": False,
        "offline": False,
        "accreditation": {"eligible": False, "reason": "IDENTITY_NOT_CLEARED"},
    }


def test_an_identity_rejected_after_approval_is_never_eligible(
    idv_event, legal_versions, manager
) -> None:
    """After the invalidation above, a final rejection closes the
    registration: its participation decision becomes NOT_APPROVED too
    (IDV-Q2), superseding the approval."""
    registration = _approved_with_verified_identity(idv_event, legal_versions, manager)
    case = case_for(registration)
    # Invalidate directly (as the rebinding would), then reject finally.
    from apps.people.models import IdentityVerification

    IdentityVerification.objects.filter(pk=case.pk).update(
        status=IdentityStatus.MANUAL_REVIEW,
        verification_source="",
        reason_code="IDENTITY_DATA_CHANGED",
    )
    case.refresh_from_db()
    review.reject_identity(
        case.pk,
        actor=manager,
        expected_version=case.version,
        reason_code="IDENTITY_OF_ANOTHER_PERSON",
        note="The document belongs to someone else.",
        confirmed=True,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    current = registration.decisions.get(is_current=True)
    assert current.outcome == "NOT_APPROVED"
    assert current.supersedes.outcome == "APPROVED"
    assert _eligible_everywhere(registration)["queryset"] is False
    assert _eligible_everywhere(registration)["in_memory"] is False


def test_a_legacy_approved_registration_without_identity_case_is_not_eligible(
    idv_event, manager
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.tests.conftest import make_registration

    registration = make_registration(
        event=idv_event, person=resolve_or_create_participant_for_email("q1d-legacy@example.test")
    )
    assign_approval_prerequisites(registration, manager)
    Registration.objects.filter(pk=registration.pk).update(
        public_status=RegistrationPublicStatus.APPROVED
    )
    result = _eligible_everywhere(registration)
    assert result["in_memory"] is False
    assert result["queryset"] is False
    assert result["offline"] is False
    assert result["accreditation"] == {"eligible": False, "reason": "IDENTITY_NOT_CLEARED"}


def test_a_simulated_identity_is_eligible_only_where_the_simulation_is_allowed(
    idv_event, manager, settings
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.reviews.tests.conftest import make_registration

    registration = make_registration(
        event=idv_event, person=resolve_or_create_participant_for_email("q1d-sim@example.test")
    )
    assign_approval_prerequisites(registration, manager)
    make_verified_identity_case(registration, status="API_VERIFIED", source="SIMULATED_API")
    Registration.objects.filter(pk=registration.pk).update(
        public_status=RegistrationPublicStatus.APPROVED
    )
    assert _eligible_everywhere(registration)["queryset"] is True
    settings.IDENTITY_ALLOW_SIMULATED_PROVIDER = False
    result = _eligible_everywhere(registration)
    assert result["in_memory"] is False
    assert result["queryset"] is False
    assert result["accreditation"]["reason"] == "IDENTITY_NOT_CLEARED"


# ---------------------------------------------------------------------------
# The two definitions agree on every identity fact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow_simulation", [True, False])
@pytest.mark.parametrize(
    ("status", "source", "identifier_status", "expires_in_days"),
    [
        (None, None, None, None),  # no identity case
        ("PENDING", "", "DECLARED", 400),
        ("MANUAL_REVIEW", "", "DECLARED", 400),
        ("RETURNED_FOR_CORRECTION", "", "DECLARED", 400),
        ("REJECTED", "", "DECLARED", 400),
        ("API_VERIFIED", "MINISTRY_API", "VERIFIED", 400),
        ("API_VERIFIED", "SIMULATED_API", "VERIFIED", 400),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "VERIFIED", 400),
        ("MANUALLY_VERIFIED", "MANUAL_NIN_EXEMPTION", "VERIFIED", 400),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "VERIFIED", 1),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "VERIFIED", 0),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "REPLACED", 400),
        ("MANUALLY_VERIFIED", "MANUAL_PASSPORT", "EXPIRED", 400),
    ],
)
def test_the_queryset_and_in_memory_definitions_agree_on_identity_facts(
    idv_event, settings, allow_simulation, status, source, identifier_status, expires_in_days
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.selectors import active_approved_context_q, is_active_approved_context
    from apps.reviews.tests.conftest import make_registration

    settings.IDENTITY_ALLOW_SIMULATED_PROVIDER = allow_simulation
    registration = make_registration(
        event=idv_event,
        person=resolve_or_create_participant_for_email(
            f"q1d-agree-{status}-{source}-{identifier_status}-{expires_in_days}@example.test".lower()
        ),
    )
    if status is not None:
        make_verified_identity_case(
            registration,
            status=status,
            source=source,
            identifier_status=identifier_status,
            expires_at=datetime.date.today() + datetime.timedelta(days=expires_in_days),
        )
    Registration.objects.filter(pk=registration.pk).update(
        public_status=RegistrationPublicStatus.APPROVED
    )
    fresh = Registration.objects.get(pk=registration.pk)
    in_queryset = Registration.objects.filter(active_approved_context_q(), pk=fresh.pk).exists()
    assert is_active_approved_context(fresh) is in_queryset
    expected = (
        status in ("API_VERIFIED", "MANUALLY_VERIFIED")
        and identifier_status == "VERIFIED"
        and expires_in_days > 0
        and (source != "SIMULATED_API" or allow_simulation)
    )
    assert in_queryset is expected
