"""Reference-value and OutboxEvent model tests (Schema §2.1, §16.5)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.core.models import Country, OutboxEvent, OutboxEventStatus, Sector

pytestmark = pytest.mark.django_db


def test_country_reference_is_keyed_by_stable_code() -> None:
    # A user-assigned code: the approved catalog (core.0005) already holds DZ.
    country = Country.objects.create(code="ZZ", name="Synthetic", name_fr="Synthétique")
    assert Country.objects.get(pk="ZZ") == country


def test_sector_reference_is_keyed_by_stable_code() -> None:
    sector = Sector.objects.create(code="TECH", name="Technology")
    assert Sector.objects.get(pk="TECH") == sector


def test_outbox_event_defaults_to_pending_status() -> None:
    event = OutboxEvent.objects.create(
        event_type="test.event",
        aggregate_type="Test",
        aggregate_id="1",
        occurred_at=timezone.now(),
    )
    assert event.status == OutboxEventStatus.PENDING
    assert event.attempts == 0
    assert event.published_at is None
