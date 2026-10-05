"""PostgreSQL-level backstop for "at most one INITIAL `RegistrationSubmission`
per Registration" (migration `0004_registrationsubmission_reg_submission_one_initial_uq`,
Prompt 6 P6-H-02 correction).

Exercises the constraint directly at the model layer, independent of the
service-layer lock-and-recheck dedup in `record_registration_submission` --
this is the database-level invariant that backs it up even if application
code were ever bypassed.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.people.services import resolve_or_create_participant_for_email
from apps.registrations.models import (
    Registration,
    RegistrationSubmission,
    RegistrationSubmissionKind,
)
from apps.registrations.services import get_or_create_active_draft

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="MIGCONS",
        name="Migration Constraint Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def registration(event: EventEdition) -> Registration:
    person = resolve_or_create_participant_for_email("migration-constraint@example.com")
    return get_or_create_active_draft(person=person, event_edition=event)


def _make_submission(
    registration: Registration, *, submission_kind: str, sequence: int
) -> RegistrationSubmission:
    return RegistrationSubmission.objects.create(
        registration=registration,
        sequence=sequence,
        submission_kind=submission_kind,
        snapshot_json={},
        snapshot_hash="a" * 64,
        submitted_by_type="PARTICIPANT",
        submitted_at=timezone.now(),
        idempotency_key=str(uuid.uuid4()),
    )


def test_a_second_initial_submission_for_the_same_registration_is_rejected_by_postgresql(
    registration: Registration,
) -> None:
    _make_submission(registration, submission_kind=RegistrationSubmissionKind.INITIAL, sequence=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_submission(
            registration, submission_kind=RegistrationSubmissionKind.INITIAL, sequence=2
        )
    # The rejected attempt created no row.
    assert (
        RegistrationSubmission.objects.filter(
            registration=registration, submission_kind=RegistrationSubmissionKind.INITIAL
        ).count()
        == 1
    )


def test_a_later_non_initial_submission_remains_allowed(registration: Registration) -> None:
    _make_submission(registration, submission_kind=RegistrationSubmissionKind.INITIAL, sequence=1)
    later = _make_submission(
        registration,
        submission_kind=RegistrationSubmissionKind.ADDITIONAL_INFORMATION_RESPONSE,
        sequence=2,
    )
    assert later.pk is not None
    assert RegistrationSubmission.objects.filter(registration=registration).count() == 2


def test_the_constraint_is_scoped_per_registration_not_global(
    registration: Registration, event: EventEdition
) -> None:
    other_person = resolve_or_create_participant_for_email("migration-constraint-2@example.com")
    other_registration = get_or_create_active_draft(person=other_person, event_edition=event)

    _make_submission(registration, submission_kind=RegistrationSubmissionKind.INITIAL, sequence=1)
    other = _make_submission(
        other_registration, submission_kind=RegistrationSubmissionKind.INITIAL, sequence=1
    )
    assert other.pk is not None
