"""UPD-01: a registration withdrawn or cancelled during identity manual review.

The participant may still withdraw. The registration then leaves every
actionable identity tab and its count (kept, with its history, under Closed
registrations), staff identity actions are refused even from a page opened
before the withdrawal, a late Ministry result changes nothing and creates no
participation-review work, the comprehensive registrations list keeps it, and
registering again creates a new request without reviving the old one.
Synthetic identities only.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.people.models import (
    IdentityReasonCode,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationAttempt,
)
from apps.people.selectors.identity import (
    CLOSED_REGISTRATIONS,
    clean_queue_filters,
    queue_queryset,
    status_counts,
)
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME
from apps.reviews.models import ReviewCase

from .conftest import (
    SIM_MATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_staff,
    process_all,
    staff_client,
    submit_case,
    upload_national_id_card,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "upd01-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manager(idv_event):
    return make_staff(
        "upd01-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manual_case(idv_event, legal_versions):
    registration = submit_case(idv_event, legal_versions, nin=UNKNOWN_NIN)
    process_all()
    upload_national_id_card(registration)
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    return case


def _withdraw(registration):
    from apps.reviews.services import withdraw_registration

    registration.refresh_from_db()
    return withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )


def _queue_ids(user, status):
    filters = clean_queue_filters({"status": status})
    return set(queue_queryset(user, filters).values_list("pk", flat=True))


def test_withdrawal_removes_the_case_from_actionable_tabs_and_counts(manual_case, reviewer):
    before = status_counts(reviewer, clean_queue_filters({}))
    assert before[IdentityStatus.MANUAL_REVIEW] == 1
    assert manual_case.pk in _queue_ids(reviewer, IdentityStatus.MANUAL_REVIEW)

    _withdraw(manual_case.registration)

    after = status_counts(reviewer, clean_queue_filters({}))
    assert after[IdentityStatus.MANUAL_REVIEW] == 0
    assert after[CLOSED_REGISTRATIONS] == 1
    assert after["ALL"] == 1  # history stays countable, under its own tab
    assert manual_case.pk not in _queue_ids(reviewer, IdentityStatus.MANUAL_REVIEW)
    assert manual_case.pk in _queue_ids(reviewer, CLOSED_REGISTRATIONS)
    # The verification history is kept.
    case = case_for(manual_case.registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert IdentityVerificationAttempt.objects.filter(registration=case.registration).exists()


def test_already_stuck_withdrawn_entries_are_excluded_by_the_query(manual_case, reviewer):
    """A registration withdrawn before this update (its case left in manual
    review with any reason) is excluded without rewriting any row."""
    Registration.objects.filter(pk=manual_case.registration_id).update(
        public_status=RegistrationPublicStatus.WITHDRAWN
    )
    IdentityVerification.objects.filter(pk=manual_case.pk).update(
        reason_code=IdentityReasonCode.NOT_FOUND
    )
    assert manual_case.pk not in _queue_ids(reviewer, IdentityStatus.MANUAL_REVIEW)
    counts = status_counts(reviewer, clean_queue_filters({}))
    assert counts[IdentityStatus.MANUAL_REVIEW] == 0
    assert counts[CLOSED_REGISTRATIONS] == 1


def test_the_queue_page_shows_closed_cases_only_in_their_tab(manual_case, reviewer):
    _withdraw(manual_case.registration)
    client = staff_client(reviewer)
    default = client.get(reverse("identity:queue")).content.decode()
    assert 'data-status-count="MANUAL_REVIEW">0<' in default
    assert 'data-status-count="CLOSED">1<' in default
    assert manual_case.registration.public_reference not in default
    closed = client.get(reverse("identity:queue"), {"status": "CLOSED"}).content.decode()
    assert manual_case.registration.public_reference in closed


def test_the_case_page_says_processing_stopped_and_offers_no_action(manual_case, reviewer):
    _withdraw(manual_case.registration)
    response = staff_client(reviewer).get(reverse("identity:case", kwargs={"pk": manual_case.pk}))
    content = response.content.decode()
    assert "data-idv-registration-cancelled" in content
    assert "Identity verification processing has stopped" in content
    assert "data-idv-actions" not in content


@pytest.mark.parametrize("action", ["verify", "return", "correct", "reject", "correct_verify"])
def test_actions_from_a_page_opened_before_the_withdrawal_are_refused(
    manual_case, reviewer, manager, action
):
    version = manual_case.version  # the page was rendered with this version
    card = manual_case.registration.documents.get(document_type="NATIONAL_ID_CARD")
    _withdraw(manual_case.registration)
    actor = manager if action == "reject" else reviewer
    data = {"expected_version": version, "advance": "stay"}
    url, extra = {
        "verify": (
            "identity:verify",
            {"evidence_document": str(card.pk), "reason_code": "MINISTRY_RECORD_UNAVAILABLE"},
        ),
        "return": ("identity:return", {"items": ["NIN_NUMBER"]}),
        "correct": (
            "identity:correct-nin",
            {"new_nin": UNKNOWN_NIN, "reason_code": "RECHECK_AFTER_TECHNICAL_FAILURE"},
        ),
        "reject": (
            "identity:reject",
            {
                "reason_code": "NO_VALID_EVIDENCE",
                "note": "Synthetic reason text",
                "confirmed": "on",
            },
        ),
        "correct_verify": (
            "identity:correct-and-verify",
            {
                "given_names": "Amina",
                "family_name": "Bentest",
                "date_of_birth": "1990-03-07",
                "nin": UNKNOWN_NIN,
                "evidence_document": str(card.pk),
                "reason_code": "DATA_CORRECTED_FROM_DOCUMENT",
            },
        ),
    }[action]
    response = staff_client(actor).post(
        reverse(url, kwargs={"pk": manual_case.pk}), {**data, **extra}
    )
    assert response.status_code == 409
    assert "identity processing has stopped" in response.content.decode()
    case = case_for(manual_case.registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    registration = Registration.objects.get(pk=manual_case.registration_id)
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert not ReviewCase.objects.filter(
        registration=registration, status__in=("QUEUED", "ASSIGNED", "IN_PROGRESS", "WAITING")
    ).exists()


def test_a_delayed_ministry_result_after_withdrawal_revives_nothing(idv_event, legal_versions):
    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    assert case_for(registration).status == IdentityStatus.PENDING
    _withdraw(registration)
    # The job was discarded at withdrawal; a delivery that still arrives is stale.
    outcomes = process_all()
    assert "API_VERIFIED" not in outcomes
    case = case_for(registration)
    assert case.status != IdentityStatus.API_VERIFIED
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    assert (
        not ReviewCase.objects.filter(registration=registration)
        .exclude(status="CANCELLED")
        .exists()
    )


def test_a_result_already_running_when_the_registration_was_withdrawn_is_discarded(
    idv_event, legal_versions
):
    """The worker claimed the job, then the participant withdrew, then the
    Ministry answered: the answer is recorded as not applied."""
    from apps.people.nin_provider import LocalSimulationNinProvider
    from apps.people.services import identity_verification as worker

    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    job = case_for(registration).jobs.get()

    class WithdrawDuringLookup(LocalSimulationNinProvider):
        def lookup(self, nin):
            _withdraw(Registration.objects.get(pk=registration.pk))
            return super().lookup(nin)

    assert worker.process_identity_job(job.pk, provider=WithdrawDuringLookup()) == "stale"
    case = case_for(registration)
    assert case.status != IdentityStatus.API_VERIFIED
    assert IdentityVerificationAttempt.objects.filter(
        registration=registration, applied=False
    ).exists()
    assert (
        not ReviewCase.objects.filter(registration=registration)
        .exclude(status="CANCELLED")
        .exists()
    )


def test_the_backfill_never_creates_work_for_a_withdrawn_registration(manual_case, reviewer):
    from apps.reviews.intake import EntryTrigger, enqueue_for_participation_review

    _withdraw(manual_case.registration)
    result = enqueue_for_participation_review(
        manual_case.registration_id, trigger=EntryTrigger.BACKFILL
    )
    assert (result.outcome, result.reason) == ("EXCLUDED", "WITHDRAWN")
    assert (
        not ReviewCase.objects.filter(registration_id=manual_case.registration_id)
        .exclude(status="CANCELLED")
        .exists()
    )


def test_the_comprehensive_list_keeps_the_withdrawn_registration(manual_case, reviewer):
    _withdraw(manual_case.registration)
    superadmin = make_staff("upd01-super@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)
    superadmin.is_superuser = True
    superadmin.save(update_fields=["is_superuser"])
    response = staff_client(superadmin).get(
        reverse("registrations:ops-intake-list"), {"status": "WITHDRAWN"}
    )
    references = [r.public_reference for r in response.context["registrations"]]
    assert manual_case.registration.public_reference in references


def test_registering_again_never_resurrects_the_withdrawn_request(manual_case, reviewer):
    from apps.registrations.services import start_registration_after_withdrawal

    withdrawn = _withdraw(manual_case.registration)
    draft = start_registration_after_withdrawal(registration=withdrawn, person=withdrawn.person)
    assert draft.pk != withdrawn.pk
    old = Registration.objects.get(pk=withdrawn.pk)
    assert old.public_status == RegistrationPublicStatus.WITHDRAWN
    assert case_for(old).status == IdentityStatus.MANUAL_REVIEW  # kept as history
    assert manual_case.pk not in _queue_ids(reviewer, IdentityStatus.MANUAL_REVIEW)
