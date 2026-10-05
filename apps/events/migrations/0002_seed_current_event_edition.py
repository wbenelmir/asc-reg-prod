"""Seed the single V1 EventEdition open for registration (Prompt 4).

V1 is single-event (apps.events.selectors.current_event_edition expects
exactly one EventEdition with status=REGISTRATION_OPEN).
"""

from __future__ import annotations

from django.db import migrations
from django.utils import timezone


def seed_event_edition(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    if EventEdition.objects.filter(code="ASC2026").exists():
        return
    now = timezone.now()
    EventEdition.objects.create(
        code="ASC2026",
        name="Africa Startup Conference 2026",
        timezone="Africa/Algiers",
        starts_at=now,
        ends_at=now,
        registration_opens_at=now,
        status="REGISTRATION_OPEN",
        default_language="en",
        supported_languages=["en", "fr", "ar"],
    )


def unseed_event_edition(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    EventEdition.objects.filter(code="ASC2026").delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [migrations.RunPython(seed_event_edition, unseed_event_edition)]
