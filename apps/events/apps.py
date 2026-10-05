from django.apps import AppConfig
from django.db.models.signals import post_migrate

# UX-4 (M24, S-17): opening, restricting or closing registration is its own
# explicitly granted capability. No existing group receives it.
REGISTRATION_CHANNEL_MANAGER_GROUP_NAME = "Registration Channel Manager"
REGISTRATION_CHANNEL_MANAGER_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("events", "manage_registration_channels"),
    ("events", "view_eventedition"),
)


def _ensure_registration_channel_group(sender, **kwargs):
    """Idempotently ensure the channel-manager group exists (same idiom as
    `apps.invitations.apps._ensure_invitation_groups`). Only adds the listed
    permissions; never removes one granted separately."""
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME)
    for app_label, codename in REGISTRATION_CHANNEL_MANAGER_PERMISSIONS:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is not None:
            group.permissions.add(permission)


class EventsConfig(AppConfig):
    """Event editions, dates and registration windows."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.events"
    label = "events"

    def ready(self) -> None:
        post_migrate.connect(_ensure_registration_channel_group, sender=self)
