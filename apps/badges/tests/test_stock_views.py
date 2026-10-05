"""View-level authorization and scope for generic physical badge stock
(Phase 3 Prompt 3, ADR-0020).

Mirrors the discipline `apps/badges/tests/test_views.py` already
established for the credential surface: every mutation is POST-only,
permission-checked, and an out-of-scope resource is indistinguishable from
a nonexistent one (404, never 403).
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.badges.models import (
    BadgeIssuanceStatus,
    BadgeStockLedgerEntry,
    BadgeStockOperation,
    StockAllocationPurpose,
    StockLocation,
)
from apps.badges.services import (
    allocate_stock,
    available_balance,
    change_batch_status,
    create_print_batch,
    current_balance,
    new_operation_id,
    receive_print_batch,
    reserved_balance,
)
from apps.badges.tests.conftest import sign_in_operational

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


# ---------------------------------------------------------------------------
# Authentication and scope
# ---------------------------------------------------------------------------


def test_the_stock_dashboard_requires_authentication(client, event):
    response = client.get(reverse("badges:stock-dashboard", kwargs={"event_pk": event.pk}))
    assert response.status_code in (302, 403)


def test_an_authorized_administrator_sees_the_stock_dashboard(client, event, stock_admin):
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.get(reverse("badges:stock-dashboard", kwargs={"event_pk": event.pk}))
    assert response.status_code == 200


def test_an_out_of_scope_event_is_a_404_not_a_403(client, event, other_event, stock_admin):
    """`stock_admin`'s membership is scoped to `event`; `other_event` is a
    different event the same user was never granted access to."""
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.get(reverse("badges:stock-dashboard", kwargs={"event_pk": other_event.pk}))
    assert response.status_code == 404


def test_an_issuer_cannot_manage_stock_locations(client, event, stock_issuer):
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.post(
        reverse("badges:stock-location-create", kwargs={"event_pk": event.pk}),
        {
            "operation_id": new_operation_id(),
            "code": "SHOULD-FAIL",
            "name": "Should fail",
            "location_type": "CENTRAL",
        },
    )
    assert response.status_code in (302, 403)
    assert not StockLocation.objects.filter(code="SHOULD-FAIL").exists()


def test_an_administrator_cannot_issue_a_badge(
    client, event, stock_admin, stock_location, badge_type, current_badge_assignment
):
    """The two stock groups are deliberately separate (ADR-0017/apps.py):
    production/accounting authority never implies handover authority."""
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    assert response.status_code in (302, 403)
    from apps.badges.models import BadgeIssuance

    assert not BadgeIssuance.objects.filter(badge_assignment=current_badge_assignment).exists()


# ---------------------------------------------------------------------------
# Location, batch, transfer, adjustment, reconciliation (Badge Stock Administrators)
# ---------------------------------------------------------------------------


def test_creating_a_location_via_the_view_is_audited(client, event, stock_admin):
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.post(
        reverse("badges:stock-location-create", kwargs={"event_pk": event.pk}),
        {
            "operation_id": new_operation_id(),
            "code": "NEWLOC",
            "name": "New Location",
            "location_type": "CENTRAL",
        },
    )
    assert response.status_code == 302
    assert StockLocation.objects.filter(event_edition=event, code="NEWLOC").exists()
    assert AuditEvent.objects.filter(action_code="BDG_STOCK_LOCATION_CREATED").exists()


def test_transfer_view_moves_stock_between_locations(
    client, event, stock_admin, stock_location, other_location, badge_type
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=20, actor=stock_admin
    )
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.post(
        reverse(
            "badges:stock-transfer-create",
            kwargs={"event_pk": event.pk, "source_pk": stock_location.pk},
        ),
        {
            "operation_id": new_operation_id(),
            "destination_location_id": str(other_location.pk),
            "badge_type_id": str(badge_type.pk),
            "quantity": "5",
        },
    )
    assert response.status_code == 302
    assert BadgeStockLedgerEntry.objects.filter(
        location=other_location, entry_type="TRANSFER_IN", quantity_delta=5
    ).exists()


def test_reconciliation_view_records_a_discrepancy(
    client, event, stock_admin, stock_location, badge_type
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    sign_in_operational(client, stock_admin.email_normalized)
    response = client.post(
        reverse(
            "badges:stock-reconciliation-create",
            kwargs={"event_pk": event.pk, "location_pk": stock_location.pk},
        ),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "counted_quantity": "9",
            "notes": "Spot check.",
        },
    )
    assert response.status_code == 302
    assert AuditEvent.objects.filter(action_code="BDG_STOCK_RECONCILIATION_RECORDED").exists()


# ---------------------------------------------------------------------------
# Badge issuance (Badge Stock Issuers)
# ---------------------------------------------------------------------------


def test_an_issuer_can_issue_a_badge(
    client, event, stock_admin, stock_issuer, stock_location, badge_type, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    assert response.status_code == 302
    from apps.badges.models import BadgeIssuance

    assert BadgeIssuance.objects.filter(
        badge_assignment=current_badge_assignment, status=BadgeIssuanceStatus.ISSUED
    ).exists()
    assert AuditEvent.objects.filter(action_code="BDG_STOCK_ISSUED").exists()


def test_issuing_the_wrong_badge_type_via_the_view_is_refused_with_no_side_effect(
    client, event, stock_admin, stock_issuer, stock_location, current_badge_assignment
):
    from apps.accreditation.models import BadgeType

    wrong_type = BadgeType.objects.create(event_edition=event, code="WRONGTYPE", name="Wrong")
    _receive_into(
        event=event, badge_type=wrong_type, location=stock_location, quantity=5, actor=stock_admin
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(wrong_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    assert response.status_code == 302
    from apps.badges.models import BadgeIssuance

    assert not BadgeIssuance.objects.filter(badge_assignment=current_badge_assignment).exists()
    assert AuditEvent.objects.filter(action_code="BDG_STOCK_ISSUANCE_BLOCKED").exists()


def test_an_issuer_can_choose_and_consume_an_allocation_via_the_view(
    client, event, stock_admin, stock_issuer, stock_location, badge_type, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
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
    operation_id = new_operation_id()
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": operation_id,
            "badge_type_id": str(badge_type.pk),
            "location_id": str(stock_location.pk),
            "allocation_id": str(allocation.pk),
        },
    )

    assert response.status_code == 302
    allocation.refresh_from_db()
    assert allocation.consumed_quantity == 1
    assert current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 4
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 2
    )
    assert (
        available_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 2
    )
    operation = BadgeStockOperation.objects.get(operation_id=operation_id)
    assert operation.allocation_id == allocation.pk
    assert operation.issuance_id is not None


def test_a_forged_cross_event_issue_post_is_refused_with_no_side_effect(
    client,
    event,
    badge_type,
    stock_admin,
    stock_issuer,
    stock_location,
    current_badge_assignment,
    foreign_badge_type,
    foreign_location,
):
    """A forged POST naming another event's Badge Type or location must not
    create an issuance, move stock, or write a ledger row (§2)."""
    from apps.badges.models import BadgeIssuance, BadgeStockLedgerEntry

    # Deliberately no stock is staged for the foreign objects: the point is
    # that the request is refused on SCOPE, before availability is ever
    # consulted, so there is nothing for it to draw on either way.
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    ledger_before = BadgeStockLedgerEntry.objects.count()

    foreign_type_post = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(foreign_badge_type.pk),
            "location_id": str(stock_location.pk),
        },
    )
    foreign_location_post = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(foreign_badge_type.pk),
            "location_id": str(foreign_location.pk),
        },
    )

    # 404 -- an out-of-event object is indistinguishable from a nonexistent one.
    assert foreign_type_post.status_code == 404
    assert foreign_location_post.status_code == 404
    assert not BadgeIssuance.objects.filter(
        registration_id=current_badge_assignment.registration_id
    ).exists()
    assert BadgeStockLedgerEntry.objects.count() == ledger_before


def test_the_issuance_page_lists_an_active_allocation_with_remaining_stock(
    client, event, stock_admin, stock_issuer, stock_location, badge_type, current_badge_assignment
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=3, actor=stock_admin
    )
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=2,
        purpose_code=StockAllocationPurpose.CHECKPOINT_OPENING,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.get(
        reverse(
            "badges:registration-badge-issuance",
            kwargs={"pk": current_badge_assignment.registration_id},
        )
    )

    assert response.status_code == 200
    assert str(allocation.pk) in response.content.decode()


def test_a_forged_allocation_for_another_location_is_refused_without_side_effects(
    client,
    event,
    stock_admin,
    stock_issuer,
    stock_location,
    other_location,
    badge_type,
    current_badge_assignment,
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=4, actor=stock_admin
    )
    allocation = allocate_stock(
        event_edition=event,
        badge_type=badge_type,
        location=stock_location,
        quantity=2,
        purpose_code=StockAllocationPurpose.OPERATIONAL_BUFFER,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    ledger_before = BadgeStockLedgerEntry.objects.count()
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": current_badge_assignment.registration_id}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "location_id": str(other_location.pk),
            "allocation_id": str(allocation.pk),
        },
    )

    assert response.status_code == 404
    allocation.refresh_from_db()
    assert allocation.consumed_quantity == 0
    assert BadgeStockLedgerEntry.objects.count() == ledger_before
    assert not BadgeStockOperation.objects.filter(operation_type="ISSUE").exists()


def test_a_forged_cross_event_replacement_post_is_refused_with_no_side_effect(
    client,
    event,
    stock_admin,
    stock_issuer,
    stock_location,
    badge_type,
    current_badge_assignment,
    foreign_badge_type,
):
    from apps.badges.models import BadgeIssuance, BadgeIssuanceStatus, BadgeStockLedgerEntry
    from apps.badges.services import issue_badge

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
    sign_in_operational(client, stock_issuer.email_normalized)
    ledger_before = BadgeStockLedgerEntry.objects.count()

    response = client.post(
        reverse(
            "badges:badge-issuance-replace",
            kwargs={
                "registration_pk": current_badge_assignment.registration_id,
                "pk": issuance.pk,
            },
        ),
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": str(issuance.version),
            "badge_type_id": str(foreign_badge_type.pk),
            "reason_code": "WRONG_BADGE_TYPE",
            "reason_text": "",
        },
    )

    assert response.status_code == 404
    issuance.refresh_from_db()
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert issuance.replaced_by_id is None
    assert BadgeStockLedgerEntry.objects.count() == ledger_before
    assert (
        BadgeIssuance.objects.filter(
            registration_id=current_badge_assignment.registration_id
        ).count()
        == 1
    )


def test_a_forged_substitution_to_another_local_badge_type_is_refused(
    client, event, stock_admin, stock_issuer, stock_location, badge_type, current_badge_assignment
):
    """Same event, wrong Badge Type: refused by the service's
    current-assignment check, with no substitution (§3)."""
    from apps.accreditation.models import BadgeType
    from apps.badges.models import BadgeIssuanceStatus, BadgeStockLedgerEntry
    from apps.badges.services import current_balance, issue_badge

    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    other_type = BadgeType.objects.create(event_edition=event, code="OTHERTYPE", name="Other")
    _receive_into(
        event=event, badge_type=other_type, location=stock_location, quantity=5, actor=stock_admin
    )
    issuance = issue_badge(
        badge_assignment=current_badge_assignment,
        badge_type=badge_type,
        location=stock_location,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    ledger_before = BadgeStockLedgerEntry.objects.count()

    response = client.post(
        reverse(
            "badges:badge-issuance-replace",
            kwargs={
                "registration_pk": current_badge_assignment.registration_id,
                "pk": issuance.pk,
            },
        ),
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": str(issuance.version),
            "badge_type_id": str(other_type.pk),
            "reason_code": "WRONG_BADGE_TYPE",
            "reason_text": "",
        },
    )

    assert response.status_code == 302
    issuance.refresh_from_db()
    assert issuance.status == BadgeIssuanceStatus.ISSUED
    assert BadgeStockLedgerEntry.objects.count() == ledger_before
    assert current_balance(event_edition=event, badge_type=other_type, location=stock_location) == 5


def test_an_issuer_cannot_reach_the_stock_accounting_dashboard(client, event, stock_issuer):
    """The documented handover-only boundary (Prompt 3 correction §8).

    `Badge Stock Issuers` keeps `view_stocklocation` so the issuance page
    can offer an authorized active issuing location -- but that permission
    must NOT open the stock-accounting dashboard, which carries the ledger,
    print batches, transfers, adjustments, and reconciliation.
    """
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.get(reverse("badges:stock-dashboard", kwargs={"event_pk": event.pk}))
    assert response.status_code == 404


def test_an_issuer_still_sees_the_issuing_location_selector(
    client, event, stock_admin, stock_issuer, stock_location, badge_type, current_badge_assignment
):
    """The other half of the same boundary: an issuer must still be able to
    pick an authorized active location when handing a badge over."""
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=2, actor=stock_admin
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.get(
        reverse(
            "badges:registration-badge-issuance",
            kwargs={"pk": current_badge_assignment.registration_id},
        )
    )
    assert response.status_code == 200
    assert stock_location.code in response.content.decode()


def test_an_issuer_cannot_reach_print_batches(client, event, stock_admin, stock_issuer, badge_type):
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=5,
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    sign_in_operational(client, stock_issuer.email_normalized)
    response = client.get(reverse("badges:print-batch-detail", kwargs={"pk": batch.pk}))
    assert response.status_code in (302, 403, 404)


def test_an_issuer_cannot_allocate_or_transfer_stock(
    client, event, stock_admin, stock_issuer, stock_location, other_location, badge_type
):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=10, actor=stock_admin
    )
    sign_in_operational(client, stock_issuer.email_normalized)

    allocate = client.post(
        reverse(
            "badges:stock-allocation-create",
            kwargs={"event_pk": event.pk, "location_pk": stock_location.pk},
        ),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(badge_type.pk),
            "quantity": "3",
            "purpose_code": "SHIFT_RESERVE",
        },
    )
    transfer = client.post(
        reverse(
            "badges:stock-transfer-create",
            kwargs={"event_pk": event.pk, "source_pk": stock_location.pk},
        ),
        {
            "operation_id": new_operation_id(),
            "destination_location_id": str(other_location.pk),
            "badge_type_id": str(badge_type.pk),
            "quantity": "3",
        },
    )
    assert allocate.status_code in (302, 403, 404)
    assert transfer.status_code in (302, 403, 404)

    from apps.badges.models import BadgeStockAllocation
    from apps.badges.services import current_balance, reserved_balance

    assert not BadgeStockAllocation.objects.exists()
    assert (
        reserved_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 0
    )
    assert (
        current_balance(event_edition=event, badge_type=badge_type, location=stock_location) == 10
    )
