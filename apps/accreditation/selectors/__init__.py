"""Scope-filtering selectors for accreditation assignments (Phase 2 Prompt 4 §3).

Every selector enforces scope at the QUERYSET level via
`apps.accounts.selectors.scope_filtered_queryset` -- the same proven
isolation rule used throughout `apps.invitations`/`apps.reviews`: a
`ScopedGroupMembership` only contributes scope when its OWN `Group` grants
the named permission; holding that permission through an unrelated Group
never combines with a differently-scoped membership to leak out-of-scope
rows. `codename` defaults to the VIEW permission -- a caller performing a
mutation MUST pass that mutation's own codename.
"""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.accounts.selectors import scope_filtered_queryset


def _effective_assignments(queryset, registration, reference_field):
    now = timezone.now()
    return queryset.filter(
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
        effective_from__lte=now,
        **{f"{reference_field}__event_edition_id": registration.event_edition_id},
    ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))


def role_assignments_visible_to(user, *, codename: str = "view_participantroleassignment"):
    from apps.accreditation.models import ParticipantRoleAssignment

    return scope_filtered_queryset(
        user, ParticipantRoleAssignment.objects, app_label="accreditation", codename=codename
    )


def registrations_visible_for_accreditation_action(user, *, codename: str):
    """Registrations authorized by the exact accreditation action permission."""
    from apps.registrations.models import Registration

    return scope_filtered_queryset(
        user,
        Registration.objects,
        app_label="accreditation",
        codename=codename,
        organization_field="source_organization_id",
    )


def badge_assignments_visible_to(user, *, codename: str = "view_badgetypeassignment"):
    from apps.accreditation.models import BadgeTypeAssignment

    return scope_filtered_queryset(
        user, BadgeTypeAssignment.objects, app_label="accreditation", codename=codename
    )


def access_profile_assignments_visible_to(user, *, codename: str = "view_accessprofileassignment"):
    from apps.accreditation.models import AccessProfileAssignment

    return scope_filtered_queryset(
        user, AccessProfileAssignment.objects, app_label="accreditation", codename=codename
    )


def access_rule_assignments_visible_to(user, *, codename: str = "view_accessruleassignment"):
    from apps.accreditation.models import AccessRuleAssignment

    return scope_filtered_queryset(
        user, AccessRuleAssignment.objects, app_label="accreditation", codename=codename
    )


def current_role_assignments(registration):
    from apps.accreditation.models import AssignmentStatus

    return _effective_assignments(
        registration.role_assignments.filter(status=AssignmentStatus.CURRENT),
        registration,
        "role",
    ).select_related("role")


def current_badge_assignment(registration):
    from apps.accreditation.models import AssignmentStatus

    return (
        _effective_assignments(
            registration.badge_assignments.filter(status=AssignmentStatus.CURRENT),
            registration,
            "badge_type",
        )
        .select_related("badge_type")
        .first()
    )


def current_access_profile_assignments(registration):
    from apps.accreditation.models import AssignmentStatus

    return _effective_assignments(
        registration.access_profile_assignments.filter(status=AssignmentStatus.CURRENT),
        registration,
        "access_profile",
    ).select_related("access_profile")


def current_access_rule_assignments(registration):
    from apps.accreditation.models import AssignmentStatus

    return _effective_assignments(
        registration.access_rule_assignments.filter(status=AssignmentStatus.CURRENT),
        registration,
        "access_rule",
    ).select_related("access_rule")


def participant_safe_badge_projection(registration) -> dict | None:
    """Return `{"code", "name"}` for the registration's current badge type
    ONLY IF that badge type is configured `participant_visible=True`
    (Phase 2 Prompt 4 §6 "conservative configurable visibility... default
    must remain hidden") -- `None` in every other case, including no
    current assignment at all."""
    assignment = current_badge_assignment(registration)
    if assignment is None or not assignment.badge_type.participant_visible:
        return None
    return {"code": assignment.badge_type.code, "name": assignment.badge_type.localized_name}


def bulk_assignable_references(user, *, model, codename: str):
    """Active reference objects (roles, badge types, access profiles or
    rules) `user` may bulk-assign: those of an event edition in which the
    user holds `accreditation.<codename>` (UI/UX Completion Gate F8).

    Uses the same event rule as `has_scoped_permission(user, …,
    event_edition_id=…)`, the check the bulk preview already applied to the
    chosen object; it is now also what the control offers, so an object the
    user may not assign is neither listed nor accepted.
    """
    from apps.accounts.policies import effective_scoped_memberships

    queryset = model.objects.filter(is_active=True).select_related("event_edition")
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    if user.is_superuser:
        return queryset.order_by("event_edition__name", "code")
    event_ids = set(
        effective_scoped_memberships(user)
        .filter(
            group__permissions__content_type__app_label="accreditation",
            group__permissions__codename=codename,
        )
        .values_list("event_edition_id", flat=True)
    )
    if not event_ids:
        return queryset.none()
    if None not in event_ids:
        queryset = queryset.filter(event_edition_id__in=event_ids)
    return queryset.order_by("event_edition__name", "code")


def registration_references(registration_ids) -> dict[str, str]:
    """`{registration id: public reference}` for ids a caller has ALREADY
    scoped (a bulk operation's own targets), so result tables show the
    reference staff know instead of a database id."""
    from apps.registrations.models import Registration

    return {
        str(pk): reference
        for pk, reference in Registration.objects.filter(pk__in=list(registration_ids)).values_list(
            "pk", "public_reference"
        )
    }
