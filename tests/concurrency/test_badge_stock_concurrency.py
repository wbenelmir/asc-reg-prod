"""Real multi-connection PostgreSQL concurrency for generic physical badge
stock (Phase 3 Prompt 3, ADR-0020).

Two real threads, each on its OWN PostgreSQL connection, released together
from a `threading.Barrier` so they genuinely overlap -- matches the
established pattern in `tests/concurrency/test_pass_credential_concurrency.py`.

Four guarantees are proven here, each of which a single-threaded test
cannot establish:

1. two simultaneous issuances against ONE remaining unit of stock never
   both succeed -- negative stock is structurally impossible;
2. two simultaneous issuances for the SAME badge assignment never both
   succeed -- double issuance is structurally impossible;
3. a retried issuance command carrying the same `operation_id` never
   consumes stock twice even when both requests are genuinely in flight
   together;
4. two transfers between the SAME pair of locations, in opposite
   directions, running concurrently, never deadlock and always leave the
   ledger and the projection in agreement.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accreditation.models import (
    AccessProfile,
    AccessProfileAssignment,
    AssignmentStatus,
    BadgeType,
    BadgeTypeAssignment,
    ParticipantRole,
    ParticipantRoleAssignment,
)
from apps.badges.services import (
    StockServiceError,
    StockStateError,
    allocate_stock,
    available_balance,
    change_batch_status,
    create_print_batch,
    create_stock_location,
    current_balance,
    issue_badge,
    new_operation_id,
    receive_print_batch,
    reconstruct_balance_from_ledger,
    replace_issuance,
    reserved_balance,
    transfer_stock,
)
from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.people.models import Person, PersonStatus
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


def _receive_into(*, event, badge_type, location, quantity, actor):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=quantity,
        actor=actor,
        operation_id=new_operation_id(),
        destination_location=location,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="READY",
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="IN_PRODUCTION",
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    receive_print_batch(
        batch=batch,
        produced_quantity=quantity,
        accepted_quantity=quantity,
        damaged_quantity=0,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )


@pytest.fixture
def scenario():
    """One event, one stock location, and a badge type with `stock_quantity`
    units of stock -- overridable per test via the caller."""
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"STK{uuid.uuid4().hex[:6].upper()}",
        name="Stock Concurrency Edition",
        timezone="UTC",
        starts_at=now,
        ends_at=now + _days(30),
        status="EVENT_OPERATIONS",
    )
    organization = Organization.objects.create(
        official_name=f"Stock Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"stock org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    admin = OperationalUser.objects.create_user(
        email=f"stockadmin.{uuid.uuid4().hex[:8]}@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=admin,
        group=Group.objects.get(name="Badge Stock Administrators"),
        event_edition=event,
        organization=organization,
        granted_by=admin,
    )
    badge_type = BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")
    location = create_stock_location(
        event_edition=event,
        code="CENTRAL",
        name="Central",
        location_type="CENTRAL",
        actor=admin,
        operation_id=new_operation_id(),
    )
    other_location = create_stock_location(
        event_edition=event,
        code="GATE-A",
        name="Gate A",
        location_type="CHECKPOINT",
        actor=admin,
        operation_id=new_operation_id(),
    )

    yield {
        "event": event,
        "organization": organization,
        "admin": admin,
        "badge_type": badge_type,
        "location": location,
        "other_location": other_location,
    }


def _make_assignment(*, event, organization, admin, badge_type, suffix: str):
    role = ParticipantRole.objects.create(
        event_edition=event, code=f"DELEGATE{suffix}", name="Delegate"
    )
    access_profile = AccessProfile.objects.create(
        event_edition=event, code=f"EXHIBITOR{suffix}", name="Exhibitor"
    )
    person = Person.objects.create(status=PersonStatus.ACTIVE)
    registration = Registration.objects.create(
        public_reference=f"STK-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=RegistrationPublicStatus.APPROVED,
        internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
        submitted_at=timezone.now(),
    )
    # Owner decision IDV-Q1: an approved context needs a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)
    common = {
        "event_edition": event,
        "organization": organization,
        "status": AssignmentStatus.CURRENT,
        "effective_from": timezone.now(),
        "created_by": admin,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    assignment = BadgeTypeAssignment.objects.create(
        registration=registration, badge_type=badge_type, **common
    )
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )
    return assignment


def test_two_simultaneous_issuances_against_one_unit_never_both_succeed(scenario):
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=1, actor=admin)
    assignment_a = _make_assignment(
        event=event,
        organization=scenario["organization"],
        admin=admin,
        badge_type=badge_type,
        suffix="A",
    )
    assignment_b = _make_assignment(
        event=event,
        organization=scenario["organization"],
        admin=admin,
        badge_type=badge_type,
        suffix="B",
    )

    def _issue(assignment):
        def _call():
            return issue_badge(
                badge_assignment=assignment,
                badge_type=badge_type,
                location=location,
                actor=admin,
                operation_id=new_operation_id(),
            )

        return _call

    results, errors = _run_callables_in_threads([_issue(assignment_a), _issue(assignment_b)])

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one issuance must win, got {results!r}"
    assert len(errors) == 1, f"exactly one worker must be refused, got {errors!r}"
    assert isinstance(errors[0], StockServiceError)
    assert not isinstance(errors[0], IntegrityError)

    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 0
    assert (
        reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=location
        )
        == 0
    )


def test_two_simultaneous_issuances_for_the_same_assignment_never_both_succeed(scenario):
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=5, actor=admin)
    assignment = _make_assignment(
        event=event,
        organization=scenario["organization"],
        admin=admin,
        badge_type=badge_type,
        suffix="C",
    )

    def _issue():
        return issue_badge(
            badge_assignment=assignment,
            badge_type=badge_type,
            location=location,
            actor=admin,
            operation_id=new_operation_id(),
        )

    results, errors = _run_callables_in_threads([_issue, _issue])

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one issuance must win, got {results!r}"
    assert len(errors) == 1
    assert isinstance(errors[0], StockStateError)
    assert not isinstance(errors[0], IntegrityError)
    # 5 received, exactly one issued.
    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 4


def test_the_same_operation_id_in_flight_twice_resolves_as_original_plus_replay(scenario):
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=5, actor=admin)
    assignment = _make_assignment(
        event=event,
        organization=scenario["organization"],
        admin=admin,
        badge_type=badge_type,
        suffix="D",
    )
    operation_id = new_operation_id()

    def _issue():
        return issue_badge(
            badge_assignment=assignment,
            badge_type=badge_type,
            location=location,
            actor=admin,
            operation_id=operation_id,
        )

    results, errors = _run_callables_in_threads([_issue, _issue])

    assert errors == [], f"no worker may raise: {errors!r}"
    assert all(result is not None for result in results)
    assert results[0].pk == results[1].pk
    # Stock is consumed exactly once, not twice, even though both workers
    # genuinely overlapped.
    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 4


def test_two_opposite_direction_transfers_between_the_same_locations_never_deadlock(scenario):
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]
    other_location = scenario["other_location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=50, actor=admin)
    _receive_into(
        event=event, badge_type=badge_type, location=other_location, quantity=50, actor=admin
    )

    def _forward():
        return transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=location,
            destination_location=other_location,
            quantity=10,
            actor=admin,
            operation_id=new_operation_id(),
        )

    def _backward():
        return transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=other_location,
            destination_location=location,
            quantity=7,
            actor=admin,
            operation_id=new_operation_id(),
        )

    results, errors = _run_callables_in_threads([_forward, _backward])

    assert errors == [], f"no worker may raise, and neither may deadlock: {errors!r}"
    assert all(result is not None for result in results)

    # Net effect regardless of interleaving: location gave 10, received 7
    # (net -3); other_location gave 7, received 10 (net +3).
    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 47
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=other_location) == 53
    )
    for loc in (location, other_location):
        assert reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=loc
        ) == current_balance(event_edition=event, badge_type=badge_type, location=loc)


def test_a_cross_location_replacement_and_a_transfer_never_deadlock(scenario):
    """The correction-pass case for the single global lock order (§7).

    A replacement with `return_original=True` across TWO locations touches
    the same pair of balance rows a concurrent transfer touches, in the
    opposite logical direction (it CREDITS the original's location and
    DEBITS the replacement's, while the transfer debits the first and
    credits the second). Before `_lock_balances` imposed one global order,
    these two commands could take the rows in opposite orders and deadlock.
    """
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]
    other_location = scenario["other_location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=40, actor=admin)
    _receive_into(
        event=event, badge_type=badge_type, location=other_location, quantity=40, actor=admin
    )
    assignment = _make_assignment(
        event=event,
        organization=scenario["organization"],
        admin=admin,
        badge_type=badge_type,
        suffix="E",
    )
    issuance = issue_badge(
        badge_assignment=assignment,
        badge_type=badge_type,
        location=location,
        actor=admin,
        operation_id=new_operation_id(),
    )
    issuance.refresh_from_db()

    def _replace_across_locations():
        # Returns the original at `location`, issues the replacement from
        # `other_location`: both rows, opposite directions.
        return replace_issuance(
            issuance=issuance,
            badge_type=badge_type,
            location=other_location,
            actor=admin,
            operation_id=new_operation_id(),
            expected_lock_version=issuance.version,
            reason_code="WRONG_BADGE_TYPE",
            return_original=True,
        )

    def _transfer():
        return transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=other_location,
            destination_location=location,
            quantity=5,
            actor=admin,
            operation_id=new_operation_id(),
        )

    results, errors = _run_callables_in_threads([_replace_across_locations, _transfer])

    assert errors == [], f"no worker may raise, and neither may deadlock: {errors!r}"
    assert all(result is not None for result in results)

    # Exact accounting, independent of interleaving:
    #   location:       40 - 1 (issue) + 1 (return) + 5 (transfer in)  = 45
    #   other_location: 40 - 1 (replacement issue) - 5 (transfer out)  = 34
    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 45
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=other_location) == 34
    )
    for loc in (location, other_location):
        assert reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=loc
        ) == current_balance(event_edition=event, badge_type=badge_type, location=loc)


def test_two_simultaneous_allocations_cannot_over_commit_the_same_units(scenario):
    """Allocation accounting under real contention (§1 + §7).

    Two operators reserve 6 units each against 10 on hand. Exactly one can
    succeed: reserved stock must never exceed stock on hand, and the
    database constraint plus the balance row lock make that structural.
    """
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=10, actor=admin)

    def _allocate():
        return allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=location,
            quantity=6,
            purpose_code="SHIFT_RESERVE",
            actor=admin,
            operation_id=new_operation_id(),
        )

    results, errors = _run_callables_in_threads([_allocate, _allocate])

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one allocation must win, got {results!r}"
    assert len(errors) == 1, f"exactly one worker must be refused, got {errors!r}"
    assert isinstance(errors[0], StockServiceError)
    assert not isinstance(errors[0], IntegrityError)

    assert reserved_balance(event_edition=event, badge_type=badge_type, location=location) == 6
    assert available_balance(event_edition=event, badge_type=badge_type, location=location) == 4
    # Allocation moves nothing: on-hand and the ledger are untouched by it.
    assert current_balance(event_edition=event, badge_type=badge_type, location=location) == 10
    assert (
        reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=location
        )
        == 10
    )


def test_a_retried_allocation_command_reserves_exactly_once(scenario):
    """Retry safety for allocation: the same `operation_id` in flight twice
    reserves one quantity, not two."""
    event = scenario["event"]
    admin = scenario["admin"]
    badge_type = scenario["badge_type"]
    location = scenario["location"]

    _receive_into(event=event, badge_type=badge_type, location=location, quantity=10, actor=admin)
    operation_id = new_operation_id()

    def _allocate():
        return allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=location,
            quantity=4,
            purpose_code="SHIFT_RESERVE",
            actor=admin,
            operation_id=operation_id,
        )

    results, errors = _run_callables_in_threads([_allocate, _allocate])

    assert errors == [], f"no worker may raise: {errors!r}"
    assert results[0].pk == results[1].pk
    assert reserved_balance(event_edition=event, badge_type=badge_type, location=location) == 4
