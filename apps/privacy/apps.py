"""Phase 2 Prompt 5 least-privilege group for privacy-request/Legal Hold
handling (mirrors `apps.accreditation.apps`'s bootstrap idiom exactly)."""

from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate

PRIVACY_ADMINISTRATORS_GROUP_NAME = "Privacy Administrators"

PRIVACY_ADMINISTRATORS_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("privacy", "view_privacyrequest"),
    ("privacy", "change_privacyrequest"),
    ("privacy", "view_legalhold"),
    ("privacy", "add_legalhold"),
    ("privacy", "change_legalhold"),
    ("privacy", "view_retentioncategory"),
)


def _ensure_privacy_administrators_group(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=PRIVACY_ADMINISTRATORS_GROUP_NAME)
    for app_label, codename in PRIVACY_ADMINISTRATORS_PERMISSIONS:
        try:
            permission = Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        except Permission.DoesNotExist:
            continue
        group.permissions.add(permission)


class PrivacyConfig(AppConfig):
    """Legal documents, acceptances, consents, retention, and Legal Hold."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.privacy"
    label = "privacy"

    def ready(self) -> None:
        post_migrate.connect(_ensure_privacy_administrators_group, sender=self)
        from apps.privacy import checks  # noqa: F401 - registers system checks
