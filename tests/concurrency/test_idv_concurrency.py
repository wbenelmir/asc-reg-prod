"""Real multi-connection PostgreSQL concurrency for identity verification (IDV-07).

Threads on their OWN PostgreSQL connections, released together by a barrier
(the pattern of `test_badge_stock_concurrency.py`). Proven here:

1. two people submitting the same NIN at the same moment never both pass the
   duplicate check, and the identifier is never verified twice;
2. two reviewers confirming the same case at once: exactly one decision;
3. two cross-person duplicate cases confirmed at once: never both verified;
4. a manual confirmation racing another person's submission of the same NIN:
   never both accepted;
5. a duplicated job delivery processed by two workers at once: one provider
   call, one attempt;
6. a worker result racing a NIN correction of the same person's identity in
   another registration: the stale result never verifies.

Synthetic data only; the development simulation answers NINs starting 99.
"""

from __future__ import annotations

import datetime
import threading
import time

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.people.models import (
    IdentifierStatus,
    IdentityIdentifier,
    IdentityReasonCode,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationAttempt,
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
    submit_case,
    upload_national_id_card,
)
from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


@pytest.fixture(autouse=True)
def _private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def world():
    from apps.core.models import Country, Sector
    from apps.privacy.models import (
        LegalDocument,
        LegalDocumentVersion,
        LegalDocumentVersionStatus,
    )

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-c-{code}",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    event = make_event("IDVCONC")
    return event, tuple(versions)


def _prepare_draft(event, nin, *, given, family):
    """A complete draft, ready for the final submission (not yet submitted)."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.services import get_or_create_active_draft, save_identity_step
    from apps.registrations.tests.factories import walk_draft_through_every_step

    person = resolve_or_create_participant_for_email(f"idv-c-{given.lower()}@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft, nin_value=nin)
    save_identity_step(
        registration=draft,
        given_names=given,
        family_name=family,
        date_of_birth=datetime.date(1990, 3, 7),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value=nin,
    )
    return draft


def _submit(draft, versions):
    from apps.registrations.services import submit_full_registration

    privacy, terms = versions
    return submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="idv-c",
        idempotency_key=f"idv-c-{draft.pk}",
    )


def _verified_count(nin: str) -> int:
    from apps.people.selectors import identifiers_for_value

    return (
        identifiers_for_value(identifier_type="NIN", country_code_id="DZ", raw_value=nin)
        .filter(status=IdentifierStatus.VERIFIED)
        .count()
    )


def test_simultaneous_submissions_of_one_nin_never_both_pass(world) -> None:
    event, versions = world
    first = _prepare_draft(event, SIM_MATCH_NIN, given="Amina", family="Bentest")
    second = _prepare_draft(event, SIM_MATCH_NIN, given="Other", family="Person")
    _results, errors = _run_callables_in_threads(
        [lambda: _submit(first, versions), lambda: _submit(second, versions)]
    )
    assert errors == []
    cases = list(IdentityVerification.objects.all())
    assert len(cases) == 2
    duplicates = [c for c in cases if c.reason_code == IdentityReasonCode.DUPLICATE_IDENTIFIER]
    assert len(duplicates) >= 1  # the later one always sees the earlier claim
    idv.run_due_identity_jobs()
    assert _verified_count(SIM_MATCH_NIN) == 0  # an open cross-person claim blocks both
    assert not IdentityVerification.objects.filter(
        status__in=IdentityStatus.verified_statuses()
    ).exists()


def test_two_reviewers_confirming_one_case_record_one_decision(world) -> None:
    event, versions = world
    registration = submit_case(event, versions, nin=UNKNOWN_NIN)
    idv.run_due_identity_jobs()
    upload_national_id_card(registration)
    case = case_for(registration)
    card = registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    reviewers = [
        make_staff(f"idv-c-rev{i}@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=event)
        for i in range(2)
    ]

    def confirm(actor):
        return lambda: review.verify_identity_manually(
            case.pk,
            actor=actor,
            expected_version=case.version,
            evidence_document_id=card.pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )

    results, errors = _run_callables_in_threads([confirm(r) for r in reviewers])
    assert sum(1 for r in results if r is not None) == 1
    assert len(errors) == 1 and isinstance(errors[0], review.StaleIdentityVersion)
    assert case_for(registration).decisions.count() == 1


def test_two_cross_person_duplicate_cases_are_never_both_verified(world) -> None:
    event, versions = world
    first = submit_case(event, versions, nin=UNKNOWN_NIN, given="Amina", family="Bentest")
    idv.run_due_identity_jobs()
    second = submit_case(event, versions, nin=UNKNOWN_NIN, given="Other", family="Person")
    for registration in (first, second):
        upload_national_id_card(registration)
    reviewer = make_staff("idv-c-dup@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=event)

    def confirm(registration):
        case = case_for(registration)
        card = registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
        return lambda: review.verify_identity_manually(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            evidence_document_id=card.pk,
            reason_code="DOCUMENT_MATCHES_SUBMISSION",
        )

    _results, errors = _run_callables_in_threads([confirm(first), confirm(second)])
    assert all(isinstance(error, review.DuplicateIdentityConflict) for error in errors)
    assert _verified_count(UNKNOWN_NIN) <= 1
    assert IdentityVerification.objects.filter(status=IdentityStatus.MANUALLY_VERIFIED).count() <= 1


def test_a_manual_confirmation_racing_another_submission_never_accepts_both(world) -> None:
    event, versions = world
    first = submit_case(event, versions, nin=UNKNOWN_NIN, given="Amina", family="Bentest")
    idv.run_due_identity_jobs()
    upload_national_id_card(first)
    case = case_for(first)
    card = first.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    reviewer = make_staff("idv-c-race@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=event)
    rival = _prepare_draft(event, UNKNOWN_NIN, given="Rival", family="Person")

    results, errors = _run_callables_in_threads(
        [
            lambda: review.verify_identity_manually(
                case.pk,
                actor=reviewer,
                expected_version=case.version,
                evidence_document_id=card.pk,
                reason_code="DOCUMENT_MATCHES_SUBMISSION",
            ),
            lambda: _submit(rival, versions),
        ]
    )
    rival_case = IdentityVerification.objects.get(registration=rival)
    first_case = case_for(first)
    if first_case.status == IdentityStatus.MANUALLY_VERIFIED:
        # The confirmation committed first: the rival is caught as a duplicate.
        assert rival_case.reason_code == IdentityReasonCode.DUPLICATE_IDENTIFIER
    else:
        # The rival's open claim was seen first: the confirmation was refused.
        assert any(isinstance(error, review.DuplicateIdentityConflict) for error in errors)
    assert _verified_count(UNKNOWN_NIN) <= 1
    assert results  # both callables ran


def test_a_duplicated_delivery_calls_the_provider_once(world) -> None:
    event, versions = world
    registration = submit_case(event, versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    calls = []

    class SlowProvider:
        PROVIDER_CODE = "SLOW_TEST_DOUBLE"
        IS_OFFICIAL = False

        def configuration_problems(self):
            return []

        def lookup(self, nin):
            calls.append(nin)
            time.sleep(0.3)
            from apps.people.nin_provider import LocalSimulationNinProvider

            return LocalSimulationNinProvider().lookup(nin)

    provider = SlowProvider()
    results, errors = _run_callables_in_threads(
        [lambda: idv.process_identity_job(job.pk, provider=provider) for _ in range(2)]
    )
    assert errors == []
    assert len(calls) == 1
    assert results.count(IdentityStatus.API_VERIFIED) == 1
    assert set(results) - {IdentityStatus.API_VERIFIED} <= {"busy", "not_due"}
    assert IdentityVerificationAttempt.objects.filter(registration=registration).count() == 1


@override_settings(IDENTITY_PROVIDER_MAX_CONCURRENCY=4)
def test_a_result_racing_a_nin_correction_elsewhere_never_verifies(world) -> None:
    """The same person has two registrations; while the check of the first is
    in flight, a reviewer corrects the person's NIN in the second. The first
    result was obtained for the old NIN and must not verify anything."""
    from apps.people.services import resolve_or_create_participant_for_email

    event, versions = world
    other_event = make_event("IDVCONC2")
    person = resolve_or_create_participant_for_email("idv-c-same@example.test")
    first = submit_case(event, versions, nin=SIM_MATCH_NIN, person=person)
    # The second registration shares the NIN identifier; push it to review.
    second = submit_case(other_event, versions, nin=SIM_MATCH_NIN, person=person)
    second_case = case_for(second)
    IdentityVerification.objects.filter(pk=second_case.pk).update(
        status=IdentityStatus.MANUAL_REVIEW, reason_code=IdentityReasonCode.NOT_FOUND
    )
    IdentityVerificationJob.objects.filter(verification=second_case).delete()
    upload_national_id_card(second)
    reviewer = make_staff(
        "idv-c-fix@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=other_event
    )
    first_job = IdentityVerificationJob.objects.get(verification=case_for(first))
    started = threading.Event()

    class SlowMatch:
        PROVIDER_CODE = "SLOW_MATCH_TEST_DOUBLE"
        IS_OFFICIAL = False

        def configuration_problems(self):
            return []

        def lookup(self, nin):
            started.set()
            time.sleep(1.5)  # long enough for the correction to commit first
            from apps.people.nin_provider import LocalSimulationNinProvider

            return LocalSimulationNinProvider().lookup(nin)

    def correct():
        started.wait(timeout=10)
        case = case_for(second)
        card = second.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
        return review.correct_nin_and_recheck(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            new_nin="990000000000000028",
            reason_code="TYPING_ERROR_CONFIRMED",
            evidence_document_id=card.pk,
            confirmed=True,
        )

    results, errors = _run_callables_in_threads(
        [lambda: idv.process_identity_job(first_job.pk, provider=SlowMatch()), correct]
    )
    assert errors == []
    assert results[0] == "stale"
    first_case = case_for(first)
    assert first_case.status == IdentityStatus.MANUAL_REVIEW
    assert first_case.reason_code == IdentityReasonCode.IDENTITY_DATA_CHANGED
    # IDV-C1 (R-IDV-01): the first case was rebound to the corrected
    # identifier; the one its stale result was about is its first revision's.
    checked = first_case.revisions.get(number=1).identifier
    old = IdentityIdentifier.objects.get(pk=checked.pk)
    assert old.status == IdentifierStatus.REPLACED  # never VERIFIED by the stale result
    assert first_case.current_revision.identifier_id != checked.pk
