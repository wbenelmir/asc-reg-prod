"""Queryset-level scope-filtering selectors for invitations (Schema §12.3).

Reuses `apps.accounts.selectors.scope_filtered_queryset` -- the exact same
isolation rule `registrations_visible_to` uses: a `ScopedGroupMembership`
only contributes scope when its OWN `Group` grants the named permission.
Link possession alone (knowing/using a token) never grants workspace
visibility -- only an explicit `ScopedGroupMembership` does.
"""

from __future__ import annotations

from apps.accounts.selectors import scope_filtered_queryset


def campaigns_visible_to(user, *, codename: str = "view_invitationcampaign"):
    """Return the `invitations.InvitationCampaign` queryset `user` may act on.

    `codename` defaults to the VIEW permission -- callers performing a
    specific mutation (change status, issue/rotate a link) MUST pass that
    exact mutation's own codename (Phase 2 Prompt 2 V2 correction pass
    "enforce action-specific object scope on every mutation"): fetching a
    mutation target through the view-only selector let a user holding
    change-scope for organization A but only VIEW-scope for organization B
    successfully load and then mutate a campaign belonging to B.
    """
    from apps.invitations.models import InvitationCampaign

    return scope_filtered_queryset(
        user,
        InvitationCampaign.objects,
        app_label="invitations",
        codename=codename,
    )


def delegation_batches_visible_to(user, *, codename: str = "view_delegationbatch"):
    """Return the `invitations.DelegationBatch` queryset `user` may act on.

    `codename` defaults to the VIEW permission -- a caller applying a batch
    MUST pass `"add_delegationbatch"` (the actual mutation permission,
    Phase 2 Prompt 2 V2 correction pass "enforce action-specific object
    scope on every mutation"), never fetch the target through the
    view-only default and mutate it regardless.

    Scoped by both `event_edition_id` and `organization_id`. The explicit
    `event_field` preserves the event dimension so a membership for an old
    edition cannot see or mutate a batch belonging to the current edition,
    even when both batches belong to the same organization.
    """
    from apps.invitations.models import DelegationBatch

    return scope_filtered_queryset(
        user,
        DelegationBatch.objects,
        app_label="invitations",
        codename=codename,
        event_field="event_edition_id",
    )


def organization_workspace_registrations(user):
    """Return the `Registration` queryset visible in the Organization Workspace.

    Identical to `apps.accounts.selectors.registrations_visible_to` -- a
    thin, named wrapper for workspace call sites. Organization access
    requires an explicit `ScopedGroupMembership`; it is never inferred from
    a Registration's `invitation_campaign.organization` alone (TRD
    `WORK-003`, `FR-INV-015`, Gate G3 "organization users cannot query
    unrelated records").
    """
    from apps.accounts.selectors import registrations_visible_to

    return registrations_visible_to(user)
