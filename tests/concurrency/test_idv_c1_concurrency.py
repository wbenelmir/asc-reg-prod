"""IDV-C1: real multi-connection PostgreSQL races behind R-IDV-01 and R-IDV-06.

Threads on their OWN PostgreSQL connections (the pattern of
`test_idv_concurrency.py`). Proven here:

1. a ministry result being applied for the same person's identifier while a
   reviewer corrects that NIN in another registration: the replaced identifier
   is never made VERIFIED and no case keeps a verification of it (the worker is
   paused inside its final update, so the interleaving of the review finding
   is forced, not hoped for);
2. a stale manual confirmation racing such a correction: never a verification
   of the replaced NIN, no deadlock;
3. two corrections of one person's NIN in two registrations at once: one wins,
   the other is a visible conflict, and the person never ends with two current
   NINs;
4. a withdrawal racing a correction resubmission: a withdrawn registration is
   never resubmitted.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.people.models import (
    IdentifierStatus,
    IdentifierType,
    IdentityIdentifier,
    IdentityJobStatus,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationJob,
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
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

CORRECTED_NIN = "123456789012345670"
OTHER_CORRECTED_NIN = "123456789012345671"


def _run_named(callables: dict) -> tuple[dict, list[BaseException]]:
    """Run each callable on its own thread (named after its key) and its own
    connection, released together; returns results by name and the errors."""
    barrier = threading.Barrier(len(callables))
    results: dict = {}
    errors: list[BaseException] = []

    def _worker(name, target) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=_worker, args=(name, target), name=name)
        for name, target in callables.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    return results, errors


@pytest.fixture(autouse=True)
def _private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def versions():
    from apps.core.models import Country, Sector
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    result = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        result.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-c1-c-{code}",
                content="Synthetic",
                content_hash="e" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(result)


@pytest.fixture
def reviewer(versions):
    return make_staff("idv-c1-conc@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


def _person():
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email(f"idv-c1c-{uuid.uuid4().hex[:10]}@example.test")


def _card(registration):
    return registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")


def _correct(registration, reviewer, *, new_nin=CORRECTED_NIN, version=None):
    case = case_for(registration)
    return review.correct_nin_and_recheck(
        case.pk,
        actor=reviewer,
        expected_version=case.version if version is None else version,
        new_nin=new_nin,
        reason_code="TYPING_ERROR_CONFIRMED",
        evidence_document_id=_card(registration).pk,
        confirmed=True,
    )


def _current_nins(person) -> int:
    return (
        IdentityIdentifier.objects.filter(person=person, identifier_type=IdentifierType.NIN)
        .exclude(status=IdentifierStatus.REPLACED)
        .count()
    )


def test_a_ministry_result_racing_a_correction_elsewhere_never_resurrects(
    versions, reviewer, monkeypatch
) -> None:
    person = _person()
    first = submit_case(make_event("IDVC1CA"), versions, nin=SIM_MATCH_NIN, person=person)
    second = submit_case(
        make_event("IDVC1CB"), versions, nin=SIM_MATCH_NIN, person=person, given="Samira"
    )
    # The second case goes to review (different names); the first job waits.
    first_job = IdentityVerificationJob.objects.get(verification=case_for(first))
    second_job = IdentityVerificationJob.objects.get(verification=case_for(second))
    idv.process_identity_job(second_job.pk)
    assert case_for(second).status == IdentityStatus.MANUAL_REVIEW
    upload_national_id_card(second)
    old_id = case_for(first).current_revision.identifier_id
    paused = threading.Event()
    original = idv.lock_identifier_values

    def pausing_lock(*args, **kwargs):
        if threading.current_thread().name == "worker":
            paused.set()
            time.sleep(1.0)  # holding the first case's row, before the final update
        return original(*args, **kwargs)

    monkeypatch.setattr(idv, "lock_identifier_values", pausing_lock)

    def correction():
        assert paused.wait(timeout=20)
        return _correct(second, reviewer)

    results, errors = _run_named(
        {"worker": lambda: idv.process_identity_job(first_job.pk), "corrector": correction}
    )

    assert errors == []
    assert IdentityIdentifier.objects.get(pk=old_id).status == IdentifierStatus.REPLACED
    first_case = case_for(first)
    assert first_case.status not in IdentityStatus.verified_statuses()
    assert first_case.current_revision.identifier_id != old_id
    assert _current_nins(person) == 1


def test_a_stale_confirmation_racing_a_correction_elsewhere(versions, reviewer) -> None:
    person = _person()
    first = submit_case(make_event("IDVC1CA"), versions, nin=UNKNOWN_NIN, person=person)
    second = submit_case(make_event("IDVC1CB"), versions, nin=UNKNOWN_NIN, person=person)
    process_all()
    for registration in (first, second):
        upload_national_id_card(registration)
    stale = case_for(first)
    old_id = stale.current_revision.identifier_id

    def confirm():
        return review.verify_identity_manually(
            stale.pk,
            actor=reviewer,
            expected_version=stale.version,
            evidence_document_id=_card(first).pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )

    _results, errors = _run_named(
        {"confirmer": confirm, "corrector": lambda: _correct(second, reviewer)}
    )

    assert all(
        isinstance(error, (review.StaleIdentityVersion, review.IdentityStateError))
        for error in errors
    ), errors
    assert IdentityIdentifier.objects.get(pk=old_id).status == IdentifierStatus.REPLACED
    first_case = case_for(first)
    assert first_case.status == IdentityStatus.MANUAL_REVIEW  # never verified on the old NIN
    assert first_case.current_revision.identifier_id != old_id


def test_two_corrections_of_one_person_never_leave_two_current_nins(versions, reviewer) -> None:
    person = _person()
    first = submit_case(make_event("IDVC1CA"), versions, nin=UNKNOWN_NIN, person=person)
    second = submit_case(make_event("IDVC1CB"), versions, nin=UNKNOWN_NIN, person=person)
    process_all()
    for registration in (first, second):
        upload_national_id_card(registration)
    first_version, second_version = case_for(first).version, case_for(second).version

    _results, errors = _run_named(
        {
            "one": lambda: _correct(first, reviewer, new_nin=CORRECTED_NIN, version=first_version),
            "two": lambda: _correct(
                second, reviewer, new_nin=OTHER_CORRECTED_NIN, version=second_version
            ),
        }
    )

    assert len(errors) == 1 and isinstance(errors[0], review.StaleIdentityVersion), errors
    assert _current_nins(person) == 1
    cases = IdentityVerification.objects.filter(person=person)
    assert len({case.current_revision.identifier_id for case in cases}) == 1


def test_a_withdrawal_racing_a_ministry_result_does_not_deadlock(
    versions, reviewer, monkeypatch
) -> None:
    """The closure step locks the case after the Registration, as every staff
    command does; the worker must take the same order (the result it applies
    also inserts rows that reference the Registration)."""
    from apps.reviews.services import withdraw_registration

    registration = submit_case(make_event("IDVC1CR"), versions, nin=SIM_MATCH_NIN)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    registration.refresh_from_db()
    registration_version = registration.version
    paused = threading.Event()
    original = idv.lock_identifier_values

    def pausing_lock(*args, **kwargs):
        if threading.current_thread().name == "worker":
            paused.set()
            time.sleep(1.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(idv, "lock_identifier_values", pausing_lock)

    def withdraw():
        assert paused.wait(timeout=20)
        return withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration_version,
        )

    _results, errors = _run_named(
        {"worker": lambda: idv.process_identity_job(job.pk), "withdraw": withdraw}
    )

    assert errors == []
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
    final = case_for(registration)
    # The result committed first (the withdrawal waited for it); a verified
    # identity is kept as a fact, and nothing is pending any more.
    assert final.status in (IdentityStatus.API_VERIFIED, IdentityStatus.MANUAL_REVIEW)
    assert not IdentityVerificationJob.objects.filter(
        verification=final, status=IdentityJobStatus.PENDING
    ).exists()


def test_a_withdrawal_racing_a_resubmission_never_resubmits_a_withdrawn_registration(
    versions, reviewer
) -> None:
    from apps.reviews.services import StaleVersionError, withdraw_registration

    registration = submit_case(make_event("IDVC1CW"), versions, nin=UNKNOWN_NIN)
    process_all()
    case = case_for(registration)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    registration.refresh_from_db()
    registration_version = registration.version
    case_version = case_for(registration).version
    revisions_before = case_for(registration).revisions.count()

    def withdraw():
        return withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration_version,
        )

    def resubmit():
        return review.resubmit_identity_correction(
            registration,
            person=registration.person,
            expected_version=case_version,
            given_names="Amina",
            family_name="Bentest",
            date_of_birth=datetime.date(1990, 3, 7),
            nin_value=CORRECTED_NIN,
        )

    _results, errors = _run_named({"withdraw": withdraw, "resubmit": resubmit})

    assert len(errors) <= 1, errors
    assert all(
        isinstance(
            error, (StaleVersionError, review.IdentityStateError, review.StaleIdentityVersion)
        )
        for error in errors
    ), errors
    registration.refresh_from_db()
    if registration.withdrawn_at is not None:
        assert registration.public_status == RegistrationPublicStatus.WITHDRAWN
        final = case_for(registration)
        assert final.status != IdentityStatus.PENDING
        assert final.revisions.count() == revisions_before
        assert not IdentityVerificationJob.objects.filter(
            verification=final, status=IdentityJobStatus.PENDING
        ).exists()
    else:
        assert registration.public_status == RegistrationPublicStatus.SUBMITTED
