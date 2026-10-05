from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Append-oriented audit events and authorized search."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    label = "audit"
