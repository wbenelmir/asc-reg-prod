# Generic physical badge stock — permissions, audit, and privacy classification

Phase 3 Prompt 3. Covers `StockLocation`, `PrintBatch`, `StockTransfer`,
`BadgeIssuance`, `BadgeStockLedgerEntry`, `BadgeStockBalance`,
`StockReconciliation`, and `BadgeStockOperation` (ADR-0020). Digital Entry
Pass credential permissions and privacy classification are documented
separately in `pass_permissions_and_privacy.md` and are unchanged by this
prompt.

## 1. Permissions

| Codename | Model | Meaning |
| --- | --- | --- |
| `badges.manage_stocklocation` | `StockLocation` | Create and manage stock locations |
| `badges.view_stocklocation` | `StockLocation` | Django default `view` permission — enough to pick an issuing location, and nothing more |
| `badges.view_stockaccounting` | `StockLocation` | The stock-accounting dashboard: balances, ledger, transfers, adjustments, reconciliation, allocations |
| `badges.manage_printbatch` | `PrintBatch` | Create, plan, and cancel print batches |
| `badges.receive_printbatch` | `PrintBatch` | Record production receipt |
| `badges.view_printbatch` | `PrintBatch` | Django default `view` permission |
| `badges.transfer_badgestock` | `StockTransfer` | Move stock between locations |
| `badges.allocate_badgestock` | `BadgeStockAllocation` | Reserve stock at a location and release a reservation |
| `badges.reconcile_badgestock` | `StockReconciliation` | Record a physical-count reconciliation |
| `badges.issue_badgeissuance` | `BadgeIssuance` | Issue and replace a physical badge at handover |
| `badges.return_badgeissuance` | `BadgeIssuance` | Record a return, a loss, or a void |
| `badges.view_badgeissuance` | `BadgeIssuance` | Django default `view` permission |

Reasoned adjustment (`record_adjustment`) is gated by
`badges.manage_stocklocation`, the same permission that gates the location
and per-location controls on the stock dashboard, rather than a separate
codename: an adjustment is a location-scoped correction, not a distinct
capability.

### 1.1 Groups

* **`Badge Stock Administrators`** — `manage_stocklocation`,
  `view_stocklocation`, `view_stockaccounting`, `manage_printbatch`,
  `receive_printbatch`, `view_printbatch`, `transfer_badgestock`,
  `allocate_badgestock`, `reconcile_badgestock`, `view_badgeissuance`.
  Runs production and stock accounting. Does **not** hold
  `issue_badgeissuance` or `return_badgeissuance`: this group cannot hand a
  physical badge to anyone.
* **`Badge Stock Issuers`** — `issue_badgeissuance`,
  `return_badgeissuance`, `view_badgeissuance`, `view_stocklocation`,
  `registrations.view_registration`. Runs handover only.

  The handover-only boundary is enforced by a **separate**
  `view_stockaccounting` permission, which this group does not hold
  (Prompt 3 correction §8). `view_stocklocation` is granted solely so the
  issuance page can offer an authorized **active** location in this
  registration's own event; it does not open the stock dashboard, the
  ledger, print batches, transfers, adjustments, reconciliation, or
  allocations. A compromised handover account can hand over and take back
  badges, and can never rewrite the accounting record.

This mirrors the separation ADR-0017/apps.py established for `Pass
Administrators`/`Credential Key Custodians`: the ability to move physical
stock never implies the ability to hand a badge to a specific person, and
vice versa.

### 1.2 Organization scoping (ADR-0020 §6)

`StockLocation` and `PrintBatch` carry no organization field: physical
stock belongs to the event as a whole. Their selectors
(`stock_locations_visible_to`, `print_batches_visible_to`) pass no
`organization_field` override to `scope_filtered_queryset`, so a
`ScopedGroupMembership` granting a stock-administration permission must be
event-scoped only — scoping it to an organization as well is a
misconfiguration this layer does not need to validate, because no such
membership can ever legitimately exist for these two resources.

`BadgeIssuance` is different: `issuances_visible_to` scopes on
`badge_assignment__registration__source_organization_id`, exactly
mirroring `passes_visible_to` for `DigitalEntryPass`, because an issuance
genuinely belongs to one Registration Context and therefore to that
context's source organization.

### 1.3 Enforcement boundaries

Every mutating view is POST-only, CSRF-protected (Django's default), and
gated by `operational_permission_required`, exactly like the credential
lifecycle views. An `EventEdition`-scoped screen (the stock dashboard,
batch/location/transfer/allocation/adjustment/reconciliation actions)
additionally checks `has_scoped_permission(user, codename,
event_edition_id=event.pk)` for the SPECIFIC event named in the URL,
returning 404 — never 403 — for an event outside the actor's granted
scope, so an out-of-scope event is indistinguishable from a nonexistent
one. A `StockLocation`, `PrintBatch`, or `BadgeIssuance` reached by its own
primary key is instead filtered through the matching `*_visible_to`
selector before `get_object_or_404`, following the same "absent from the
queryset, not merely hidden" rule every other operational surface in this
project uses.

### 1.4 The EventEdition boundary (Prompt 3 correction §2)

Enforced in **two independent layers**, because either alone is
insufficient:

* **Service.** `require_same_event` runs at the top of every stock
  command, before the transaction opens, covering the Badge Type, every
  location (source, destination, issuing, return, replacement), the print
  batch, the `BadgeTypeAssignment`, the Registration Context, the
  issuance, and any allocation. A cross-event combination raises
  `CrossEventScopeError` before a balance, ledger row, issuance,
  allocation, operation record, audit event, or outbox message exists.
* **HTTP.** Every Badge Type and Stock Location is loaded **scoped to the
  event** rather than by bare UUID, and the issuing-location lookup
  additionally requires `is_active=True`, so a forged POST naming another
  event's object is a 404 before the service is reached.

`issuances_visible_to` scopes on `location__event_edition_id` explicitly:
`BadgeIssuance` has no `event_edition` column of its own, and leaving the
default would raise a `FieldError` for any event-scoped membership rather
than filtering.

## 2. Audit

New action codes (`apps/audit/action_codes.py`):

`BDG_STOCK_LOCATION_CREATED`, `BDG_PRINT_BATCH_CREATED`,
`BDG_PRINT_BATCH_STATUS_CHANGED`, `BDG_PRINT_BATCH_RECEIVED`,
`BDG_STOCK_TRANSFERRED`, `BDG_STOCK_ALLOCATED`,
`BDG_STOCK_ALLOCATION_RELEASED`, `BDG_STOCK_ISSUED`,
`BDG_STOCK_ISSUANCE_REPLACED`, `BDG_STOCK_RETURNED`,
`BDG_STOCK_ISSUANCE_LOST`, `BDG_STOCK_ISSUANCE_VOIDED`,
`BDG_STOCK_ISSUANCE_BLOCKED`, `BDG_STOCK_ADJUSTED`,
`BDG_STOCK_RECONCILIATION_RECORDED`.

An allocation audit records the badge type, location, quantity, and the
resulting reserved and available figures — quantities only, never a
participant reference.

A denied issuance (`BDG_STOCK_ISSUANCE_BLOCKED`, reason
`WRONG_BADGE_TYPE` or `INSUFFICIENT_STOCK`) is recorded in its **own**
transaction, exactly like `apps.badges.services.generate_pass`'s denial
audit: writing it inside the failed command's own transaction would roll
the evidence back with everything else, which is worse than no refusal at
all.

### 2.1 What audit summaries contain

Every stock audit `before`/`after` summary carries only: a status value, a
quantity, a location or badge-type reference, or a reason code. None ever
carries a participant name, a national identity or passport value, a
contact address, or any value from `apps.people`/`apps.documents`. An
issuance's audit target is `BadgeTypeAssignment` or `BadgeIssuance` — an
operational classification record, never the `Registration` or `Person`
row directly.

## 3. Privacy classification

Physical badge stock data is **operational and aggregate**, not personal
data:

* `StockLocation`, `PrintBatch`, `StockTransfer`,
  `BadgeStockLedgerEntry`, `BadgeStockBalance`, and
  `StockReconciliation` contain event codes, badge-type codes, location
  codes, quantities, and operator (`OperationalUser`) references only.
  None contains a participant name, identity document value, contact
  address, or free-text field capable of holding one, beyond an
  operator-entered reason/notes string bounded to 300 characters and
  never rendered back to a participant.
* `BadgeIssuance` is the one model in this prompt that references a
  Registration Context (`badge_assignment`), and even there it carries no
  participant identity value directly — the identity path runs through
  the already-reviewed `accreditation.BadgeTypeAssignment` →
  `registrations.Registration` chain, unchanged by this prompt.
* The generic physical badge itself carries no participant-identifying
  print: "Physical badges are generic by type and prepared before the
  event" (fixed product rule), and this prompt does not
  introduce any entry-time personalized badge-printing dependency.
