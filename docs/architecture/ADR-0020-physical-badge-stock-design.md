# ADR-0020: Generic physical badge stock design

| Field | Value |
| --- | --- |
| Status | Accepted (revised by the Prompt 3 correction pass) |
| Date | 2026-09-22 |
| Related requirements | Schema §10; TRD `PRINT-002`; App Flow §9; UI/UX §9.3; Implementation Plan §13 (Phase 6); ADR-0017; Phase 3 Prompt 3 |

## Context

Phase 3 Prompt 3 (its work-package brief is historical, kept outside the repository)
authorizes generic physical badge production, stock, and issuance only. It
does not authorize entry devices, device enrollment, device sessions,
scanner workflows, online gate admission, `EntryEvent` recording,
overrides, security restrictions, offline verification, Offline Packages,
synchronization, or PWA behaviour.

`docs/specifications/05_BACKEND_SCHEMA.md` §10 describes a richer supporting
model (`PrintBatch`, `StockLocation`, `BadgeStockLedgerEntry`,
`StockTransfer`, `BadgeIssuance`, an optional `BadgeStockBalance`
projection) that includes fields referencing `EntryDevice` (§11), which does
not exist yet and is out of scope. Four implementation choices are recorded
here because the schema is silent or only partially specific, and each one
materially affects behaviour.

## Decisions

### 1. Stock models join `apps.badges`, not a new app

ADR-0017 already anticipated this: "When physical stock and entry work
begin, stock models may join `apps.badges`... neither requires moving
anything that exists today." No Digital Entry Pass model moves. Stock
models live in new submodules (`models/stock.py`, `services/stock.py`,
`selectors/stock.py`) inside the existing app, following the split already
established by `apps.communications.services` (`messaging.py` /
`rendering.py`). `views.py` and `forms/__init__.py` stay flat, matching
every other app in the repository.

### 2. Device references are omitted, not stubbed

`BadgeStockLedgerEntry.device_id` and `BadgeIssuance.device_id` are
described in the schema as nullable "offline-capable source" columns
referencing `EntryDevice` (§11), a model Prompt 3 explicitly excludes.
Creating a nullable FK to a model that does not exist is not possible, and
stubbing a placeholder `EntryDevice` model would itself be out-of-scope
device-enrollment work. Both columns are omitted entirely; every ledger
entry and issuance in this prompt is `recorded_by`/`issued_by` an
`OperationalUser` acting online. Adding the device columns later is an
additive migration, not a breaking one.

### 3. Damage is split across two existing mechanisms, not a new ledger entry type

The schema's `BadgeStockLedgerEntry.entry_type` choice list names "damage"
alongside receive/transfer/issue/return/adjustment/reconciliation, but the
Prompt 3 bullet list itself asks only for "receipt, allocation, transfer,
reasoned adjustment, and reconciliation" plus "replacement, return, and loss
handling" — it never mentions damage as its own ledger concept. Two
existing mechanisms already cover it without a new entry type:

- **Damage discovered at production receipt** is captured by
  `PrintBatch.damaged_quantity` versus `accepted_quantity`: only the
  accepted quantity is ever posted to the ledger as a `RECEIVE` entry, so
  units damaged before acceptance never enter stock at all.
- **Damage discovered while units are already in stock** is a reasoned
  `ADJUSTMENT` entry with a controlled reason code (`DAMAGED_IN_STOCK`),
  exactly the "reasoned adjustment" the prompt asks for.

`entry_type` is therefore `RECEIVE`, `TRANSFER_OUT`, `TRANSFER_IN`, `ISSUE`,
`RETURN`, `ADJUSTMENT`, `RECONCILIATION` — seven values, not eight.

### 4. Idempotency is one shared ledger, mirroring `PassLifecycleOperation` exactly

The schema places `operation_id` directly on `BadgeStockLedgerEntry`, which
would bind idempotency only for the subset of commands that move quantity.
Prompt 3 asks for "row locking and idempotency preventing negative stock
and double issuance" across the whole subsystem, including commands that
never touch the ledger (creating a location, changing a batch's production
status, marking an issuance lost). `BadgeStockOperation` is added as one
shared idempotency ledger for every stock command, structurally identical
to `PassLifecycleOperation`: `operation_id` (caller-supplied, globally
unique), `command_fingerprint` (binding the identifier to one exact
command, reusing `apps.badges.services.command_fingerprint` and
`normalize_reason_text` unchanged), and target references. A
`BadgeStockLedgerEntry` still carries its own `operation_id` (schema
fidelity, and a convenient direct link from a ledger row to its command),
but replay/conflict detection for every command goes through
`BadgeStockOperation`, not through a second, parallel mechanism.

### 5. Negative-stock prevention needs no reconciliation override

The schema notes "stock cannot become negative unless a separately
authorized reconciliation policy permits and records the exception." No
such exception is implemented, because none is needed: `StockReconciliation`
always posts a `RECONCILIATION` ledger entry whose delta is computed to
bring the projected balance to exactly the operator's counted quantity, and
a physical count is validated non-negative before it is accepted. The
resulting balance after any reconciliation is therefore always the counted
value itself, which can never be negative by construction — there is no
scenario where a legitimate reconciliation needs to force a negative
balance through. Every other operation (`RECEIVE`, `TRANSFER_OUT`,
`TRANSFER_IN`, `ISSUE`, `RETURN`, ordinary `ADJUSTMENT`) refuses
unconditionally if it would take a location's balance below zero.

### 6. Organization scoping applies to issuance only, never to locations or batches

`StockLocation` and `PrintBatch` carry no `organization` field: physical
stock belongs to the event as a whole, never to one organization (no PRD
or TRD rule ties badge stock to an organization, unlike a Registration or
an invitation). Their `scope_filtered_queryset` calls therefore pass no
`organization_field` override, matching `apps.reviews.selectors`'
resources that likewise carry no organization dimension. A
`ScopedGroupMembership` granting `manage_stocklocation`/`manage_printbatch`
must be event-scoped only; scoping it to an organization as well is a
misconfiguration outside what this layer validates, exactly as for the
existing `reviews` precedent.

`BadgeIssuance` is different: it flows through the Registration Context's
own `source_organization`, so `issuances_visible_to` scopes on
`badge_assignment__registration__source_organization_id`, mirroring
`passes_visible_to` for `DigitalEntryPass` exactly. `Badge Stock
Administrators` is therefore an event-only group in practice, while `Badge
Stock Issuers` may legitimately be scoped by organization as well as by
event.

### 7. Allocation is a reservation against availability, and posts no ledger row

The authoritative Prompt 3 list is "receipt, allocation, transfer, reasoned
adjustment, and reconciliation". The approved sources define allocation
without ambiguity, and it is **not** a stock movement:

- TRD `PRINT-002 [REQUIRED]`: "Received, spoiled, **reserved** and issued
  quantities MUST reconcile." Allocation is that reserved quantity.
- UI/UX §9.3 lists the stock figures an operator sees as "planned;
  produced; **available**; issued; damaged; adjusted with reason" —
  `available` is its own figure, distinct from what is physically present.
- App Flow §9.5 enumerates the append-only stock movements as Receive,
  Transfer, Issue, Return, Damage, Adjustment, Reconciliation. Allocation
  is absent, and so is any allocation member in Schema §10.4's
  `entry_type` list.
- App Flow §9.7 speaks of "a device-scoped local stock **allocation**" —
  stock set aside for a holder, not stock moved to one.

So `BadgeStockAllocation` reserves a quantity of one Badge Type at one
Stock Location and `BadgeStockBalance.reserved_quantity` rises by the same
amount. Nothing moves: `quantity` (on hand) is unchanged, no
`BadgeStockLedgerEntry` is written, and
`reconstruct_balance_from_ledger` still equals `current_balance` exactly.
What changes is `available = quantity − reserved_quantity`, and every
decrementing command (`TRANSFER_OUT`, `ISSUE`, negative `ADJUSTMENT`) is
checked against *available*, so one operator's reservation can never be
silently consumed by another's transfer.

Issuing against an allocation decrements `quantity` and `reserved_quantity`
together, leaving availability unchanged — the reservation is spent on
exactly what it was held for. Releasing an allocation returns only its
still-unconsumed units. A reconciliation counted below the reserved
quantity is refused with a domain error telling the operator to release
the over-committed allocation first, rather than surfacing an
`IntegrityError` from the `reserved_quantity <= quantity` constraint.

### 8. The EventEdition boundary is enforced in the service, not only the view

`require_same_event` runs at the top of every stock command, before the
transaction opens, and covers the Badge Type, the locations (source,
destination, issuing, return, replacement), the print batch, the
`BadgeTypeAssignment`, the Registration Context, the issuance, and any
allocation. A cross-event combination raises `CrossEventScopeError` before
any balance, ledger row, issuance, allocation, operation record, audit
event, or outbox message exists. The HTTP layer additionally loads every
Badge Type and Stock Location **scoped to the event** (and, for issuance,
only `is_active` locations) rather than by bare UUID, so a forged POST is a
404 before the service is even called. Both layers are load-bearing: the
view stops the common case cleanly, the service stops anything that
bypasses the view.

### 9. One current physical badge is enforced per Registration Context

`BadgeIssuance.registration` is denormalized from the assignment and
carries the partial unique constraint
`bdg_issuance_one_current_per_reg_uq`. The original per-assignment
constraint was insufficient: changing a participant's Badge Type creates a
*new* `BadgeTypeAssignment` row, so a second issuance against it satisfied
the old constraint while leaving one Registration Context holding two live
badges. The per-assignment constraint is retained as a strictly narrower
companion. A replacement's Badge Type is re-derived from the Registration
Context's CURRENT assignment under lock and must match exactly, so neither
a forged POST nor a reassignment landing mid-request can substitute a
different type.

### 10. The ledger is append-only in the database, with enforced directions

A `BEFORE UPDATE OR DELETE` trigger on `badges_stock_ledger_entry` refuses
both statements, exactly as `audit_event` has since `audit/0001_initial`
(ADR-0008). Application-level care is not enough for a table that is the
authoritative quantity record. As with the audit trigger, the owning local
role can still drop it, so real database-role isolation remains a
separately verified deployment fact and is never claimed here. Check
constraints additionally pin the five unambiguous entry directions
(`RECEIVE`, `TRANSFER_IN`, `RETURN` positive; `TRANSFER_OUT`, `ISSUE`
negative); `ADJUSTMENT` and `RECONCILIATION` remain bidirectional but
never zero.

### 11. One global balance-lock order for the whole module

`_lock_balances` is the only place that decides lock order —
ascending `(event_edition_id, badge_type_id, location_id)` as strings —
and every command that touches more than one balance row goes through it,
including a replacement that returns the original to one location while
issuing from another. Two commands can therefore never take the same pair
of rows in opposite orders. A real multi-connection test overlaps exactly
that replacement with a transfer over the same pair and proves both
complete with the ledger and the projection in agreement.

### 12. `Badge Stock Issuers` is handover-only, enforced by a separate permission

`view_stocklocation` is what handover staff need to choose an authorized
active issuing location, so it cannot also be what opens the accounting
surface. A dedicated `view_stockaccounting` permission now gates the stock
dashboard (balances, ledger, transfers, adjustments, reconciliation,
allocations) and is granted to `Badge Stock Administrators` only.

## Consequences

- Extending device-scoped stock operations (Phase 3 Prompt 4+ / Phase 4) is
  additive: nullable `device` FKs can be added to `BadgeStockLedgerEntry`
  and `BadgeIssuance` without touching any constraint or migration created
  here.
- `BadgeStockBalance` is a maintained projection (schema §10.7), updated
  transactionally with every ledger row; `reconstruct_balance_from_ledger`
  recomputes the same figure by summing the ledger directly, and a test
  asserts the two always agree, satisfying the Gate G6 criterion "ledger
  reconstructs every location balance."
- No Venue/Gate scope is attached to `StockLocation` (ADR-0010 defers that
  model); a location's physical description is presently limited to a code,
  a name, and a `CENTRAL`/`CHECKPOINT` type.
