"""Project-owned authorization policy predicates (Schema §12.3, TRD authorization boundary).

Django `Group`/`Permission` remain the atomic permission catalogue
(Schema §12.2); `ScopedGroupMembership` narrows WHERE a granted permission
applies. Authorization is decided here, in application code -- never by a
hidden template link, a client-side check, or navigation structure.
"""

from __future__ import annotations

from functools import wraps
from uuid import UUID

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.shortcuts import redirect
from django.utils import timezone

from apps.accounts.models import ScopedGroupMembership, ScopedGroupMembershipStatus


def effective_scoped_memberships(
    user,
    *,
    event_edition_id: UUID | None = None,
    organization_id: UUID | None = None,
    venue_id: UUID | None = None,
    gate_id: UUID | None = None,
) -> QuerySet[ScopedGroupMembership]:
    """Return this user's ACTIVE, time-valid memberships matching the given scope.

    A membership with a NULL event/organization scope matches every value
    of that dimension (broad scope, Schema §12.3); a membership scoped to
    a SPECIFIC event or organization matches only that exact value -- it
    never leaks into a different event or organization.

    Venue and gate (Phase 3 Prompt 4, ADR-0021) are deliberately
    asymmetric and fail closed: a membership narrowed to a venue or gate
    matches ONLY when the caller names that exact venue or gate. A caller
    that names no venue/gate -- every pre-existing, non-checkpoint screen --
    never sees a venue- or gate-narrowed membership at all, so a
    gate-scoped entry operator can never acquire event-wide capability.
    """
    now = timezone.now()
    queryset = (
        ScopedGroupMembership.objects.filter(user=user, status=ScopedGroupMembershipStatus.ACTIVE)
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
    )
    if event_edition_id is not None:
        queryset = queryset.filter(
            Q(event_edition_id=event_edition_id) | Q(event_edition_id__isnull=True)
        )
    if organization_id is not None:
        queryset = queryset.filter(
            Q(organization_id=organization_id) | Q(organization_id__isnull=True)
        )
    if venue_id is None:
        queryset = queryset.filter(venue_id__isnull=True)
    else:
        queryset = queryset.filter(Q(venue_id=venue_id) | Q(venue_id__isnull=True))
    if gate_id is None:
        queryset = queryset.filter(gate_id__isnull=True)
    else:
        queryset = queryset.filter(Q(gate_id=gate_id) | Q(gate_id__isnull=True))
    return queryset


def has_scoped_permission(
    user,
    permission_codename: str,
    *,
    event_edition_id: UUID | None = None,
    organization_id: UUID | None = None,
    venue_id: UUID | None = None,
    gate_id: UUID | None = None,
) -> bool:
    """True if `user` holds `permission_codename` THROUGH a scoped membership.

    Superuser behavior is explicit (always `True`), never an accidental
    side effect of an unscoped query. An anonymous, unauthenticated, or
    inactive user is always denied, never allowed to fall through to a
    permissive default.

    Deliberately does NOT call `user.has_perm(...)`: that checks every
    Django Group the user's account happens to belong to, which is
    independent of `ScopedGroupMembership`. A membership only authorizes
    this permission when the SAME membership's own `Group` grants it --
    holding the permission via one (unscoped) Group while a DIFFERENT,
    unrelated Group merely happens to have an active scoped membership
    must never combine into a scoped grant (Prompt 4 final closure pass §6).
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if user.is_superuser:
        return True
    app_label, _, codename = permission_codename.partition(".")
    return (
        effective_scoped_memberships(
            user,
            event_edition_id=event_edition_id,
            organization_id=organization_id,
            venue_id=venue_id,
            gate_id=gate_id,
        )
        .filter(
            group__permissions__content_type__app_label=app_label,
            group__permissions__codename=codename,
        )
        .exists()
    )


def scoped_permission_codenames(
    user,
    *,
    event_edition_id: UUID | None = None,
    organization_id: UUID | None = None,
    venue_id: UUID | None = None,
    gate_id: UUID | None = None,
) -> set[str]:
    """Return every ``app_label.codename`` granted in one exact scope.

    This is the batched counterpart to :func:`has_scoped_permission` for
    screens that must render several independently authorized controls.
    The permissions still come from the same effective membership rows;
    unrelated groups and scopes are never combined.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return set()
    if user.is_superuser:
        from django.contrib.auth.models import Permission

        return {
            f"{app_label}.{codename}"
            for app_label, codename in Permission.objects.values_list(
                "content_type__app_label", "codename"
            )
        }
    return {
        f"{app_label}.{codename}"
        for app_label, codename in effective_scoped_memberships(
            user,
            event_edition_id=event_edition_id,
            organization_id=organization_id,
            venue_id=venue_id,
            gate_id=gate_id,
        )
        .values_list(
            "group__permissions__content_type__app_label",
            "group__permissions__codename",
        )
        .distinct()
    }


def deny_operational_access(request, *, login_url: str = "accounts:operational-sign-in"):
    """The one response for "this operational request is not permitted".

    Unauthenticated: redirect to operational sign-in, carrying the requested
    path as `next` (validated again by the sign-in view before use). A
    command (POST) is never replayed after sign-in, so it gets no `next`.
    Authenticated: raise `PermissionDenied`, so Django renders the localized
    403 page. Redirecting a signed-in user to sign-in instead caused an
    endless loop, because sign-in sends signed-in users straight back to an
    operations page (UI/UX Completion Gate finding F3).

    The 403 is raised before any object lookup, so it says nothing about
    whether a requested object exists.
    """
    if getattr(request.user, "is_authenticated", False):
        raise PermissionDenied
    if request.method not in ("GET", "HEAD"):
        return redirect(login_url)
    return redirect_to_login(request.get_full_path(), login_url=login_url)


def operational_permission_required(permission_codename: str, *, login_url: str):
    """View decorator gating access on `has_scoped_permission`, not `user.has_perm`.

    Replaces the combination of `@login_required` + Django's own
    `@permission_required` used previously: that pairing let a user through
    on an unscoped, unrelated Group grant of the same permission codename,
    even with zero active `ScopedGroupMembership` rows -- confusing at best
    (an "authorized" screen that always renders empty) and inconsistent
    with the scoped selector at worst. Checked with no event/organization
    filter (a broad "does this user have this permission through ANY active
    scoped membership" gate) -- the view's own queryset (e.g.
    `registrations_visible_to`) still narrows to the exact scope.

    A denied request goes through `deny_operational_access`: sign-in for an
    anonymous user, a 403 page for a signed-in one.
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not has_scoped_permission(request.user, permission_codename):
                return deny_operational_access(request, login_url=login_url)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
