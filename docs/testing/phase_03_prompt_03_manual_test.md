# Phase 3 Prompt 3 — manual verification record

Scope: generic physical badge production, stock, and issuance.
Related: [ADR-0020](../architecture/ADR-0020-physical-badge-stock-design.md),
[badge_stock_permissions_and_privacy.md](../security/badge_stock_permissions_and_privacy.md)

**Status of this document:** the scenarios below are written for a human to
execute locally. **None is marked as manually executed.** Automated
evidence is cited per scenario; a scenario is marked executed only when a
person has actually run it and recorded the result here.

## Prerequisites

```bash
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py runserver 8000
```

An `EventEdition`, a `BadgeType`, an operator in `Badge Stock
Administrators` (event-scoped only), and an operator in `Badge Stock
Issuers` (event- and organization-scoped) must already exist. A
Registration Context with a current `BadgeTypeAssignment` is required for
the issuance scenarios.

---

## 1. Stock location creation and event scoping

**Steps.** Sign in as a `Badge Stock Administrators` member. Open
`/ops/badges/stock/<event_pk>/`. Create a `CENTRAL` and a `CHECKPOINT`
location. Then sign in as the same operator and navigate to a
**different** event's stock dashboard URL by editing the `event_pk`.

**Expect.** Both locations appear on the correct event's dashboard. The
other event's dashboard returns a plain 404, not a permission error page.

**Automated evidence.** `apps/badges/tests/test_stock_views.py`
(`test_an_authorized_administrator_sees_the_stock_dashboard`,
`test_an_out_of_scope_event_is_a_404_not_a_403`).

**Executed:** not yet.

---

## 2. Print batch lifecycle and the damage split at receipt

**Steps.** Create a print batch for 200 units. Mark it ready, then in
production. Record receipt: 190 accepted, 10 damaged.

**Expect.** The batch reads `Received`. The stock balance at the
destination location increases by exactly 190, never 200. The batch
detail page shows Produced 200 / Accepted 190 / Damaged 10.

**Then.** Attempt to cancel a batch that is already `IN_PRODUCTION`.

**Expect.** Refused — cancellation is only offered before production
starts.

**Automated evidence.**
`apps/badges/tests/test_stock_services.py`
(`test_print_batch_lifecycle_to_received_posts_only_the_accepted_quantity`,
`test_a_fully_damaged_batch_receives_with_no_ledger_entry`,
`test_cancellation_is_refused_once_production_has_started`).

**Executed:** not yet.

---

## 3. Transfer between locations

**Steps.** With stock received at `CENTRAL`, transfer 20 units to
`GATE-A`. Then attempt to transfer more units than `CENTRAL` currently
holds.

**Expect.** The first transfer succeeds and both balances update
correctly (a location's dashboard row shows the new figure immediately).
The second is refused with a message about insufficient stock, and
neither location's balance changes.

**Automated evidence.** `apps/badges/tests/test_stock_services.py`
(`test_transfer_moves_stock_and_posts_balanced_ledger_rows`,
`test_transfer_refuses_to_take_source_below_zero`),
`tests/concurrency/test_badge_stock_concurrency.py`
(`test_two_opposite_direction_transfers_between_the_same_locations_never_deadlock`).

**Executed:** not yet.

---

## 4. Issuance: no automatic substitution, no negative stock, no double issuance

**Steps.** Sign in as a `Badge Stock Issuers` member. Open a registration's
`/ops/badges/registrations/<pk>/badge/` page. Issue its assigned Badge
Type. Attempt to issue a **second** badge for the same registration.

**Expect.** The first issuance succeeds and appears as the current
issuance. The second is refused: "already has a current physical badge
issuance." The issue form itself only ever offers the assignment's own
Badge Type — there is no control on the page that could request a
different one.

**Then.** With a location's balance at zero, attempt to issue there.

**Expect.** Refused with "no stock ... available at this location"; no
`BadgeIssuance` row is created.

**Automated evidence.** `apps/badges/tests/test_stock_services.py`
(`test_issuing_the_wrong_badge_type_is_refused_and_nothing_moves`,
`test_issuing_with_no_stock_is_refused`,
`test_a_second_issuance_for_the_same_assignment_is_refused`),
`tests/browser/test_badge_stock.py`
(`test_the_issue_form_only_ever_offers_the_assignments_own_badge_type`),
`tests/concurrency/test_badge_stock_concurrency.py`
(`test_two_simultaneous_issuances_against_one_unit_never_both_succeed`,
`test_two_simultaneous_issuances_for_the_same_assignment_never_both_succeed`).

**Executed:** not yet.

---

## 5. Replacement, return, loss, and void

**Steps.** With one badge issued, replace it choosing "the original badge
is being returned to stock." Then, for a different issuance, record a
return; for another, report it lost; for another, void it.

**Expect.** Replacement with return-checked increases the location's
balance by one (the returned original) before decreasing it by one again
(the new issue) — net unchanged from before the replacement, minus
nothing extra. Replacement without the checkbox leaves the balance one
lower overall. A return and a void both restock one unit. A loss changes
no balance at all. Each transition appears correctly in the issuance
history table.

**Automated evidence.** `apps/badges/tests/test_stock_services.py`
(`test_replacement_without_returning_the_original_does_not_restock_it`,
`test_replacement_that_returns_the_original_nets_to_one_unit_consumed`,
`test_returning_an_issuance_restocks_it`,
`test_marking_an_issuance_lost_posts_no_ledger_entry`,
`test_voiding_an_issuance_restocks_it`).

**Executed:** not yet.

---

## 6. Allocation: reserving stock without moving it

**Steps.** With 45 units on hand at `CENTRAL`, allocate 10 with purpose
"Checkpoint opening stock". Then attempt to transfer 40 units away.

**Expect.** The dashboard shows On hand 45, Allocated 10, Available 35.
The ledger gains **no** new row — allocation moves nothing. The transfer
of 40 is refused: only 35 are available even though 45 are physically
present. Transferring 35 succeeds.

**Then.** Open a registration's physical-badge issuance page, select the
allocation whose option shows `CENTRAL`, the purpose, and remaining
quantity, then issue the badge. Return to the accounting dashboard and
release the allocation.

**Expect.** After the issuance, on hand and allocated have each fallen by
one and available is unchanged. Releasing returns only the still-unused
units; the consumed one does not come back.

**Automated evidence.** `apps/badges/tests/test_stock_corrections.py`
(`test_allocating_reserves_without_moving_stock`,
`test_an_unreserved_transfer_cannot_consume_allocated_stock`,
`test_issuing_against_an_allocation_consumes_the_reservation`,
`test_releasing_an_allocation_returns_only_unconsumed_units`),
`apps/badges/tests/test_stock_views.py`
(`test_the_issuance_page_lists_an_active_allocation_with_remaining_stock`,
`test_an_issuer_can_choose_and_consume_an_allocation_via_the_view`,
`test_a_forged_allocation_for_another_location_is_refused_without_side_effects`),
`tests/browser/test_badge_stock.py`
(`test_allocating_stock_reduces_available_without_moving_on_hand`,
`test_issuing_a_badge_can_draw_from_a_visible_allocation`),
`tests/concurrency/test_badge_stock_concurrency.py`
(`test_two_simultaneous_allocations_cannot_over_commit_the_same_units`).

**Executed:** not yet.

---

## 7. Reasoned adjustment and reconciliation

**Steps.** On the stock dashboard, record a reasoned adjustment of −1 with
reason "Damaged while in stock." Then record a reconciliation whose
counted quantity differs from the current balance.

**Expect.** The adjustment posts a ledger row and updates the balance
immediately. The reconciliation shows the expected, counted, and
discrepancy values, and the balance moves to exactly the counted figure.
Recomputing the balance by summing every ledger row for that location and
Badge Type gives the same number the dashboard shows.

**Automated evidence.** `apps/badges/tests/test_stock_services.py`
(`test_a_positive_adjustment_increases_the_balance`,
`test_reconciliation_corrects_the_balance_to_the_counted_value`,
`test_ledger_reconstructs_the_same_balance_the_projection_holds`).

**Executed:** not yet.

---

## 8. Event boundary and the handover-only boundary

**Steps.** Sign in as a `Badge Stock Administrators` member scoped to one
event. Open a second event's stock dashboard URL by editing `event_pk`.
Then sign in as a `Badge Stock Issuers` member and open the stock
dashboard, a print batch, and the issuance page for one of their
registrations.

**Expect.** The administrator gets a plain 404 for the other event. The
issuer gets a 404 for the dashboard and the batch, but the issuance page
opens normally and its issuing-location selector lists the event's active
locations.

**Then.** Using browser developer tools, edit the hidden `badge_type_id`
on the issuance form to a Badge Type from a different event and submit.
Repeat on the replacement form.

**Expect.** Both are refused with a 404; no issuance is created, no stock
moves, and no ledger row appears.

**Automated evidence.** `apps/badges/tests/test_stock_views.py`
(`test_a_forged_cross_event_issue_post_is_refused_with_no_side_effect`,
`test_a_forged_cross_event_replacement_post_is_refused_with_no_side_effect`,
`test_a_forged_substitution_to_another_local_badge_type_is_refused`,
`test_an_issuer_cannot_reach_the_stock_accounting_dashboard`,
`test_an_issuer_still_sees_the_issuing_location_selector`,
`test_an_issuer_cannot_allocate_or_transfer_stock`),
`apps/badges/tests/test_stock_corrections.py` (§2 service-layer cases).

**Executed:** not yet.

---

## 9. One physical badge per Registration Context, across reassignment

**Steps.** Issue a badge. Change the participant's Badge Type assignment
to a different type. Attempt to issue again against the new assignment.

**Expect.** Refused: the Registration Context already holds a live badge.
Return or replace the existing one first. A replacement must then use the
**new** current Badge Type; the old one is refused.

**Automated evidence.** `apps/badges/tests/test_stock_corrections.py`
(`test_a_reassignment_cannot_produce_a_second_live_badge`,
`test_the_database_itself_refuses_two_live_issuances_for_one_registration`,
`test_replacement_follows_a_reassignment_to_the_new_current_badge_type`).

**Executed:** not yet.

---

## 10. Localization and RTL

**Steps.** Open the stock dashboard and the badge issuance page. Switch
language to French, then to Arabic.

**Expect.** Every label (location type, ledger entry type, reason
choices, button text) is translated in both languages. In Arabic, the
page direction is `rtl` and the heading reads right-to-left; badge-type
codes and references stay left-to-right within the page.

**Automated evidence.**
`tests/browser/test_badge_stock.py`
(`test_the_stock_dashboard_renders_right_to_left_in_arabic`,
`test_the_badge_issuance_page_renders_right_to_left_in_arabic`),
`tests/foundation/test_locale_catalogs.py` (catalog completeness).

**Executed:** not yet.

---

## Deployment-only boundaries (not claimed)

Real managed production database-role isolation, real Redis/Celery
broker behaviour, real S3-compatible storage, and any entry-device,
scanner, or gate-admission workflow — none of that is implemented or
claimed by this prompt.
