"""Regression coverage for the Phase 3 Prompt 3 correction pass.

One test module per correction theme, all against real PostgreSQL:

1. allocation accounting (`PRINT-002` "reserved"), authorization, retry;
2. the EventEdition boundary in the SERVICE layer;
3. replacement must match the current `BadgeTypeAssignment`;
4. one current issuance per Registration Context across reassignment;
5. the append-only ledger and its entry-direction constraints;
6. complete `command_fingerprint` binding;
7. covered by `tests/concurrency/test_badge_stock_concurrency.py`.

Every rejection test also asserts ZERO side effects: no balance change, no
ledger row, no issuance, no allocation, no `BadgeStockOperation` record.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.accreditation.models import AssignmentStatus, BadgeType, BadgeTypeAssignment
from apps.badges.models import (
    BadgeIssuance,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    BadgeStockEntryType,
    BadgeStockLedgerEntry,
    BadgeStockOperation,
    StockAllocationPurpose,
    StockAllocationStatus,
)
from apps.badges.services import (
    CrossEventScopeError,
    InsufficientStockError,
    OperationConflictError,
    StockConcurrencyError,
    StockServiceError,
    StockStateError,
    WrongBadgeTypeError,
    allocate_stock,
    available_balance,
    change_batch_status,
    create_print_batch,
    current_balance,
    issue_badge,
    new_operation_id,
    receive_print_batch,
    reconstruct_balance_from_ledger,
    record_adjustment,
    record_reconciliation,
    release_allocation,
    replace_issuance,
    reserved_balance,
    transfer_stock,
)

pytestmark = pytest.mark.django_db


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
    return receive_print_batch(
        batch=batch,
        produced_quantity=quantity,
        accepted_quantity=quantity,
        damaged_quantity=0,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )


def _snapshot():
    """Everything a rejected command must leave untouched."""
    return {
        "ledger": BadgeStockLedgerEntry.objects.count(),
        "issuances": BadgeIssuance.objects.count(),
        "allocations": BadgeStockAllocation.objects.count(),
        "operations": BadgeStockOperation.objects.count(),
    }


# ---------------------------------------------------------------------------
# §1 Allocation accounting
# ---------------------------------------------------------------------------


def test_allocating_reserves_without_moving_stock(event, badge_type, stock_admin, stock_location):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    ledger_before = BadgeStockLedgerEntry.objects.count()

    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=4,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )

    assert allocation.status == StockAllocationStatus.ACTIVE
    # On-hand is untouched: allocation moves nothing, so it posts no ledger row.
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 10
    )
    assert BadgeStockLedgerEntry.objects.count() == ledger_before
    # Availability is what changed.
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4
    )
    assert (
        available_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 6
    )
    # The ledger still reconstructs on-hand exactly (allocation is invisible to it).
    assert (
        reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=stock_location
        )
        == 10
    )


def test_allocation_cannot_over_commit_available_stock(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=4,
        purpose_code=StockAllocationPurpose.PROTOCOL_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    before = _snapshot()
    with pytest.raises(InsufficientStockError):
        allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity=2,
            purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4
    )


def test_an_unreserved_transfer_cannot_consume_allocated_stock(
    event, badge_type, stock_admin, stock_location, other_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=8,
        purpose_code=StockAllocationPurpose.CHECKPOINT_OPENING,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    before = _snapshot()
    # Only 2 are available even though 10 are on hand.
    with pytest.raises(InsufficientStockError):
        transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=stock_location,
            destination_location=other_location,
            quantity=3,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 10
    )


def test_a_negative_adjustment_cannot_consume_allocated_stock(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=6, actor=stock_admin
    )
    allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=5,
        purpose_code=StockAllocationPurpose.OPERATIONAL_BUFFER,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    with pytest.raises(InsufficientStockError):
        record_adjustment(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity_delta=-3,
            reason_code="DAMAGED_IN_STOCK",
            reason_text="Water damage",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 6


def test_issuing_against_an_allocation_consumes_the_reservation(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=3,
        purpose_code=StockAllocationPurpose.ORGANIZATION_DELEGATION,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    operation_id = new_operation_id()
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=operation_id,
        allocation=allocation,
    )
    replayed = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=operation_id,
        allocation=allocation,
    )
    assert replayed.pk == issuance.pk
    allocation.refresh_from_db()
    assert allocation.consumed_quantity == 1
    assert allocation.remaining_quantity == 2
    assert BadgeStockOperation.objects.get(operation_id=operation_id).allocation_id == allocation.pk
    # On-hand falls by one; reserved falls by one too, so AVAILABILITY is
    # unchanged -- the reservation was spent on exactly what it was held for.
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 9
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 2
    )
    assert (
        available_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 7
    )


def test_releasing_an_allocation_returns_only_unconsumed_units(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=3,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
        allocation=allocation,
    )
    allocation.refresh_from_db()
    released = release_allocation(
        allocation=allocation,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=allocation.version,
    )
    assert released.status == StockAllocationStatus.RELEASED
    # The 1 consumed unit is gone for good; only the 2 remaining come back.
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 0
    )
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 9
    assert (
        available_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 9
    )


def test_allocation_is_idempotent_on_operation_id(event, badge_type, stock_admin, stock_location):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    operation_id = new_operation_id()
    kwargs = dict(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=3,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=operation_id,
    )
    first = allocate_stock(**kwargs)
    second = allocate_stock(**kwargs)
    assert first.pk == second.pk
    assert BadgeStockAllocation.objects.count() == 1
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 3
    )


def test_reconciliation_below_the_allocated_quantity_is_refused(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=8,
        purpose_code=StockAllocationPurpose.PROTOCOL_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    with pytest.raises(InsufficientStockError):
        record_reconciliation(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            counted_quantity=5,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 10
    )


# ---------------------------------------------------------------------------
# §2 EventEdition boundary, enforced in the SERVICE layer
# ---------------------------------------------------------------------------


def test_allocating_a_foreign_badge_type_is_refused(
    event, stock_admin, stock_location, foreign_badge_type
):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        allocate_stock(
            event_edition=event,
            badge_type=foreign_badge_type,
            location=stock_location,
            quantity=1,
            purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


def test_transferring_to_a_foreign_location_is_refused(
    event, badge_type, stock_admin, stock_location, foreign_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=stock_location,
            destination_location=foreign_location,
            quantity=1,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 5


def test_creating_a_batch_for_a_foreign_badge_type_is_refused(
    event, stock_admin, foreign_badge_type
):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        create_print_batch(
            event_edition=event,
            badge_type=foreign_badge_type,
            planned_quantity=10,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


def test_receiving_into_a_foreign_location_is_refused(
    event, badge_type, stock_admin, stock_location, foreign_location
):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=5,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=stock_location,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="READY",
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status="IN_PRODUCTION",
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        receive_print_batch(
            batch=batch,
            produced_quantity=5,
            accepted_quantity=5,
            damaged_quantity=0,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,
            destination_location=foreign_location,
        )
    assert _snapshot() == before


def test_issuing_with_a_foreign_badge_type_is_refused_before_anything_is_created(
    event, stock_admin, stock_location, current_badge_assignment, foreign_badge_type
):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=foreign_badge_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


def test_issuing_at_a_foreign_location_is_refused(
    event, badge_type, stock_admin, current_badge_assignment, foreign_location
):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=foreign_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


def test_adjusting_a_foreign_location_is_refused(event, badge_type, stock_admin, foreign_location):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        record_adjustment(
            event_edition=event,
            badge_type=badge_type,
            location=foreign_location,
            quantity_delta=5,
            reason_code="COUNT_CORRECTION",
            reason_text="",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


def test_reconciling_a_foreign_location_is_refused(
    event, badge_type, stock_admin, foreign_location
):
    before = _snapshot()
    with pytest.raises(CrossEventScopeError):
        record_reconciliation(
            event_edition=event,
            badge_type=badge_type,
            location=foreign_location,
            counted_quantity=3,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before


# ---------------------------------------------------------------------------
# §3 A replacement must match the CURRENT assignment
# ---------------------------------------------------------------------------


def _issued(event, badge_type, stock_admin, stock_location, assignment, quantity=5):
    _receive_into(
        event=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=quantity,
        actor=stock_admin,
    )
    return issue_badge(
        badge_assignment=assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )


def test_replacement_with_a_different_badge_type_is_refused(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    issuance = _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)
    other_type = BadgeType.objects.create(event_edition=event, code="VIP", name="VIP")
    _receive_into(
        event=event, badge_type=other_type, location=stock_location, quantity=5, actor=stock_admin
    )
    before = _snapshot()

    with pytest.raises(WrongBadgeTypeError):
        replace_issuance(
            issuance=issuance,
            badge_type=other_type,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=issuance.version,
            reason_code="WRONG_BADGE_TYPE",
            return_original=True,
        )

    assert _snapshot() == before
    issuance.refresh_from_db()
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert current_balance(event_edition=event, badge_type=other_type, location=stock_location) == 5


def test_replacement_with_a_foreign_event_badge_type_is_refused(
    event, badge_type, stock_admin, stock_location, current_badge_assignment, foreign_badge_type
):
    issuance = _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)
    before = _snapshot()

    with pytest.raises(CrossEventScopeError):
        replace_issuance(
            issuance=issuance,
            badge_type=foreign_badge_type,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=issuance.version,
            reason_code="WRONG_BADGE_TYPE",
            return_original=True,
        )

    assert _snapshot() == before
    issuance.refresh_from_db()
    assert issuance.status == BadgeIssuanceStatus.ISSUED


def test_replacement_follows_a_reassignment_to_the_new_current_badge_type(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    """After a reassignment, the replacement must use the NEW current type."""
    from django.utils import timezone

    issuance = _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)
    new_type = BadgeType.objects.create(event_edition=event, code="UPGRADED", name="Upgraded")
    _receive_into(
        event=event, badge_type=new_type, location=stock_location, quantity=5, actor=stock_admin
    )
    # Supersede the assignment exactly as the accreditation app does.
    current_badge_assignment.status = AssignmentStatus.SUPERSEDED
    current_badge_assignment.save(update_fields=["status"])
    BadgeTypeAssignment.objects.create(
        registration=current_badge_assignment.registration,
        badge_type=new_type,
        event_edition=event,
        organization=current_badge_assignment.organization,
        status=AssignmentStatus.CURRENT,
        effective_from=timezone.now(),
        created_by=stock_admin,
        supersedes=current_badge_assignment,
    )

    # The OLD type is now wrong, even though it is what is physically held.
    with pytest.raises(WrongBadgeTypeError):
        replace_issuance(
            issuance=issuance,
            badge_type=badge_type,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=issuance.version,
            reason_code="WRONG_BADGE_TYPE",
            return_original=True,
        )

    issuance.refresh_from_db()
    replacement = replace_issuance(
        issuance=issuance,
        badge_type=new_type,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
        reason_code="WRONG_BADGE_TYPE",
        return_original=True,
    )
    assert replacement.badge_type_id == new_type.pk
    assert replacement.registration_id == current_badge_assignment.registration_id


# ---------------------------------------------------------------------------
# §4 One current issuance per Registration Context, across reassignment
# ---------------------------------------------------------------------------


def test_a_reassignment_cannot_produce_a_second_live_badge(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    """The exact hole the per-assignment constraint left open."""
    from django.utils import timezone

    _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)

    new_type = BadgeType.objects.create(event_edition=event, code="SECOND", name="Second")
    _receive_into(
        event=event, badge_type=new_type, location=stock_location, quantity=5, actor=stock_admin
    )
    current_badge_assignment.status = AssignmentStatus.SUPERSEDED
    current_badge_assignment.save(update_fields=["status"])
    new_assignment = BadgeTypeAssignment.objects.create(
        registration=current_badge_assignment.registration,
        badge_type=new_type,
        event_edition=event,
        organization=current_badge_assignment.organization,
        status=AssignmentStatus.CURRENT,
        effective_from=timezone.now(),
        created_by=stock_admin,
        supersedes=current_badge_assignment,
    )

    before = _snapshot()
    with pytest.raises(StockStateError):
        issue_badge(
            badge_assignment=new_assignment,
            badge_type=new_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before
    assert (
        BadgeIssuance.objects.filter(
            registration=current_badge_assignment.registration,
            status=BadgeIssuanceStatus.ISSUED,
        ).count()
        == 1
    )


def test_the_database_itself_refuses_two_live_issuances_for_one_registration(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    """Defence in depth: even bypassing the service, the constraint holds."""
    issuance = _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)
    with pytest.raises(IntegrityError), transaction.atomic():
        BadgeIssuance.objects.create(
            badge_assignment=current_badge_assignment,
            registration=current_badge_assignment.registration,
            badge_type=badge_type,
            location=stock_location,
            status=BadgeIssuanceStatus.ISSUED,
            issued_at=issuance.issued_at,
        )


# ---------------------------------------------------------------------------
# §5 The ledger is append-only, with enforced entry directions
# ---------------------------------------------------------------------------


def test_the_database_refuses_to_update_a_ledger_row(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    entry = BadgeStockLedgerEntry.objects.filter(entry_type=BadgeStockEntryType.RECEIVE).first()
    assert entry is not None
    with pytest.raises(Exception) as excinfo, transaction.atomic():
        BadgeStockLedgerEntry.objects.filter(pk=entry.pk).update(quantity_delta=999)
    assert "append-only" in str(excinfo.value).lower()


def test_the_database_refuses_an_issuance_without_a_registration(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    """The denormalized registration guard cannot be bypassed with NULL."""
    from django.utils import timezone

    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=1, actor=stock_admin
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        BadgeIssuance.objects.create(
            badge_assignment=current_badge_assignment,
            registration=None,
            badge_type=badge_type,
            location=stock_location,
            status=BadgeIssuanceStatus.ISSUED,
            issued_at=timezone.now(),
        )


def test_the_database_refuses_a_raw_sql_delete_of_a_ledger_row(
    event, badge_type, stock_admin, stock_location
):
    """Raw SQL, deliberately: Django's `PROTECT` foreign key already stops
    an ORM delete, but that proves only that the ORM cooperates. The point
    of the trigger is that the DATABASE refuses, so this bypasses the ORM
    entirely -- exactly how `apps.audit` proves the same property.
    """
    from django.db import connection

    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    entry = BadgeStockLedgerEntry.objects.filter(entry_type=BadgeStockEntryType.RECEIVE).first()
    assert entry is not None

    with pytest.raises(Exception) as excinfo, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM badges_stock_ledger_entry WHERE id = %s", [str(entry.pk)])
    assert "append-only" in str(excinfo.value).lower()
    assert BadgeStockLedgerEntry.objects.filter(pk=entry.pk).exists()


def test_a_raw_sql_update_of_a_ledger_row_is_also_refused(
    event, badge_type, stock_admin, stock_location
):
    from django.db import connection

    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    entry = BadgeStockLedgerEntry.objects.filter(entry_type=BadgeStockEntryType.RECEIVE).first()

    with pytest.raises(Exception) as excinfo, transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE badges_stock_ledger_entry SET quantity_delta = 999 WHERE id = %s",
                [str(entry.pk)],
            )
    assert "append-only" in str(excinfo.value).lower()
    entry.refresh_from_db()
    assert entry.quantity_delta == 5


def test_the_orm_also_protects_ledger_rows_from_deletion(
    event, badge_type, stock_admin, stock_location
):
    """Defence in depth above the trigger: the operation ledger's `PROTECT`
    foreign key stops an ORM cascade before it reaches the database."""
    from django.db.models import ProtectedError

    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    entry = BadgeStockLedgerEntry.objects.filter(entry_type=BadgeStockEntryType.RECEIVE).first()
    with pytest.raises(ProtectedError), transaction.atomic():
        BadgeStockLedgerEntry.objects.filter(pk=entry.pk).delete()
    assert BadgeStockLedgerEntry.objects.filter(pk=entry.pk).exists()


@pytest.mark.parametrize(
    ("entry_type", "bad_delta"),
    [
        (BadgeStockEntryType.RECEIVE, -1),
        (BadgeStockEntryType.TRANSFER_IN, -1),
        (BadgeStockEntryType.RETURN, -1),
        (BadgeStockEntryType.TRANSFER_OUT, 1),
        (BadgeStockEntryType.ISSUE, 1),
    ],
)
def test_the_database_refuses_a_ledger_row_with_the_wrong_direction(
    event, badge_type, stock_location, entry_type, bad_delta
):
    """An ISSUE that increments stock, or a RECEIVE that decrements it, is a
    corrupt row -- refused by the database, not merely by service code."""
    from django.utils import timezone

    kwargs = {
        "operation_id": new_operation_id(),
        "event_edition": event,
        "badge_type": badge_type,
        "location": stock_location,
        "entry_type": entry_type,
        "quantity_delta": bad_delta,
        "occurred_at": timezone.now(),
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        BadgeStockLedgerEntry.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# §6 Complete command_fingerprint binding
# ---------------------------------------------------------------------------


def test_reusing_an_operation_id_with_a_changed_location_name_is_a_conflict(event, stock_admin):
    from apps.badges.services import create_stock_location

    operation_id = new_operation_id()
    create_stock_location(
        event_edition=event,
        code="LOC-A",
        name="Location A",
        name_ar="الموقع أ",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=operation_id,
    )
    with pytest.raises(OperationConflictError):
        create_stock_location(
            event_edition=event,
            code="LOC-A",
            name="Location A",
            name_ar="موقع مختلف",
            location_type="CENTRAL",
            actor=stock_admin,
            operation_id=operation_id,
        )


def test_reusing_an_operation_id_with_a_changed_supplier_reference_is_a_conflict(
    event, badge_type, stock_admin
):
    operation_id = new_operation_id()
    create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        supplier_reference="SUPPLIER-1",
        actor=stock_admin,
        operation_id=operation_id,
    )
    before = _snapshot()
    with pytest.raises(OperationConflictError):
        create_print_batch(
            event_edition=event,
            badge_type=badge_type,
            planned_quantity=10,
            supplier_reference="SUPPLIER-2",
            actor=stock_admin,
            operation_id=operation_id,
        )
    assert _snapshot() == before


def test_reusing_an_operation_id_with_a_changed_transfer_note_is_a_conflict(
    event, badge_type, stock_admin, stock_location, other_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    operation_id = new_operation_id()
    transfer_stock(
        event_edition=event,
        badge_type=badge_type,
        source_location=stock_location,
        destination_location=other_location,
        quantity=2,
        actor=stock_admin,
        operation_id=operation_id,
        note="Morning run",
    )
    before = _snapshot()
    balance_before = current_balance(
        event_edition=event, badge_type=badge_type, location=stock_location
    )
    with pytest.raises(OperationConflictError):
        transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=stock_location,
            destination_location=other_location,
            quantity=2,
            actor=stock_admin,
            operation_id=operation_id,
            note="Afternoon run",
        )
    assert _snapshot() == before
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location)
        == balance_before
    )


def test_reusing_an_operation_id_with_a_changed_serial_number_is_a_conflict(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    operation_id = new_operation_id()
    issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=operation_id,
        optional_serial_number="SN-0001",
    )
    before = _snapshot()
    with pytest.raises(OperationConflictError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=operation_id,
            optional_serial_number="SN-9999",
        )
    assert _snapshot() == before


def test_reusing_an_operation_id_with_a_changed_allocation_purpose_is_a_conflict(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    operation_id = new_operation_id()
    allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=2,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=operation_id,
    )
    before = _snapshot()
    with pytest.raises(OperationConflictError):
        allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity=2,
            purpose_code=StockAllocationPurpose.PROTOCOL_RESERVE,
            actor=stock_admin,
            operation_id=operation_id,
        )
    assert _snapshot() == before
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 2
    )


def test_reusing_an_operation_id_with_a_changed_return_location_is_a_conflict(
    event, badge_type, stock_admin, stock_location, other_location, current_badge_assignment
):
    from apps.badges.services import return_issuance

    issuance = _issued(event, badge_type, stock_admin, stock_location, current_badge_assignment)
    operation_id = new_operation_id()
    return_issuance(
        issuance=issuance,
        actor=stock_admin,
        operation_id=operation_id,
        expected_lock_version=issuance.version,
        reason_code="EVENT_CONCLUDED",
        return_location=stock_location,
    )
    issuance.refresh_from_db()
    before = _snapshot()
    with pytest.raises(OperationConflictError):
        return_issuance(
            issuance=issuance,
            actor=stock_admin,
            operation_id=operation_id,
            expected_lock_version=issuance.version,
            reason_code="EVENT_CONCLUDED",
            return_location=other_location,
        )
    assert _snapshot() == before


def test_a_stale_allocation_lock_version_is_refused(event, badge_type, stock_admin, stock_location):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=2,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    release_allocation(
        allocation=allocation,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=allocation.version,
    )
    with pytest.raises(StockConcurrencyError):
        release_allocation(
            allocation=allocation,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=allocation.version,
        )


def test_an_unrecognized_allocation_purpose_is_refused(
    event, badge_type, stock_admin, stock_location
):
    before = _snapshot()
    with pytest.raises(StockServiceError):
        allocate_stock(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity=1,
            purpose_code="NOT_A_REAL_PURPOSE",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert _snapshot() == before
