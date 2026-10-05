"""Seed the localized registration-confirmation message template (Schema §13.1, Prompt 4).

Placeholder copy, mirroring the DRAFT status of the seeded legal notices --
subject to approved communications review before production use.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

TEMPLATE_CODE = "REGISTRATION_CONFIRMATION"

CONTENT = {
    "en": (
        "Your ASC 2026 registration is confirmed",
        "Thank you for registering for {{event_name}}. Your Registration Reference "
        "is {{public_reference}}. You can track your status at any time from your "
        "participant workspace.",
    ),
    "fr": (
        "Votre inscription à ASC 2026 est confirmée",
        "Merci de vous être inscrit(e) à {{event_name}}. Votre référence "
        "d'inscription est {{public_reference}}. Vous pouvez suivre votre statut "
        "à tout moment depuis votre espace participant.",
    ),
    "ar": (
        "تم تأكيد تسجيلك في ASC 2026",
        "شكرًا لتسجيلك في {{event_name}}. رقم مرجع تسجيلك هو {{public_reference}}. "
        "يمكنك متابعة حالتك في أي وقت من مساحة المشارك الخاصة بك.",
    ),
}


def seed_template(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")

    template, _ = MessageTemplate.objects.get_or_create(
        code=TEMPLATE_CODE,
        defaults={"channel": "EMAIL", "purpose_code": "REGISTRATION_CONFIRMATION"},
    )
    now = timezone.now()
    for language, (subject, body) in CONTENT.items():
        if MessageTemplateVersion.objects.filter(
            template=template, language=language, version_label="v1-draft"
        ).exists():
            continue
        MessageTemplateVersion.objects.create(
            template=template,
            language=language,
            version_label="v1-draft",
            subject=subject,
            body=body,
            allowed_variables=["event_name", "public_reference"],
            status="PUBLISHED",
            effective_from=now,
            content_hash=hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest(),
        )


def unseed_template(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplate.objects.filter(code=TEMPLATE_CODE).delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0001_initial")]

    operations = [migrations.RunPython(seed_template, unseed_template)]
