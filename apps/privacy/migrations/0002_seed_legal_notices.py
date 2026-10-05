"""Seed versioned Privacy Notice and Terms in en/fr/ar, and the MARKETING consent purpose.

DRAFT PLACEHOLDER CONTENT: the exact legal text remains subject to approved
legal and data-protection governance (Schema §14) -- every version's body
opens with an explicit "pending Legal review" notice so it can never be
mistaken for approved final copy. This migration exists only so the
registration flow has real, presentable content to acknowledge/accept
during Prompt 4 development and testing.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

PRIVACY_NOTICE_EN = (
    "[DRAFT -- pending Legal review]\n\n"
    "This Privacy Notice explains how the Ministry of Knowledge Economy, "
    "Start-ups and Micro-enterprises, as Data Controller, collects and "
    "processes your personal data when you register for Africa Startup "
    "Conference 2026, in accordance with Algerian Law No. 18-07 as amended "
    "by Law No. 25-11. We collect only the information required to process "
    "your registration, verify your identity where required, and "
    "communicate with you about the event. You may exercise your rights of "
    "access, correction, objection, restriction or deletion by contacting "
    "the Privacy Officer through your participant workspace."
)
PRIVACY_NOTICE_FR = (
    "[BROUILLON -- en attente de révision juridique]\n\n"
    "Cette notice de confidentialité explique comment le Ministère de "
    "l'Économie de la Connaissance, des Start-ups et des Micro-entreprises, "
    "en tant que responsable du traitement, collecte et traite vos données "
    "personnelles lors de votre inscription à l'Africa Startup Conference "
    "2026, conformément à la loi algérienne n° 18-07 modifiée par la loi "
    "n° 25-11. Nous ne collectons que les informations nécessaires au "
    "traitement de votre inscription, à la vérification de votre identité "
    "lorsque requise, et à la communication avec vous au sujet de "
    "l'événement. Vous pouvez exercer vos droits d'accès, de rectification, "
    "d'opposition, de limitation ou de suppression en contactant le "
    "délégué à la protection des données depuis votre espace participant."
)
PRIVACY_NOTICE_AR = (
    "[مسودة -- قيد المراجعة القانونية]\n\n"
    "توضح إشعارية الخصوصية هذه كيفية قيام وزارة اقتصاد المعرفة والمؤسسات "
    "الناشئة والمصغرة، بصفتها الجهة المتحكمة في البيانات، بجمع ومعالجة "
    "بياناتك الشخصية عند تسجيلك في مؤتمر أفريقيا للشركات الناشئة 2026، "
    "وفقًا للقانون الجزائري رقم 18-07 المعدل والمتمم بالقانون رقم 25-11. "
    "نجمع فقط المعلومات اللازمة لمعالجة تسجيلك، والتحقق من هويتك عند "
    "الاقتضاء، والتواصل معك بخصوص الحدث. يمكنك ممارسة حقوقك في الوصول "
    "والتصحيح والاعتراض والتقييد أو الحذف من خلال التواصل مع مسؤول حماية "
    "البيانات عبر مساحة المشارك الخاصة بك."
)

TERMS_EN = (
    "[DRAFT -- pending Legal review]\n\n"
    "By registering, you confirm that the information you provide is "
    "accurate and that you understand your registration status does not "
    "guarantee a specific role, badge type or access level. Assignments "
    "are made separately by authorized organizers after registration. You "
    "may withdraw your registration at any time from your participant "
    "workspace."
)
TERMS_FR = (
    "[BROUILLON -- en attente de révision juridique]\n\n"
    "En vous inscrivant, vous confirmez que les informations fournies sont "
    "exactes et que vous comprenez que le statut de votre inscription ne "
    "garantit ni un rôle, ni un type de badge, ni un niveau d'accès "
    "spécifique. Les attributions sont effectuées séparément par les "
    "organisateurs autorisés après l'inscription. Vous pouvez retirer "
    "votre inscription à tout moment depuis votre espace participant."
)
TERMS_AR = (
    "[مسودة -- قيد المراجعة القانونية]\n\n"
    "بتسجيلك، فإنك تؤكد أن المعلومات التي قدمتها دقيقة وأنك تدرك أن حالة "
    "تسجيلك لا تضمن دورًا أو نوع شارة أو مستوى وصول محددًا. تتم عمليات "
    "الإسناد بشكل منفصل من قبل المنظمين المخولين بعد التسجيل. يمكنك سحب "
    "تسجيلك في أي وقت من مساحة المشارك الخاصة بك."
)


def seed_legal_notices(apps, schema_editor):
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    LegalDocumentVersion = apps.get_model("privacy", "LegalDocumentVersion")
    ConsentPurpose = apps.get_model("privacy", "ConsentPurpose")

    now = timezone.now()
    content_by_code_and_language = {
        ("PRIVACY_NOTICE", "en"): PRIVACY_NOTICE_EN,
        ("PRIVACY_NOTICE", "fr"): PRIVACY_NOTICE_FR,
        ("PRIVACY_NOTICE", "ar"): PRIVACY_NOTICE_AR,
        ("TERMS", "en"): TERMS_EN,
        ("TERMS", "fr"): TERMS_FR,
        ("TERMS", "ar"): TERMS_AR,
    }
    document_types = {"PRIVACY_NOTICE": "PRIVACY_NOTICE", "TERMS": "TERMS"}

    for code, document_type in document_types.items():
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": document_type}
        )
        for language in ("en", "fr", "ar"):
            content = content_by_code_and_language[(code, language)]
            if LegalDocumentVersion.objects.filter(
                legal_document=document, language=language, version_label="v1-draft"
            ).exists():
                continue
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language=language,
                version_label="v1-draft",
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                effective_from=now,
                status="PUBLISHED",
            )

    ConsentPurpose.objects.get_or_create(
        code="MARKETING",
        defaults={
            "name": "Future edition communications",
            "name_fr": "Communications sur les prochaines éditions",
            "name_ar": "التواصل بخصوص الدورات القادمة",
        },
    )


def unseed_legal_notices(apps, schema_editor):
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    ConsentPurpose = apps.get_model("privacy", "ConsentPurpose")
    LegalDocument.objects.filter(code__in=["PRIVACY_NOTICE", "TERMS"]).delete()
    ConsentPurpose.objects.filter(code="MARKETING").delete()


class Migration(migrations.Migration):
    dependencies = [("privacy", "0001_initial")]

    operations = [migrations.RunPython(seed_legal_notices, unseed_legal_notices)]
