"""Unit coverage for generic physical badge stock services (Phase 3 Prompt 3).

Every test here exercises `apps.badges.services.stock` directly against a
real PostgreSQL test database (row locking and constraints are
PostgreSQL-specific and cannot be represented accurately by SQLite).
Concurrency and true negative-stock-under-contention proofs live in
`tests/concurrency/test_badge_stock_concurrency.py`.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.badges.models import (
    BadgeIssuanceReasonCode,
    BadgeIssuanceStatus,
    BadgeStockBalance,
    BadgeStockEntryType,
    BadgeStockLedgerEntry,
    PrintBatch,
    PrintBatchStatus,
    StockAdjustmentReasonCode,
    StockLocation,
)
from apps.badges.services import (
    DuplicateStockLocationError,
    InsufficientStockError,
    OperationConflictError,
    StockConcurrencyError,
    StockServiceError,
    StockStateError,
    WrongBadgeTypeError,
    change_batch_status,
    create_print_batch,
    create_stock_location,
    current_balance,
    issue_badge,
    mark_issuance_lost,
    new_operation_id,
    receive_print_batch,
    reconstruct_balance_from_ledger,
    record_adjustment,
    record_reconciliation,
    replace_issuance,
    return_issuance,
    transfer_stock,
    void_issuance,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Stock locations
# ---------------------------------------------------------------------------


def test_creating_a_location_is_idempotent_on_operation_id(event, stock_admin):
    operation_id = new_operation_id()
    first = create_stock_location(
        event_edition=event,
        code="CTR",
        name="Central",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=operation_id,
    )
    second = create_stock_location(
        event_edition=event,
        code="CTR",
        name="Central",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=operation_id,
    )
    assert first.pk == second.pk
    assert StockLocation.objects.filter(event_edition=event, code="CTR").count() == 1


def test_a_duplicate_location_code_is_refused_cleanly(event, stock_admin):
    create_stock_location(
        event_edition=event,
        code="CTR",
        name="Central",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    with pytest.raises(DuplicateStockLocationError):
        create_stock_location(
            event_edition=event,
            code="CTR",
            name="Central Again",
            location_type="CENTRAL",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


def test_reusing_an_operation_id_for_a_different_location_is_a_conflict(event, stock_admin):
    operation_id = new_operation_id()
    create_stock_location(
        event_edition=event,
        code="CTR",
        name="Central",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=operation_id,
    )
    with pytest.raises(OperationConflictError):
        create_stock_location(
            event_edition=event,
            code="GATE-A",
            name="Gate A",
            location_type="CHECKPOINT",
            actor=stock_admin,
            operation_id=operation_id,
        )


# ---------------------------------------------------------------------------
# Print batches: creation, status lifecycle, damage split at receipt
# ---------------------------------------------------------------------------


def test_print_batch_lifecycle_to_received_posts_only_the_accepted_quantity(
    event, badge_type, stock_admin, stock_location
):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=100,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=stock_location,
    )
    assert batch.status == PrintBatchStatus.DRAFT

    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.READY,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.IN_PRODUCTION,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = receive_print_batch(
        batch=batch,
        produced_quantity=100,
        accepted_quantity=93,
        damaged_quantity=7,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    assert batch.status == PrintBatchStatus.RECEIVED
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 93
    )

    entry = BadgeStockLedgerEntry.objects.get(print_batch=batch)
    assert entry.entry_type == BadgeStockEntryType.RECEIVE
    assert entry.quantity_delta == 93, "damaged units never reach the ledger"


def test_a_fully_damaged_batch_receives_with_no_ledger_entry(
    event, badge_type, stock_admin, stock_location
):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=stock_location,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.READY,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.IN_PRODUCTION,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = receive_print_batch(
        batch=batch,
        produced_quantity=10,
        accepted_quantity=0,
        damaged_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    assert not BadgeStockLedgerEntry.objects.filter(print_batch=batch).exists()
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 0


def test_receiving_before_production_is_refused(event, badge_type, stock_admin, stock_location):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=stock_location,
    )
    with pytest.raises(StockStateError):
        receive_print_batch(
            batch=batch,
            produced_quantity=10,
            accepted_quantity=10,
            damaged_quantity=0,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,
        )


def test_produced_must_equal_accepted_plus_damaged(event, badge_type, stock_admin, stock_location):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=stock_location,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.READY,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.IN_PRODUCTION,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    with pytest.raises(StockServiceError):
        receive_print_batch(
            batch=batch,
            produced_quantity=10,
            accepted_quantity=8,
            damaged_quantity=1,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,
        )


def test_cancellation_is_refused_once_production_has_started(
    event, badge_type, stock_admin, stock_location
):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.READY,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.IN_PRODUCTION,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    with pytest.raises(StockStateError):
        change_batch_status(
            batch=batch,
            target_status=PrintBatchStatus.CANCELLED,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,
        )


def test_a_stale_lock_version_is_refused(event, badge_type, stock_admin, stock_location):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.READY,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    with pytest.raises(StockConcurrencyError):
        change_batch_status(
            batch=batch,
            target_status=PrintBatchStatus.IN_PRODUCTION,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,  # stale: already advanced to READY
        )


# ---------------------------------------------------------------------------
# Receipt into stock, then transfer between locations
# ---------------------------------------------------------------------------


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
        target_status=PrintBatchStatus.READY,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    batch = change_batch_status(
        batch=batch,
        target_status=PrintBatchStatus.IN_PRODUCTION,
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


def test_transfer_moves_stock_and_posts_balanced_ledger_rows(
    event, badge_type, stock_admin, stock_location, other_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=50, actor=stock_admin
    )
    transfer = transfer_stock(
        event_edition=event,
        badge_type=badge_type,
        source_location=stock_location,
        destination_location=other_location,
        quantity=20,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 30
    )
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=other_location) == 20
    )

    out_entry = BadgeStockLedgerEntry.objects.get(transfer=transfer, entry_type="TRANSFER_OUT")
    in_entry = BadgeStockLedgerEntry.objects.get(transfer=transfer, entry_type="TRANSFER_IN")
    assert out_entry.quantity_delta == -20
    assert in_entry.quantity_delta == 20


def test_transfer_refuses_to_take_source_below_zero(
    event, badge_type, stock_admin, stock_location, other_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    with pytest.raises(InsufficientStockError):
        transfer_stock(
            event_edition=event,
            badge_type=badge_type,
            source_location=stock_location,
            destination_location=other_location,
            quantity=6,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 5
    assert current_balance(event_edition=event, badge_type=badge_type, location=other_location) == 0


def test_ledger_reconstructs_the_same_balance_the_projection_holds(
    event, badge_type, stock_admin, stock_location, other_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=40, actor=stock_admin
    )
    transfer_stock(
        event_edition=event,
        badge_type=badge_type,
        source_location=stock_location,
        destination_location=other_location,
        quantity=15,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    for location in (stock_location, other_location):
        assert reconstruct_balance_from_ledger(
            event_edition=event, badge_type=badge_type, location=location
        ) == current_balance(event_edition=event, badge_type=badge_type, location=location)


# ---------------------------------------------------------------------------
# Issuance: no automatic substitution, no negative stock, no double issuance
# ---------------------------------------------------------------------------


def test_issuing_the_wrong_badge_type_is_refused_and_nothing_moves(
    event, stock_admin, stock_location, current_badge_assignment, other_organization
):
    from apps.accreditation.models import BadgeType

    wrong_type = BadgeType.objects.create(event_edition=event, code="VIP", name="VIP")
    _receive_into(
        event=event, badge_type=wrong_type, location=stock_location, quantity=10, actor=stock_admin
    )
    with pytest.raises(WrongBadgeTypeError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=wrong_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )
    assert not BadgeStockLedgerEntry.objects.filter(issuance__isnull=False).exists()
    assert (
        current_balance(event_edition=event, badge_type=wrong_type, location=stock_location) == 10
    )


def test_issuing_with_no_stock_is_refused(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    with pytest.raises(InsufficientStockError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


def test_issuing_decrements_stock_and_creates_one_issuance(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=3, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 2


def test_a_second_issuance_for_the_same_assignment_is_refused(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    with pytest.raises(StockStateError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=stock_location,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


def test_replacement_without_returning_the_original_does_not_restock_it(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    original = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    replacement = replace_issuance(
        issuance=original,
        badge_type=badge_type,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=original.version,
        reason_code=BadgeIssuanceReasonCode.LOST_BADGE,
        return_original=False,
    )
    original.refresh_from_db()
    assert original.status == BadgeIssuanceStatus.REPLACED
    assert original.replaced_by_id == replacement.pk
    assert replacement.status == BadgeIssuanceStatus.ISSUED
    # 5 received - 1 (original issue) - 1 (replacement issue) = 3; the
    # original is never returned to stock.
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 3


def test_replacement_that_returns_the_original_nets_to_one_unit_consumed(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    original = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    replace_issuance(
        issuance=original,
        badge_type=badge_type,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=original.version,
        reason_code=BadgeIssuanceReasonCode.WRONG_BADGE_TYPE,
        return_original=True,
    )
    # 5 - 1 (original issue) + 1 (return) - 1 (replacement issue) = 4
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4


def test_returning_an_issuance_restocks_it(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    returned = return_issuance(
        issuance=issuance,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
        reason_code=BadgeIssuanceReasonCode.EVENT_CONCLUDED,
    )
    assert returned.status == BadgeIssuanceStatus.RETURNED
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 5


def test_marking_an_issuance_lost_posts_no_ledger_entry(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    ledger_count_before = BadgeStockLedgerEntry.objects.count()
    lost = mark_issuance_lost(
        issuance=issuance,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
    )
    assert lost.status == BadgeIssuanceStatus.LOST
    assert BadgeStockLedgerEntry.objects.count() == ledger_count_before
    # 5 - 1 (issue) = 4, and it stays 4: nothing physically returns.
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4


def test_voiding_an_issuance_restocks_it(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    voided = void_issuance(
        issuance=issuance,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
    )
    assert voided.status == BadgeIssuanceStatus.VOIDED
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 5


def test_a_stale_issuance_lock_version_is_refused(
    event, badge_type, stock_admin, stock_location, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    return_issuance(
        issuance=issuance,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
        reason_code=BadgeIssuanceReasonCode.EVENT_CONCLUDED,
    )
    with pytest.raises(StockConcurrencyError):
        return_issuance(
            issuance=issuance,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=issuance.version,  # stale: already RETURNED
            reason_code=BadgeIssuanceReasonCode.EVENT_CONCLUDED,
        )


# ---------------------------------------------------------------------------
# Reasoned adjustment and reconciliation
# ---------------------------------------------------------------------------


def test_a_negative_adjustment_cannot_take_stock_below_zero(
    event, badge_type, stock_admin, stock_location
):
    with pytest.raises(InsufficientStockError):
        record_adjustment(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity_delta=-1,
            reason_code=StockAdjustmentReasonCode.DAMAGED_IN_STOCK,
            reason_text="Water damage discovered on shelf.",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


def test_a_positive_adjustment_increases_the_balance(
    event, badge_type, stock_admin, stock_location
):
    entry = record_adjustment(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity_delta=4,
        reason_code=StockAdjustmentReasonCode.COUNT_CORRECTION,
        reason_text="Found four extra units during a spot check.",
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    assert entry.entry_type == BadgeStockEntryType.ADJUSTMENT
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4


def test_zero_adjustment_is_refused(event, badge_type, stock_admin, stock_location):
    with pytest.raises(StockServiceError):
        record_adjustment(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            quantity_delta=0,
            reason_code=StockAdjustmentReasonCode.COUNT_CORRECTION,
            reason_text="",
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


def test_reconciliation_corrects_the_balance_to_the_counted_value(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    reconciliation = record_reconciliation(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        counted_quantity=8,
        actor=stock_admin,
        operation_id=new_operation_id(),
        notes="Two units unaccounted for at the morning count.",
    )
    assert reconciliation.expected_quantity_at_count == 10
    assert reconciliation.counted_quantity == 8
    assert reconciliation.discrepancy == -2
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 8

    entry = BadgeStockLedgerEntry.objects.get(reconciliation=reconciliation)
    assert entry.entry_type == BadgeStockEntryType.RECONCILIATION
    assert entry.quantity_delta == -2


def test_a_reconciliation_matching_the_projection_posts_no_ledger_row(
    event, badge_type, stock_admin, stock_location
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=6, actor=stock_admin
    )
    ledger_count_before = BadgeStockLedgerEntry.objects.count()
    record_reconciliation(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        counted_quantity=6,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    assert BadgeStockLedgerEntry.objects.count() == ledger_count_before


def test_reconciliation_refuses_a_negative_count(event, badge_type, stock_admin, stock_location):
    with pytest.raises(StockServiceError):
        record_reconciliation(
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            counted_quantity=-1,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )


# ---------------------------------------------------------------------------
# Database constraints (PostgreSQL-specific -- exercised for real)
# ---------------------------------------------------------------------------


def test_the_ledger_rejects_a_zero_quantity_delta_row_at_the_database_level(
    event, badge_type, stock_location
):
    with pytest.raises(IntegrityError):
        BadgeStockLedgerEntry.objects.create(
            operation_id=new_operation_id(),
            event_edition=event,
            badge_type=badge_type,
            location=stock_location,
            entry_type=BadgeStockEntryType.ADJUSTMENT,
            quantity_delta=0,
            occurred_at=stock_location.created_at,
        )


def test_the_balance_projection_rejects_a_negative_quantity_at_the_database_level(
    event, badge_type, stock_location
):
    with pytest.raises(IntegrityError):
        BadgeStockBalance.objects.create(
            event_edition=event, badge_type=badge_type, location=stock_location, quantity=-1
        )


def test_print_batch_receipt_balance_constraint_is_enforced_at_the_database_level(
    event, badge_type, stock_location
):
    with pytest.raises(IntegrityError):
        PrintBatch.objects.create(
            public_reference="X" * 22,
            event_edition=event,
            badge_type=badge_type,
            planned_quantity=10,
            produced_quantity=10,
            accepted_quantity=9,
            damaged_quantity=0,
            status=PrintBatchStatus.RECEIVED,
        )
