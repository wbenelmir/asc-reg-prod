"""IDV-C1, R-IDV-06: closing a registration ends its identity correction work.

A participant withdrawal, an operational cancellation or a NOT_APPROVED
decision closes the Registration. In the same transaction its identity case
stops: a pending check is discarded, an open correction request is closed (the
case goes to manual review with `REGISTRATION_CLOSED`), the participant no
longer sees the correction, and a stale or direct resubmission is refused
under the Registration lock. No identity service reopens a closed
Registration; the existing authorized reopening still works. Staff identity
commands refuse a closed registration as well.

Synthetic identities only.
"""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse

from apps.people.models import (
    IdentityDecisionAction,
    IdentityJobStatus,
    IdentityReasonCode,
    IdentityStatus,
    IdentityVerificationJob,
)
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_staff,
    participant_client,
    process_all,
    submit_case,
    upload_national_id_card,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

CORRECTED_NIN = "123456789012345670"


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "idv-c1-close-r@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manager(idv_event):
    return make_staff(
        "idv-c1-close-m@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )


def _returned(idv_event, legal_versions, reviewer):
    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    case = case_for(registration)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED
    return registration


def _resubmit(registration, version, nin=CORRECTED_NIN):
    return review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=version,
        given_names="Amina",
        family_name="Bentest",
        date_of_birth=datetime.date(1990, 3, 7),
        nin_value=nin,
    )


def _close(how: str, registration, manager) -> None:
    from apps.reviews.services import (
        cancel_registration_operationally,
        record_not_approved_decision,
        withdraw_registration,
    )

    registration.refresh_from_db()
    if how == "withdraw":
        withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration.version,
        )
    elif how == "cancel":
        cancel_registration_operationally(
            registration=registration,
            expected_version=registration.version,
            reason="synthetic operational cancellation",
            actor=manager,
        )
    else:
        record_not_approved_decision(
            registration=registration,
            expected_version=registration.version,
            internal_reason_code="SYNTHETIC_TEST",
            decided_by=manager,
        )


def _footprint(registration) -> dict:
    case = case_for(registration)
    return {
        "revisions": case.revisions.count(),
        "jobs": IdentityVerificationJob.objects.filter(verification=case).count(),
        "submissions": registration.submissions.count(),
        "messages": registration.messages.count(),
        "resubmissions": case.decisions.filter(
            action=IdentityDecisionAction.PARTICIPANT_RESUBMISSION
        ).count(),
    }


@pytest.mark.parametrize("how", ["withdraw", "cancel"])
def test_a_stale_resubmission_never_reopens_a_closed_registration(
    idv_event, legal_versions, reviewer, manager, how
) -> None:
    registration = _returned(idv_event, legal_versions, reviewer)
    stale_version = case_for(registration).version
    _close(how, registration, manager)
    before = _footprint(registration)

    with pytest.raises((review.IdentityStateError, review.StaleIdentityVersion)):
        _resubmit(registration, stale_version)
    # Not even with the case's current version.
    with pytest.raises(review.IdentityStateError):
        _resubmit(registration, case_for(registration).version)

    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert _footprint(registration) == before
    assert case_for(registration).status != IdentityStatus.PENDING


def test_closing_the_registration_ends_the_correction_request(
    idv_event, legal_versions, reviewer, manager
) -> None:
    registration = _returned(idv_event, legal_versions, reviewer)
    client = participant_client(registration.person)
    url = reverse("identity:participant-correction", kwargs={"pk": registration.pk})
    assert url in client.get(reverse("registrations:workspace")).content.decode()
    stale_version = case_for(registration).version

    _close("withdraw", registration, manager)

    closed = case_for(registration)
    assert closed.status == IdentityStatus.MANUAL_REVIEW
    assert closed.reason_code == IdentityReasonCode.REGISTRATION_CLOSED
    assert closed.decisions.filter(action=IdentityDecisionAction.REGISTRATION_CLOSED).exists()
    assert review.open_correction_request(registration) is None
    # The workspace no longer offers it; the page and a stale POST go back.
    assert url not in client.get(reverse("registrations:workspace")).content.decode()
    page = client.get(url)
    assert page.status_code == 302 and page["Location"] == reverse("registrations:workspace")
    before = _footprint(registration)
    response = client.post(
        url,
        {
            "expected_version": stale_version,
            "given_names": "Amina",
            "family_name": "Bentest",
            "date_of_birth_day": "07",
            "date_of_birth_month": "03",
            "date_of_birth_year": "1990",
            "nin_value": CORRECTED_NIN,
        },
    )
    assert response.status_code == 302
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert _footprint(registration) == before


def test_a_pending_check_is_discarded_when_the_registration_closes(
    idv_event, legal_versions, manager
) -> None:
    from apps.people.tests.conftest import SIM_MATCH_NIN

    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    _close("withdraw", registration, manager)

    job.refresh_from_db()
    assert job.status == IdentityJobStatus.DISCARDED_STALE
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == IdentityReasonCode.REGISTRATION_CLOSED
    calls = []

    class CountingProvider:
        PROVIDER_CODE = "COUNTING_TEST_DOUBLE"
        IS_OFFICIAL = False

        def lookup(self, nin):  # pragma: no cover - must never be called
            calls.append(nin)
            raise AssertionError("no check for a closed registration")

    process_all(provider=CountingProvider())
    assert calls == []


def test_staff_identity_actions_refuse_a_not_approved_registration(
    idv_event, legal_versions, reviewer, manager
) -> None:
    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    upload_national_id_card(registration)
    case = case_for(registration)
    card = registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    _close("not_approved", registration, manager)
    case = case_for(registration)
    attempts = {
        "verify": lambda: review.verify_identity_manually(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            evidence_document_id=card.pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        ),
        "return": lambda: review.return_identity_for_correction(
            case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
        ),
        "correct": lambda: review.correct_nin_and_recheck(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            new_nin=CORRECTED_NIN,
            reason_code="TYPING_ERROR_CONFIRMED",
            evidence_document_id=card.pk,
            confirmed=True,
        ),
        "reject": lambda: review.reject_identity(
            case.pk,
            actor=manager,
            expected_version=case.version,
            reason_code="NO_VALID_EVIDENCE",
            note="Synthetic explanation for the test.",
            confirmed=True,
        ),
    }
    for name, attempt in attempts.items():
        with pytest.raises(review.IdentityStateError):
            attempt()
        assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW, name
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED


def test_an_authorized_reopening_restores_the_correction_path(
    idv_event, legal_versions, reviewer, manager
) -> None:
    from apps.reviews.services import reopen_registration

    registration = _returned(idv_event, legal_versions, reviewer)
    _close("withdraw", registration, manager)
    registration.refresh_from_db()
    reopen_registration(
        registration=registration,
        expected_version=registration.version,
        reason="synthetic authorized reopening",
        actor=manager,
    )
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW  # waiting for staff, not for the participant
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    _resubmit(registration, case_for(registration).version)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    assert case_for(registration).status == IdentityStatus.PENDING  # the changed NIN is checked
