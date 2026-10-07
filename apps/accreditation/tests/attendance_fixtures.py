"""Test-only helpers for attendance entitlements (synthetic dates and capacity).

The dates below are test fixtures only. Real conference days and the
opening-day capacity are configured by an authorized operator.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone

from apps.accreditation.models import AttendancePolicy

#: A synthetic opening day used by tests that do not care about the date.
TEST_OPENING_DAY = date(2026, 12, 5)


def configure_attendance(
    event,
    *,
    capacity: int | None = 100,
    opening: date = TEST_OPENING_DAY,
    active: bool = False,
    activated_at=None,
) -> AttendancePolicy:
    """Set the three consecutive days and the capacity directly (no audit);
    `active=True` also switches enforcement on, as if activated just now."""
    values = {
        "opening_date": opening,
        "second_date": opening + timedelta(days=1),
        "third_date": opening + timedelta(days=2),
        "opening_day_capacity": capacity,
    }
    if active:
        values |= {
            "enforcement_active": True,
            "enforcement_activated_at": activated_at or timezone.now() - timedelta(days=30),
        }
    policy, _created = AttendancePolicy.objects.update_or_create(
        event_edition=event, defaults=values
    )
    if active and not policy.enforcement_intervals.filter(ended_at__isnull=True).exists():
        policy.enforcement_intervals.create(started_at=values["enforcement_activated_at"])
    return policy


def grant_entitlement(registration, category: str, *, actor, effective_from=None):
    """Record a CURRENT entitlement directly (for fixtures that build an
    APPROVED registration without the approval service)."""
    from apps.accreditation.models import (
        AttendanceEntitlement,
        AttendanceEntitlementOrigin,
        AttendanceEntitlementStatus,
    )

    now = timezone.now()
    previous = AttendanceEntitlement.objects.filter(
        registration=registration, status=AttendanceEntitlementStatus.CURRENT
    ).first()
    if previous is not None:
        previous.status = AttendanceEntitlementStatus.SUPERSEDED
        previous.effective_until = now
        previous.save(update_fields=["status", "effective_until", "updated_at"])
    return AttendanceEntitlement.objects.create(
        registration=registration,
        event_edition_id=registration.event_edition_id,
        category=category,
        status=AttendanceEntitlementStatus.CURRENT,
        origin=AttendanceEntitlementOrigin.LEGACY_CLASSIFICATION,
        effective_from=effective_from or now - timedelta(days=60),
        decided_by=actor,
        supersedes=previous,
    )


def local_moment(event, day: date, hour: int = 12, minute: int = 0):
    """An aware datetime at `day` `hour:minute` in the event timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(event.timezone))
