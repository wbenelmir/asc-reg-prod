"""Operational groups for online entry (Phase 3 Prompt 4, ADR-0021).

Same idempotent, additive-only `post_migrate` idiom as `apps.badges.apps`.
Four groups, deliberately separate (TRD §16.2 example groups):

* `Entry Operators` verify and record entry decisions only. They may use
  the Digital Entry Pass QR, the registration reference, and the NIN /
  passport lookup (UJ-08 names identity lookup as an entry-control
  capability). They hold NO manual search, NO override, NO device
  administration, and -- critically -- NO `registrations.view_registration`:
  an entry user never gains general participant browsing (Prompt 4).
* `Entry Supervisors` add the controlled manual search, reason-coded
  overrides, and the checkpoint monitor (recent and denied attempts,
  ENTRY-005). From Phase 4 Prompt 3 they also review and close offline
  reconciliation cases at the checkpoints of their scope. Still no
  participant browsing, documents, or exports.
* `Entry Device Administrators` enroll, rescope, suspend, and revoke
  devices, and (Phase 3 Prompt 5) view the identifier-free entry
  observability dashboard -- latency, result codes, device health, queue
  depth, stock exceptions, anomaly signals. They hold no verification
  permission, so enrolling a device never implies being able to admit
  anyone through it.
* `Security Restriction Managers` create and revoke security restrictions
  and may see a restriction's category at a checkpoint. From Phase 4 Prompt
  2 they alone may order a destructive emergency wipe of a device local
  store (binding decision P2-F), and they may view devices to do so.

These groups are only ever effective through a `ScopedGroupMembership`
(event, and optionally venue/gate). The zero-permission
`External Security (Temporary)` group (Phase 2 Prompt 4) is untouched: an
external account gains entry capability only when an administrator scopes
it into one of the groups above, and its checkpoint view is then reduced
further by `apps.entry.selectors.viewer_projection_flags`.
"""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

ENTRY_OPERATORS_GROUP_NAME = "Entry Operators"
ENTRY_SUPERVISORS_GROUP_NAME = "Entry Supervisors"
ENTRY_DEVICE_ADMINISTRATORS_GROUP_NAME = "Entry Device Administrators"
SECURITY_RESTRICTION_MANAGERS_GROUP_NAME = "Security Restriction Managers"

ENTRY_OPERATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("entry", "verify_entry"),
    ("entry", "lookup_registration_reference"),
    ("entry", "lookup_identity_reference"),
)

ENTRY_SUPERVISORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    *ENTRY_OPERATORS_PERMISSIONS,
    ("entry", "manual_search_entry"),
    ("entry", "override_entry"),
    ("entry", "view_entryevent"),
    ("entry", "view_entrydevice"),
    # Phase 4 Prompt 3 (ADR-0024): the offline reconciliation queue, scoped
    # like every checkpoint permission (event, venue, gate).
    ("entry", "reconcile_offline_operation"),
)

ENTRY_DEVICE_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("entry", "manage_entrydevice"),
    ("entry", "view_entrydevice"),
    ("entry", "view_entry_observability"),
    # Phase 4 Prompt 2: per-event offline enablement (binding decision P2-E).
    ("entry", "enable_offline_entry"),
)

SECURITY_RESTRICTION_MANAGERS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("entry", "manage_securityrestriction"),
    ("entry", "view_restriction_reason"),
    # Phase 4 Prompt 2 (binding decision P2-F): the DISTINCT destructive
    # emergency-wipe authority, plus the device view needed to find a device.
    ("entry", "emergency_wipe_device"),
    ("entry", "view_entrydevice"),
)

_ALL_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (ENTRY_OPERATORS_GROUP_NAME, ENTRY_OPERATORS_PERMISSIONS),
    (ENTRY_SUPERVISORS_GROUP_NAME, ENTRY_SUPERVISORS_PERMISSIONS),
    (ENTRY_DEVICE_ADMINISTRATORS_GROUP_NAME, ENTRY_DEVICE_ADMINISTRATORS_PERMISSIONS),
    (SECURITY_RESTRICTION_MANAGERS_GROUP_NAME, SECURITY_RESTRICTION_MANAGERS_PERMISSIONS),
)


def _ensure_entry_groups(sender, **kwargs):
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


class EntryConfig(AppConfig):
    """Entry devices, checkpoint sessions, online verification, restrictions,
    overrides, Entry Events, (Phase 4 Prompt 2) offline PREPARATION, and
    (Phase 4 Prompt 3) offline operation synchronization and reconciliation.
    No Event Edge concept exists in this app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.entry"
    label = "entry"

    def ready(self) -> None:
        from apps.entry import checks  # noqa: F401 - registers system checks

        post_migrate.connect(_ensure_entry_groups, sender=self)
