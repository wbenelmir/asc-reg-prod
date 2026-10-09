"""Real multi-connection PostgreSQL concurrency for the review queue entry and
the decision workbook (`apps.reviews.intake`, `apps.reviews.workbook`).

Threads on their OWN connections, released together by a barrier (the
pattern of `test_review_decision_and_response_concurrency.py`). Proven here:

1. two simultaneous entries of the same verified registration: one case;
2. the backfill racing a manual identity verification of the same
   registration: one case, never a second;
3. a double click on "Validate and apply decisions": one application, one
   decision per row;
4. a batch application racing an individual decision on one of its rows and
   an individual opening-day approval outside it (attendance policy lock):
   no deadlock, never two decisions for a registration, all or nothing.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import threading

import pytest
from django.db import connection
from django.utils import timezone

from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews import intake, workbook
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME
from apps.reviews.models import RegistrationDecision, ReviewCase, ReviewDecisionImport
from apps.reviews.tests.conftest import make_operational_user_with_membership
from apps.reviews.tests.test_decision_workbook import (
    REVIEW_DECISION_WORKBOOK_GROUP_NAME,
    _export,
    _grant,
    _preview,
    actionable,
    edit,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run(callables: list) -> tuple[list, list[BaseException]]:
    barrier = threading.Barrier(len(callables))
    results: list = [None] * len(callables)
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(len(callables))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results, errors


@pytest.fixture
def world():
    from apps.core.models import Country, Sector
    from apps.events.models import EventEdition

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    event = EventEdition.objects.create(
        code="RQCONC",
        name="Review queue concurrency",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    operator = make_operational_user_with_membership(
        email="rq-conc-operator@example.test",
        group_name=ACCREDITATION_MANAGERS_GROUP_NAME,
        event_edition=event,
    )
    _grant(operator, REVIEW_DECISION_WORKBOOK_GROUP_NAME, event=event)
    configure_attendance(event, capacity=10)
    return event, operator


def test_simultaneous_entries_create_one_case(world) -> None:
    event, operator = world
    registration = actionable(event, operator, enqueue=False)
    results, errors = _run(
        [
            lambda: intake.enqueue_for_participation_review(
                registration.pk, trigger=intake.EntryTrigger.BACKFILL
            )
        ]
        * 2
    )
    assert errors == []
    assert sorted(result.outcome for result in results) == [
        intake.EntryOutcome.CREATED,
        intake.EntryOutcome.EXISTING_CASE,
    ]
    assert ReviewCase.objects.filter(registration=registration).count() == 1
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW


def test_backfill_racing_a_manual_verification_creates_one_case(world) -> None:
    from apps.people.models import IdentityStatus, IdentityVerification
    from apps.people.services import identity_review as review
    from apps.people.tests.conftest import case_for, make_staff, submit_case
    from apps.privacy.models import LegalDocument, LegalDocumentVersion

    event, _operator = world
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"rq-{code}",
                content="Synthetic",
                content_hash="r" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status="PUBLISHED",
            )
        )
    registration = submit_case(event, tuple(versions), nationality="FR", passport_number="RQ000001")
    reviewer = make_staff("rq-conc-reviewer@example.test", "Registration Reviewers", event=event)
    case = case_for(registration)
    page = registration.documents.get(document_type="PASSPORT_IDENTITY_PAGE", status="ACTIVE")
    assert case.status == IdentityStatus.MANUAL_REVIEW
    results, errors = _run(
        [
            lambda: review.verify_identity_manually(
                case.pk,
                actor=reviewer,
                expected_version=case.version,
                evidence_document_id=page.pk,
                reason_code="DOCUMENT_MATCHES_SUBMISSION",
            ),
            lambda: intake.run_backfill(event_edition=event, apply=True),
        ]
    )
    assert errors == []
    assert IdentityVerification.objects.get(registration=registration).status == (
        IdentityStatus.MANUALLY_VERIFIED
    )
    assert ReviewCase.objects.filter(registration=registration).count() == 1
    # Whatever the order, a later backfill finds nothing more to do.
    again = intake.run_backfill(event_edition=event, apply=True)
    assert again.created == 0 and again.transitioned == 0


def test_a_double_click_applies_the_batch_once(world) -> None:
    event, operator = world
    first, second = actionable(event, operator), actionable(event, operator)
    _export_record, content = _export(operator)
    preview = _preview(
        operator,
        edit(
            content,
            {
                first.public_reference: {"decision": "ACCEPT", "profile": "FOLLOWING_TWO_DAYS"},
                second.public_reference: {"decision": "REJECT", "reason": "NOT_ELIGIBLE"},
            },
        ),
    )
    results, errors = _run(
        [lambda: workbook.apply_decision_import(decision_import_id=preview.pk, user=operator)] * 2
    )
    assert errors == []
    assert all(result.status == "APPLIED" for result in results)
    assert RegistrationDecision.objects.filter(registration__in=[first, second]).count() == 2
    assert ReviewDecisionImport.objects.get(pk=preview.pk).counts["applied"] == 2


def test_a_batch_racing_individual_decisions_never_deadlocks_or_doubles(world) -> None:
    from apps.accreditation.attendance import AttendanceError
    from apps.reviews.services import (
        InvalidStateTransitionError,
        StaleVersionError,
        record_approved_decision,
    )

    event, operator = world
    first, second, outside = (actionable(event, operator) for _ in range(3))
    _export_record, content = _export(operator, ReviewCase.objects.exclude(registration=outside))
    preview = _preview(
        operator,
        edit(
            content,
            {
                first.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
                second.public_reference: {"decision": "ACCEPT", "profile": "ALL_DAYS"},
            },
        ),
    )
    assert workbook.is_applicable(preview)

    def individual(registration):
        def _decide():
            fresh = Registration.objects.get(pk=registration.pk)
            return record_approved_decision(
                registration=fresh,
                expected_version=fresh.version,
                decided_by=operator,
                attendance_category="ALL_CONFERENCE_DAYS",
            )

        return _decide

    results, errors = _run(
        [
            lambda: workbook.apply_decision_import(decision_import_id=preview.pk, user=operator),
            individual(first),
            individual(outside),
        ]
    )
    expected = (
        workbook.ApplyRefused,
        StaleVersionError,
        InvalidStateTransitionError,
        AttendanceError,
    )
    assert all(isinstance(error, expected) for error in errors), errors
    # Never two current decisions for a registration; all or nothing for the batch.
    for registration in (first, second, outside):
        assert (
            RegistrationDecision.objects.filter(registration=registration, is_current=True).count()
            <= 1
        )
        assert RegistrationDecision.objects.filter(registration=registration).count() <= 1
    batch = ReviewDecisionImport.objects.get(pk=preview.pk)
    second_decided = RegistrationDecision.objects.filter(registration=second).exists()
    if batch.status == "APPLIED":
        assert second_decided
    else:
        assert not second_decided
    assert RegistrationDecision.objects.filter(registration=outside).count() == 1
