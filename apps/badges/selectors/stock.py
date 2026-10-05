"""Read-only projections for generic physical badge stock (Phase 3 Prompt 3).

Every operational selector routes through
`apps.accounts.selectors.scope_filtered_queryset`, exactly like every other
selector in this app -- there is deliberately no second filtering path.
"""

from __future__ import annotations

from django.db import models

from apps.accounts.selectors import scope_filtered_queryset
from apps.badges.models import (
    ISSUANCE_TERMINAL_STATUSES,
    BadgeIssuance,
    BadgeIssuanceStatus,
    BadgeStockAllocation,
    BadgeStockBalance,
    BadgeStockLedgerEntry,
    PrintBatch,
    StockAllocationStatus,
    StockLocation,
    StockReconciliation,
    StockTransfer,
)


def stock_locations_visible_to(user, *, codename: str = "view_stocklocation"):
    """No `organization_field` override: `StockLocation` has no organization
    dimension at all (ADR-0020 -- physical stock belongs to the event, not
    to any one organization). A `ScopedGroupMembership` granting a stock
    permission must be event-scoped only; organization-scoping it would be
    a misconfiguration, exactly as for `apps.reviews.selectors` resources
    that likewise carry no organization field."""
    return scope_filtered_queryset(
        user, StockLocation.objects, app_label="badges", codename=codename
    ).select_related("event_edition")


def print_batches_visible_to(user, *, codename: str = "view_printbatch"):
    """See `stock_locations_visible_to`: no organization dimension exists
    on `PrintBatch` either."""
    return scope_filtered_queryset(
        user, PrintBatch.objects, app_label="badges", codename=codename
    ).select_related("event_edition", "badge_type", "destination_location")


def issuances_visible_to(user, *, codename: str = "view_badgeissuance"):
    """Unlike locations and batches, an issuance IS meaningfully
    organization-scopable: it flows through the Registration Context's own
    `source_organization`, exactly like `passes_visible_to` for
    `DigitalEntryPass`.

    BOTH scope fields are given explicitly. `BadgeIssuance` carries no
    `event_edition` column of its own -- it reaches its event through the
    issuing location -- so leaving `event_field` at its default silently
    broke every event-scoped membership with a `FieldError` instead of
    filtering (Prompt 3 correction §2).
    """
    return scope_filtered_queryset(
        user,
        BadgeIssuance.objects,
        app_label="badges",
        codename=codename,
        event_field="location__event_edition_id",
        organization_field="registration__source_organization_id",
    ).select_related("badge_assignment", "badge_type", "location")


def current_issuance_for_assignment(badge_assignment) -> BadgeIssuance | None:
    return (
        BadgeIssuance.objects.select_related("badge_type", "location")
        .filter(badge_assignment=badge_assignment, status=BadgeIssuanceStatus.ISSUED)
        .first()
    )


def issuance_history_for_assignment(badge_assignment):
    return BadgeIssuance.objects.filter(badge_assignment=badge_assignment).order_by("-issued_at")


def location_balances(location: StockLocation):
    """Every non-zero balance projection row at one location, for a
    production/reconciliation summary screen."""
    return (
        BadgeStockBalance.objects.filter(location=location)
        .select_related("badge_type")
        .order_by("badge_type__code")
    )


def event_balances(event_edition):
    return (
        BadgeStockBalance.objects.filter(event_edition=event_edition)
        .select_related("badge_type", "location")
        .order_by("location__code", "badge_type__code")
    )


def ledger_entries_for(*, event_edition, badge_type=None, location=None, limit: int = 200):
    """Ledger rows for a reconciliation/production summary, newest first."""
    queryset = BadgeStockLedgerEntry.objects.filter(event_edition=event_edition)
    if badge_type is not None:
        queryset = queryset.filter(badge_type=badge_type)
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset.select_related("badge_type", "location").order_by("-recorded_at")[:limit]


def reconciliations_for(*, event_edition, location=None, limit: int = 100):
    queryset = StockReconciliation.objects.filter(event_edition=event_edition)
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset.select_related("badge_type", "location").order_by("-recorded_at")[:limit]


def active_allocations_for(*, event_edition, location=None, badge_type=None, limit: int = 100):
    """Currently reserved quantities (TRD `PRINT-002`), newest first."""
    queryset = BadgeStockAllocation.objects.filter(
        event_edition=event_edition, status=StockAllocationStatus.ACTIVE
    )
    if location is not None:
        queryset = queryset.filter(location=location)
    if badge_type is not None:
        queryset = queryset.filter(badge_type=badge_type)
    return queryset.select_related("badge_type", "location").order_by("-allocated_at")[:limit]


def allocations_available_for_issuance(*, badge_type, event_edition=None, location=None):
    """Active allocations an issuer may legitimately draw one unit from.

    Always scoped to the exact Badge Type and only those with units left.
    Callers may additionally bind the event and location. The issuance
    page binds the event when listing choices; the POST handler binds both
    event and location before accepting the submitted UUID.
    """
    queryset = (
        BadgeStockAllocation.objects.filter(
            badge_type=badge_type,
            status=StockAllocationStatus.ACTIVE,
        )
        .filter(consumed_quantity__lt=models.F("quantity"))
        .select_related("location")
    )
    if event_edition is not None:
        queryset = queryset.filter(event_edition=event_edition)
    if location is not None:
        queryset = queryset.filter(location=location)
    return queryset.order_by("allocated_at")


def transfers_for(*, event_edition, limit: int = 100):
    return (
        StockTransfer.objects.filter(event_edition=event_edition)
        .select_related("badge_type", "source_location", "destination_location")
        .order_by("-completed_at")[:limit]
    )


__all__ = [
    "ISSUANCE_TERMINAL_STATUSES",
    "active_allocations_for",
    "allocations_available_for_issuance",
    "current_issuance_for_assignment",
    "event_balances",
    "issuance_history_for_assignment",
    "issuances_visible_to",
    "ledger_entries_for",
    "location_balances",
    "print_batches_visible_to",
    "reconciliations_for",
    "stock_locations_visible_to",
    "transfers_for",
]
