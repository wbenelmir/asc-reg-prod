"""A first "two following days" grant racing a change of the conference days (R06).

The days are frozen once any entitlement exists. Every first grant -- an
approval or the classification of an earlier approval, whatever the category
-- locks the attendance policy after the Registration, so it serializes with
`update_policy`. Only two outcomes are valid:

* the grant commits first: the change of days is refused (`DAYS_LOCKED`)
  and the participant was told the unchanged days;
* the change of days commits first: the grant reads and communicates the
  new days.

Never: new days stored while the participant was told the old ones.

Each test forces one order on real PostgreSQL connections: the first party
is paused INSIDE its transaction (holding its locks) and released once
PostgreSQL reports the second party waiting for a lock, or after a bounded
wait if it never waits (which is how the earlier, unserialized code
behaves). The final assertions decide. Synthetic data; test outbox only.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import connection, connections
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accreditation import attendance
from apps.accreditation.models import AttendanceCategory, AttendanceEntitlement, AttendancePolicy
from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.accreditation.tests.conftest import make_registration
from apps.events.models import EventEdition
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.services import record_approved_decision

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS
OLD = date(2026, 12, 5)
NEW = date(2026, 12, 12)
WAIT = 15
NO_WAITER_GRACE = 4


def _days(opening):
    return (opening, opening + timedelta(days=1), opening + timedelta(days=2))


def _wait_for_lock_waiter(timeout) -> bool:
    deadline = time.monotonic() + timeout
    with connections["default"].cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.02)
    return False


def _thread(target, results, name):
    def run():
        try:
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            results[name] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


@pytest.fixture
def world():
    from apps.core.models import Country, Sector

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"DAT{uuid.uuid4().hex[:6].upper()}",
        name="Attendance date races",
        timezone="UTC",
        starts_at=now,
        ends_at=now + timedelta(days=90),
        status="REGISTRATION_OPEN",
    )
    manager = OperationalUser.objects.create_user(
        email=f"date-race-{uuid.uuid4().hex[:6]}@example.test",
        password=None,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=manager,
        group=Group.objects.get(name="Accreditation Managers"),
        event_edition=event,
        granted_by=manager,
    )
    configure_attendance(event, capacity=10, opening=OLD)
    registration = make_registration(event=event)
    assign_approval_prerequisites(registration, manager)
    make_verified_identity_case(registration)
    registration.refresh_from_db()
    return event, manager, registration


@pytest.fixture
def hooks(monkeypatch):
    """Pause points and a record of the days each notification stated."""
    state = {"pause": None, "holding": threading.Event(), "release": threading.Event()}
    communicated: list[tuple] = []
    original_notify = attendance.queue_attendance_notification
    original_count = attendance.opening_allocation_count

    def maybe_pause():
        if threading.current_thread().name == state["pause"]:
            state["holding"].set()
            assert state["release"].wait(WAIT)

    def notify(*, policy, **kwargs):
        communicated.append(attendance.conference_days(policy))
        maybe_pause()
        return original_notify(policy=policy, **kwargs)

    def count(event_edition_id):
        maybe_pause()
        return original_count(event_edition_id)

    monkeypatch.setattr(attendance, "queue_attendance_notification", notify)
    monkeypatch.setattr(attendance, "opening_allocation_count", count)
    state["communicated"] = communicated
    return state


def _grant_call(kind, registration, manager):
    if kind == "approval":
        version = registration.version
        return lambda: record_approved_decision(
            registration=registration,
            expected_version=version,
            decided_by=manager,
            attendance_category=FOLLOWING,
        )
    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])  # an earlier, unclassified approval
    return lambda: attendance.change_entitlement(
        registration=registration,
        category=FOLLOWING,
        actor=manager,
        reason="Legacy classification",
        expected_entitlement_id="",
    )


def _edit_call(event, manager):
    return lambda: attendance.update_policy(
        event_edition_id=event.pk,
        actor=manager,
        opening_date=_days(NEW)[0],
        second_date=_days(NEW)[1],
        third_date=_days(NEW)[2],
        opening_day_capacity=10,
        expected_version=None,
        reason="Date correction",
    )


def _race(first, second, hooks):
    """Start `first` (paused inside its transaction), then `second`; release
    `first` once `second` waits for a lock (or after a bounded grace)."""
    results: dict = {}
    hooks["pause"] = first[0]
    first_thread = _thread(first[1], results, first[0])
    assert hooks["holding"].wait(WAIT), "the first party never reached its pause"
    second_thread = _thread(second[1], results, second[0])
    try:
        waited = _wait_for_lock_waiter(NO_WAITER_GRACE)
    finally:
        hooks["release"].set()
    for thread in (first_thread, second_thread):
        thread.join(60)
        assert not thread.is_alive()
    return results, waited


def _assert_valid_serial_outcome(event, registration, results, hooks):
    from django.db import OperationalError

    for value in results.values():
        assert not isinstance(value, OperationalError), value
    live = attendance.conference_days(AttendancePolicy.objects.get(event_edition=event))
    assert AttendanceEntitlement.objects.filter(registration=registration).count() == 1
    assert hooks["communicated"], "no notification stated the days"
    # The participant was told exactly the days now in force.
    assert set(hooks["communicated"]) == {live}
    edit = results["edit"]
    if isinstance(edit, BaseException):
        assert getattr(edit, "code", None) == "DAYS_LOCKED", edit
        assert live == _days(OLD)
    else:
        assert live == _days(NEW)
    return live


@pytest.mark.parametrize("kind", ["approval", "classification"])
def test_a_first_following_days_grant_that_locks_first_freezes_the_days(world, hooks, kind):
    event, manager, registration = world
    results, waited = _race(
        ("grant", _grant_call(kind, registration, manager)),
        ("edit", _edit_call(event, manager)),
        hooks,
    )
    assert not isinstance(results["grant"], BaseException), results["grant"]
    live = _assert_valid_serial_outcome(event, registration, results, hooks)
    assert live == _days(OLD)
    assert waited, "the change of days did not wait for the grant's policy lock"


@pytest.mark.parametrize("kind", ["approval", "classification"])
def test_a_change_of_days_that_locks_first_is_what_the_grant_communicates(world, hooks, kind):
    event, manager, registration = world
    results, waited = _race(
        ("edit", _edit_call(event, manager)),
        ("grant", _grant_call(kind, registration, manager)),
        hooks,
    )
    assert not isinstance(results["grant"], BaseException), results["grant"]
    live = _assert_valid_serial_outcome(event, registration, results, hooks)
    assert live == _days(NEW)
    assert waited, "the grant did not wait for the change of days"
