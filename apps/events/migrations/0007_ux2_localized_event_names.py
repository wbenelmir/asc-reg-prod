"""UX-2 (M04 follow-up, D-03, delegated decision): localized names for ASC2026.

Sets the French and Arabic display names of the seeded ASC2026 edition only
when they are empty; the English `name` is not changed. The Arabic name is the
exact owner-approved string (gate S-04) followed by the edition year.
"""

from __future__ import annotations

from django.db import migrations

NAME_FR = "African Startup Conference 2026"
NAME_AR = "المؤتمر الإفريقي للمؤسسات الناشئة ASC 2026"


def forward(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    EventEdition.objects.filter(code="ASC2026", name_fr="").update(name_fr=NAME_FR)
    EventEdition.objects.filter(code="ASC2026", name_ar="").update(name_ar=NAME_AR)


def backward(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    EventEdition.objects.filter(code="ASC2026", name_fr=NAME_FR).update(name_fr="")
    EventEdition.objects.filter(code="ASC2026", name_ar=NAME_AR).update(name_ar="")


class Migration(migrations.Migration):
    dependencies = [("events", "0006_eventedition_minimum_participant_age_and_more")]

    operations = [migrations.RunPython(forward, backward)]
