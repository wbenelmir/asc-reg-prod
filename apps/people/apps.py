from django.apps import AppConfig


class PeopleConfig(AppConfig):
    """People, identity references, matching and contact data."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.people"
    label = "people"

    def ready(self) -> None:
        from apps.people import checks  # noqa: F401 - registers the deploy check
