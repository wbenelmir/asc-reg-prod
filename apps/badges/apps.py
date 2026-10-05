"""Operational groups for Digital Entry Pass credentials (Phase 3 Prompt 2)
and generic physical badge stock (Phase 3 Prompt 3, ADR-0020).

Follows the established `post_migrate` idiom used by
`apps.reviews.apps`/`apps.invitations.apps` exactly: idempotent, additive
only, and it never removes a permission an administrator granted separately.

Five groups, deliberately separate rather than one "badge admin" role:

* `Pass Administrators` run the Digital Entry Pass credential lifecycle;
* `Pass Viewers` read credential state without being able to change it;
* `Credential Key Custodians` manage signing-key publication and rotation
  and hold NOTHING else -- key custody is isolated from participant data on
  purpose, so the ability to rotate a signing key never implies the ability
  to read or alter a participant's credential.
* `Badge Stock Administrators` run production and stock accounting --
  locations, print batches, transfers, reasoned adjustments, and
  reconciliation -- but do NOT issue a physical badge at handover.
* `Badge Stock Issuers` do the opposite: they issue, replace, return, and
  report physical badges lost or voided at handover, but hold NO batch,
  transfer, adjustment, or reconciliation permission. This mirrors the
  real-world separation between central production/stock office staff and
  front-line registration-desk handover staff, and means a compromised
  handover account can never rewrite the stock ledger.

`registrations.view_registration` is granted to the credential and issuance
groups because their operational screens are reached through a
registration, and it remains subject to the same scoped-membership
enforcement as everywhere else. No group here receives any document,
export, review, or entry-device permission.
"""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

PASS_ADMINISTRATORS_GROUP_NAME = "Pass Administrators"  # noqa: S105 - a group name
PASS_VIEWERS_GROUP_NAME = "Pass Viewers"  # noqa: S105 - a group name
CREDENTIAL_KEY_CUSTODIANS_GROUP_NAME = "Credential Key Custodians"

PASS_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("badges", "add_digitalentrypass"),
    ("badges", "view_digitalentrypass"),
    ("badges", "activate_digitalentrypass"),
    ("badges", "suspend_digitalentrypass"),
    ("badges", "resume_digitalentrypass"),
    ("badges", "revoke_digitalentrypass"),
    ("registrations", "view_registration"),
)

PASS_VIEWERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("badges", "view_digitalentrypass"),
    ("registrations", "view_registration"),
)

CREDENTIAL_KEY_CUSTODIANS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("badges", "manage_verificationkey"),
)

BADGE_STOCK_ADMINISTRATORS_GROUP_NAME = "Badge Stock Administrators"
BADGE_STOCK_ISSUERS_GROUP_NAME = "Badge Stock Issuers"

BADGE_STOCK_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("badges", "manage_stocklocation"),
    ("badges", "view_stocklocation"),
    # The stock-accounting surface: dashboard, balances, ledger, transfers,
    # adjustments, reconciliation. Deliberately its OWN codename rather than
    # `view_stocklocation`, which issuers also need merely to pick an
    # issuing location (Prompt 3 correction §8).
    ("badges", "view_stockaccounting"),
    ("badges", "manage_printbatch"),
    ("badges", "receive_printbatch"),
    ("badges", "view_printbatch"),
    ("badges", "transfer_badgestock"),
    ("badges", "allocate_badgestock"),
    ("badges", "reconcile_badgestock"),
    ("badges", "view_badgeissuance"),
)

BADGE_STOCK_ISSUERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("badges", "issue_badgeissuance"),
    ("badges", "return_badgeissuance"),
    ("badges", "view_badgeissuance"),
    # Enough to choose an authorized ACTIVE issuing location at handover,
    # and nothing more: no `view_stockaccounting`, so the dashboard, the
    # ledger, print batches, transfers, adjustments, and reconciliation all
    # stay closed to this group (Prompt 3 correction §8).
    ("badges", "view_stocklocation"),
    ("registrations", "view_registration"),
)

_ALL_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (PASS_ADMINISTRATORS_GROUP_NAME, PASS_ADMINISTRATORS_PERMISSIONS),
    (PASS_VIEWERS_GROUP_NAME, PASS_VIEWERS_PERMISSIONS),
    (CREDENTIAL_KEY_CUSTODIANS_GROUP_NAME, CREDENTIAL_KEY_CUSTODIANS_PERMISSIONS),
    (BADGE_STOCK_ADMINISTRATORS_GROUP_NAME, BADGE_STOCK_ADMINISTRATORS_PERMISSIONS),
    (BADGE_STOCK_ISSUERS_GROUP_NAME, BADGE_STOCK_ISSUERS_PERMISSIONS),
)


def _ensure_badge_groups(sender, **kwargs):
    """Idempotently ensure the Phase 3 Prompt 2 operational groups exist."""
    from django.contrib.auth.models import Group, Permission

    for group_name, permission_pairs in _ALL_GROUPS:
        group, _ = Group.objects.get_or_create(name=group_name)
        for app_label, codename in permission_pairs:
            try:
                permission = Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            except Permission.DoesNotExist:
                continue
            group.permissions.add(permission)


class BadgesConfig(AppConfig):
    """Digital Entry Pass credentials, public verification keys, and generic
    physical badge stock (production, locations, ledger, issuance).

    Entry devices, device scope, device sessions, entry events, offline
    packages, and synchronization are explicitly NOT part of this app yet
    (Phase 3 Prompt 3 scope boundary; ADR-0017; ADR-0020).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.badges"
    label = "badges"

    def ready(self) -> None:
        post_migrate.connect(_ensure_badge_groups, sender=self)
