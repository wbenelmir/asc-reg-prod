"""Real multi-connection PostgreSQL concurrency test for the registration
submission idempotency fix (Prompt 6 P6-H-02).

Reproduces the ORIGINAL defect directly: two real threads, each opening its
OWN PostgreSQL connection, call `submit_full_registration` for the SAME
complete Draft with two DIFFERENT synthetic idempotency keys -- exactly
what the pre-fix session-key race (two near-simultaneous requests sharing
one browser session, each reading the session before either write
committed) could produce. Both must converge on the SAME INITIAL
submission, and the final database state must contain exactly one of every
required evidence row -- never a mock, never a sequential call.

Marked `concurrency`, matching `pyproject.toml`'s definition: "real
multi-connection PostgreSQL concurrency tests (not Redis integration)".
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.models import Country, OutboxEvent, Sector
from apps.events.models import EventEdition
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    AcceptanceRecord,
    ConsentPurpose,
    ConsentRecord,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import (
    Registration,
    RegistrationPublicStatus,
    RegistrationSubmission,
    RegistrationSubmissionKind,
)
from apps.registrations.services import get_or_create_active_draft, submit_full_registration
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    """Run each of `callables` in its own real thread, all released from one
    `threading.Barrier` so they genuinely overlap in time (matches the
    established pattern in `tests/concurrency/test_otp_advisory_locks.py`).
    """
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def _build_complete_draft() -> Registration:
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    event = EventEdition.objects.create(
        code="CONCSUB",
        name="Concurrent Submission Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    person = resolve_or_create_participant_for_email("concurrent-submit@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    return draft


def test_concurrent_submissions_with_different_keys_converge_on_one_result() -> None:
    draft = _build_complete_draft()

    privacy_doc, _ = LegalDocument.objects.get_or_create(
        code="PRIVACY_NOTICE", defaults={"document_type": "PRIVACY_NOTICE"}
    )
    terms_doc, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": "TERMS"}
    )
    privacy_version = LegalDocumentVersion.objects.create(
        legal_document=privacy_doc,
        language="en",
        version_label="concurrency-v1",
        content="Privacy text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_version = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="concurrency-v1",
        content="Terms text",
        content_hash="b" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    purpose, _ = ConsentPurpose.objects.get_or_create(
        code="MARKETING", defaults={"name": "Marketing"}
    )

    # Two DIFFERENT synthetic idempotency keys -- exactly what the pre-fix
    # session-key race could produce for two near-simultaneous requests
    # sharing one browser session (Prompt 6 P6-H-02).
    keys = [f"concurrent-key-{index}-{uuid.uuid4()}" for index in range(2)]

    def _make_submit(key: str):
        def _submit():
            return submit_full_registration(
                registration=draft,
                privacy_notice_version=privacy_version,
                terms_version=terms_version,
                data_processing_consent_granted=True,
                marketing_consent_purpose=purpose,
                marketing_consent_granted=True,
                session_reference="concurrency-test",
                idempotency_key=key,
            )

        return _submit

    results, errors = _run_callables_in_threads([_make_submit(key) for key in keys])

    assert not errors, f"unexpected error(s) (IntegrityError, deadlock, or otherwise): {errors}"
    assert all(result is not None for result in results)
    assert results[0].pk == results[1].pk

    draft.refresh_from_db()
    assert draft.public_status == RegistrationPublicStatus.SUBMITTED

    assert (
        RegistrationSubmission.objects.filter(
            registration=draft, submission_kind=RegistrationSubmissionKind.INITIAL
        ).count()
        == 1
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=privacy_version
        ).count()
        == 1
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=terms_version
        ).count()
        == 1
    )
    assert ConsentRecord.objects.filter(person=draft.person, purpose=purpose).count() == 1
    assert (
        AuditEvent.objects.filter(
            action_code="REGISTRATION_SUBMISSION_RECORDED", target_uuid=draft.pk, result="SUCCESS"
        ).count()
        == 1
    )
    assert (
        OutboxEvent.objects.filter(
            event_type="registration.submission_recorded", aggregate_id=str(draft.pk)
        ).count()
        == 1
    )
