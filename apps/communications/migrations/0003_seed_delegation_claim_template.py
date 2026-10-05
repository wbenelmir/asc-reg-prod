"""Seed the localized delegated-claim message template (Phase 2 Prompt 2 V2
correction pass "make delegated-claim delivery durable and recoverable").

Placeholder copy, mirroring the DRAFT-then-PUBLISHED pattern used by
`0002_seed_confirmation_template` -- subject to approved communications
review before production use.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

TEMPLATE_CODE = "DELEGATION_CLAIM"

CONTENT = {
    "en": (
        "Complete your ASC 2026 registration",
        "You have been invited to complete an ASC 2026 registration. Please use "
        "this link to continue: {{claim_url}}",
    ),
    "fr": (
        "Terminez votre inscription à ASC 2026",
        "Vous avez été invité(e) à compléter une inscription à ASC 2026. "
        "Veuillez utiliser ce lien pour continuer : {{claim_url}}",
    ),
    "ar": (
        "أكمل تسجيلك في ASC 2026",
        "تمت دعوتك لإكمال تسجيل في ASC 2026. يرجى استخدام هذا الرابط للمتابعة: {{claim_url}}",
    ),
}


def seed_template(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")

    template, _ = MessageTemplate.objects.get_or_create(
        code=TEMPLATE_CODE,
        defaults={"channel": "EMAIL", "purpose_code": "DELEGATION_CLAIM"},
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
            allowed_variables=["claim_url"],
            status="PUBLISHED",
            effective_from=now,
            content_hash=hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest(),
        )


def unseed_template(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplate.objects.filter(code=TEMPLATE_CODE).delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0002_seed_confirmation_template")]

    operations = [migrations.RunPython(seed_template, unseed_template)]
