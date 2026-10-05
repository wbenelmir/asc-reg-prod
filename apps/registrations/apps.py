from django.apps import AppConfig
from django.db.models.signals import post_migrate

# UX-3 (M21, D-10): reading accommodation requests is its own explicitly
# granted capability. No existing group, including registration intake and
# review, receives it.
ACCOMMODATION_SUPPORT_GROUP_NAME = "Accommodation Support Coordinator"
ACCOMMODATION_SUPPORT_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("registrations", "coordinate_accommodation_support"),
)


def _ensure_accommodation_support_group(sender, **kwargs):
    """Idempotent, additive group bootstrap (same idiom as the other apps)."""
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=ACCOMMODATION_SUPPORT_GROUP_NAME)
    for app_label, codename in ACCOMMODATION_SUPPORT_PERMISSIONS:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is not None:
            group.permissions.add(permission)


class RegistrationsConfig(AppConfig):
    """Registration profile, workflow, requests and qualification."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.registrations"
    label = "registrations"

    def ready(self) -> None:
        post_migrate.connect(_ensure_accommodation_support_group, sender=self)
