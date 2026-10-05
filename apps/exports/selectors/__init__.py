"""Scope-filtered visibility for `ExportRequest` and its source data (Phase 2
Prompt 5 §4.5)."""

from __future__ import annotations

from apps.accounts.selectors import scope_filtered_queryset
from apps.exports.models import ExportRequest


def export_requests_visible_to(user, *, codename: str = "view_exportrequest"):
    """Return the `ExportRequest` queryset `user` is authorized to see.

    A `ScopedGroupMembership` only contributes scope when its OWN `Group`
    grants `exports.<codename>` (Prompt 4 final closure pass §6's rule,
    reused unchanged) -- an unauthorized caller gets an EMPTY queryset,
    never an exception.
    """
    return scope_filtered_queryset(
        user,
        ExportRequest.objects,
        app_label="exports",
        codename=codename,
        event_field="event_edition_id",
        organization_field="organization_id",
    )


def registrations_visible_for_export(user, *, codename: str = "add_exportrequest"):
    """Return the `registrations.Registration` queryset an export MAY draw
    from for `user` -- scoped by the EXACT export action permission, never
    by the broader/narrower `registrations.view_registration` scope (Phase
    2 Prompt 4 §3's "action-specific object scope on every mutation" rule,
    applied here to export generation)."""
    from apps.registrations.models import Registration

    return scope_filtered_queryset(
        user,
        Registration.objects,
        app_label="exports",
        codename=codename,
        organization_field="source_organization_id",
    )


def export_scope_choices(user):
    """The event editions and organizations `user` may export from (UI/UX
    Completion Gate F8): exactly those of the registrations
    `registrations_visible_for_export` returns. They are both the choices
    the export form offers and the only values it accepts."""
    from apps.events.models import EventEdition
    from apps.organizations.models import Organization

    scoped = registrations_visible_for_export(user)
    events = EventEdition.objects.filter(
        pk__in=scoped.values_list("event_edition_id", flat=True)
    ).order_by("name")
    organizations = Organization.objects.filter(
        pk__in=scoped.exclude(source_organization_id__isnull=True).values_list(
            "source_organization_id", flat=True
        )
    ).order_by("official_name")
    return events, organizations
