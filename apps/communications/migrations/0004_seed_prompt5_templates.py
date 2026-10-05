"""Seed neutral, placeholder localized templates for the four Phase 2
Prompt 5 communication purposes (§4.1): information request,
information-response confirmation, decision/status, and account access.

Placeholder copy only -- like the Prompt 4 registration-confirmation and
delegation-claim seeds, subject to approved communications review before
production use. No legal, policy, eligibility, acceptance, or rejection
language is invented here; "decision_label"/"decision_note" are supplied
by the caller from the project's own existing, already-approved status
vocabulary (`RegistrationPublicStatus`), never authored by this migration.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

CONTENT: dict[str, dict[str, tuple[str, str, list[str]]]] = {
    "INFORMATION_REQUEST": {
        "en": (
            "Additional information needed for {{event_name}}",
            "We need additional information to continue processing your registration "
            "({{public_reference}}) for {{event_name}}. Please sign in to your "
            "participant workspace to review the request and respond.",
            ["public_reference", "event_name"],
        ),
        "fr": (
            "Informations complémentaires requises pour {{event_name}}",
            "Nous avons besoin d'informations complémentaires pour poursuivre le "
            "traitement de votre inscription ({{public_reference}}) pour "
            "{{event_name}}. Veuillez vous connecter à votre espace participant "
            "pour consulter la demande et y répondre.",
            ["public_reference", "event_name"],
        ),
        "ar": (
            "معلومات إضافية مطلوبة لـ {{event_name}}",
            "نحتاج إلى معلومات إضافية لمواصلة معالجة تسجيلك ({{public_reference}}) "
            "في {{event_name}}. يرجى تسجيل الدخول إلى مساحة المشارك الخاصة بك "
            "للاطلاع على الطلب والرد عليه.",
            ["public_reference", "event_name"],
        ),
    },
    "INFORMATION_RESPONSE_CONFIRMATION": {
        "en": (
            "Your response has been received",
            "Thank you. Your response to the information request for "
            "{{public_reference}} ({{event_name}}) has been received and is being "
            "reviewed.",
            ["public_reference", "event_name"],
        ),
        "fr": (
            "Votre réponse a été reçue",
            "Merci. Votre réponse à la demande d'informations pour "
            "{{public_reference}} ({{event_name}}) a été reçue et est en cours "
            "d'examen.",
            ["public_reference", "event_name"],
        ),
        "ar": (
            "تم استلام ردك",
            "شكرًا لك. تم استلام ردك على طلب المعلومات الخاص بـ {{public_reference}} "
            "({{event_name}}) وهو قيد المراجعة.",
            ["public_reference", "event_name"],
        ),
    },
    "DECISION_STATUS": {
        "en": (
            "An update on your registration for {{event_name}}",
            "The status of your registration ({{public_reference}}) for "
            "{{event_name}} is now: {{decision_label}}. You can view full details "
            "from your participant workspace at any time.",
            ["public_reference", "event_name", "decision_label"],
        ),
        "fr": (
            "Mise à jour concernant votre inscription à {{event_name}}",
            "Le statut de votre inscription ({{public_reference}}) pour "
            "{{event_name}} est désormais : {{decision_label}}. Vous pouvez "
            "consulter tous les détails depuis votre espace participant à tout "
            "moment.",
            ["public_reference", "event_name", "decision_label"],
        ),
        "ar": (
            "تحديث بخصوص تسجيلك في {{event_name}}",
            "حالة تسجيلك ({{public_reference}}) في {{event_name}} أصبحت الآن: "
            "{{decision_label}}. يمكنك الاطلاع على كافة التفاصيل من مساحة "
            "المشارك الخاصة بك في أي وقت.",
            ["public_reference", "event_name", "decision_label"],
        ),
    },
    "ACCOUNT_ACCESS": {
        "en": (
            "An operational account has been created for you",
            "An operational account ({{display_name}}) has been created for you on "
            "the ASC 2026 platform. Contact your event coordinator for sign-in "
            "instructions.",
            ["display_name"],
        ),
        "fr": (
            "Un compte opérationnel a été créé pour vous",
            "Un compte opérationnel ({{display_name}}) a été créé pour vous sur la "
            "plateforme ASC 2026. Contactez votre coordinateur d'événement pour les "
            "instructions de connexion.",
            ["display_name"],
        ),
        "ar": (
            "تم إنشاء حساب تشغيلي لك",
            "تم إنشاء حساب تشغيلي ({{display_name}}) لك على منصة ASC 2026. يرجى "
            "التواصل مع منسق الفعالية للحصول على تعليمات تسجيل الدخول.",
            ["display_name"],
        ),
    },
}


def seed_templates(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")

    now = timezone.now()
    for purpose_code, by_language in CONTENT.items():
        template, _ = MessageTemplate.objects.get_or_create(
            code=purpose_code,
            defaults={"channel": "EMAIL", "purpose_code": purpose_code},
        )
        for language, (subject, body, allowed_variables) in by_language.items():
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
                allowed_variables=allowed_variables,
                status="PUBLISHED",
                effective_from=now,
                content_hash=hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest(),
            )


def unseed_templates(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplate.objects.filter(code__in=CONTENT.keys()).delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0003_seed_delegation_claim_template")]

    operations = [migrations.RunPython(seed_templates, unseed_templates)]
