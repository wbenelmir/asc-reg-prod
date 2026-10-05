"""`current_event_edition` fail-closed selection tests (Prompt 4 final closure pass §10)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.events.models import EventEdition, EventEditionStatus
from apps.events.selectors import (
    MultipleOpenEventEditions,
    NoOpenEventEdition,
    current_event_edition,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _close_the_seed_migration_edition():
    """`events.0002` seeds one open `EventEdition(code="ASC2026")` -- these tests need
    exact control over how many editions are open, so close the seeded one first
    rather than assuming an empty table."""
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )


def _make_edition(code: str, status: str) -> EventEdition:
    return EventEdition.objects.create(
        code=code,
        name=code,
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status=status,
    )


def test_fails_closed_when_no_edition_is_open() -> None:
    with pytest.raises(NoOpenEventEdition):
        current_event_edition()


def test_returns_the_single_open_edition() -> None:
    edition = _make_edition("SOLO", EventEditionStatus.REGISTRATION_OPEN)
    assert current_event_edition().pk == edition.pk


def test_fails_closed_when_more_than_one_edition_is_open_never_silently_picks_first() -> None:
    _make_edition("FIRSTOPEN", EventEditionStatus.REGISTRATION_OPEN)
    _make_edition("SECONDOPEN", EventEditionStatus.REGISTRATION_OPEN)
    with pytest.raises(MultipleOpenEventEditions):
        current_event_edition()


def test_multiple_open_editions_still_fails_closed_for_a_no_open_edition_catch() -> None:
    _make_edition("A", EventEditionStatus.REGISTRATION_OPEN)
    _make_edition("B", EventEditionStatus.REGISTRATION_OPEN)
    with pytest.raises(NoOpenEventEdition):
        current_event_edition()


def test_a_closed_edition_never_counts_as_open() -> None:
    _make_edition("CLOSED", EventEditionStatus.REGISTRATION_CLOSED)
    with pytest.raises(NoOpenEventEdition):
        current_event_edition()
