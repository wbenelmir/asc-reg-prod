from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    """Purpose-bound requests, private files and lifecycle."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    label = "documents"
