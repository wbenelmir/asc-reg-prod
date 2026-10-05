from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Shared primitives, public IDs, configuration, health checks and common utilities."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        from apps.core import checks  # noqa: F401 - registers system checks
