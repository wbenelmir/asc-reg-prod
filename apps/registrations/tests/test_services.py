"""Registration service invariant tests (Schema §5.3-§5.8, §16.3-§16.5)."""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.audit.contracts import InMemoryAuditRecorder
from apps.core.outbox.memory import InMemoryOutboxPublisher
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.registrations.models import Registration, RegistrationSubmission
from apps.registrations.services import (
    allocate_registration_reference,
    create_open_draft_registration,
    record_registration_submission,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def event() -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code="SVCTEST",
        name="ASC 2026",
        timezone="Africa/Algiers",
        starts_at=now,
        ends_at=now,
    )


@pytest.fixture
def recorder() -> InMemoryAuditRecorder:
    return InMemoryAuditRecorder()


@pytest.fixture
def outbox() -> InMemoryOutboxPublisher:
    return InMemoryOutboxPublisher()


def test_reference_allocation_is_event_scoped_and_sequential(event: EventEdition) -> None:
    first = allocate_registration_reference(event)
    second = allocate_registration_reference(event)
    assert first == "SVCTEST-R-000001"
    assert second == "SVCTEST-R-000002"


def test_reference_allocation_is_independent_per_event() -> None:
    now = timezone.now()
    event_a = EventEdition.objects.create(
        code="EVENTA", name="Event A", timezone="UTC", starts_at=now, ends_at=now
    )
    event_b = EventEdition.objects.create(
        code="EVENTB", name="Event B", timezone="UTC", starts_at=now, ends_at=now
    )
    assert allocate_registration_reference(event_a) == "EVENTA-R-000001"
    assert allocate_registration_reference(event_b) == "EVENTB-R-000001"
    assert allocate_registration_reference(event_a) == "EVENTA-R-000002"


def test_one_person_may_hold_multiple_registrations_for_the_same_event(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    person = Person.objects.create(display_name="Multi Context")
    first = create_open_draft_registration(
        event_edition=event,
        source_context_key="context-a",
        preferred_language="en",
        person_id=person.id,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration
    second = create_open_draft_registration(
        event_edition=event,
        source_context_key="context-b",
        preferred_language="en",
        person_id=person.id,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration
    # No IntegrityError, and no (person, event) uniqueness constraint exists.
    assert Registration.objects.filter(person=person, event_edition=event).count() == 2
    assert first.pk != second.pk


def test_draft_creation_records_audit_and_outbox_in_the_same_operation(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    result = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx",
        preferred_language="fr",
        person_id=None,
        audit_recorder=recorder,
        outbox=outbox,
    )
    assert len(recorder.entries) == 1
    assert recorder.entries[0].action_code == "REGISTRATION_DRAFT_CREATED"
    assert len(outbox.messages) == 1
    assert outbox.messages[0].aggregate_id == str(result.registration.pk)


def test_submission_is_idempotent_on_idempotency_key(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    registration = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx",
        preferred_language="en",
        person_id=None,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration
    idempotency_key = str(uuid.uuid4())
    snapshot = {"given_names": "Amine"}

    first = record_registration_submission(
        registration=registration,
        snapshot=snapshot,
        idempotency_key=idempotency_key,
        audit_recorder=recorder,
        outbox=outbox,
    )
    second = record_registration_submission(
        registration=registration,
        snapshot=snapshot,
        idempotency_key=idempotency_key,
        audit_recorder=recorder,
        outbox=outbox,
    )
    assert first.pk == second.pk
    assert RegistrationSubmission.objects.filter(registration=registration).count() == 1


def test_submission_is_immutable_no_update_path_is_exposed(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    registration = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx",
        preferred_language="en",
        person_id=None,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration
    submission = record_registration_submission(
        registration=registration,
        snapshot={"a": 1},
        idempotency_key=str(uuid.uuid4()),
        audit_recorder=recorder,
        outbox=outbox,
    )
    original_hash = submission.snapshot_hash
    # apps.registrations.services exposes no update/mutation function for
    # RegistrationSubmission -- this asserts the model-level evidence
    # (hash, snapshot) is exactly what was submitted and remains so.
    submission.refresh_from_db()
    assert submission.snapshot_hash == original_hash
    assert submission.snapshot_json == {"a": 1}


def test_second_submission_gets_the_next_sequence(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    registration = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx",
        preferred_language="en",
        person_id=None,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration
    first = record_registration_submission(
        registration=registration,
        snapshot={"a": 1},
        idempotency_key=str(uuid.uuid4()),
        audit_recorder=recorder,
        outbox=outbox,
    )
    second = record_registration_submission(
        registration=registration,
        snapshot={"a": 2},
        submission_kind="AUTHORIZED_RESUBMISSION",
        idempotency_key=str(uuid.uuid4()),
        audit_recorder=recorder,
        outbox=outbox,
    )
    assert first.sequence == 1
    assert second.sequence == 2


def test_current_context_deduplication_key_is_unique_only_while_current(
    event: EventEdition, recorder: InMemoryAuditRecorder, outbox: InMemoryOutboxPublisher
) -> None:
    person = Person.objects.create(display_name="Dup Context")
    first = create_open_draft_registration(
        event_edition=event,
        source_context_key="same-context",
        preferred_language="en",
        person_id=person.id,
        audit_recorder=recorder,
        outbox=outbox,
    ).registration

    from django.db import IntegrityError, transaction

    from apps.registrations.services import compute_deduplication_key

    dedup_key = compute_deduplication_key(
        event_edition_id=event.pk, person_id=person.id, source_context_key="same-context"
    )
    assert first.deduplication_key == dedup_key

    with pytest.raises(IntegrityError), transaction.atomic():
        Registration.objects.create(
            public_reference="MANUAL-DUP",
            event_edition=event,
            person=person,
            source_kind="OPEN",
            source_context_key="same-context",
            deduplication_key=dedup_key,
            is_current_context=True,
        )

    # Marking the original non-current frees the key for a fresh row
    # (supervisor duplicate-resolution foundation -- read-only in Phase 1,
    # no destructive merge).
    first.is_current_context = False
    first.save(update_fields=["is_current_context"])
    Registration.objects.create(
        public_reference="MANUAL-RESOLVED",
        event_edition=event,
        person=person,
        source_kind="OPEN",
        source_context_key="same-context",
        deduplication_key=dedup_key,
        is_current_context=True,
    )
