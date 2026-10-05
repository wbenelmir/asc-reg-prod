"""Queryset-level scope-filtering selectors (Schema §12.3).

An unauthorized caller receives an EMPTY queryset, never an exception and
never a queryset that would let existence be inferred by timing or error
shape (policies must not leak object existence).
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.accounts.policies import effective_scoped_memberships

REGISTRATION_VIEW_PERMISSION_APP_LABEL = "registrations"
REGISTRATION_VIEW_PERMISSION_CODENAME = "view_registration"


def scope_filtered_queryset(
    user,
    queryset: QuerySet,
    *,
    app_label: str,
    codename: str,
    event_field: str = "event_edition_id",
    organization_field: str = "organization_id",
):
    """Generic scoped-visibility selector (Schema §12.3, Phase 2 Prompt 2).

    Shared by every app that needs "the rows this user may see through a
    `ScopedGroupMembership`" -- extracted from the Phase 1
    `registrations_visible_to` implementation so every new selector
    (invitation campaigns, delegation batches, ...) follows the exact same
    proven isolation rule instead of a hand-copied variant that could
    silently drift: a `ScopedGroupMembership` only contributes scope when
    its OWN `Group` grants `app_label.codename` -- holding that permission
    through an unrelated Group never combines with a differently-scoped
    membership to leak out-of-scope rows (Prompt 4 final closure pass §6).

    An unauthorized caller receives an EMPTY queryset, never an exception
    and never a queryset that would let existence be inferred by timing or
    error shape (policies must not leak object existence).
    """
    model = queryset.model
    if not getattr(user, "is_authenticated", False):
        return model.objects.none()
    if not getattr(user, "is_active", False):
        return model.objects.none()
    if user.is_superuser:
        return queryset.all()

    memberships = effective_scoped_memberships(user).filter(
        group__permissions__content_type__app_label=app_label,
        group__permissions__codename=codename,
    )
    scope_filter = Q(pk__in=[])
    for event_id, organization_id in memberships.values_list(
        "event_edition_id", "organization_id"
    ).distinct():
        if event_id is None and organization_id is None:
            return queryset.all()
        membership_filter = Q()
        if event_id is not None:
            membership_filter &= Q(**{event_field: event_id})
        if organization_id is not None:
            membership_filter &= Q(**{organization_field: organization_id})
        scope_filter |= membership_filter

    if scope_filter == Q(pk__in=[]):
        return model.objects.none()
    return queryset.filter(scope_filter).distinct()


def registrations_visible_to(user):
    """Return the `registrations.Registration` queryset `user` is authorized to see.

    A `ScopedGroupMembership` only contributes scope when its OWN `Group`
    grants `registrations.view_registration` -- holding that permission
    through an unrelated Group never combines with a differently-scoped
    membership to leak out-of-scope rows (Prompt 4 final closure pass §6).
    """
    from apps.registrations.models import Registration

    return scope_filtered_queryset(
        user,
        Registration.objects,
        app_label=REGISTRATION_VIEW_PERMISSION_APP_LABEL,
        codename=REGISTRATION_VIEW_PERMISSION_CODENAME,
        organization_field="source_organization_id",
    )
