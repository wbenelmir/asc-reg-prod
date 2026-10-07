"""Generic physical badge stock: production, locations, ledger, issuance.

Phase 3 Prompt 3.
See `ADR-0020` for the design decisions this module implements:

* stock models join `apps.badges` rather than a new app (ADR-0017 already
  anticipated this);
* there is no `device` reference anywhere here -- `EntryDevice` does not
  exist yet and creating one would be out-of-scope device-enrollment work;
* "damage" is not a ledger `entry_type`. Damage discovered at production
  receipt is `PrintBatch.damaged_quantity` (never posted to the ledger at
  all); damage discovered in stock is an `ADJUSTMENT` with a controlled
  reason code;
* idempotency for every stock command -- not only the ones that move
  quantity -- goes through `BadgeStockOperation`, structurally identical to
  `apps.badges.models.PassLifecycleOperation`.

This module contains NO entry device, device scope, device session, entry
event, override, security restriction, offline package, or synchronization
model. Those remain out of scope for this prompt.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel, VersionedModel

# ---------------------------------------------------------------------------
# Stock locations
# ---------------------------------------------------------------------------


class StockLocationType(models.TextChoices):
    CENTRAL = "CENTRAL", _("Central store")
    CHECKPOINT = "CHECKPOINT", _("Checkpoint")


class StockLocation(UUIDPrimaryKeyModel, TimestampedModel):
    """A central store or checkpoint stock location (Schema §10.3).

    No Venue/Gate scope is attached: ADR-0010 defers venue/zone/gate models,
    and none exist yet. `custodian_group` is an OPTIONAL pointer to an
    existing `auth.Group` -- it never grants a permission by itself, it only
    records who is operationally responsible for reconciling this location.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="stock_locations"
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=200)
    name_fr = models.CharField(max_length=200, blank=True, default="")
    name_ar = models.CharField(max_length=200, blank=True, default="")
    location_type = models.CharField(max_length=16, choices=StockLocationType.choices)
    custodian_group = models.ForeignKey(
        "auth.Group", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "badges_stock_location"
        permissions = [
            ("manage_stocklocation", "Can create and manage stock locations"),
            # The stock-ACCOUNTING surface (dashboard, balances, ledger,
            # transfers, adjustments, reconciliation), kept separate from
            # the Django-default `view_stocklocation` that handover staff
            # need only to choose an issuing location (Prompt 3 correction
            # §8).
            ("view_stockaccounting", "Can view the badge stock accounting dashboard"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "code"], name="bdg_stockloc_event_code_uq"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}:{self.location_type}"

    @property
    def localized_name(self) -> str:
        from django.utils.translation import get_language

        language = get_language() or "en"
        if language == "fr" and self.name_fr:
            return self.name_fr
        if language == "ar" and self.name_ar:
            return self.name_ar
        return self.name


# ---------------------------------------------------------------------------
# Print batches
# ---------------------------------------------------------------------------


class PrintBatchStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    READY = "READY", _("Ready for production")
    IN_PRODUCTION = "IN_PRODUCTION", _("In production")
    RECEIVED = "RECEIVED", _("Received")
    RECONCILED = "RECONCILED", _("Reconciled")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")


#: Statuses reachable from each status. Cancellation is only permitted
#: before production genuinely starts (Prompt 3 text: "with cancellation
#: before production"); once physical production has begun, the batch must
#: be seen through to receipt, not silently cancelled.
PRINT_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    PrintBatchStatus.DRAFT: frozenset({PrintBatchStatus.READY, PrintBatchStatus.CANCELLED}),
    PrintBatchStatus.READY: frozenset({PrintBatchStatus.IN_PRODUCTION, PrintBatchStatus.CANCELLED}),
    PrintBatchStatus.IN_PRODUCTION: frozenset({PrintBatchStatus.RECEIVED}),
    PrintBatchStatus.RECEIVED: frozenset({PrintBatchStatus.RECONCILED}),
    PrintBatchStatus.RECONCILED: frozenset({PrintBatchStatus.CLOSED}),
    PrintBatchStatus.CLOSED: frozenset(),
    PrintBatchStatus.CANCELLED: frozenset(),
}


class PrintBatch(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """One pre-event production run for one Badge Type (Schema §10.2).

    `produced_quantity`/`accepted_quantity`/`damaged_quantity` are recorded
    together at receipt. Only `accepted_quantity` is ever posted to the
    stock ledger as a `RECEIVE` entry -- units the supplier delivered
    damaged never enter countable stock (ADR-0020 §3).
    """

    public_reference = models.CharField(max_length=22, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    artwork_version = models.CharField(max_length=32, blank=True, default="")
    planned_quantity = models.PositiveIntegerField()
    produced_quantity = models.PositiveIntegerField(default=0)
    accepted_quantity = models.PositiveIntegerField(default=0)
    damaged_quantity = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=PrintBatchStatus.choices, default=PrintBatchStatus.DRAFT
    )
    supplier_reference = models.CharField(max_length=200, blank=True, default="")
    destination_location = models.ForeignKey(
        StockLocation,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_batches",
        help_text="The stock location the accepted quantity is receipted into.",
    )
    production_started_at = models.DateTimeField(null=True, blank=True)
    production_completed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "badges_print_batch"
        permissions = [
            ("manage_printbatch", "Can create, plan, and cancel print batches"),
            ("receive_printbatch", "Can record production receipt for a print batch"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["public_reference"], name="bdg_printbatch_ref_uq"),
            models.CheckConstraint(
                condition=models.Q(planned_quantity__gte=1),
                name="bdg_printbatch_planned_positive_ck",
            ),
            # Once received, the accounting must balance: every unit is
            # either accepted or recorded as damaged, and neither exceeds
            # what was actually produced.
            models.CheckConstraint(
                condition=~models.Q(
                    status__in=[
                        PrintBatchStatus.RECEIVED,
                        PrintBatchStatus.RECONCILED,
                        PrintBatchStatus.CLOSED,
                    ]
                )
                | models.Q(
                    produced_quantity=models.F("accepted_quantity") + models.F("damaged_quantity")
                ),
                name="bdg_printbatch_receipt_balances_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["event_edition", "status"], name="bdg_printbatch_event_idx"),
            models.Index(fields=["badge_type"], name="bdg_printbatch_badgetype_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"batch:{self.public_reference}:{self.status}"


# ---------------------------------------------------------------------------
# Stock transfers
# ---------------------------------------------------------------------------


class StockTransferStatus(models.TextChoices):
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")


class StockTransfer(UUIDPrimaryKeyModel, TimestampedModel):
    """One completed movement of stock between two locations (Schema §10.5).

    Posted atomically: `apps.badges.services.stock.transfer_stock` creates
    this row and its balanced `TRANSFER_OUT`/`TRANSFER_IN` ledger entries in
    one transaction. There is no separate "in transit" phase requiring a
    later confirmation step -- no device or offline confirmation workflow is
    in scope for this prompt (ADR-0020 §2), so a transfer is recorded by an
    already-authorized operator at the point stock physically changes hands.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    source_location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="transfers_out"
    )
    destination_location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="transfers_in"
    )
    quantity = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16, choices=StockTransferStatus.choices, default=StockTransferStatus.COMPLETED
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.CharField(max_length=300, blank=True, default="")
    completed_at = models.DateTimeField()

    class Meta:
        db_table = "badges_stock_transfer"
        permissions = [
            ("transfer_badgestock", "Can transfer badge stock between locations"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="bdg_transfer_qty_ck"),
            models.CheckConstraint(
                condition=~models.Q(source_location=models.F("destination_location")),
                name="bdg_transfer_distinct_locations_ck",
            ),
        ]
        indexes = [
            models.Index(fields=["event_edition", "badge_type"], name="bdg_transfer_event_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"transfer:{self.source_location_id}->{self.destination_location_id}:{self.quantity}"


# ---------------------------------------------------------------------------
# Badge issuance (generic physical badge handover to a Registration Context)
# ---------------------------------------------------------------------------


class BadgeIssuanceStatus(models.TextChoices):
    ISSUED = "ISSUED", _("Issued")
    REPLACED = "REPLACED", _("Replaced")
    RETURNED = "RETURNED", _("Returned")
    LOST = "LOST", _("Reported lost")
    VOIDED = "VOIDED", _("Voided")


#: Terminal for the purposes of "one current physical badge per assignment".
ISSUANCE_TERMINAL_STATUSES: tuple[str, ...] = (
    BadgeIssuanceStatus.REPLACED,
    BadgeIssuanceStatus.RETURNED,
    BadgeIssuanceStatus.LOST,
    BadgeIssuanceStatus.VOIDED,
)


class BadgeIssuanceReasonCode(models.TextChoices):
    """Controlled reasons for issuance and for ending an issuance."""

    INITIAL_HANDOVER = "INITIAL_HANDOVER", _("Initial handover")
    WRONG_BADGE_TYPE = "WRONG_BADGE_TYPE", _("Wrong badge type issued")
    DAMAGED_BADGE = "DAMAGED_BADGE", _("Physical badge damaged")
    LOST_BADGE = "LOST_BADGE", _("Physical badge lost")
    PARTICIPANT_REQUEST = "PARTICIPANT_REQUEST", _("Participant request")
    EVENT_CONCLUDED = "EVENT_CONCLUDED", _("Returned at end of event")
    ADMINISTRATIVE_ERROR = "ADMINISTRATIVE_ERROR", _("Administrative error")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved reason")


class BadgeIssuance(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """One physical badge handed to the exact Registration Context (Schema §10.6).

    `badge_assignment` is the same `accreditation.BadgeTypeAssignment` the
    Digital Entry Pass credential snapshots -- the exact Registration
    Context, never a bare Badge Type. `badge_type` is a denormalized
    snapshot of `badge_assignment.badge_type` at issuance time, kept for
    direct filtering without a join, mirroring the snapshot pattern already
    used by `DigitalEntryPass`.

    A wrong Badge Type is never substituted automatically: `issue_badge`
    refuses unless the caller's requested Badge Type matches the
    Registration's CURRENT `BadgeTypeAssignment` exactly.

    `registration` is a denormalized snapshot of
    `badge_assignment.registration`, and it is what the "one current
    physical badge" rule is actually enforced on. Constraining
    `badge_assignment` alone was insufficient (Prompt 3 correction §4): a
    `BadgeTypeAssignment` is superseded by a NEW row whenever an operator
    changes the assigned Badge Type, so a second issuance against the new
    assignment satisfied a per-assignment uniqueness constraint while
    leaving the participant holding two live physical badges for one
    Registration Context. The per-assignment constraint is kept as well --
    it is strictly narrower, and both are cheap.
    """

    badge_assignment = models.ForeignKey(
        "accreditation.BadgeTypeAssignment", on_delete=models.PROTECT, related_name="+"
    )
    #: The exact Registration Context, denormalized from `badge_assignment`
    #: at issuance time so the "one current physical badge" rule survives a
    #: Badge Type reassignment (Prompt 3 correction §4).
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="issuances")
    status = models.CharField(
        max_length=16, choices=BadgeIssuanceStatus.choices, default=BadgeIssuanceStatus.ISSUED
    )
    optional_serial_number = models.CharField(max_length=64, blank=True, default="")

    reason_code = models.CharField(
        max_length=32,
        choices=BadgeIssuanceReasonCode.choices,
        default=BadgeIssuanceReasonCode.INITIAL_HANDOVER,
    )
    reason_text = models.CharField(max_length=300, blank=True, default="")
    status_reason_code = models.CharField(
        max_length=32, choices=BadgeIssuanceReasonCode.choices, blank=True, default=""
    )
    status_reason_text = models.CharField(max_length=300, blank=True, default="")

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    issued_at = models.DateTimeField()
    replaced_at = models.DateTimeField(null=True, blank=True)
    replaced_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    lost_reported_at = models.DateTimeField(null=True, blank=True)
    lost_reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    #: The attendance marking (sticker, overlay or print variant) the issuing
    #: operator confirmed on THIS physical badge: one of
    #: `accreditation.AttendanceCategory`, or blank while none was recorded.
    #: The generic stock is per Badge Type, so the attendance days are never
    #: inferred from the Badge Type; they are recorded explicitly and must
    #: equal the registration's current attendance entitlement
    #: (`apps.badges.services.stock.record_attendance_marking`).
    #: `db_default` keeps inserts by a release without this column valid.
    attendance_marking = models.CharField(max_length=24, blank=True, default="", db_default="")
    attendance_marking_recorded_at = models.DateTimeField(null=True, blank=True)
    attendance_marking_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    #: Replacement chain, mirroring `DigitalEntryPass.replaced_by` exactly:
    #: the outgoing issuance points forward to its replacement once one
    #: exists; the replacement is never created before the outgoing row is
    #: safely transitioned out of the "current" set.
    replaced_by = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="replaces"
    )

    class Meta:
        db_table = "badges_badge_issuance"
        permissions = [
            ("issue_badgeissuance", "Can issue and replace a physical badge"),
            ("return_badgeissuance", "Can record a badge return, loss, or void"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["badge_assignment"],
                condition=models.Q(status=BadgeIssuanceStatus.ISSUED),
                name="bdg_issuance_one_current_uq",
            ),
            # The load-bearing one: exactly one live physical badge per
            # Registration Context, no matter how many times the Badge Type
            # assignment is superseded (Prompt 3 correction §4). Partial on
            # ISSUED and on a non-null registration, so the column could be
            # introduced additively.
            models.UniqueConstraint(
                fields=["registration"],
                condition=models.Q(status=BadgeIssuanceStatus.ISSUED, registration__isnull=False),
                name="bdg_issuance_one_current_per_reg_uq",
            ),
        ]
        indexes = [
            models.Index(fields=["badge_assignment", "status"], name="bdg_issuance_assign_idx"),
            models.Index(fields=["location", "status"], name="bdg_issuance_location_idx"),
            models.Index(fields=["registration", "status"], name="bdg_issuance_reg_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"issuance:{self.pk}:{self.status}"

    @property
    def is_current(self) -> bool:
        return self.status == BadgeIssuanceStatus.ISSUED


# ---------------------------------------------------------------------------
# Append-only stock ledger (authoritative quantity record)
# ---------------------------------------------------------------------------


class BadgeStockEntryType(models.TextChoices):
    """Deliberately seven values, not the schema's eight -- ADR-0020 §3:
    "damage" is not a ledger entry type. Damage at production receipt is
    `PrintBatch.damaged_quantity`; damage found in stock is an `ADJUSTMENT`.
    """

    RECEIVE = "RECEIVE", _("Receive")
    TRANSFER_OUT = "TRANSFER_OUT", _("Transfer out")
    TRANSFER_IN = "TRANSFER_IN", _("Transfer in")
    ISSUE = "ISSUE", _("Issue")
    RETURN = "RETURN", _("Return")
    ADJUSTMENT = "ADJUSTMENT", _("Reasoned adjustment")
    RECONCILIATION = "RECONCILIATION", _("Reconciliation")


class StockAdjustmentReasonCode(models.TextChoices):
    DAMAGED_IN_STOCK = "DAMAGED_IN_STOCK", _("Damaged while in stock")
    COUNT_CORRECTION = "COUNT_CORRECTION", _("Manual count correction")
    ADMINISTRATIVE_ERROR = "ADMINISTRATIVE_ERROR", _("Administrative error")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved reason")


class BadgeStockLedgerEntry(UUIDPrimaryKeyModel):
    """Append-only authoritative quantity ledger (Schema §10.4).

    Deliberately NOT `TimestampedModel`: an append-only row never carries a
    generic `updated_at` (the same convention `audit.AuditEvent` documents
    for itself). `recorded_at` is the server persistence time; `occurred_at`
    is the physical event time an operator may backdate slightly (e.g.
    recording a receipt the morning after goods actually arrived) but never
    forward of `recorded_at` by more than the small operational tolerance
    the service layer enforces.

    `operation_id`/`command_fingerprint` mirror `PassLifecycleOperation`
    exactly (ADR-0020 §4): every stock command is idempotency-bound through
    `BadgeStockOperation`, and this column is a convenient direct copy on
    the resulting ledger row, not a second independent mechanism.
    """

    operation_id = models.CharField(max_length=64, unique=True)
    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    entry_type = models.CharField(max_length=16, choices=BadgeStockEntryType.choices)
    quantity_delta = models.IntegerField()

    print_batch = models.ForeignKey(
        PrintBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    transfer = models.ForeignKey(
        StockTransfer,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    issuance = models.ForeignKey(
        BadgeIssuance,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    reconciliation = models.ForeignKey(
        "badges.StockReconciliation",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )

    reason_code = models.CharField(
        max_length=32, choices=StockAdjustmentReasonCode.choices, blank=True, default=""
    )
    reason_text = models.CharField(max_length=300, blank=True, default="")

    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "badges_stock_ledger_entry"
        constraints = [
            models.UniqueConstraint(fields=["operation_id"], name="bdg_ledger_operation_id_uq"),
            models.CheckConstraint(
                condition=~models.Q(quantity_delta=0), name="bdg_ledger_qty_nonzero_ck"
            ),
            # Exactly one source reference for the entry types that have
            # one; none for ADJUSTMENT.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        entry_type=BadgeStockEntryType.RECEIVE,
                        print_batch__isnull=False,
                        transfer__isnull=True,
                        issuance__isnull=True,
                        reconciliation__isnull=True,
                    )
                    | models.Q(
                        entry_type__in=[
                            BadgeStockEntryType.TRANSFER_OUT,
                            BadgeStockEntryType.TRANSFER_IN,
                        ],
                        print_batch__isnull=True,
                        transfer__isnull=False,
                        issuance__isnull=True,
                        reconciliation__isnull=True,
                    )
                    | models.Q(
                        entry_type__in=[BadgeStockEntryType.ISSUE, BadgeStockEntryType.RETURN],
                        print_batch__isnull=True,
                        transfer__isnull=True,
                        issuance__isnull=False,
                        reconciliation__isnull=True,
                    )
                    | models.Q(
                        entry_type=BadgeStockEntryType.RECONCILIATION,
                        print_batch__isnull=True,
                        transfer__isnull=True,
                        issuance__isnull=True,
                        reconciliation__isnull=False,
                    )
                    | models.Q(
                        entry_type=BadgeStockEntryType.ADJUSTMENT,
                        print_batch__isnull=True,
                        transfer__isnull=True,
                        issuance__isnull=True,
                        reconciliation__isnull=True,
                    )
                ),
                name="bdg_ledger_entry_type_reference_ck",
            ),
            # Entry direction, at the database level (Prompt 3 correction
            # §5). Five of the seven entry types have exactly one
            # arithmetically valid sign; a RECEIVE that decrements stock or
            # an ISSUE that increments it is not a business decision to be
            # trusted to the service layer, it is a corrupt row. ADJUSTMENT
            # and RECONCILIATION are genuinely bidirectional (a count can
            # correct upward or downward) and are covered by the existing
            # non-zero constraint alone.
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        entry_type__in=[
                            BadgeStockEntryType.RECEIVE,
                            BadgeStockEntryType.TRANSFER_IN,
                            BadgeStockEntryType.RETURN,
                        ]
                    )
                    | models.Q(quantity_delta__gt=0)
                ),
                name="bdg_ledger_positive_direction_ck",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        entry_type__in=[
                            BadgeStockEntryType.TRANSFER_OUT,
                            BadgeStockEntryType.ISSUE,
                        ]
                    )
                    | models.Q(quantity_delta__lt=0)
                ),
                name="bdg_ledger_negative_direction_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "badge_type", "location"], name="bdg_ledger_balance_idx"
            ),
            models.Index(fields=["recorded_at"], name="bdg_ledger_recorded_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"ledger:{self.entry_type}:{self.quantity_delta:+d}"


class BadgeStockBalance(UUIDPrimaryKeyModel):
    """Maintained projection of the ledger (Schema §10.7): the ledger stays
    authoritative -- `reconstruct_balance_from_ledger` recomputes the exact
    same figure by summing every ledger row, and a test proves the two
    always agree. Not `TimestampedModel`: `updated_at` alone (no
    `created_at` meaning) is enough to see when a projection last moved.

    `quantity` is the ON-HAND count: exactly the ledger's running sum.
    `reserved_quantity` is how much of that on-hand stock is currently
    ALLOCATED (TRD `PRINT-002`: "Received, spoiled, reserved and issued
    quantities MUST reconcile"). Reserved units have not moved anywhere --
    they are physically present at this location and still in the ledger's
    sum -- so allocation deliberately posts NO ledger row (Schema §10.4's
    `entry_type` list has no allocation member, because allocation is not a
    stock movement).

    `available = quantity - reserved_quantity` is what an unreserved
    consumer may draw on (UI/UX §9.3 lists "available" as its own figure
    beside planned/produced/issued/damaged).
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="balances")
    quantity = models.IntegerField(default=0)
    reserved_quantity = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "badges_stock_balance"
        constraints = [
            models.UniqueConstraint(
                fields=["event_edition", "badge_type", "location"], name="bdg_balance_scope_uq"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0), name="bdg_balance_nonneg_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_quantity__gte=0), name="bdg_balance_reserved_nonneg_ck"
            ),
            # Reserved stock can never exceed stock actually on hand, so
            # `available` can never go negative either.
            models.CheckConstraint(
                condition=models.Q(reserved_quantity__lte=models.F("quantity")),
                name="bdg_balance_reserved_within_onhand_ck",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"balance:{self.badge_type_id}@{self.location_id}:{self.quantity}"

    @property
    def available_quantity(self) -> int:
        """On-hand minus what is currently allocated."""
        return self.quantity - self.reserved_quantity


# ---------------------------------------------------------------------------
# Allocation (the "reserved" quantity of TRD `PRINT-002`)
# ---------------------------------------------------------------------------


class StockAllocationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    CONSUMED = "CONSUMED", _("Fully consumed")
    RELEASED = "RELEASED", _("Released")


class StockAllocationPurpose(models.TextChoices):
    """Why a quantity is being held back rather than left generally
    available. Deliberately a controlled vocabulary, like every other
    reason field in this app -- an allocation that cannot say what it is
    for is indistinguishable from stock quietly going missing.
    """

    CHECKPOINT_OPENING = "CHECKPOINT_OPENING", _("Checkpoint opening stock")
    SHIFT_RESERVE = "SHIFT_RESERVE", _("Shift reserve")
    ORGANIZATION_DELEGATION = "ORGANIZATION_DELEGATION", _("Delegation collection")
    PROTOCOL_RESERVE = "PROTOCOL_RESERVE", _("Protocol reserve")
    OPERATIONAL_BUFFER = "OPERATIONAL_BUFFER", _("Operational buffer")
    OTHER_APPROVED = "OTHER_APPROVED", _("Other approved purpose")


class BadgeStockAllocation(UUIDPrimaryKeyModel, TimestampedModel, VersionedModel):
    """A reserved quantity of one Badge Type at one Stock Location.

    This is the "allocation" step of the authoritative Prompt 3 list
    ("receipt, allocation, transfer, reasoned adjustment, and
    reconciliation") and the "reserved" quantity TRD `PRINT-002` requires
    to reconcile alongside received, spoiled, and issued.

    An allocation moves NO stock: the units stay physically at
    `location`, stay in the ledger's running sum, and stay in
    `BadgeStockBalance.quantity`. What changes is that they are no longer
    *available* -- `BadgeStockBalance.reserved_quantity` rises by the same
    amount, so an unrelated transfer, issuance, or negative adjustment can
    no longer consume them. That is exactly why allocation posts no
    `BadgeStockLedgerEntry`: Schema §10.4's `entry_type` list has no
    allocation member because the ledger records movements, and this is
    not one.

    `consumed_quantity` counts units issued against this allocation.
    Consuming one unit decrements both `quantity` and `reserved_quantity`
    on the balance, so availability is unchanged by an issuance that was
    already reserved for -- the whole point of reserving.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="allocations"
    )
    quantity = models.PositiveIntegerField()
    consumed_quantity = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=StockAllocationStatus.choices,
        default=StockAllocationStatus.ACTIVE,
    )
    purpose_code = models.CharField(max_length=32, choices=StockAllocationPurpose.choices)
    purpose_text = models.CharField(max_length=300, blank=True, default="")
    #: Optional operational owner of the reservation. Never grants a
    #: permission by itself -- it records who the stock is being held for.
    held_for_group = models.ForeignKey(
        "auth.Group", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    allocated_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    release_reason_text = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "badges_stock_allocation"
        permissions = [
            ("allocate_badgestock", "Can allocate and release reserved badge stock"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="bdg_allocation_qty_positive_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(consumed_quantity__lte=models.F("quantity")),
                name="bdg_allocation_consumed_within_qty_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "badge_type", "location", "status"],
                name="bdg_allocation_scope_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"allocation:{self.badge_type_id}@{self.location_id}:{self.remaining_quantity}"

    @property
    def remaining_quantity(self) -> int:
        """Units still reserved and not yet consumed."""
        return self.quantity - self.consumed_quantity


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class StockReconciliation(UUIDPrimaryKeyModel):
    """One physical-count reconciliation event for one (event, badge type,
    location) triple (Schema §10.1 relationship, §10.7 "any projection
    mismatch creates a reconciliation alert").

    Append-only historical evidence: once recorded, a reconciliation is
    never edited. `discrepancy` is `counted_quantity -
    expected_quantity_at_count`, persisted rather than recomputed, so the
    historical record of what the projection said at the time is preserved
    even if later entries change the current balance.
    """

    event_edition = models.ForeignKey(
        "events.EventEdition", on_delete=models.PROTECT, related_name="+"
    )
    badge_type = models.ForeignKey(
        "accreditation.BadgeType", on_delete=models.PROTECT, related_name="+"
    )
    location = models.ForeignKey(
        StockLocation, on_delete=models.PROTECT, related_name="reconciliations"
    )
    expected_quantity_at_count = models.IntegerField()
    counted_quantity = models.PositiveIntegerField()
    discrepancy = models.IntegerField()
    notes = models.CharField(max_length=300, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "badges_stock_reconciliation"
        permissions = [
            ("reconcile_badgestock", "Can record a badge stock reconciliation count"),
        ]
        indexes = [
            models.Index(
                fields=["event_edition", "badge_type", "location", "recorded_at"],
                name="bdg_reconciliation_scope_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"reconciliation:{self.badge_type_id}@{self.location_id}:{self.discrepancy:+d}"


# ---------------------------------------------------------------------------
# Idempotency ledger, shared by every stock command (ADR-0020 §4)
# ---------------------------------------------------------------------------


class StockOperationType(models.TextChoices):
    LOCATION_CREATE = "LOCATION_CREATE", _("Create stock location")
    BATCH_CREATE = "BATCH_CREATE", _("Create print batch")
    BATCH_STATUS_CHANGE = "BATCH_STATUS_CHANGE", _("Change print batch status")
    BATCH_RECEIVE = "BATCH_RECEIVE", _("Receive print batch")
    TRANSFER = "TRANSFER", _("Transfer stock")
    ALLOCATE = "ALLOCATE", _("Allocate stock")
    RELEASE_ALLOCATION = "RELEASE_ALLOCATION", _("Release stock allocation")
    ISSUE = "ISSUE", _("Issue badge")
    REPLACE_ISSUANCE = "REPLACE_ISSUANCE", _("Replace issued badge")
    RETURN_ISSUANCE = "RETURN_ISSUANCE", _("Return issued badge")
    MARK_LOST = "MARK_LOST", _("Mark issued badge lost")
    VOID_ISSUANCE = "VOID_ISSUANCE", _("Void issuance")
    ADJUSTMENT = "ADJUSTMENT", _("Reasoned stock adjustment")
    RECONCILIATION = "RECONCILIATION", _("Record stock reconciliation")


class BadgeStockOperation(models.Model):
    """Append-only idempotency record for one stock command.

    Structurally identical to `PassLifecycleOperation` (see its docstring
    for the full rationale): `operation_id` is caller-supplied and globally
    unique, but uniqueness alone is not enough -- `command_fingerprint`
    binds the identifier to one exact command (type, actor, target,
    material parameters) so a reused identifier for a genuinely different
    command is a conflict, never an unrestricted replay.
    """

    id = models.BigAutoField(primary_key=True)
    operation_id = models.CharField(max_length=64, unique=True)
    operation_type = models.CharField(max_length=24, choices=StockOperationType.choices)
    command_fingerprint = models.CharField(max_length=64, blank=True, default="")
    target_type = models.CharField(max_length=32, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")

    location = models.ForeignKey(
        StockLocation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    print_batch = models.ForeignKey(
        PrintBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    transfer = models.ForeignKey(
        StockTransfer, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    issuance = models.ForeignKey(
        BadgeIssuance, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    ledger_entry = models.ForeignKey(
        BadgeStockLedgerEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reconciliation = models.ForeignKey(
        StockReconciliation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    allocation = models.ForeignKey(
        BadgeStockAllocation, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "badges_stock_operation"
        constraints = [
            models.UniqueConstraint(fields=["operation_id"], name="bdg_stockop_operation_id_uq"),
        ]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="bdg_stockop_target_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.operation_type}:{self.operation_id}"
