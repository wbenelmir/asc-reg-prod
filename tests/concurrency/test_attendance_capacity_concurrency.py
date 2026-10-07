"""Opening-day capacity under real multi-connection PostgreSQL races
(`apps.accreditation.attendance`).

Threads on their OWN PostgreSQL connections, released together by a barrier
(the pattern of `test_idv_q_owner_decisions_concurrency.py`). Whatever the
order the database chooses, each test proves the invariant, without a
deadlock:

1. N approvals competing for the last M places: exactly M opening-day
   approvals, every other one refused and not approved at all;
2. an upgrade racing an approval for the last place: exactly one wins;
3. a withdrawal racing an upgrade: never more places than the capacity;
4. two downgrades of one entitlement: one change, one stale refusal, the
   place released once;
5. a capacity reduction racing an approval: the allocation never exceeds
   the capacity that ends up stored.

Synthetic data only; mail is never sent (the test outbox only).
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accreditation import attendance
from apps.accreditation.models import (
    AttendanceCategory,
    AttendanceEntitlement,
    AttendanceEntitlementStatus,
)
from apps.accreditation.tests.attendance_fixtures import configure_attendance
from apps.accreditation.tests.conftest import make_registration
from apps.events.models import EventEdition
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.services import record_approved_decision, withdraw_registration

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

ALL = AttendanceCategory.ALL_CONFERENCE_DAYS
FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS


def _run(callables: list) -> tuple[list, list[BaseException]]:
    barrier = threading.Barrier(len(callables))
    results: list = [None] * len(callables)
    errors: list[BaseException] = []

    def _worker(index, target) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
            results[index] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i, c)) for i, c in enumerate(callables)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    return results, errors


def _no_deadlock(errors) -> None:
    from django.db import OperationalError

    assert not any(
        isinstance(error, OperationalError) or "deadlock" in str(error).lower() for error in errors
    ), errors


@pytest.fixture
def world():
    from apps.core.models import Country, Sector

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"ATT{uuid.uuid4().hex[:6].upper()}",
        name="Attendance concurrency",
        timezone="UTC",
        starts_at=now,
        ends_at=now + timedelta(days=30),
        status="REGISTRATION_OPEN",
    )
    manager = OperationalUser.objects.create_user(
        email=f"att-conc-{uuid.uuid4().hex[:6]}@example.test",
        password=None,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=manager,
        group=Group.objects.get(name="Accreditation Managers"),
        event_edition=event,
        granted_by=manager,
    )
    return event, manager


def _approvable(event, actor):
    registration = make_registration(event=event)
    assign_approval_prerequisites(registration, actor)
    make_verified_identity_case(registration)
    registration.refresh_from_db()
    return registration


def _approve_call(registration, actor, category):
    version = registration.version

    def call():
        return record_approved_decision(
            registration=registration,
            expected_version=version,
            decided_by=actor,
            attendance_category=category,
        )

    return call


def _change_call(registration, actor, category, expected):
    def call():
        return attendance.change_entitlement(
            registration=registration,
            category=category,
            actor=actor,
            reason="Concurrent change",
            expected_entitlement_id=expected,
        )

    return call


@pytest.mark.parametrize("callers, capacity", [(2, 1), (4, 1), (4, 2)])
def test_competing_approvals_never_exceed_the_opening_day_places(world, callers, capacity):
    event, manager = world
    configure_attendance(event, capacity=capacity)
    registrations = [_approvable(event, manager) for _ in range(callers)]
    results, errors = _run([_approve_call(r, manager, ALL) for r in registrations])
    _no_deadlock(errors)
    refused = [e for e in errors if isinstance(e, attendance.OpeningDayCapacityReachedError)]
    assert len(errors) == len(refused), errors
    assert len(refused) == callers - capacity
    assert attendance.opening_allocation_count(event.pk) == capacity
    approved = [r for r in registrations if _status(r) == RegistrationPublicStatus.APPROVED]
    assert len(approved) == capacity
    # A refused approval is not approved at all, and holds no entitlement.
    for registration in registrations:
        if registration not in approved:
            assert not AttendanceEntitlement.objects.filter(registration=registration).exists()


def test_an_upgrade_racing_an_approval_for_the_last_place(world):
    event, manager = world
    configure_attendance(event, capacity=1)
    waiting = _approvable(event, manager)
    _approve_call(waiting, manager, FOLLOWING)()
    newcomer = _approvable(event, manager)
    expected = str(attendance.current_entitlement(waiting.pk).pk)
    results, errors = _run(
        [_change_call(waiting, manager, ALL, expected), _approve_call(newcomer, manager, ALL)]
    )
    _no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], attendance.OpeningDayCapacityReachedError)
    assert attendance.opening_allocation_count(event.pk) == 1


def test_a_withdrawal_racing_an_upgrade_never_overallocates(world):
    event, manager = world
    configure_attendance(event, capacity=1)
    holder = _approvable(event, manager)
    _approve_call(holder, manager, ALL)()
    waiting = _approvable(event, manager)
    _approve_call(waiting, manager, FOLLOWING)()
    holder.refresh_from_db()
    expected = str(attendance.current_entitlement(waiting.pk).pk)

    def withdraw():
        return withdraw_registration(
            registration=holder, person=holder.person, expected_version=holder.version
        )

    results, errors = _run([withdraw, _change_call(waiting, manager, ALL, expected)])
    _no_deadlock(errors)
    assert all(isinstance(e, attendance.OpeningDayCapacityReachedError) for e in errors), errors
    assert attendance.opening_allocation_count(event.pk) <= 1
    if not errors:  # the upgrade saw the released place
        assert attendance.current_entitlement(waiting.pk).category == ALL
        assert _status(holder) == RegistrationPublicStatus.WITHDRAWN


def test_two_downgrades_of_one_entitlement_release_the_place_once(world):
    event, manager = world
    configure_attendance(event, capacity=1)
    holder = _approvable(event, manager)
    _approve_call(holder, manager, ALL)()
    expected = str(attendance.current_entitlement(holder.pk).pk)
    results, errors = _run(
        [
            _change_call(holder, manager, FOLLOWING, expected),
            _change_call(holder, manager, FOLLOWING, expected),
        ]
    )
    _no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], attendance.AttendanceConcurrencyError)
    assert attendance.opening_allocation_count(event.pk) == 0
    assert (
        AttendanceEntitlement.objects.filter(
            registration=holder, status=AttendanceEntitlementStatus.CURRENT
        ).count()
        == 1
    )


def test_a_capacity_reduction_racing_an_approval(world):
    event, manager = world
    policy = configure_attendance(event, capacity=2)
    first = _approvable(event, manager)
    _approve_call(first, manager, ALL)()
    second = _approvable(event, manager)
    version = attendance.policy_for(event.pk).version

    def reduce():
        return attendance.update_policy(
            event_edition_id=event.pk,
            actor=manager,
            opening_date=policy.opening_date,
            second_date=policy.second_date,
            third_date=policy.third_date,
            opening_day_capacity=1,
            expected_version=version,
            reason="Venue constraint",
        )

    results, errors = _run([reduce, _approve_call(second, manager, ALL)])
    _no_deadlock(errors)
    assert len(errors) == 1, errors
    assert isinstance(
        errors[0], attendance.AttendancePolicyError | attendance.OpeningDayCapacityReachedError
    )
    final = attendance.policy_for(event.pk)
    assert attendance.opening_allocation_count(event.pk) <= final.opening_day_capacity


def _status(registration) -> str:
    registration.refresh_from_db()
    return registration.public_status
