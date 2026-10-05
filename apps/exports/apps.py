"""Phase 2 Prompt 5 least-privilege group for controlled exports (mirrors
`apps.accreditation.apps`'s bootstrap idiom exactly)."""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

EXPORT_ADMINISTRATORS_GROUP_NAME = "Export Administrators"

EXPORT_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("exports", "view_exportrequest"),
    ("exports", "add_exportrequest"),
    ("registrations", "view_registration"),
)


def _ensure_export_administrators_group(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=EXPORT_ADMINISTRATORS_GROUP_NAME)
    for app_label, codename in EXPORT_ADMINISTRATORS_PERMISSIONS:
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            continue
        group.permissions.add(permission)


class ExportsConfig(AppConfig):
    """Purpose-bound controlled exports (Phase 2 Prompt 5 §4.5)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.exports"
    label = "exports"

    def ready(self) -> None:
        post_migrate.connect(_ensure_export_administrators_group, sender=self)
