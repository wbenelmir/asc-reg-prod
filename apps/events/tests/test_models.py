"""EventEdition invariant tests (Schema §5.1)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.events.models import EventEdition

pytestmark = pytest.mark.django_db


def test_event_edition_code_is_unique() -> None:
    now = timezone.now()
    EventEdition.objects.create(
        code="DUP", name="First", timezone="UTC", starts_at=now, ends_at=now
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        EventEdition.objects.create(
            code="DUP", name="Second", timezone="UTC", starts_at=now, ends_at=now
        )


def test_ends_at_must_not_precede_starts_at() -> None:
    now = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        EventEdition.objects.create(
            code="BADRANGE",
            name="Bad range",
            timezone="UTC",
            starts_at=now,
            ends_at=now - timedelta(days=1),
        )


def test_next_registration_sequence_defaults_to_one() -> None:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="SEQDEFAULT", name="Seq", timezone="UTC", starts_at=now, ends_at=now
    )
    assert event.next_registration_sequence == 1
