from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    """Organizations, aliases and professional affiliations."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organizations"
    label = "organizations"
