"""Checkpoint authorization policies (Phase 3 Prompt 4, ADR-0021).

TRD §16.2: `allow = authenticated AND permission_granted(action) AND
object_in_scope(user, object) AND contextual_policy_allows(action, object,
mode)`. For a checkpoint action the object scope is the exact
(event edition, venue, gate) of the operator's current device session, and
the contextual policy is the device scope (`DeviceScope`).

Both halves are always required. Holding `entry.verify_entry` for gate A
never authorizes an action at gate B, and a device scoped to gate A can
never be used at gate B by anyone -- including a user whose own membership
covers both gates.
"""

from __future__ import annotations

from apps.accounts.models import OperationalUserAccountType
from apps.accounts.policies import effective_scoped_memberships, has_scoped_permission


def has_checkpoint_permission(user, codename: str, *, event_edition_id, venue_id, gate_id) -> bool:
    """True if `user` holds `entry.<codename>` scoped to this exact checkpoint.

    A checkpoint is scoped by event edition, venue and gate -- never by an
    organization. The shared `has_scoped_permission` applies no organization
    filter when the caller names none, so an organization-scoped membership
    would silently authorize every participant at every gate of the event.
    Here the grant must therefore come from ONE membership that is
    organization-UNscoped (`organization IS NULL`) and whose own group holds
    the permission, whose event/venue/gate scope matches and whose time
    window is open (Phase 3 Prompt 8, P8-08). An organization-scoped
    membership alone grants no checkpoint capability; an operator who needs
    one receives a separate organization-unscoped event/venue/gate
    membership. This is the same rule `devices_visible_to` applies to
    devices, which also belong to an edition rather than an organization.

    Superuser behavior is unchanged and explicit, exactly as in
    `has_scoped_permission`; organization back-office authorization, which
    does not pass through this function, is unchanged too.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if user.is_superuser:
        return True
    return (
        effective_scoped_memberships(
            user, event_edition_id=event_edition_id, venue_id=venue_id, gate_id=gate_id
        )
        .filter(
            organization__isnull=True,
            group__permissions__content_type__app_label="entry",
            group__permissions__codename=codename,
        )
        .exists()
    )


def checkpoint_permission(user, codename: str, checkpoint) -> bool:
    return has_checkpoint_permission(
        user,
        codename,
        event_edition_id=checkpoint.event_edition.pk,
        venue_id=checkpoint.gate.venue_id,
        gate_id=checkpoint.gate.pk,
    )


def method_permission_codename(method: str) -> str:
    """The user permission each lookup method requires (FR-ENT-010: every
    lookup follows the same permission rules, never a weaker path)."""
    from apps.entry.models import VerificationMethod

    return {
        VerificationMethod.QR: "verify_entry",
        VerificationMethod.NIN: "lookup_identity_reference",
        VerificationMethod.PASSPORT: "lookup_identity_reference",
        VerificationMethod.REFERENCE: "lookup_registration_reference",
        VerificationMethod.MANUAL: "manual_search_entry",
    }[method]


def method_is_permitted(user, method: str, checkpoint) -> bool:
    """A lookup method is usable only when BOTH the device scope lists it
    and the user holds its permission at this checkpoint."""
    if method not in checkpoint.verification_methods:
        return False
    if not checkpoint_permission(user, "verify_entry", checkpoint):
        return False
    return checkpoint_permission(user, method_permission_codename(method), checkpoint)


def is_external_security_user(user) -> bool:
    return getattr(user, "account_type", "") == OperationalUserAccountType.EXTERNAL_SECURITY


def may_manage_devices(user, *, event_edition_id) -> bool:
    return has_scoped_permission(
        user, "entry.manage_entrydevice", event_edition_id=event_edition_id
    )
