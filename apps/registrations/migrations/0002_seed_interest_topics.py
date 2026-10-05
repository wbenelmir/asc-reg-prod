"""Seed a small set of event-configurable interest topics (Schema §5.7, Prompt 4)."""

from __future__ import annotations

from django.db import migrations

TOPICS = [
    ("FUNDING", "Access to funding", "Accès au financement", "الوصول إلى التمويل"),
    ("MENTORSHIP", "Mentorship and coaching", "Mentorat et accompagnement", "الإرشاد والتوجيه"),
    ("MARKET_ACCESS", "Market access", "Accès au marché", "الوصول إلى السوق"),
    (
        "TECHNOLOGY",
        "Technology and innovation",
        "Technologie et innovation",
        "التكنولوجيا والابتكار",
    ),
    ("POLICY", "Policy and regulation", "Politique et réglementation", "السياسات والتنظيم"),
]


def seed_interest_topics(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    InterestTopic = apps.get_model("registrations", "InterestTopic")
    event = EventEdition.objects.filter(code="ASC2026").first()
    if event is None:
        return
    for code, label_en, label_fr, label_ar in TOPICS:
        InterestTopic.objects.get_or_create(
            event_edition=event,
            code=code,
            defaults={"label": label_en, "label_fr": label_fr, "label_ar": label_ar},
        )


def unseed_interest_topics(apps, schema_editor):
    InterestTopic = apps.get_model("registrations", "InterestTopic")
    InterestTopic.objects.filter(code__in=[code for code, *_ in TOPICS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("registrations", "0001_initial"),
        ("events", "0002_seed_current_event_edition"),
    ]

    operations = [migrations.RunPython(seed_interest_topics, unseed_interest_topics)]
