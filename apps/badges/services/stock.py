"""Generic physical badge stock services (Phase 3 Prompt 3).

Every state change lives here, inside an explicit transaction, never in a
view, a form, a model `save()`, or a signal -- exactly the discipline
`apps.badges.services` (pass lifecycle) already established. Three
invariants this module is built around, each named in ADR-0020:

1. **Idempotency is universal, not just for quantity-moving commands.**
   Every stock command -- creating a location, changing a batch's
   production status, marking a badge lost -- is bound to a caller-supplied
   `operation_id` through `BadgeStockOperation`, structurally identical to
   `PassLifecycleOperation`. `command_fingerprint` and `normalize_reason_text`
   are reused UNCHANGED from `apps.badges.services` rather than
   reimplemented, per the project's own reuse rule.
2. **Stock can never go negative.** Every quantity-decreasing command locks
   the affected `BadgeStockBalance` row(s) before checking the resulting
   quantity, in a single transaction with the ledger insert. A transfer
   locks BOTH balance rows it touches, in a fixed order (by location UUID),
   so two transfers that touch the same pair of locations from opposite
   directions can never deadlock.
3. **A wrong Badge Type is never substituted automatically.** `issue_badge`
   requires the caller to name the exact Badge Type being physically handed
   over, and refuses -- posting no ledger entry and creating no issuance --
   unless it is byte-identical to the registration's current
   `BadgeTypeAssignment.badge_type`.

No entry device, device scope, device session, entry event, override,
security restriction, offline package, or synchronization concept appears
anywhere in this module.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.badges.models import (
    PRINT_BATCH_TRANSITIONS,
    BadgeIssuance,
    BadgeIssuanceReasonCode,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    BadgeStockBalance,
    BadgeStockEntryType,
    BadgeStockLedgerEntry,
    BadgeStockOperation,
    PrintBatch,
    PrintBatchStatus,
    StockAdjustmentReasonCode,
    StockAllocationPurpose,
    StockAllocationStatus,
    StockLocation,
    StockOperationType,
    StockReconciliation,
    StockTransfer,
    StockTransferStatus,
)
from apps.badges.services import (
    OperationConflictError,
    command_fingerprint,
    normalize_reason_text,
)
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StockServiceError(Exception):
    """Base for every physical badge stock domain failure."""


class StockStateError(StockServiceError):
    """Raised for a transition or action the current state does not permit."""


class StockConcurrencyError(StockServiceError):
    """Raised when the caller's expected lock version is stale."""


class InsufficientStockError(StockServiceError):
    """Raised when a command would take a location's balance below zero."""


class WrongBadgeTypeError(StockServiceError):
    """Raised when the requested Badge Type does not match the assignment.

    No automatic substitution ever happens: the caller's requested Badge
    Type must be byte-identical to the registration's current assignment.
    """


class RegistrationNotEligibleError(StockServiceError):
    """Raised when the Registration Context is no longer an active approved
    context -- withdrawn, operationally cancelled, superseded, or not
    approved -- so no physical badge may be newly issued or replaced for it
    (Phase 3 Prompt 8, P8-02; PRD FR-BDG-012 "approved context").

    Always raised before any balance, allocation, ledger row, issuance,
    operation record, or outbox message is written.
    """

    #: The controlled reason recorded on the denial audit.
    reason_code = "REGISTRATION_NOT_ELIGIBLE"


class AttendanceMarkingError(StockServiceError):
    """Raised when a physical badge's attendance marking cannot be issued or
    recorded: the registration's attendance days are not classified while
    attendance enforcement is active (`UNCLASSIFIED`), or the confirmed
    marking differs from the current attendance days (`MISMATCH`). Never
    inferred from the Badge Type."""

    def __init__(self, message: str = "", *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class DuplicateStockLocationError(StockServiceError):
    """Raised when a location code is reused within the same event edition."""


class CrossEventScopeError(StockServiceError):
    """Raised when a command's inputs span more than one EventEdition.

    Always raised BEFORE anything is created or changed, so a rejected
    cross-event command leaves no balance, ledger row, issuance, operation
    record, audit event, or outbox message behind (Prompt 3 correction §2).
    """


# ---------------------------------------------------------------------------
# Idempotency (mirrors `apps.badges.services` exactly -- ADR-0020 §4)
# ---------------------------------------------------------------------------

#: Distinct from every other advisory-lock classid in the project (core's
#: 1-6, badges pass lifecycle's 0x42444745 "BDGE") so a stock command can
#: never contend with an unrelated advisory lock.
_STOCK_LOCK_CLASSID = 0x42445354  # "BDST"


def _require_operation_id(operation_id: object) -> str:
    if not isinstance(operation_id, str) or not 16 <= len(operation_id) <= 64:
        raise StockServiceError("A stock operation identifier is required.")
    return operation_id


def _acquire_stock_lock(operation_id: str) -> None:
    """Serialize concurrent attempts that share one `operation_id`.

    Transaction-scoped and released automatically on commit or rollback,
    exactly mirroring `apps.badges.services._acquire_idempotency_lock`.
    """
    from apps.core.concurrency import lock_key, order_lock_keys

    digest = hashlib.sha256(operation_id.encode("utf-8")).digest()
    pairs = order_lock_keys([lock_key(_STOCK_LOCK_CLASSID, digest)])
    with connection.cursor() as cursor:
        for classid, objid in pairs:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [classid, objid])


def _match_existing_stock_operation(
    operation_id: str, fingerprint: str
) -> BadgeStockOperation | None:
    existing = BadgeStockOperation.objects.filter(operation_id=operation_id).first()
    if existing is None:
        return None
    if not existing.command_fingerprint:
        raise OperationConflictError(
            "This operation identifier predates command binding and cannot be replayed."
        )
    if existing.command_fingerprint != fingerprint:
        raise OperationConflictError(
            "This operation identifier was already used for a different command."
        )
    return existing


def _record_stock_operation(
    *,
    operation_id: str,
    operation_type: str,
    actor,
    fingerprint: str,
    target_type: str,
    target_id: str,
    location: StockLocation | None = None,
    print_batch: PrintBatch | None = None,
    transfer: StockTransfer | None = None,
    issuance: BadgeIssuance | None = None,
    ledger_entry: BadgeStockLedgerEntry | None = None,
    reconciliation: StockReconciliation | None = None,
    allocation: BadgeStockAllocation | None = None,
) -> BadgeStockOperation:
    return BadgeStockOperation.objects.create(
        operation_id=operation_id,
        operation_type=operation_type,
        command_fingerprint=fingerprint,
        target_type=target_type,
        target_id=target_id,
        location=location,
        print_batch=print_batch,
        transfer=transfer,
        issuance=issuance,
        ledger_entry=ledger_entry,
        reconciliation=reconciliation,
        allocation=allocation,
        performed_by=actor if getattr(actor, "pk", None) else None,
    )


def _stock_audit(
    *,
    action_code: str,
    actor,
    result: str = "SUCCESS",
    reason_code: str | None = None,
    target_type: str = "",
    target_uuid=None,
    event_edition_id=None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=target_type,
            target_uuid=target_uuid,
            event_edition_id=event_edition_id,
            result=result,
            reason_code=reason_code,
            before_summary=before,
            after_summary=after,
        )
    )


def _lock_eligible_registration(registration_id):
    """Lock the Registration row and require an active approved context.

    The FIRST row lock a physical-badge issuance or replacement takes, so the
    global order is Registration -> issuance -> assignment -> allocation ->
    balances. That is the order `apps.accreditation.services` already uses
    (Registration before assignment) and the one participant withdrawal and
    operational cancellation serialize on, so no lock-order inversion is
    introduced: a concurrent withdrawal either commits first and is seen
    here, or waits until this command has committed (P8-02).
    """
    from apps.registrations.models import Registration
    from apps.registrations.selectors import is_active_approved_context

    registration = Registration.objects.select_for_update(of=("self",)).get(pk=registration_id)
    if not is_active_approved_context(registration):
        raise RegistrationNotEligibleError(
            "This Registration Context is no longer approved, so no physical badge may be "
            "issued for it."
        )
    return registration


def _require_attendance_classified(registration) -> None:
    from apps.accreditation import attendance

    policy = attendance.policy_for(registration.event_edition_id)
    if policy is not None and policy.enforcement_active:
        if attendance.current_entitlement(registration.pk) is None:
            raise AttendanceMarkingError(
                "Attendance enforcement is active and this registration's attendance days "
                "are not classified.",
                code="UNCLASSIFIED",
            )


def record_attendance_marking(
    *, issuance: BadgeIssuance, attendance_marking: str, actor, expected_lock_version: int
) -> BadgeIssuance:
    """Record that the handed-over badge now carries the attendance marking
    (sticker, overlay or print variant) of the registration's CURRENT
    attendance days.

    `attendance_marking` is what the operator confirms having applied; it
    must equal the current entitlement exactly -- no substitution, no
    inference from the Badge Type. Only a current (ISSUED) badge of an active
    approved registration can be marked. Lock order: Registration first,
    then the issuance (as every issuance command)."""
    from apps.accreditation import attendance
    from apps.accreditation.models import AttendanceCategory

    if attendance_marking not in AttendanceCategory.values:
        raise AttendanceMarkingError("Unknown attendance marking.", code="MISMATCH")
    with transaction.atomic():
        locked_registration = _lock_eligible_registration(issuance.registration_id)
        locked = BadgeIssuance.objects.select_for_update().get(pk=issuance.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This badge issuance changed meanwhile.")
        if locked.status != BadgeIssuanceStatus.ISSUED:
            raise StockStateError("Only a current physical badge can be marked.")
        current = attendance.current_entitlement(locked_registration.pk)
        if current is None:
            raise AttendanceMarkingError(
                "This registration's attendance days are not classified.", code="UNCLASSIFIED"
            )
        if current.category != attendance_marking:
            raise AttendanceMarkingError(
                "The marking differs from the current attendance days.", code="MISMATCH"
            )
        before = locked.attendance_marking
        locked.attendance_marking = attendance_marking
        locked.attendance_marking_recorded_at = timezone.now()
        locked.attendance_marking_recorded_by = actor if getattr(actor, "pk", None) else None
        locked.version += 1
        locked.save(
            update_fields=[
                "attendance_marking",
                "attendance_marking_recorded_at",
                "attendance_marking_recorded_by",
                "version",
                "updated_at",
            ]
        )
        _stock_audit(
            action_code=action_codes.BADGE_ATTENDANCE_MARKING_RECORDED,
            actor=actor,
            target_type="BadgeIssuance",
            target_uuid=locked.pk,
            event_edition_id=locked_registration.event_edition_id,
            before={"attendance_marking": before or None},
            after={"attendance_marking": attendance_marking},
        )
    return locked


def _new_ledger_operation_id() -> str:
    """An internally generated identifier for one leg of a multi-row command.

    A transfer posts TWO ledger rows (`TRANSFER_OUT` and `TRANSFER_IN`), and
    `BadgeStockLedgerEntry.operation_id` is unique, so both cannot carry the
    literal command `operation_id`. Idempotency for the *command* itself
    still runs entirely through `BadgeStockOperation`; this is only the
    schema-required per-row identifier, linked back to the command through
    the `transfer` foreign key both rows share.
    """
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Event-edition boundary (Prompt 3 correction §2)
# ---------------------------------------------------------------------------


def _event_id_of(obj) -> object:
    """The `EventEdition` id an object belongs to, however it is reached.

    `BadgeTypeAssignment` and `Registration` carry `event_edition_id`
    directly; a `BadgeIssuance` reaches one through its location. Anything
    without a resolvable event is a programming error, not a user error.
    """
    direct = getattr(obj, "event_edition_id", None)
    if direct is not None:
        return direct
    location = getattr(obj, "location", None)
    if location is not None:
        return location.event_edition_id
    raise StockServiceError("Cannot resolve the event edition for this object.")


def require_same_event(event_edition_id, **named_objects) -> None:
    """Refuse any command whose inputs span more than one EventEdition.

    Enforced HERE, in the service boundary, not only in the views (Prompt 3
    correction §2): a forged POST, a mistyped identifier, or a future
    caller that bypasses the view must all fail the same way. Raised BEFORE
    any balance, ledger row, issuance, operation record, audit event, or
    outbox message is created, so a rejected cross-event command leaves
    nothing behind at all.

    The message deliberately names only the offending argument, never the
    other event's code or identifier.
    """
    for name, obj in named_objects.items():
        if obj is None:
            continue
        if _event_id_of(obj) != event_edition_id:
            raise CrossEventScopeError(
                f"The supplied {name} does not belong to this event edition."
            )


# ---------------------------------------------------------------------------
# Balance projection (Schema §10.7) -- the ledger stays authoritative
# ---------------------------------------------------------------------------


def _balance_key(*, event_edition_id, badge_type_id, location_id) -> tuple[str, str, str]:
    """The canonical, totally ordered identity of one balance row."""
    return (str(event_edition_id), str(badge_type_id), str(location_id))


def _get_or_create_balance_locked(
    *, event_edition_id, badge_type_id, location_id
) -> BadgeStockBalance:
    """Return the locked balance row for one (event, badge type, location).

    Creates it at zero on first use. The bounded retry mirrors
    `apps.badges.services._get_or_create_pseudonym`: two concurrent first
    movements for the same triple may both miss the existence check, so one
    loses the create to `IntegrityError` and simply re-reads.

    Callers that touch MORE THAN ONE balance row must go through
    `_lock_balances`, never call this twice in an ad-hoc order.
    """
    locked = (
        BadgeStockBalance.objects.select_for_update()
        .filter(
            event_edition_id=event_edition_id, badge_type_id=badge_type_id, location_id=location_id
        )
        .first()
    )
    if locked is not None:
        return locked
    for _attempt in range(5):
        try:
            with transaction.atomic():
                BadgeStockBalance.objects.create(
                    event_edition_id=event_edition_id,
                    badge_type_id=badge_type_id,
                    location_id=location_id,
                    quantity=0,
                )
            break
        except IntegrityError:
            pass
    return BadgeStockBalance.objects.select_for_update().get(
        event_edition_id=event_edition_id, badge_type_id=badge_type_id, location_id=location_id
    )


def _lock_balances(keys) -> dict[tuple[str, str, str], BadgeStockBalance]:
    """Lock every balance row a command touches, in ONE global order.

    The single ordering rule for the whole module (Prompt 3 correction §7):
    ascending `(event_edition_id, badge_type_id, location_id)` as strings.
    Every command that touches more than one balance row -- a transfer, a
    replacement that returns the original to one location and issues from
    another, a replacement across Badge Types -- acquires its locks through
    this one function, so no two commands can ever take the same pair of
    rows in opposite orders and deadlock.

    Duplicate keys collapse to one lock, so a caller may safely pass the
    same row twice (a replacement whose return and issue locations
    coincide).
    """
    locked: dict[tuple[str, str, str], BadgeStockBalance] = {}
    for key in sorted(set(keys)):
        event_edition_id, badge_type_id, location_id = key
        locked[key] = _get_or_create_balance_locked(
            event_edition_id=event_edition_id,
            badge_type_id=badge_type_id,
            location_id=location_id,
        )
    return locked


def current_balance(*, event_edition, badge_type, location) -> int:
    """The projected ON-HAND balance, or 0 if nothing ever touched this triple."""
    row = BadgeStockBalance.objects.filter(
        event_edition=event_edition, badge_type=badge_type, location=location
    ).first()
    return row.quantity if row is not None else 0


def reserved_balance(*, event_edition, badge_type, location) -> int:
    """How much of the on-hand stock is currently allocated (TRD `PRINT-002`)."""
    row = BadgeStockBalance.objects.filter(
        event_edition=event_edition, badge_type=badge_type, location=location
    ).first()
    return row.reserved_quantity if row is not None else 0


def available_balance(*, event_edition, badge_type, location) -> int:
    """On-hand minus allocated: what an unreserved consumer may draw on."""
    row = BadgeStockBalance.objects.filter(
        event_edition=event_edition, badge_type=badge_type, location=location
    ).first()
    return row.available_quantity if row is not None else 0


def reconstruct_balance_from_ledger(*, event_edition, badge_type, location) -> int:
    """Recompute the ON-HAND balance by summing every ledger row directly.

    The ledger remains authoritative (Schema §10.7): this must always equal
    `current_balance` for the same triple. Allocation deliberately does not
    appear here, because reserving stock moves nothing -- that is precisely
    why it posts no ledger row (ADR-0020 §7).
    """
    from django.db.models import Sum

    total = BadgeStockLedgerEntry.objects.filter(
        event_edition=event_edition, badge_type=badge_type, location=location
    ).aggregate(total=Sum("quantity_delta"))["total"]
    return total or 0


def _apply_locked_balance_delta(
    balance: BadgeStockBalance, delta: int, *, from_reservation: bool = False
) -> BadgeStockBalance:
    """Mutate an ALREADY-LOCKED balance row by `delta`.

    A decrement is checked against AVAILABLE stock, not raw on-hand, so a
    transfer, issuance, or negative adjustment can never quietly consume
    units another operator has allocated. The one exception is
    `from_reservation=True`: an issuance drawing on its own allocation
    releases one reserved unit at the same moment it removes it from
    on-hand, so availability is unchanged and the availability check would
    be wrong.
    """
    new_quantity = balance.quantity + delta
    if new_quantity < 0:
        raise InsufficientStockError("This operation would take the location's stock below zero.")
    fields = ["quantity", "updated_at"]
    if from_reservation:
        if balance.reserved_quantity + delta < 0:
            raise InsufficientStockError(
                "This operation would release more reserved stock than is allocated."
            )
        balance.reserved_quantity = balance.reserved_quantity + delta
        fields.append("reserved_quantity")
    elif new_quantity < balance.reserved_quantity:
        raise InsufficientStockError(
            "This operation would consume stock that is currently allocated."
        )
    balance.quantity = new_quantity
    balance.save(update_fields=fields)
    return balance


def _apply_locked_reservation_delta(balance: BadgeStockBalance, delta: int) -> BadgeStockBalance:
    """Reserve (`delta > 0`) or release (`delta < 0`) without moving stock."""
    new_reserved = balance.reserved_quantity + delta
    if new_reserved < 0:
        raise StockStateError("This would release more stock than is currently allocated.")
    if new_reserved > balance.quantity:
        raise InsufficientStockError(
            "There is not enough available stock at this location to allocate."
        )
    balance.reserved_quantity = new_reserved
    balance.save(update_fields=["reserved_quantity", "updated_at"])
    return balance


# ---------------------------------------------------------------------------
# Stock locations
# ---------------------------------------------------------------------------


def create_stock_location(
    *,
    event_edition,
    code: str,
    name: str,
    location_type: str,
    actor,
    operation_id: str,
    name_fr: str = "",
    name_ar: str = "",
    custodian_group=None,
) -> StockLocation:
    operation_id = _require_operation_id(operation_id)
    # Every material input is bound, including the translated names and the
    # custodian group (Prompt 3 correction §6): a replay that silently kept
    # the first request's Arabic name or custodian group while the operator
    # believed they had corrected it is a real defect, not a harmless retry.
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.LOCATION_CREATE,
        actor=actor,
        target_type="EventEdition",
        target_id=str(event_edition.pk),
        parameters={
            "code": code,
            "name": name,
            "name_fr_text": name_fr,
            "name_ar_text": name_ar,
            "location_type": location_type,
            "custodian_group_id": str(getattr(custodian_group, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.location

        if StockLocation.objects.filter(event_edition=event_edition, code=code).exists():
            raise DuplicateStockLocationError(
                "A stock location with this code already exists for this event edition."
            )
        try:
            location = StockLocation.objects.create(
                event_edition=event_edition,
                code=code,
                name=name,
                name_fr=name_fr,
                name_ar=name_ar,
                location_type=location_type,
                custodian_group=custodian_group,
                created_by=actor if getattr(actor, "pk", None) else None,
            )
        except IntegrityError as exc:
            raise DuplicateStockLocationError(
                "A stock location with this code already exists for this event edition."
            ) from exc

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.LOCATION_CREATE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="EventEdition",
            target_id=str(event_edition.pk),
            location=location,
        )
        _stock_audit(
            action_code=action_codes.STOCK_LOCATION_CREATED,
            actor=actor,
            target_type="StockLocation",
            target_uuid=location.pk,
            event_edition_id=event_edition.pk,
            after={"code": location.code, "location_type": location.location_type},
        )
    return location


# ---------------------------------------------------------------------------
# Print batches
# ---------------------------------------------------------------------------


def _new_batch_reference() -> str:
    import base64

    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:22]


def create_print_batch(
    *,
    event_edition,
    badge_type,
    planned_quantity: int,
    actor,
    operation_id: str,
    artwork_version: str = "",
    supplier_reference: str = "",
    destination_location: StockLocation | None = None,
) -> PrintBatch:
    operation_id = _require_operation_id(operation_id)
    if planned_quantity < 1:
        raise StockServiceError("Planned quantity must be at least 1.")
    require_same_event(
        event_edition.pk, badge_type=badge_type, destination_location=destination_location
    )
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.BATCH_CREATE,
        actor=actor,
        target_type="BadgeType",
        target_id=str(badge_type.pk),
        parameters={
            "event_edition_id": str(event_edition.pk),
            "planned_quantity": planned_quantity,
            "artwork_version": artwork_version,
            "supplier_reference_text": supplier_reference,
            "destination_location_id": str(getattr(destination_location, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.print_batch

        last_error: Exception | None = None
        batch: PrintBatch | None = None
        for _attempt in range(5):
            try:
                with transaction.atomic():
                    batch = PrintBatch.objects.create(
                        public_reference=_new_batch_reference(),
                        event_edition=event_edition,
                        badge_type=badge_type,
                        artwork_version=artwork_version,
                        planned_quantity=planned_quantity,
                        supplier_reference=supplier_reference,
                        destination_location=destination_location,
                        status=PrintBatchStatus.DRAFT,
                        created_by=actor if getattr(actor, "pk", None) else None,
                    )
                break
            except IntegrityError as exc:
                last_error = exc
                batch = None
        if batch is None:
            raise StockServiceError(
                "Could not allocate a unique print batch reference."
            ) from last_error

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.BATCH_CREATE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeType",
            target_id=str(badge_type.pk),
            print_batch=batch,
        )
        _stock_audit(
            action_code=action_codes.PRINT_BATCH_CREATED,
            actor=actor,
            target_type="PrintBatch",
            target_uuid=batch.pk,
            event_edition_id=event_edition.pk,
            after={"public_reference": batch.public_reference, "status": batch.status},
        )
    return batch


def change_batch_status(
    *,
    batch: PrintBatch,
    target_status: str,
    actor,
    operation_id: str,
    expected_lock_version: int,
) -> PrintBatch:
    """Every DRAFT/READY/IN_PRODUCTION/CANCELLED transition that moves no quantity.

    Receiving a batch (`IN_PRODUCTION` -> `RECEIVED`) is `receive_print_batch`,
    not this function, because receipt also posts a ledger entry.
    """
    operation_id = _require_operation_id(operation_id)
    if target_status == PrintBatchStatus.RECEIVED:
        raise StockStateError("Use receive_print_batch to record a batch's receipt.")
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.BATCH_STATUS_CHANGE,
        actor=actor,
        target_type="PrintBatch",
        target_id=str(batch.pk),
        parameters={
            "target_status": target_status,
            "expected_lock_version": expected_lock_version,
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.print_batch

        locked = PrintBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This print batch changed since it was loaded.")
        if target_status not in PRINT_BATCH_TRANSITIONS.get(locked.status, frozenset()):
            raise StockStateError(
                f"A print batch cannot move from {locked.status} to {target_status}."
            )

        before = {"status": locked.status}
        now = timezone.now()
        locked.status = target_status
        locked.version = locked.version + 1
        fields = ["status", "version", "updated_at"]
        if target_status == PrintBatchStatus.IN_PRODUCTION:
            locked.production_started_at = now
            fields.append("production_started_at")
        locked.save(update_fields=fields)

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.BATCH_STATUS_CHANGE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="PrintBatch",
            target_id=str(locked.pk),
            print_batch=locked,
        )
        _stock_audit(
            action_code=action_codes.PRINT_BATCH_STATUS_CHANGED,
            actor=actor,
            target_type="PrintBatch",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            before=before,
            after={"status": locked.status},
        )
    return locked


def receive_print_batch(
    *,
    batch: PrintBatch,
    produced_quantity: int,
    accepted_quantity: int,
    damaged_quantity: int,
    actor,
    operation_id: str,
    expected_lock_version: int,
    destination_location: StockLocation | None = None,
) -> PrintBatch:
    """`IN_PRODUCTION` -> `RECEIVED`. Only `accepted_quantity` ever reaches stock.

    Units the supplier delivered damaged never enter the ledger at all
    (ADR-0020 §3): `produced_quantity` must equal `accepted_quantity +
    damaged_quantity`, enforced by both this check and the database's
    `bdg_printbatch_receipt_balances_ck` constraint.
    """
    operation_id = _require_operation_id(operation_id)
    if produced_quantity != accepted_quantity + damaged_quantity:
        raise StockServiceError("Produced quantity must equal accepted plus damaged quantity.")
    if accepted_quantity < 0 or damaged_quantity < 0 or produced_quantity < 0:
        raise StockServiceError("Quantities cannot be negative.")
    require_same_event(batch.event_edition_id, destination_location=destination_location)

    fingerprint = command_fingerprint(
        operation_type=StockOperationType.BATCH_RECEIVE,
        actor=actor,
        target_type="PrintBatch",
        target_id=str(batch.pk),
        parameters={
            "produced_quantity": produced_quantity,
            "accepted_quantity": accepted_quantity,
            "damaged_quantity": damaged_quantity,
            "expected_lock_version": expected_lock_version,
            "destination_location_id": str(getattr(destination_location, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.print_batch

        locked = PrintBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This print batch changed since it was loaded.")
        if locked.status != PrintBatchStatus.IN_PRODUCTION:
            raise StockStateError("Only a batch in production can be received.")

        location = destination_location or locked.destination_location
        if location is None:
            raise StockServiceError("A destination stock location is required to receive a batch.")

        before = {"status": locked.status}
        now = timezone.now()
        locked.status = PrintBatchStatus.RECEIVED
        locked.version = locked.version + 1
        locked.produced_quantity = produced_quantity
        locked.accepted_quantity = accepted_quantity
        locked.damaged_quantity = damaged_quantity
        locked.destination_location = location
        locked.received_at = now
        locked.received_by = actor if getattr(actor, "pk", None) else None
        locked.save(
            update_fields=[
                "status",
                "version",
                "produced_quantity",
                "accepted_quantity",
                "damaged_quantity",
                "destination_location",
                "received_at",
                "received_by",
                "updated_at",
            ]
        )

        ledger_entry = None
        if accepted_quantity > 0:
            key = _balance_key(
                event_edition_id=locked.event_edition_id,
                badge_type_id=locked.badge_type_id,
                location_id=location.pk,
            )
            balance = _lock_balances([key])[key]
            _apply_locked_balance_delta(balance, accepted_quantity)
            ledger_entry = BadgeStockLedgerEntry.objects.create(
                operation_id=operation_id,
                event_edition=locked.event_edition,
                badge_type=locked.badge_type,
                location=location,
                entry_type=BadgeStockEntryType.RECEIVE,
                quantity_delta=accepted_quantity,
                print_batch=locked,
                occurred_at=now,
                recorded_by=actor if getattr(actor, "pk", None) else None,
            )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.BATCH_RECEIVE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="PrintBatch",
            target_id=str(locked.pk),
            print_batch=locked,
            ledger_entry=ledger_entry,
        )
        _stock_audit(
            action_code=action_codes.PRINT_BATCH_RECEIVED,
            actor=actor,
            target_type="PrintBatch",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            before=before,
            after={
                "status": locked.status,
                "accepted_quantity": accepted_quantity,
                "damaged_quantity": damaged_quantity,
            },
        )
        if accepted_quantity > 0:
            PersistentOutboxPublisher().enqueue(
                OutboxMessage(
                    event_type="badges.stock.received",
                    aggregate_type="PrintBatch",
                    aggregate_id=str(locked.pk),
                    payload={
                        "badge_type_id": str(locked.badge_type_id),
                        "location_id": str(location.pk),
                        "accepted_quantity": accepted_quantity,
                    },
                )
            )
    return locked


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def transfer_stock(
    *,
    event_edition,
    badge_type,
    source_location: StockLocation,
    destination_location: StockLocation,
    quantity: int,
    actor,
    operation_id: str,
    note: str = "",
    received_by=None,
) -> StockTransfer:
    operation_id = _require_operation_id(operation_id)
    if quantity < 1:
        raise StockServiceError("Transfer quantity must be at least 1.")
    if source_location.pk == destination_location.pk:
        raise StockServiceError("Source and destination locations must differ.")
    require_same_event(
        event_edition.pk,
        badge_type=badge_type,
        source_location=source_location,
        destination_location=destination_location,
    )

    fingerprint = command_fingerprint(
        operation_type=StockOperationType.TRANSFER,
        actor=actor,
        target_type="StockLocation",
        target_id=str(source_location.pk),
        parameters={
            "destination_location_id": str(destination_location.pk),
            "badge_type_id": str(badge_type.pk),
            "quantity": quantity,
            "note_text": note,
            "received_by_id": str(getattr(received_by, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.transfer

        # ONE global lock order for every multi-balance command in this
        # module (Prompt 3 correction §7). A transfer and a replacement that
        # spans two locations must not order their locks differently, or two
        # of them can deadlock; `_lock_balances` is the only place that
        # decides the order.
        source_key = _balance_key(
            event_edition_id=event_edition.pk,
            badge_type_id=badge_type.pk,
            location_id=source_location.pk,
        )
        destination_key = _balance_key(
            event_edition_id=event_edition.pk,
            badge_type_id=badge_type.pk,
            location_id=destination_location.pk,
        )
        locks = _lock_balances([source_key, destination_key])
        source_balance = locks[source_key]
        destination_balance = locks[destination_key]

        now = timezone.now()
        transfer = StockTransfer.objects.create(
            event_edition=event_edition,
            badge_type=badge_type,
            source_location=source_location,
            destination_location=destination_location,
            quantity=quantity,
            status=StockTransferStatus.COMPLETED,
            initiated_by=actor,
            received_by=received_by,
            note=normalize_reason_text(note),
            completed_at=now,
        )

        _apply_locked_balance_delta(source_balance, -quantity)
        _apply_locked_balance_delta(destination_balance, quantity)

        BadgeStockLedgerEntry.objects.create(
            operation_id=_new_ledger_operation_id(),
            event_edition=event_edition,
            badge_type=badge_type,
            location=source_location,
            entry_type=BadgeStockEntryType.TRANSFER_OUT,
            quantity_delta=-quantity,
            transfer=transfer,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )
        BadgeStockLedgerEntry.objects.create(
            operation_id=_new_ledger_operation_id(),
            event_edition=event_edition,
            badge_type=badge_type,
            location=destination_location,
            entry_type=BadgeStockEntryType.TRANSFER_IN,
            quantity_delta=quantity,
            transfer=transfer,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.TRANSFER,
            actor=actor,
            fingerprint=fingerprint,
            target_type="StockLocation",
            target_id=str(source_location.pk),
            transfer=transfer,
        )
        _stock_audit(
            action_code=action_codes.STOCK_TRANSFERRED,
            actor=actor,
            target_type="StockTransfer",
            target_uuid=transfer.pk,
            event_edition_id=event_edition.pk,
            after={
                "source_location_id": str(source_location.pk),
                "destination_location_id": str(destination_location.pk),
                "quantity": quantity,
            },
        )
        PersistentOutboxPublisher().enqueue(
            OutboxMessage(
                event_type="badges.stock.transferred",
                aggregate_type="StockTransfer",
                aggregate_id=str(transfer.pk),
                payload={
                    "source_location_id": str(source_location.pk),
                    "destination_location_id": str(destination_location.pk),
                    "quantity": quantity,
                },
            )
        )
    return transfer


# ---------------------------------------------------------------------------
# Allocation -- the "reserved" quantity of TRD `PRINT-002`
#
# Allocation is the one step in the authoritative Prompt 3 list ("receipt,
# allocation, transfer, reasoned adjustment, and reconciliation") that
# moves no stock. It holds a quantity back at a location so no unrelated
# transfer, issuance, or negative adjustment can consume it, and it posts
# NO ledger row, because Schema §10.4's `entry_type` list records movements
# and this is not one (ADR-0020 §7).
# ---------------------------------------------------------------------------


def allocate_stock(
    *,
    event_edition,
    badge_type,
    location: StockLocation,
    quantity: int,
    purpose_code: str,
    actor,
    operation_id: str,
    purpose_text: str = "",
    held_for_group=None,
) -> BadgeStockAllocation:
    """Reserve `quantity` units of `badge_type` at `location`.

    On-hand stock is unchanged; available stock falls by `quantity`. Fails
    closed when the location does not currently have that much AVAILABLE --
    two allocations can never over-commit the same units.
    """
    operation_id = _require_operation_id(operation_id)
    if quantity < 1:
        raise StockServiceError("Allocation quantity must be at least 1.")
    if purpose_code not in StockAllocationPurpose.values:
        raise StockServiceError("Unrecognized stock allocation purpose.")
    require_same_event(event_edition.pk, badge_type=badge_type, location=location)
    purpose_text = normalize_reason_text(purpose_text)

    fingerprint = command_fingerprint(
        operation_type=StockOperationType.ALLOCATE,
        actor=actor,
        target_type="StockLocation",
        target_id=str(location.pk),
        parameters={
            "badge_type_id": str(badge_type.pk),
            "quantity": quantity,
            "purpose_code": purpose_code,
            "purpose_text": purpose_text,
            "held_for_group_id": str(getattr(held_for_group, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.allocation

        key = _balance_key(
            event_edition_id=event_edition.pk,
            badge_type_id=badge_type.pk,
            location_id=location.pk,
        )
        balance = _lock_balances([key])[key]
        _apply_locked_reservation_delta(balance, quantity)

        now = timezone.now()
        allocation = BadgeStockAllocation.objects.create(
            event_edition=event_edition,
            badge_type=badge_type,
            location=location,
            quantity=quantity,
            consumed_quantity=0,
            status=StockAllocationStatus.ACTIVE,
            purpose_code=purpose_code,
            purpose_text=purpose_text,
            held_for_group=held_for_group,
            allocated_by=actor if getattr(actor, "pk", None) else None,
            allocated_at=now,
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.ALLOCATE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="StockLocation",
            target_id=str(location.pk),
            location=location,
            allocation=allocation,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ALLOCATED,
            actor=actor,
            reason_code=purpose_code,
            target_type="BadgeStockAllocation",
            target_uuid=allocation.pk,
            event_edition_id=event_edition.pk,
            after={
                "badge_type_id": str(badge_type.pk),
                "location_id": str(location.pk),
                "quantity": quantity,
                "reserved_after": balance.reserved_quantity,
                "available_after": balance.available_quantity,
            },
        )
        PersistentOutboxPublisher().enqueue(
            OutboxMessage(
                event_type="badges.stock.allocated",
                aggregate_type="BadgeStockAllocation",
                aggregate_id=str(allocation.pk),
                payload={
                    "badge_type_id": str(badge_type.pk),
                    "location_id": str(location.pk),
                    "quantity": quantity,
                },
            )
        )
    return allocation


def release_allocation(
    *,
    allocation: BadgeStockAllocation,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_text: str = "",
) -> BadgeStockAllocation:
    """Return an allocation's still-unconsumed units to general availability.

    Only the REMAINING units are released: anything already issued against
    this allocation has already left both on-hand and reserved, so
    releasing it again would inflate availability out of nothing.
    """
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.RELEASE_ALLOCATION,
        actor=actor,
        target_type="BadgeStockAllocation",
        target_id=str(allocation.pk),
        parameters={
            "expected_lock_version": expected_lock_version,
            "reason_text": reason_text,
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.allocation

        locked = BadgeStockAllocation.objects.select_for_update().get(pk=allocation.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This allocation changed since it was loaded.")
        if locked.status != StockAllocationStatus.ACTIVE:
            raise StockStateError("Only an active allocation can be released.")

        remaining = locked.remaining_quantity
        key = _balance_key(
            event_edition_id=locked.event_edition_id,
            badge_type_id=locked.badge_type_id,
            location_id=locked.location_id,
        )
        balance = _lock_balances([key])[key]
        if remaining:
            _apply_locked_reservation_delta(balance, -remaining)

        before = {"status": locked.status, "remaining": remaining}
        now = timezone.now()
        locked.status = StockAllocationStatus.RELEASED
        locked.version = locked.version + 1
        locked.released_at = now
        locked.released_by = actor if getattr(actor, "pk", None) else None
        locked.release_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "version",
                "released_at",
                "released_by",
                "release_reason_text",
                "updated_at",
            ]
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.RELEASE_ALLOCATION,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeStockAllocation",
            target_id=str(locked.pk),
            location=locked.location,
            allocation=locked,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ALLOCATION_RELEASED,
            actor=actor,
            target_type="BadgeStockAllocation",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            before=before,
            after={
                "status": locked.status,
                "released_quantity": remaining,
                "reserved_after": balance.reserved_quantity,
                "available_after": balance.available_quantity,
            },
        )
    return locked


def _consume_allocation_unit(allocation: BadgeStockAllocation) -> BadgeStockAllocation:
    """Book one unit of an ALREADY-LOCKED allocation as consumed."""
    if allocation.status != StockAllocationStatus.ACTIVE:
        raise StockStateError("Only an active allocation can be drawn on.")
    if allocation.remaining_quantity < 1:
        raise InsufficientStockError("This allocation has no remaining reserved units.")
    allocation.consumed_quantity = allocation.consumed_quantity + 1
    allocation.version = allocation.version + 1
    fields = ["consumed_quantity", "version", "updated_at"]
    if allocation.remaining_quantity == 0:
        allocation.status = StockAllocationStatus.CONSUMED
        fields.append("status")
    allocation.save(update_fields=fields)
    return allocation


# ---------------------------------------------------------------------------
# Issuance -- generic physical badge handover to the exact Registration Context
# ---------------------------------------------------------------------------


def issue_badge(
    *,
    badge_assignment,
    badge_type,
    location: StockLocation,
    actor,
    operation_id: str,
    reason_code: str = BadgeIssuanceReasonCode.INITIAL_HANDOVER,
    reason_text: str = "",
    optional_serial_number: str = "",
    allocation: BadgeStockAllocation | None = None,
) -> BadgeIssuance:
    """Hand one physical badge to the exact Registration Context.

    `badge_type` is what the operator is physically about to hand over. It
    must be byte-identical to the registration's CURRENT
    `BadgeTypeAssignment.badge_type` -- there is no fallback, no nearest
    match, and no silent substitution. A stale or wrong badge_type refuses
    before any stock is touched and before any `BadgeIssuance` row exists.

    When `allocation` is supplied, the unit is drawn from that reservation:
    on-hand and reserved both fall by one, so availability is unchanged --
    the reservation is spent on exactly what it was held for. Without one,
    the unit must come from AVAILABLE stock, so an issuance can never
    quietly consume another operator's allocation.
    """
    from apps.accreditation.models import AssignmentStatus

    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    # Every object in this command must belong to the location's event, and
    # so must the Registration Context (Prompt 3 correction §2). Checked
    # before the transaction opens, so a cross-event attempt creates
    # nothing at all -- not even a denial-side effect beyond the audit.
    require_same_event(
        location.event_edition_id,
        badge_type=badge_type,
        badge_assignment=badge_assignment,
        allocation=allocation,
    )
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.ISSUE,
        actor=actor,
        target_type="BadgeTypeAssignment",
        target_id=str(badge_assignment.pk),
        parameters={
            "badge_type_id": str(badge_type.pk),
            "location_id": str(location.pk),
            "reason_code": reason_code,
            "reason_text": reason_text,
            "optional_serial_number_text": optional_serial_number,
            "allocation_id": str(getattr(allocation, "pk", "") or ""),
        },
    )
    try:
        with transaction.atomic():
            _acquire_stock_lock(operation_id)
            replay = _match_existing_stock_operation(operation_id, fingerprint)
            if replay is not None:
                return replay.issuance

            from apps.accreditation.models import BadgeTypeAssignment

            # Registration first: refuses a withdrawn or cancelled context
            # before anything is locked further or written (P8-02). An
            # assignment's registration never changes.
            locked_registration = _lock_eligible_registration(badge_assignment.registration_id)
            # Attendance days (apps.accreditation.attendance): while
            # enforcement is active, a badge is handed over only to a
            # classified registration, so its marking can be applied.
            _require_attendance_classified(locked_registration)
            locked_assignment = BadgeTypeAssignment.objects.select_for_update().get(
                pk=badge_assignment.pk
            )
            if locked_assignment.registration_id != locked_registration.pk:  # pragma: no cover
                raise StockStateError("The badge assignment does not belong to this registration.")
            if locked_assignment.status != AssignmentStatus.CURRENT:
                raise StockStateError(
                    "This Registration Context has no current badge assignment to issue against."
                )
            if locked_assignment.badge_type_id != badge_type.pk:
                raise WrongBadgeTypeError(
                    "The requested Badge Type does not match this registration's "
                    "current assignment."
                )
            # Guarded on the REGISTRATION, not only the assignment: a Badge
            # Type reassignment creates a new assignment row, and the
            # per-assignment check alone would happily hand over a second
            # live badge for the same Registration Context (Prompt 3
            # correction §4).
            if BadgeIssuance.objects.filter(
                registration_id=locked_assignment.registration_id,
                status=BadgeIssuanceStatus.ISSUED,
            ).exists():
                raise StockStateError(
                    "This Registration Context already has a current physical badge issuance."
                )

            locked_allocation = None
            if allocation is not None:
                locked_allocation = BadgeStockAllocation.objects.select_for_update().get(
                    pk=allocation.pk
                )
                if (
                    locked_allocation.badge_type_id != badge_type.pk
                    or locked_allocation.location_id != location.pk
                ):
                    raise StockStateError(
                        "That allocation is not held for this Badge Type at this location."
                    )

            key = _balance_key(
                event_edition_id=location.event_edition_id,
                badge_type_id=badge_type.pk,
                location_id=location.pk,
            )
            balance = _lock_balances([key])[key]
            if locked_allocation is None and balance.available_quantity < 1:
                raise InsufficientStockError(
                    "There is no stock of this Badge Type available at this location."
                )

            now = timezone.now()
            issuance = BadgeIssuance.objects.create(
                badge_assignment=locked_assignment,
                registration_id=locked_assignment.registration_id,
                badge_type=badge_type,
                location=location,
                status=BadgeIssuanceStatus.ISSUED,
                optional_serial_number=optional_serial_number,
                reason_code=reason_code,
                reason_text=reason_text,
                issued_by=actor if getattr(actor, "pk", None) else None,
                issued_at=now,
            )
            if locked_allocation is not None:
                _consume_allocation_unit(locked_allocation)
                _apply_locked_balance_delta(balance, -1, from_reservation=True)
            else:
                _apply_locked_balance_delta(balance, -1)
            ledger_entry = BadgeStockLedgerEntry.objects.create(
                operation_id=operation_id,
                event_edition=location.event_edition,
                badge_type=badge_type,
                location=location,
                entry_type=BadgeStockEntryType.ISSUE,
                quantity_delta=-1,
                issuance=issuance,
                occurred_at=now,
                recorded_by=actor if getattr(actor, "pk", None) else None,
            )

            _record_stock_operation(
                operation_id=operation_id,
                operation_type=StockOperationType.ISSUE,
                actor=actor,
                fingerprint=fingerprint,
                target_type="BadgeTypeAssignment",
                target_id=str(locked_assignment.pk),
                issuance=issuance,
                ledger_entry=ledger_entry,
                allocation=locked_allocation,
            )
            _stock_audit(
                action_code=action_codes.STOCK_ISSUED,
                actor=actor,
                reason_code=reason_code,
                target_type="BadgeIssuance",
                target_uuid=issuance.pk,
                event_edition_id=location.event_edition_id,
                after={
                    "status": issuance.status,
                    "location_id": str(location.pk),
                    "allocation_id": (
                        str(locked_allocation.pk) if locked_allocation is not None else None
                    ),
                },
            )
            PersistentOutboxPublisher().enqueue(
                OutboxMessage(
                    event_type="badges.stock.issued",
                    aggregate_type="BadgeIssuance",
                    aggregate_id=str(issuance.pk),
                    payload={
                        "badge_assignment_id": str(locked_assignment.pk),
                        "badge_type_id": str(badge_type.pk),
                        "location_id": str(location.pk),
                        "allocation_id": (
                            str(locked_allocation.pk) if locked_allocation is not None else None
                        ),
                    },
                )
            )
    except (WrongBadgeTypeError, InsufficientStockError, RegistrationNotEligibleError) as exc:
        # The denial audit is written in its OWN transaction, deliberately --
        # exactly `apps.badges.services.generate_pass`'s discipline.
        # Recording it inside the failed transaction above would roll it
        # back with everything else, leaving a refusal with no evidence.
        if isinstance(exc, RegistrationNotEligibleError):
            reason = RegistrationNotEligibleError.reason_code
        elif isinstance(exc, WrongBadgeTypeError):
            reason = "WRONG_BADGE_TYPE"
        else:
            reason = "INSUFFICIENT_STOCK"
        with transaction.atomic():
            _stock_audit(
                action_code=action_codes.STOCK_ISSUANCE_BLOCKED,
                actor=actor,
                result="DENIED",
                reason_code=reason,
                target_type="BadgeTypeAssignment",
                target_uuid=badge_assignment.pk,
                event_edition_id=location.event_edition_id,
            )
        raise
    return issuance


def _issuance_summary(issuance: BadgeIssuance) -> dict[str, Any]:
    return {"status": issuance.status, "location_id": str(issuance.location_id)}


def replace_issuance(
    *,
    issuance: BadgeIssuance,
    badge_type,
    location: StockLocation | None = None,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str = "",
    return_original: bool,
) -> BadgeIssuance:
    """Supersede one physical badge issuance with a fresh one.

    `return_original` is explicit, not inferred from `reason_code`: a
    wrong-type or administrative-error replacement typically returns a
    still-usable original badge to stock; a lost or damaged one does not,
    because there is nothing left to return. The caller (the operational
    view, driven by the chosen reason) decides which applies; the service
    never guesses.

    Operates directly on `issuance`'s own primary key -- never re-derives
    "the current issuance" from the assignment -- so there is no analogue
    of the credential-replacement `expected_jti` ambiguity: the exact row
    being superseded is always the one the caller already loaded.

    The replacement Badge Type is checked against the Registration
    Context's CURRENT `BadgeTypeAssignment`, exactly as first issuance is
    (Prompt 3 correction §3): a forged POST naming any other Badge Type --
    including one belonging to a different event -- is refused before
    anything is created or any balance moves.

    Since Phase 3 Prompt 8 (P8-02) the Registration Context must also still
    be an active approved context: a withdrawn or cancelled one gets no
    replacement badge, and the refusal is audited without partial state.
    """
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    require_same_event(
        issuance.location.event_edition_id, badge_type=badge_type, replacement_location=location
    )
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.REPLACE_ISSUANCE,
        actor=actor,
        target_type="BadgeIssuance",
        target_id=str(issuance.pk),
        parameters={
            "badge_type_id": str(badge_type.pk),
            "replacement_location_id": str(getattr(location, "pk", "") or ""),
            "expected_lock_version": expected_lock_version,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "return_original": bool(return_original),
        },
    )
    try:
        return _replace_issuance_locked(
            issuance=issuance,
            badge_type=badge_type,
            location=location,
            actor=actor,
            operation_id=operation_id,
            expected_lock_version=expected_lock_version,
            reason_code=reason_code,
            reason_text=reason_text,
            return_original=return_original,
            fingerprint=fingerprint,
        )
    except RegistrationNotEligibleError:
        # Same discipline as `issue_badge`: the refusal leaves no partial
        # state (it was raised before any row was changed, and the whole
        # transaction rolled back) and is evidenced in its OWN transaction.
        with transaction.atomic():
            _stock_audit(
                action_code=action_codes.STOCK_ISSUANCE_BLOCKED,
                actor=actor,
                result="DENIED",
                reason_code=RegistrationNotEligibleError.reason_code,
                target_type="BadgeIssuance",
                target_uuid=issuance.pk,
                event_edition_id=issuance.location.event_edition_id,
            )
        raise


def _replace_issuance_locked(
    *,
    issuance: BadgeIssuance,
    badge_type,
    location,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str,
    return_original: bool,
    fingerprint: str,
) -> BadgeIssuance:
    """The transactional body of `replace_issuance`, unchanged except for
    the Registration lock taken first (P8-02)."""
    from apps.accreditation.models import AssignmentStatus, BadgeTypeAssignment

    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.issuance

        # Registration FIRST (Registration -> issuance -> assignment ->
        # balances), refusing a withdrawn or cancelled context before any
        # issuance row is locked or changed. An issuance's registration
        # never changes.
        _lock_eligible_registration(issuance.registration_id)
        locked = BadgeIssuance.objects.select_for_update().get(pk=issuance.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This badge issuance changed since it was loaded.")
        if locked.status != BadgeIssuanceStatus.ISSUED:
            raise StockStateError("Only a currently issued badge can be replaced.")

        # A replacement is an issuance: it obeys the same
        # no-automatic-substitution rule. Re-derived from the Registration
        # Context under lock, so a reassignment that landed between page
        # load and submit is caught rather than overwritten.
        current_assignment = (
            BadgeTypeAssignment.objects.select_for_update()
            .filter(
                registration_id=locked.registration_id or locked.badge_assignment.registration_id,
                status=AssignmentStatus.CURRENT,
            )
            .first()
        )
        if current_assignment is None:
            raise StockStateError(
                "This Registration Context has no current badge assignment to issue against."
            )
        if current_assignment.badge_type_id != badge_type.pk:
            raise WrongBadgeTypeError(
                "The replacement Badge Type does not match this registration's current assignment."
            )

        before = _issuance_summary(locked)
        now = timezone.now()
        target_location = location or locked.location

        # One global lock order for both balance rows this command may
        # touch -- the original's location for the return, and the
        # replacement's location for the new issue (Prompt 3 correction
        # §7). Acquired together, before either is mutated.
        original_key = _balance_key(
            event_edition_id=locked.location.event_edition_id,
            badge_type_id=locked.badge_type_id,
            location_id=locked.location_id,
        )
        replacement_key = _balance_key(
            event_edition_id=target_location.event_edition_id,
            badge_type_id=badge_type.pk,
            location_id=target_location.pk,
        )
        balances = _lock_balances([original_key, replacement_key])

        if return_original:
            original_balance = balances[original_key]
            _apply_locked_balance_delta(original_balance, 1)
            BadgeStockLedgerEntry.objects.create(
                operation_id=_new_ledger_operation_id(),
                event_edition=locked.location.event_edition,
                badge_type=locked.badge_type,
                location=locked.location,
                entry_type=BadgeStockEntryType.RETURN,
                quantity_delta=1,
                issuance=locked,
                occurred_at=now,
                recorded_by=actor if getattr(actor, "pk", None) else None,
            )

        locked.status = BadgeIssuanceStatus.REPLACED
        locked.version = locked.version + 1
        locked.replaced_at = now
        locked.replaced_by_user = actor if getattr(actor, "pk", None) else None
        locked.status_reason_code = reason_code
        locked.status_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "version",
                "replaced_at",
                "replaced_by_user",
                "status_reason_code",
                "status_reason_text",
                "updated_at",
            ]
        )

        new_balance = balances[replacement_key]
        if new_balance.available_quantity < 1:
            raise InsufficientStockError(
                "There is no stock of this Badge Type available at this location."
            )
        _apply_locked_balance_delta(new_balance, -1)

        replacement = BadgeIssuance.objects.create(
            badge_assignment=current_assignment,
            registration_id=current_assignment.registration_id,
            badge_type=badge_type,
            location=target_location,
            status=BadgeIssuanceStatus.ISSUED,
            reason_code=BadgeIssuanceReasonCode.WRONG_BADGE_TYPE
            if reason_code == BadgeIssuanceReasonCode.WRONG_BADGE_TYPE
            else BadgeIssuanceReasonCode.ADMINISTRATIVE_ERROR,
            reason_text=reason_text,
            issued_by=actor if getattr(actor, "pk", None) else None,
            issued_at=now,
        )
        BadgeStockLedgerEntry.objects.create(
            operation_id=_new_ledger_operation_id(),
            event_edition=target_location.event_edition,
            badge_type=badge_type,
            location=target_location,
            entry_type=BadgeStockEntryType.ISSUE,
            quantity_delta=-1,
            issuance=replacement,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        # Link the chain only once the replacement exists, mirroring
        # `apps.badges.services.replace_pass`: if issuing the replacement
        # had failed, the whole transaction rolls back and the outgoing
        # issuance stays exactly as it was.
        locked.replaced_by = replacement
        locked.save(update_fields=["replaced_by", "updated_at"])

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.REPLACE_ISSUANCE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeIssuance",
            target_id=str(issuance.pk),
            issuance=replacement,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ISSUANCE_REPLACED,
            actor=actor,
            reason_code=reason_code,
            target_type="BadgeIssuance",
            target_uuid=replacement.pk,
            event_edition_id=target_location.event_edition_id,
            before=before,
            after=_issuance_summary(replacement),
        )
        PersistentOutboxPublisher().enqueue(
            OutboxMessage(
                event_type="badges.stock.issuance_replaced",
                aggregate_type="BadgeIssuance",
                aggregate_id=str(replacement.pk),
                payload={"replaces_id": str(locked.pk)},
            )
        )
    return replacement


def return_issuance(
    *,
    issuance: BadgeIssuance,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str = "",
    return_location: StockLocation | None = None,
) -> BadgeIssuance:
    """ISSUED -> RETURNED. The physical badge genuinely comes back to stock."""
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    require_same_event(issuance.location.event_edition_id, return_location=return_location)
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.RETURN_ISSUANCE,
        actor=actor,
        target_type="BadgeIssuance",
        target_id=str(issuance.pk),
        parameters={
            "expected_lock_version": expected_lock_version,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "return_location_id": str(getattr(return_location, "pk", "") or ""),
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.issuance

        locked = BadgeIssuance.objects.select_for_update().get(pk=issuance.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This badge issuance changed since it was loaded.")
        if locked.status != BadgeIssuanceStatus.ISSUED:
            raise StockStateError("Only a currently issued badge can be returned.")

        before = _issuance_summary(locked)
        now = timezone.now()
        target_location = return_location or locked.location
        key = _balance_key(
            event_edition_id=target_location.event_edition_id,
            badge_type_id=locked.badge_type_id,
            location_id=target_location.pk,
        )
        balance = _lock_balances([key])[key]
        _apply_locked_balance_delta(balance, 1)
        BadgeStockLedgerEntry.objects.create(
            operation_id=operation_id,
            event_edition=target_location.event_edition,
            badge_type=locked.badge_type,
            location=target_location,
            entry_type=BadgeStockEntryType.RETURN,
            quantity_delta=1,
            issuance=locked,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        locked.status = BadgeIssuanceStatus.RETURNED
        locked.version = locked.version + 1
        locked.returned_at = now
        locked.returned_by_user = actor if getattr(actor, "pk", None) else None
        locked.status_reason_code = reason_code
        locked.status_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "version",
                "returned_at",
                "returned_by_user",
                "status_reason_code",
                "status_reason_text",
                "updated_at",
            ]
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.RETURN_ISSUANCE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeIssuance",
            target_id=str(locked.pk),
            issuance=locked,
        )
        _stock_audit(
            action_code=action_codes.STOCK_RETURNED,
            actor=actor,
            reason_code=reason_code,
            target_type="BadgeIssuance",
            target_uuid=locked.pk,
            event_edition_id=target_location.event_edition_id,
            before=before,
            after=_issuance_summary(locked),
        )
    return locked


def mark_issuance_lost(
    *,
    issuance: BadgeIssuance,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_text: str = "",
) -> BadgeIssuance:
    """ISSUED -> LOST. No ledger entry: nothing physically returns to stock."""
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.MARK_LOST,
        actor=actor,
        target_type="BadgeIssuance",
        target_id=str(issuance.pk),
        parameters={"expected_lock_version": expected_lock_version, "reason_text": reason_text},
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.issuance

        locked = BadgeIssuance.objects.select_for_update().get(pk=issuance.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This badge issuance changed since it was loaded.")
        if locked.status != BadgeIssuanceStatus.ISSUED:
            raise StockStateError("Only a currently issued badge can be reported lost.")

        before = _issuance_summary(locked)
        now = timezone.now()
        locked.status = BadgeIssuanceStatus.LOST
        locked.version = locked.version + 1
        locked.lost_reported_at = now
        locked.lost_reported_by = actor if getattr(actor, "pk", None) else None
        locked.status_reason_code = BadgeIssuanceReasonCode.LOST_BADGE
        locked.status_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "version",
                "lost_reported_at",
                "lost_reported_by",
                "status_reason_code",
                "status_reason_text",
                "updated_at",
            ]
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.MARK_LOST,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeIssuance",
            target_id=str(locked.pk),
            issuance=locked,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ISSUANCE_LOST,
            actor=actor,
            reason_code=BadgeIssuanceReasonCode.LOST_BADGE,
            target_type="BadgeIssuance",
            target_uuid=locked.pk,
            event_edition_id=locked.location.event_edition_id,
            before=before,
            after=_issuance_summary(locked),
        )
    return locked


def void_issuance(
    *,
    issuance: BadgeIssuance,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_text: str = "",
) -> BadgeIssuance:
    """ISSUED -> VOIDED. An administrative correction: the badge is restored
    to stock because the issuance record itself, not the physical handover,
    was wrong (e.g. logged against the wrong Registration Context)."""
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=StockOperationType.VOID_ISSUANCE,
        actor=actor,
        target_type="BadgeIssuance",
        target_id=str(issuance.pk),
        parameters={"expected_lock_version": expected_lock_version, "reason_text": reason_text},
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.issuance

        locked = BadgeIssuance.objects.select_for_update().get(pk=issuance.pk)
        if locked.version != expected_lock_version:
            raise StockConcurrencyError("This badge issuance changed since it was loaded.")
        if locked.status != BadgeIssuanceStatus.ISSUED:
            raise StockStateError("Only a currently issued badge can be voided.")

        before = _issuance_summary(locked)
        now = timezone.now()
        key = _balance_key(
            event_edition_id=locked.location.event_edition_id,
            badge_type_id=locked.badge_type_id,
            location_id=locked.location_id,
        )
        balance = _lock_balances([key])[key]
        _apply_locked_balance_delta(balance, 1)
        BadgeStockLedgerEntry.objects.create(
            operation_id=operation_id,
            event_edition=locked.location.event_edition,
            badge_type=locked.badge_type,
            location=locked.location,
            entry_type=BadgeStockEntryType.RETURN,
            quantity_delta=1,
            issuance=locked,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        locked.status = BadgeIssuanceStatus.VOIDED
        locked.version = locked.version + 1
        locked.voided_at = now
        locked.voided_by = actor if getattr(actor, "pk", None) else None
        locked.status_reason_code = BadgeIssuanceReasonCode.ADMINISTRATIVE_ERROR
        locked.status_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "version",
                "voided_at",
                "voided_by",
                "status_reason_code",
                "status_reason_text",
                "updated_at",
            ]
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.VOID_ISSUANCE,
            actor=actor,
            fingerprint=fingerprint,
            target_type="BadgeIssuance",
            target_id=str(locked.pk),
            issuance=locked,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ISSUANCE_VOIDED,
            actor=actor,
            reason_code=BadgeIssuanceReasonCode.ADMINISTRATIVE_ERROR,
            target_type="BadgeIssuance",
            target_uuid=locked.pk,
            event_edition_id=locked.location.event_edition_id,
            before=before,
            after=_issuance_summary(locked),
        )
    return locked


# ---------------------------------------------------------------------------
# Reasoned adjustment
# ---------------------------------------------------------------------------


def record_adjustment(
    *,
    event_edition,
    badge_type,
    location: StockLocation,
    quantity_delta: int,
    reason_code: str,
    reason_text: str,
    actor,
    operation_id: str,
) -> BadgeStockLedgerEntry:
    operation_id = _require_operation_id(operation_id)
    if quantity_delta == 0:
        raise StockServiceError("An adjustment must change the quantity by a nonzero amount.")
    if reason_code not in StockAdjustmentReasonCode.values:
        raise StockServiceError("Unrecognized stock adjustment reason code.")
    reason_text = normalize_reason_text(reason_text)
    require_same_event(event_edition.pk, badge_type=badge_type, location=location)

    fingerprint = command_fingerprint(
        operation_type=StockOperationType.ADJUSTMENT,
        actor=actor,
        target_type="StockLocation",
        target_id=str(location.pk),
        parameters={
            "badge_type_id": str(badge_type.pk),
            "quantity_delta": quantity_delta,
            "reason_code": reason_code,
            "reason_text": reason_text,
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.ledger_entry

        key = _balance_key(
            event_edition_id=event_edition.pk,
            badge_type_id=badge_type.pk,
            location_id=location.pk,
        )
        balance = _lock_balances([key])[key]
        _apply_locked_balance_delta(balance, quantity_delta)

        now = timezone.now()
        entry = BadgeStockLedgerEntry.objects.create(
            operation_id=operation_id,
            event_edition=event_edition,
            badge_type=badge_type,
            location=location,
            entry_type=BadgeStockEntryType.ADJUSTMENT,
            quantity_delta=quantity_delta,
            reason_code=reason_code,
            reason_text=reason_text,
            occurred_at=now,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.ADJUSTMENT,
            actor=actor,
            fingerprint=fingerprint,
            target_type="StockLocation",
            target_id=str(location.pk),
            ledger_entry=entry,
        )
        _stock_audit(
            action_code=action_codes.STOCK_ADJUSTED,
            actor=actor,
            reason_code=reason_code,
            target_type="BadgeStockLedgerEntry",
            target_uuid=entry.pk,
            event_edition_id=event_edition.pk,
            after={"quantity_delta": quantity_delta, "resulting_balance": balance.quantity},
        )
    return entry


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def record_reconciliation(
    *,
    event_edition,
    badge_type,
    location: StockLocation,
    counted_quantity: int,
    actor,
    operation_id: str,
    notes: str = "",
) -> StockReconciliation:
    """Record one physical count and correct the projection to match it.

    The resulting balance is always the counted value itself, which the
    caller has already validated non-negative -- so no separately-authorized
    "permit negative" escape hatch is needed (ADR-0020 §5): a reconciliation
    can never legitimately need to leave a negative balance behind.
    """
    operation_id = _require_operation_id(operation_id)
    if counted_quantity < 0:
        raise StockServiceError("A physical count cannot be negative.")
    notes = normalize_reason_text(notes)
    require_same_event(event_edition.pk, badge_type=badge_type, location=location)

    fingerprint = command_fingerprint(
        operation_type=StockOperationType.RECONCILIATION,
        actor=actor,
        target_type="StockLocation",
        target_id=str(location.pk),
        parameters={
            "badge_type_id": str(badge_type.pk),
            "counted_quantity": counted_quantity,
            "notes_text": notes,
        },
    )
    with transaction.atomic():
        _acquire_stock_lock(operation_id)
        replay = _match_existing_stock_operation(operation_id, fingerprint)
        if replay is not None:
            return replay.reconciliation

        key = _balance_key(
            event_edition_id=event_edition.pk,
            badge_type_id=badge_type.pk,
            location_id=location.pk,
        )
        balance = _lock_balances([key])[key]
        expected = balance.quantity
        discrepancy = counted_quantity - expected

        reconciliation = StockReconciliation.objects.create(
            event_edition=event_edition,
            badge_type=badge_type,
            location=location,
            expected_quantity_at_count=expected,
            counted_quantity=counted_quantity,
            discrepancy=discrepancy,
            notes=notes,
            recorded_by=actor if getattr(actor, "pk", None) else None,
        )

        ledger_entry = None
        if discrepancy != 0:
            # A count below what is currently allocated would leave
            # `reserved > on hand`, which the database forbids outright.
            # Refuse with a domain error naming the real problem rather than
            # letting an IntegrityError surface: the operator must release
            # the over-committed allocation first.
            if counted_quantity < balance.reserved_quantity:
                raise InsufficientStockError(
                    "The counted quantity is below the quantity currently allocated at this "
                    "location. Release the affected allocation before reconciling."
                )
            balance.quantity = counted_quantity
            balance.save(update_fields=["quantity", "updated_at"])
            ledger_entry = BadgeStockLedgerEntry.objects.create(
                operation_id=operation_id,
                event_edition=event_edition,
                badge_type=badge_type,
                location=location,
                entry_type=BadgeStockEntryType.RECONCILIATION,
                quantity_delta=discrepancy,
                reconciliation=reconciliation,
                occurred_at=reconciliation.recorded_at,
                recorded_by=actor if getattr(actor, "pk", None) else None,
            )

        _record_stock_operation(
            operation_id=operation_id,
            operation_type=StockOperationType.RECONCILIATION,
            actor=actor,
            fingerprint=fingerprint,
            target_type="StockLocation",
            target_id=str(location.pk),
            reconciliation=reconciliation,
            ledger_entry=ledger_entry,
        )
        _stock_audit(
            action_code=action_codes.STOCK_RECONCILIATION_RECORDED,
            actor=actor,
            target_type="StockReconciliation",
            target_uuid=reconciliation.pk,
            event_edition_id=event_edition.pk,
            after={
                "expected": expected,
                "counted": counted_quantity,
                "discrepancy": discrepancy,
            },
        )
    return reconciliation
