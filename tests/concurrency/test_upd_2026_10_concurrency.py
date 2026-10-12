"""Consolidated update 2026-10-12: real multi-connection PostgreSQL races.

Threads on their OWN connections (the pattern of `test_idv_c1_concurrency.py`),
with forced interleavings (a thread pauses inside the locked section):

1. UPD-01: a participant withdrawal racing a reviewer's manual verification,
   in both orders. A withdrawn registration never ends verified-and-queued,
   never keeps an open participation-review case, and nothing deadlocks.
2. UPD-02: a withdrawal during the Ministry lookup of "save corrections and
   verify": no database lock is held during the lookup (the withdrawal
   completes while the lookup is paused), and the reviewer's action is then
   refused with nothing saved.
3. UPD-02: two reviewers saving corrections at once: one wins, the other is a
   visible conflict, and the registration enters participation review once.

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

from apps.people.models import IdentityStatus
from apps.people.nin_provider import LocalSimulationNinProvider
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
from apps.registrations.models import Registration, RegistrationProfile, RegistrationPublicStatus
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME
from apps.reviews.models import ReviewCase

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

OPEN_CASE_STATUSES = ("QUEUED", "ASSIGNED", "IN_PROGRESS", "WAITING")


def _run_named(callables: dict) -> tuple[dict, dict]:
    barrier = threading.Barrier(len(callables))
    results: dict = {}
    errors: dict = {}

    def _worker(name, target) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors[name] = exc
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
                version_label=f"upd-c-{code}-{uuid.uuid4().hex[:6]}",
                content="Synthetic",
                content_hash="f" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(result)


@pytest.fixture
def reviewer(versions):
    return make_staff(
        f"upd-c-{uuid.uuid4().hex[:6]}@example.test", REGISTRATION_REVIEWERS_GROUP_NAME
    )


def _manual_registration(versions):
    registration = submit_case(make_event(f"UPDC{uuid.uuid4().hex[:6]}"), versions, nin=UNKNOWN_NIN)
    process_all()
    upload_national_id_card(registration)
    assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW
    return registration


def _card(registration):
    return registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")


def _withdraw(registration_id):
    from apps.reviews.services import withdraw_registration

    registration = Registration.objects.get(pk=registration_id)
    return withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )


def _verify(registration, version):
    return review.verify_identity_manually(
        case_for(registration).pk,
        actor=None,
        expected_version=version,
        evidence_document_id=_card(registration).pk,
        reason_code="MINISTRY_RECORD_UNAVAILABLE",
    )


def _assert_withdrawn_is_history(registration_id):
    registration = Registration.objects.get(pk=registration_id)
    if registration.public_status == RegistrationPublicStatus.WITHDRAWN:
        assert not ReviewCase.objects.filter(
            registration=registration, status__in=OPEN_CASE_STATUSES
        ).exists()


@pytest.mark.parametrize("first", ["withdraw", "verify"])
def test_withdrawal_racing_a_manual_verification(versions, reviewer, monkeypatch, first):
    from apps.reviews import services as review_services

    registration = _manual_registration(versions)
    case = case_for(registration)
    entered = threading.Event()
    original_terminate = review_services._terminate_open_review_work
    original_require = review._require_active

    def slow_terminate(*args, **kwargs):  # inside the withdrawal, after its lock
        if threading.current_thread().name == "withdraw" and first == "withdraw":
            entered.set()
            time.sleep(0.8)
        return original_terminate(*args, **kwargs)

    def slow_require(registration_row):  # inside the verification, after its locks
        if threading.current_thread().name == "verify" and first == "verify":
            entered.set()
            time.sleep(0.8)
        return original_require(registration_row)

    monkeypatch.setattr(review_services, "_terminate_open_review_work", slow_terminate)
    monkeypatch.setattr(review, "_require_active", slow_require)

    def verify():
        if first == "withdraw":
            assert entered.wait(timeout=20)
        return _verify(registration, case.version)

    def withdraw():
        if first == "verify":
            assert entered.wait(timeout=20)
        return _withdraw(registration.pk)

    _results, errors = _run_named(
        {"verify": lambda: _verify_as(reviewer, verify), "withdraw": withdraw}
    )
    final = Registration.objects.get(pk=registration.pk)
    if first == "withdraw":
        # The verification waited for the withdrawal's lock, then was refused.
        assert isinstance(errors.get("verify"), review.IdentityStateError)
        assert final.public_status == RegistrationPublicStatus.WITHDRAWN
        assert case_for(final).status == IdentityStatus.MANUAL_REVIEW
    else:
        # The verification committed first; the participant's withdrawal form
        # was loaded before it, so it gets the standard stale-version conflict,
        # and a reload then withdraws normally.
        from apps.reviews.services import StaleVersionError

        assert isinstance(errors.get("withdraw"), StaleVersionError)
        assert case_for(final).status == IdentityStatus.MANUALLY_VERIFIED
        _withdraw(registration.pk)
    _assert_withdrawn_is_history(registration.pk)
    final.refresh_from_db()
    assert final.public_status == RegistrationPublicStatus.WITHDRAWN
    assert not ReviewCase.objects.filter(registration=final, status__in=OPEN_CASE_STATUSES)


def _verify_as(reviewer, call):
    """Run the verification as `reviewer` (the helper passes no actor)."""
    original = review.verify_identity_manually

    def with_actor(*args, **kwargs):
        kwargs["actor"] = reviewer
        return original(*args, **kwargs)

    review.verify_identity_manually = with_actor
    try:
        return call()
    finally:
        review.verify_identity_manually = original


def test_no_lock_is_held_during_the_ministry_lookup_and_a_withdrawal_wins(versions, reviewer):
    registration = _manual_registration(versions)
    case = case_for(registration)
    in_lookup = threading.Event()
    withdrawn = threading.Event()

    class SlowProvider(LocalSimulationNinProvider):
        def lookup(self, nin):
            in_lookup.set()
            # The withdrawal takes the Registration lock and commits while
            # this lookup is still running: nothing is locked here.
            assert withdrawn.wait(timeout=20)
            return super().lookup(nin)

    def correct():
        return review.correct_and_verify_identity(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            given_names="Amina",
            family_name="Bentest",
            date_of_birth=datetime.date(1990, 3, 7),
            nin="123456789012345670",
            evidence_document_id=_card(registration).pk,
            reason_code="TYPING_ERROR_CORRECTED",
            provider=SlowProvider(),
        )

    def withdraw():
        assert in_lookup.wait(timeout=20)
        result = _withdraw(registration.pk)
        withdrawn.set()
        return result

    _results, errors = _run_named({"reviewer": correct, "participant": withdraw})
    assert "participant" not in errors
    assert isinstance(errors.get("reviewer"), review.IdentityStateError)
    final = Registration.objects.get(pk=registration.pk)
    assert final.public_status == RegistrationPublicStatus.WITHDRAWN
    assert case_for(final).status == IdentityStatus.MANUAL_REVIEW
    assert case_for(final).current_revision.identifier.value_encrypted == UNKNOWN_NIN
    assert RegistrationProfile.objects.get(registration=final).submitted_family_name == "Bentest"
    assert not ReviewCase.objects.filter(registration=final, status__in=OPEN_CASE_STATUSES)


def test_two_reviewers_saving_corrections_at_once(versions, reviewer):
    registration = _manual_registration(versions)
    case = case_for(registration)
    other = make_staff(
        f"upd-c2-{uuid.uuid4().hex[:6]}@example.test", REGISTRATION_REVIEWERS_GROUP_NAME
    )

    def correct(actor, given):
        return lambda: review.correct_and_verify_identity(
            case.pk,
            actor=actor,
            expected_version=case.version,
            given_names=given,
            family_name="Bentest",
            date_of_birth=datetime.date(1990, 3, 7),
            nin=UNKNOWN_NIN,
            evidence_document_id=_card(registration).pk,
            reason_code="NAME_VARIANT_CONFIRMED",
        )

    results, errors = _run_named(
        {"first": correct(reviewer, "Amina Sara"), "second": correct(other, "Amina Lina")}
    )
    assert len(results) == 1 and len(errors) == 1
    (error,) = errors.values()
    assert isinstance(error, (review.StaleIdentityVersion, review.IdentityStateError))
    final = case_for(registration)
    assert final.status == IdentityStatus.MANUALLY_VERIFIED
    winner = "Amina Sara" if "first" in results else "Amina Lina"
    assert (
        RegistrationProfile.objects.get(registration=registration).submitted_given_names == winner
    )
    assert ReviewCase.objects.filter(registration=registration, case_type="STANDARD").count() == 1
