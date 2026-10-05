"""Phase 2 Prompt 4 operational group (least-privilege, mirrors
`apps.invitations.apps`/`apps.reviews.apps`'s bootstrap idiom exactly).

A NEW, distinct group -- Prompt 3's own "Accreditation Managers" group
(decision/reopen/cancellation) deliberately never receives any permission
declared here (Prompt 3 rule "Do not give these groups Prompt 4
assignment permissions").
"""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

ACCREDITATION_COORDINATORS_GROUP_NAME = "Accreditation Coordinators"

ACCREDITATION_COORDINATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("accreditation", "view_participantrole"),
    ("accreditation", "view_badgetype"),
    ("accreditation", "view_accessprofile"),
    ("accreditation", "view_accessrule"),
    ("accreditation", "view_participantroleassignment"),
    ("accreditation", "add_participantroleassignment"),
    ("accreditation", "change_participantroleassignment"),
    ("accreditation", "view_badgetypeassignment"),
    ("accreditation", "add_badgetypeassignment"),
    ("accreditation", "change_badgetypeassignment"),
    ("accreditation", "view_accessprofileassignment"),
    ("accreditation", "add_accessprofileassignment"),
    ("accreditation", "change_accessprofileassignment"),
    ("accreditation", "view_accessruleassignment"),
    ("accreditation", "add_accessruleassignment"),
    ("accreditation", "change_accessruleassignment"),
    ("accreditation", "view_bulkassignmentoperation"),
    ("accreditation", "add_bulkassignmentoperation"),
    ("registrations", "view_registration"),
)


def _ensure_accreditation_group(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=ACCREDITATION_COORDINATORS_GROUP_NAME)
    for app_label, codename in ACCREDITATION_COORDINATORS_PERMISSIONS:
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            continue
        group.permissions.add(permission)


class AccreditationConfig(AppConfig):
    """Configurable participant-role/badge-type/access-profile/access-rule
    reference data and registration-scoped assignment history (Phase 2
    Prompt 4; TRD §13.1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accreditation"
    label = "accreditation"

    def ready(self) -> None:
        post_migrate.connect(_ensure_accreditation_group, sender=self)
