"""UX-2 (M15, C-05): the approved startup-ecosystem sector list.

Adds sectors with stable codes and EN/FR/AR names; never renames, deletes or
deactivates an existing row (the existing `TECH` sector is kept). The ISIC
Rev. 5 reporting map proposed in the gate is NOT stored: it could not be
verified against the official UN source in this package (report §8).
"""

from __future__ import annotations

from django.db import migrations

SECTORS = (
    (
        "AGRITECH",
        "Agriculture and agritech",
        "Agriculture et agritech",
        "الفلاحة والتكنولوجيا الزراعية",
    ),
    ("CLEANTECH", "Energy and climate", "Énergie et climat", "الطاقة والمناخ"),
    ("EDTECH", "Education and edtech", "Éducation et edtech", "التعليم وتكنولوجيا التعليم"),
    ("FINTECH", "Finance and fintech", "Finance et fintech", "المالية والتكنولوجيا المالية"),
    ("HEALTHTECH", "Health and healthtech", "Santé et healthtech", "الصحة والتكنولوجيا الصحية"),
    (
        "AI_DATA",
        "Artificial intelligence and data",
        "Intelligence artificielle et données",
        "الذكاء الاصطناعي والبيانات",
    ),
    ("CYBERSECURITY", "Cybersecurity", "Cybersécurité", "الأمن السيبراني"),
    (
        "ECOMMERCE",
        "E-commerce and retail",
        "Commerce électronique et distribution",
        "التجارة الإلكترونية والتجزئة",
    ),
    ("LOGISTICS", "Logistics and transport", "Logistique et transport", "اللوجستيك والنقل"),
    ("MOBILITY", "Mobility", "Mobilité", "التنقل"),
    (
        "INDUSTRY",
        "Manufacturing and Industry 4.0",
        "Industrie et industrie 4.0",
        "الصناعة والصناعة 4.0",
    ),
    ("TOURISM", "Tourism and hospitality", "Tourisme et hôtellerie", "السياحة والضيافة"),
    (
        "MEDIA_CREATIVE",
        "Media and creative industries",
        "Médias et industries créatives",
        "الإعلام والصناعات الإبداعية",
    ),
    ("TELECOM", "Telecommunications", "Télécommunications", "الاتصالات"),
    ("SOFTWARE", "Software and SaaS", "Logiciels et SaaS", "البرمجيات والبرمجيات كخدمة"),
    ("PROPTECH", "Construction and real estate", "Construction et immobilier", "البناء والعقار"),
    (
        "GOVTECH",
        "Govtech and public services",
        "Govtech et services publics",
        "تكنولوجيا الحكومة والخدمات العمومية",
    ),
    ("WATER", "Water and environment", "Eau et environnement", "المياه والبيئة"),
    ("BIOTECH", "Biotechnology", "Biotechnologie", "التكنولوجيا الحيوية"),
    ("SPACE", "Space technologies", "Technologies spatiales", "تكنولوجيات الفضاء"),
    ("PUBLIC_SECTOR", "Public sector", "Secteur public", "القطاع العمومي"),
    (
        "CONSULTING",
        "Consulting and professional services",
        "Conseil et services professionnels",
        "الاستشارات والخدمات المهنية",
    ),
    ("FINANCE_INVEST", "Investment", "Investissement", "الاستثمار"),
    ("OTHER", "Other sector", "Autre secteur", "قطاع آخر"),
)


def forward(apps, schema_editor):
    Sector = apps.get_model("core", "Sector")
    for code, name, name_fr, name_ar in SECTORS:
        Sector.objects.get_or_create(
            code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
        )


def backward(apps, schema_editor):
    """UX-C2 (UX-F04, owner decision R1): a guarded fail-safe reverse that keeps
    every sector row.

    The forward uses `get_or_create`, so a sector with an approved code may
    have existed before it, possibly with owner labels, and matching codes or
    names never prove which rows it created. The sectors are additive
    reference data, so the reverse deletes nothing. This is an intentionally
    partial reversal; retire unwanted rows by a reviewed roll-forward step.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_human_challenge_use"),
        ("organizations", "0004_professionalaffiliation_operating_scope_and_more"),
    ]

    operations = [migrations.RunPython(forward, backward)]
