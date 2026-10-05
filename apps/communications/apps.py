from django.apps import AppConfig
from django.db.models.signals import post_migrate

COMMUNICATION_OPERATORS_GROUP_NAME = "Communication Operators"


def _ensure_communication_operators_group(sender, **kwargs):
    from django.contrib.auth.models import Group, Permission

    group, _ = Group.objects.get_or_create(name=COMMUNICATION_OPERATORS_GROUP_NAME)
    try:
        permission = Permission.objects.get(
            content_type__app_label="communications",
            codename="view_communicationmessage",
        )
    except Permission.DoesNotExist:
        return
    group.permissions.add(permission)


class CommunicationsConfig(AppConfig):
    """Templates, localization and message delivery."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.communications"
    label = "communications"

    def ready(self) -> None:
        post_migrate.connect(_ensure_communication_operators_group, sender=self)
