"""Scope-filtering selectors for the review domain (Phase 2 Prompt 3 §6).

Every selector enforces scope at the QUERYSET level, reusing
`apps.accounts.selectors.scope_filtered_queryset` -- the exact same
isolation rule already proven by `registrations_visible_to`/
`campaigns_visible_to`: a `ScopedGroupMembership` only contributes scope
when its OWN `Group` grants the named permission, and holding that
permission through an unrelated Group never combines with a differently-
scoped membership to leak out-of-scope rows. `codename` defaults to the
VIEW permission; a caller performing a specific mutation MUST pass that
mutation's own codename (Phase 2 Prompt 2/3 "action-specific object
scope on every mutation") -- a generic view selector must never authorize
a mutation.
"""

from __future__ import annotations

from apps.accounts.selectors import scope_filtered_queryset


def review_cases_visible_to(user, *, codename: str = "view_reviewcase"):
    from apps.reviews.models import ReviewCase

    return scope_filtered_queryset(user, ReviewCase.objects, app_label="reviews", codename=codename)


def information_requests_visible_to(user, *, codename: str = "view_informationrequest"):
    from apps.reviews.models import InformationRequest

    return scope_filtered_queryset(
        user,
        InformationRequest.objects,
        app_label="reviews",
        codename=codename,
        event_field="registration__event_edition_id",
        organization_field="registration__source_organization_id",
    )


def duplicate_resolutions_visible_to(user, *, codename: str = "view_duplicatecandidates"):
    from apps.reviews.models import DuplicateCandidateResolution

    return scope_filtered_queryset(
        user,
        DuplicateCandidateResolution.objects,
        app_label="reviews",
        codename=codename,
        event_field="review_case__event_edition_id",
        organization_field="review_case__organization_id",
    )


def decisions_visible_to(user, *, codename: str = "view_registrationdecision"):
    from apps.reviews.models import RegistrationDecision

    return scope_filtered_queryset(
        user,
        RegistrationDecision.objects,
        app_label="reviews",
        codename=codename,
        event_field="event_edition_id",
        organization_field="organization_id",
    )


def duplicate_candidates_for(review_case):
    """Re-derive the candidate `IdentityIdentifier` rows a DUPLICATE
    `ReviewCase`'s own registration conflicts with (Phase 2 Prompt 3 §8).

    Never stores the raw NIN/passport value a second time: the flagged
    Registration's OWN `IdentityIdentifier` (linked via its Person) is
    still readable (it was never marked VERIFIED when the conflict was
    first detected -- since IDV-2 the identity worker's final duplicate
    recheck, `apps.people.services.identity_verification.
    verify_identifier_or_conflict`, refuses to verify it), so this decrypts that ONE
    existing encrypted value and re-runs the EXACT SAME blind-index lookup
    (`apps.people.selectors.identifiers_for_value`) used at detection time,
    excluding the reviewed Registration's own Person. Returns an empty
    queryset for a case with no resolvable identifier (never raises).
    """
    from apps.people.models import IdentifierStatus, IdentityIdentifier
    from apps.people.selectors import identifiers_for_value

    registration = review_case.registration
    if registration.person_id is None:
        return IdentityIdentifier.objects.none()
    candidate_source = (
        IdentityIdentifier.objects.filter(person_id=registration.person_id)
        .exclude(status=IdentifierStatus.VERIFIED)
        .order_by("-created_at")
        .first()
    )
    if candidate_source is None:
        return IdentityIdentifier.objects.none()
    return (
        identifiers_for_value(
            identifier_type=candidate_source.identifier_type,
            country_code_id=candidate_source.country_code_id,
            raw_value=candidate_source.value_encrypted,
        )
        .filter(status=IdentifierStatus.VERIFIED)
        .exclude(person_id=registration.person_id)
    )


def checklist_results_for(review_case):
    """Every checklist result for `review_case`, most recent per item first
    -- callers reduce to "current" state by taking the first row per
    `item_code` (Phase 2 Prompt 3 §5.3: prior evidence stays queryable)."""
    return review_case.checklist_results.select_related("checklist_definition", "actor").order_by(
        "item_code", "-recorded_at"
    )


def current_checklist_results(review_case) -> dict:
    """The CURRENT (most recently recorded) result per item code."""
    current: dict[str, object] = {}
    for result in checklist_results_for(review_case):
        current.setdefault(result.item_code, result)
    return current


#: The permission an operational user must hold, in the case's own scope,
#: to be offered (and accepted) as a review-case assignee (UI/UX Completion
#: Gate F8): an assignee must be able to open the case they are given.
ASSIGNEE_PERMISSION = ("reviews", "view_reviewcase")


def eligible_review_assignees(review_case):
    """Active operational users who may be assigned `review_case`.

    Applies, for every user at once, the rule `review_cases_visible_to`
    applies to one user: an ACTIVE, time-valid account holding the
    permission through an ACTIVE, time-valid `ScopedGroupMembership` whose
    OWN group grants it, whose event/organization scope covers the case
    (NULL = broad; an organization-scoped membership never covers a case
    without that organization) and which is not narrowed to a venue or
    gate; or an active superuser. So an assignee can always open the case
    they are given. This list is both what the assignment control offers
    and what the assignment view accepts, so a forged, stale or
    out-of-scope user id is rejected.
    """
    from django.db.models import Q
    from django.utils import timezone

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserStatus,
        ScopedGroupMembership,
        ScopedGroupMembershipStatus,
    )

    now = timezone.now()
    app_label, codename = ASSIGNEE_PERMISSION
    memberships = (
        ScopedGroupMembership.objects.filter(status=ScopedGroupMembershipStatus.ACTIVE)
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
        .filter(Q(event_edition_id=review_case.event_edition_id) | Q(event_edition_id__isnull=True))
        .filter(venue_id__isnull=True, gate_id__isnull=True)
        .filter(
            group__permissions__content_type__app_label=app_label,
            group__permissions__codename=codename,
        )
    )
    if review_case.organization_id is None:
        memberships = memberships.filter(organization_id__isnull=True)
    else:
        memberships = memberships.filter(
            Q(organization_id=review_case.organization_id) | Q(organization_id__isnull=True)
        )
    return (
        OperationalUser.objects.filter(status=OperationalUserStatus.ACTIVE)
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
        .filter(Q(is_superuser=True) | Q(pk__in=memberships.values("user_id")))
        .order_by("display_name", "email_normalized")
    )
