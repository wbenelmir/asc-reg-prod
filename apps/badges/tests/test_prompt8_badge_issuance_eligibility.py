"""Phase 3 Prompt 8 (P8-02): no physical badge for a context that is no longer approved.

`issue_badge` and `replace_issuance` checked the CURRENT Badge Type
assignment but not the Registration Context itself. Participant withdrawal
and operational cancellation leave the assignment CURRENT, so a withdrawn
or cancelled context could still receive -- or have replaced -- a physical
badge (PRD FR-BDG-012 requires an approved context). Both commands now
refuse, before any stock, allocation, ledger or issuance mutation, and
record the refusal with the existing denial-audit pattern. All data is
synthetic.
"""

from __future__ import annotations

import pytest

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import (
    BadgeIssuance,
    BadgeIssuanceReasonCode,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    BadgeStockBalance,
    BadgeStockLedgerEntry,
    BadgeStockOperation,
    PrintBatchStatus,
    StockAllocationPurpose,
)
from apps.badges.services import (
    RegistrationNotEligibleError,
    allocate_stock,
    change_batch_status,
    create_print_batch,
    issue_badge,
    new_operation_id,
    receive_print_batch,
    replace_issuance,
    return_issuance,
)
from apps.core.models import OutboxEvent
from apps.reviews.services import cancel_registration_operationally, withdraw_registration

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
    for status in (PrintBatchStatus.READY, PrintBatchStatus.IN_PRODUCTION):
        batch = change_batch_status(
            batch=batch,
            target_status=status,
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


def _end_context(kind, registration, actor):
    registration.refresh_from_db()
    if kind == "withdrawal":
        withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration.version,
        )
    else:
        cancel_registration_operationally(
            registration=registration,
            expected_version=registration.version,
            reason="SYNTHETIC_OPERATIONAL_CANCELLATION",
            actor=actor,
        )


def _state():
    """Everything a refused command must leave untouched."""
    return {
        "balances": sorted(
            BadgeStockBalance.objects.values_list(
                "location_id", "badge_type_id", "quantity", "reserved_quantity"
            )
        ),
        "allocations": sorted(
            BadgeStockAllocation.objects.values_list("pk", "consumed_quantity", "status", "version")
        ),
        "ledger": BadgeStockLedgerEntry.objects.count(),
        "issuances": sorted(BadgeIssuance.objects.values_list("pk", "status", "version")),
        "operations": BadgeStockOperation.objects.count(),
        "outbox": OutboxEvent.objects.filter(event_type__startswith="badges.stock.").count(),
    }


def _blocked_audits():
    return AuditEvent.objects.filter(action_code=action_codes.STOCK_ISSUANCE_BLOCKED)


KINDS = ["withdrawal", "cancellation"]


@pytest.fixture
def stocked(event, badge_type, stock_admin, stock_location):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    return stock_location


# ---------------------------------------------------------------------------
# Initial issuance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_initial_issuance_is_refused_after_the_context_ends(
    kind, badge_type, stock_admin, stocked, current_badge_assignment, eligible_registration
):
    _end_context(kind, eligible_registration, stock_admin)
    before = _state()

    with pytest.raises(RegistrationNotEligibleError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=stocked,
            actor=stock_admin,
            operation_id=new_operation_id(),
        )

    assert _state() == before
    audit = _blocked_audits().get()
    assert audit.result == "DENIED"
    assert audit.reason_code == RegistrationNotEligibleError.reason_code
    assert audit.target_type == "BadgeTypeAssignment"
    assert audit.target_uuid == current_badge_assignment.pk


@pytest.mark.parametrize("kind", KINDS)
def test_allocation_backed_issuance_is_refused_and_the_allocation_is_untouched(
    kind, event, badge_type, stock_admin, stocked, current_badge_assignment, eligible_registration
):
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stocked,
        quantity=2,
        purpose_code=StockAllocationPurpose.SHIFT_RESERVE,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    _end_context(kind, eligible_registration, stock_admin)
    before = _state()

    with pytest.raises(RegistrationNotEligibleError):
        issue_badge(
            badge_assignment=current_badge_assignment,
            badge_type=badge_type,
            location=stocked,
            actor=stock_admin,
            operation_id=new_operation_id(),
            allocation=allocation,
        )

    assert _state() == before
    allocation.refresh_from_db()
    assert allocation.consumed_quantity == 0
    assert _blocked_audits().count() == 1


def test_an_approved_context_is_still_issued_normally(
    badge_type, stock_admin, stocked, current_badge_assignment
):
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stocked,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert not _blocked_audits().exists()


def test_an_idempotent_replay_still_returns_the_original_after_withdrawal(
    badge_type, stock_admin, stocked, current_badge_assignment, eligible_registration
):
    """Replay semantics are preserved: a retry of an issuance that already
    committed returns it, rather than being re-judged as a new command."""
    operation_id = new_operation_id()
    kwargs = {
        "badge_assignment": current_badge_assignment,
        "badge_type": badge_type,
        "location": stocked,
        "actor": stock_admin,
        "operation_id": operation_id,
    }
    original = issue_badge(**kwargs)
    _end_context("withdrawal", eligible_registration, stock_admin)
    before = _state()
    assert issue_badge(**kwargs).pk == original.pk
    assert _state() == before
    assert not _blocked_audits().exists()


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("return_original", [False, True])
def test_replacement_is_refused_after_the_context_ends(
    kind,
    return_original,
    badge_type,
    stock_admin,
    stocked,
    current_badge_assignment,
    eligible_registration,
):
    original = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stocked,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    _end_context(kind, eligible_registration, stock_admin)
    before = _state()

    with pytest.raises(RegistrationNotEligibleError):
        replace_issuance(
            issuance=original,
            badge_type=badge_type,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=original.version,
            reason_code=BadgeIssuanceReasonCode.DAMAGED_BADGE,
            return_original=return_original,
        )

    assert _state() == before
    original.refresh_from_db()
    assert original.status == BadgeIssuanceStatus.ISSUED
    assert original.replaced_by_id is None
    audit = _blocked_audits().get()
    assert audit.result == "DENIED"
    assert audit.reason_code == RegistrationNotEligibleError.reason_code
    assert audit.target_type == "BadgeIssuance"
    assert audit.target_uuid == original.pk


# ---------------------------------------------------------------------------
# An already-issued badge is never returned automatically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_an_issued_badge_stays_issued_and_can_still_be_returned(
    kind, event, badge_type, stock_admin, stocked, current_badge_assignment, eligible_registration
):
    from apps.badges.services import current_balance

    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stocked,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    _end_context(kind, eligible_registration, stock_admin)

    issuance.refresh_from_db()
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert current_balance(event_edition=event, badge_type=badge_type, location=stocked) == 4

    # Taking the badge back remains possible and is an explicit act.
    returned = return_issuance(
        issuance=issuance,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=issuance.version,
        reason_code=BadgeIssuanceReasonCode.PARTICIPANT_REQUEST,
    )
    assert returned.status == BadgeIssuanceStatus.RETURNED
    assert current_balance(event_edition=event, badge_type=badge_type, location=stocked) == 5
