"""Attendance enforcement keeps every activation period (R03).

Each activation opens a durable interval and each deactivation closes it, so
an operation is judged by the period that covered its time even after later
switches. The migration that introduced the intervals rebuilds what the
policy row still knew, without inventing lost periods. Synthetic data,
PostgreSQL."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.accreditation import attendance
from apps.accreditation.models import (
    AttendanceEnforcementInterval,
    AttendanceEnforcementIntervalSource,
    AttendancePolicy,
)
from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.audit import action_codes
from apps.audit.models import AuditEvent

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db

DAY = datetime(2026, 12, 5, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


@pytest.fixture
def policy_manager(event):
    return make_operational_user_with_membership(
        email="history-policy@example.test",
        group_name="Attendance Policy Managers",
        event_edition=event,
    )


@pytest.fixture
def switch(event, policy_manager, monkeypatch):
    """Activate or deactivate through the service at a chosen moment."""

    def _switch(on: bool, moment: datetime):
        monkeypatch.setattr(timezone, "now", lambda: moment)
        policy = attendance.policy_for(event.pk)
        if on:
            return attendance.activate_enforcement(
                event_edition=event, actor=policy_manager, expected_version=policy.version
            )
        return attendance.deactivate_enforcement(
            event_edition=event,
            actor=policy_manager,
            expected_version=policy.version,
            reason="History test",
        )

    return _switch


def _active(event, moment) -> bool:
    return attendance.enforcement_active_at(attendance.policy_for(event.pk), moment)


def test_every_cycle_keeps_its_own_period(event, switch):
    configure_attendance(event, capacity=10)
    switch(True, at(8))
    switch(False, at(10))
    switch(True, at(11))
    # The operation at 09:00 stays enforced after the reactivation.
    assert _active(event, at(9)) is True
    assert _active(event, at(10, 30)) is False
    assert _active(event, at(7, 59)) is False
    switch(False, at(12))
    switch(True, at(13))
    assert [_active(event, at(h)) for h in (7, 8, 9, 10, 11, 12, 13, 14)] == [
        False, True, True, False, True, False, True, True
    ]  # fmt: skip
    intervals = list(
        AttendanceEnforcementInterval.objects.filter(policy__event_edition=event).values_list(
            "started_at", "ended_at", "source"
        )
    )
    assert intervals == [
        (at(8), at(10), AttendanceEnforcementIntervalSource.RECORDED),
        (at(11), at(12), AttendanceEnforcementIntervalSource.RECORDED),
        (at(13), None, AttendanceEnforcementIntervalSource.RECORDED),
    ]


def test_the_boundaries_are_start_included_end_excluded(event, switch):
    configure_attendance(event, capacity=10)
    switch(True, at(8))
    switch(False, at(10))
    micro = timedelta(microseconds=1)
    assert _active(event, at(8) - micro) is False
    assert _active(event, at(8)) is True
    assert _active(event, at(10) - micro) is True
    assert _active(event, at(10)) is False


def test_the_current_switch_and_the_open_interval_agree(event, switch):
    configure_attendance(event, capacity=10)
    activated = switch(True, at(8))
    assert activated.enforcement_active
    assert activated.enforcement_intervals.get(ended_at__isnull=True).started_at == at(8)
    deactivated = switch(False, at(9))
    assert not deactivated.enforcement_active
    assert not deactivated.enforcement_intervals.filter(ended_at__isnull=True).exists()
    # Activation and deactivation stay audited as before.
    assert (
        AuditEvent.objects.filter(
            action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATED, target_uuid=event.pk
        ).count()
        == 1
    )


def test_a_second_open_interval_is_impossible(event, switch):
    from django.db import IntegrityError, transaction

    configure_attendance(event, capacity=10)
    policy = switch(True, at(8))
    with pytest.raises(IntegrityError), transaction.atomic():
        AttendanceEnforcementInterval.objects.create(policy=policy, started_at=at(9))


def test_without_any_activation_nothing_is_enforced(event):
    configure_attendance(event, capacity=10)
    assert _active(event, at(9)) is False
    assert attendance.enforcement_active_at(None, at(9)) is False


# ---------------------------------------------------------------------------
# The migration that introduced the intervals
# ---------------------------------------------------------------------------

_migration = importlib.import_module(
    "apps.accreditation.migrations.0007_attendance_enforcement_intervals"
)


def _legacy_policy(event, *, active, activated, deactivated=None, created=None, activations=0):
    """A policy as the earlier release left it: switch fields only, no intervals."""
    policy = configure_attendance(event, capacity=10)
    AttendancePolicy.objects.filter(pk=policy.pk).update(
        enforcement_active=active,
        enforcement_activated_at=activated,
        enforcement_deactivated_at=deactivated,
        created_at=created or at(1),
    )
    AttendanceEnforcementInterval.objects.filter(policy=policy).delete()
    for _ in range(activations):
        AuditEvent.objects.create(
            occurred_at=activated,
            actor_type="SYSTEM",
            action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATED,
            target_type="EventEdition",
            target_uuid=event.pk,
            event_edition=event,
            result="SUCCESS",
            correlation_id="history-test",
        )
    return policy


def _rebuilt(policy):
    _migration.rebuild_intervals(django_apps, None)
    return list(
        AttendanceEnforcementInterval.objects.filter(policy=policy).values_list(
            "started_at", "ended_at", "source"
        )
    )


def test_migration_keeps_a_single_open_activation_as_complete_history(event):
    policy = _legacy_policy(event, active=True, activated=at(8), activations=1)
    assert _rebuilt(policy) == [(at(8), None, "RECONSTRUCTED")]
    assert _active(event, at(7)) is False
    assert _active(event, at(9)) is True


def test_migration_keeps_one_closed_cycle_as_complete_history(event):
    policy = _legacy_policy(event, active=False, activated=at(8), deactivated=at(10), activations=1)
    assert _rebuilt(policy) == [(at(8), at(10), "RECONSTRUCTED")]
    assert _active(event, at(7)) is False


def test_migration_marks_overwritten_cycles_uncertain_instead_of_inventing_them(event):
    # Activated, deactivated at 10:00, reactivated at 11:00: the start of the
    # first period was overwritten.
    policy = _legacy_policy(event, active=True, activated=at(11), deactivated=at(10), activations=2)
    assert _rebuilt(policy) == [(at(1), at(10), "UNCERTAIN"), (at(11), None, "RECONSTRUCTED")]
    assert _active(event, at(9)) is True  # unknown history: judged by the stricter rule
    assert _active(event, at(10, 30)) is False  # known to be off
    assert _active(event, at(12)) is True


def test_migration_treats_unknown_cycle_counts_conservatively(event):
    policy = _legacy_policy(event, active=False, activated=at(8), deactivated=at(10), activations=3)
    assert _rebuilt(policy) == [(at(1), at(8), "UNCERTAIN"), (at(8), at(10), "RECONSTRUCTED")]


def test_migration_ignores_a_policy_that_was_never_activated(event):
    policy = configure_attendance(event, capacity=10)
    assert _rebuilt(policy) == []


def test_an_activation_without_a_period_still_enforces_and_can_be_switched_off(event, switch):
    """A switch-on recorded by a release that predates the periods (a code
    rollback): enforced from that activation, and the switch-off records the
    period instead of refusing."""
    configure_attendance(event, capacity=10)
    AttendancePolicy.objects.filter(event_edition=event).update(
        enforcement_active=True, enforcement_activated_at=at(8)
    )
    assert _active(event, at(7)) is False
    assert _active(event, at(9)) is True
    switch(False, at(10))
    assert _active(event, at(9)) is True
    assert _active(event, at(10, 30)) is False
    assert list(
        AttendanceEnforcementInterval.objects.filter(policy__event_edition=event).values_list(
            "started_at", "ended_at", "source"
        )
    ) == [(at(8), at(10), AttendanceEnforcementIntervalSource.RECONSTRUCTED)]
