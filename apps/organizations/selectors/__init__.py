"""Bounded organization lookups (Schema §6.2, FR-ORG-004).

`search_organizations` never performs an unbounded scan: results are capped
and ordered deterministically, a query shorter than `MIN_QUERY_LENGTH`
returns an empty queryset (Phase 2 Prompt 2 correction pass "require a
sensible minimum normalized query length"), and the primary match against
`official_name`/`normalized_name` is an anchored PREFIX match
(`startswith`), which can use the existing `org_organization_norm_idx`
b-tree index -- never a leading-wildcard `%query%` scan across the whole
table. Alias matching stays a bounded `icontains` (a much smaller,
secondary join, and legitimate historical/alternate-spelling lookup would
otherwise be unfindable by anything but its own prefix).

`organizations_visible_to` is the scope-filtering counterpart: which
organizations a user may act on for a NAMED permission, used everywhere an
organization is looked up by id from an authorized workspace action so an
out-of-scope UUID 404s instead of leaking existence via a 403 (Phase 2
Prompt 2 correction pass "eliminate organization existence leaks").
"""

from __future__ import annotations

MAX_SEARCH_RESULTS = 20
MIN_QUERY_LENGTH = 2


def search_organizations(query: str, *, visible_ids=None, limit: int = MAX_SEARCH_RESULTS):
    """Return active organizations whose name matches `query`, most-relevant first.

    Callers that must restrict results to a user's own authorized scope
    (e.g. the Organization Workspace search screen) MUST pass `visible_ids`
    (typically an `organizations_visible_to(...)` queryset/iterable of pks)
    -- this function alone never applies any scope filter of its own and
    must never be exposed to an end user without it (Phase 2 Prompt 2
    correction pass "must never search all organizations merely because
    the user holds the permission somewhere").

    `visible_ids` is applied INSIDE this function's own queryset
    construction, before `order_by()`/the final `[:limit]` slice (Phase 2
    Prompt 2 V2 correction pass "fix organization search execution") --
    Django refuses to `.filter(...)` a queryset that has already been
    sliced, so a caller intersecting scope AFTER calling this function
    (`search_organizations(query).filter(pk__in=visible_ids)`) would raise
    at runtime. The slice is always this function's own last operation.
    """
    from django.db.models import Q

    from apps.organizations.models import Organization, OrganizationStatus

    normalized = (query or "").strip()
    if len(normalized) < MIN_QUERY_LENGTH:
        return Organization.objects.none()
    normalized_lower = normalized.lower()
    queryset = Organization.objects.filter(
        Q(official_name__istartswith=normalized)
        | Q(normalized_name__startswith=normalized_lower)
        # Indexed prefix match, never a leading-wildcard `icontains` scan
        # (Phase 2 Prompt 2 V2 correction pass "remove the remaining
        # leading-wildcard alias search").
        | Q(aliases__alias_name__istartswith=normalized)
    ).exclude(status=OrganizationStatus.MERGED)
    if visible_ids is not None:
        queryset = queryset.filter(pk__in=visible_ids)
    return queryset.distinct().order_by("official_name")[:limit]


def organizations_visible_to(user, *, app_label: str, codename: str, event_edition_id=None):
    """Return the `Organization` queryset `user` may act on for `app_label.codename`.

    Deliberately NOT built on `apps.accounts.selectors.scope_filtered_queryset`:
    that helper matches a membership's event/organization dimensions
    against fields on the TARGET model, which is correct for a model that
    references an organization (e.g. `Registration.source_organization_id`)
    but wrong for `Organization` itself -- here the target row's OWN `id`
    is what a membership's `organization_id` must match. A membership with
    a NULL organization (broad scope) still grants every organization,
    exactly as `scope_filtered_queryset` does for its own dimensions.

    `event_edition_id`, when given, is threaded into
    `effective_scoped_memberships` so a membership scoped to a SPECIFIC,
    now-past event never grants organization-level action on the CURRENT
    event (Phase 2 Prompt 2 V2 correction pass "preserve event scope in
    organization actions") -- only a membership matching the current event,
    or one with a NULL/broad event scope, contributes. Callers acting on a
    concrete event (campaign creation, delegation upload, on-behalf
    creation, organization search) MUST pass the CURRENT event's id;
    passing `None` (the default) preserves the previous organization-only
    scoping for call sites that are not event-specific.
    """
    from apps.accounts.policies import effective_scoped_memberships
    from apps.organizations.models import Organization

    if not getattr(user, "is_authenticated", False):
        return Organization.objects.none()
    if not getattr(user, "is_active", False):
        return Organization.objects.none()
    if user.is_superuser:
        return Organization.objects.all()

    memberships = effective_scoped_memberships(user, event_edition_id=event_edition_id).filter(
        group__permissions__content_type__app_label=app_label,
        group__permissions__codename=codename,
    )
    organization_ids: set = set()
    for organization_id in memberships.values_list("organization_id", flat=True).distinct():
        if organization_id is None:
            return Organization.objects.all()
        organization_ids.add(organization_id)

    if not organization_ids:
        return Organization.objects.none()
    return Organization.objects.filter(pk__in=organization_ids)
