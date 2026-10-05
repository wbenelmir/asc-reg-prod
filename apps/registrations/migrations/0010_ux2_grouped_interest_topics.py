"""UX-2 (M20, C-06): grouped interest topics for the seeded ASC2026 edition.

Assigns a group to the five existing topics (only where no group is set) and
adds the approved topics. Existing codes and labels are never changed, and
no topic is deleted. Other editions configure their own topics.
"""

from __future__ import annotations

from django.db import migrations

EXISTING_GROUPS = {
    "FUNDING": "FUNDING_INVESTMENT",
    "MENTORSHIP": "GROWTH",
    "MARKET_ACCESS": "GROWTH",
    "TECHNOLOGY": "INNOVATION",
    "POLICY": "ECOSYSTEM_POLICY",
}

TOPICS = (
    (
        "VENTURE_CAPITAL",
        "FUNDING_INVESTMENT",
        "Venture capital",
        "Capital-risque",
        "رأس المال المخاطر",
    ),
    (
        "PUBLIC_FUNDING",
        "FUNDING_INVESTMENT",
        "Public funding and grants",
        "Financements publics et subventions",
        "التمويل العمومي والمنح",
    ),
    (
        "EXPORT_AFRICA",
        "GROWTH",
        "Export and expansion in Africa",
        "Export et expansion en Afrique",
        "التصدير والتوسع في إفريقيا",
    ),
    ("PARTNERSHIPS", "GROWTH", "Partnerships", "Partenariats", "الشراكات"),
    (
        "AI",
        "INNOVATION",
        "Artificial intelligence",
        "Intelligence artificielle",
        "الذكاء الاصطناعي",
    ),
    (
        "OPEN_INNOVATION",
        "INNOVATION",
        "Open innovation with corporates",
        "Innovation ouverte avec les grandes entreprises",
        "الابتكار المفتوح مع المؤسسات الكبرى",
    ),
    (
        "RESEARCH_TRANSFER",
        "INNOVATION",
        "Research and technology transfer",
        "Recherche et transfert de technologie",
        "البحث ونقل التكنولوجيا",
    ),
    (
        "STARTUP_LABEL",
        "ECOSYSTEM_POLICY",
        "Startup label and support programmes",
        "Label startup et programmes d'accompagnement",
        "علامة المؤسسة الناشئة وبرامج المرافقة",
    ),
    (
        "INCUBATION",
        "ECOSYSTEM_POLICY",
        "Incubation and acceleration",
        "Incubation et accélération",
        "الاحتضان والتسريع",
    ),
    ("RECRUITMENT", "TALENT", "Recruitment", "Recrutement", "التوظيف"),
    ("SKILLS", "TALENT", "Skills and training", "Compétences et formation", "المهارات والتكوين"),
    (
        "B2B_MEETINGS",
        "NETWORKING",
        "Business-to-business meetings",
        "Rencontres d'affaires B2B",
        "لقاءات الأعمال بين المؤسسات",
    ),
    (
        "INVESTOR_MEETINGS",
        "NETWORKING",
        "Meetings with investors",
        "Rencontres avec des investisseurs",
        "لقاءات مع المستثمرين",
    ),
)


def forward(apps, schema_editor):
    EventEdition = apps.get_model("events", "EventEdition")
    InterestTopic = apps.get_model("registrations", "InterestTopic")
    event = EventEdition.objects.filter(code="ASC2026").first()
    if event is None:
        return
    for code, group in EXISTING_GROUPS.items():
        InterestTopic.objects.filter(event_edition=event, code=code, group_code="").update(
            group_code=group
        )
    for code, group, label, label_fr, label_ar in TOPICS:
        InterestTopic.objects.get_or_create(
            event_edition=event,
            code=code,
            defaults={
                "group_code": group,
                "label": label,
                "label_fr": label_fr,
                "label_ar": label_ar,
            },
        )


def backward(apps, schema_editor):
    """UX-C2 (UX-F04, owner decision R1): a guarded fail-safe reverse that keeps
    every topic row.

    The forward uses `get_or_create`, so a same-event topic with an approved
    code may have existed before it, and matching codes or labels never prove
    which rows it created. Deleting would therefore risk removing an owner's
    row, or one in another event. The added topics are additive reference data
    that older code ignores or lists harmlessly, so the reverse leaves them in
    place, and the reverse of `0009` removes only the `group_code` column.
    This is an intentionally partial reversal; remove unwanted rows by a
    reviewed roll-forward step, never by this reverse.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("registrations", "0009_interesttopic_group_code"),
        ("events", "0006_eventedition_minimum_participant_age_and_more"),
    ]

    operations = [migrations.RunPython(forward, backward)]
