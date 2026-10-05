"""Owner-requested general wording for registration notices, in three languages.

REVIEW COPY: use only if privacy.0005 has never been applied in the target
environment. For an applied migration, publish new document versions through
a new migration instead; preserve existing content hashes and acceptances.

The wording omits unresolved factual details at the owner's request. Their
omission does not establish legal completeness or production readiness.
Controller/contact information, approved retention rules and other unresolved
institutional requirements still require a separate factual assessment.
Automated retention remains unimplemented. No readiness checks are changed.

Only this module docstring and the six notice strings were edited. The v3
label, dependencies, publication operations and reverse guards are unchanged.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

VERSION_LABEL = "v3"

PRIVACY_EN = """The ASC 2026 platform collects information needed to manage participation applications: contact details, identity information, professional information, a photograph and the supporting documents requested in the form.

This information is used to process your application, verify the details provided, communicate with you and manage the administrative follow-up of your registration. Depending on your circumstances and the services enabled, identity verification may involve checking supporting documents or verification with the competent service.

Access to data is limited to authorized personnel and technical services involved in operating the platform, according to their duties and permissions.

Required fields are identified in the form. Missing required information may prevent you from completing your application. Optional information is identified as such.

Information about accessibility needs is optional and requires explicit, separate consent. It is intended for personnel authorized to arrange the support requested. You can withdraw this consent from your personal workspace.

Protective measures are implemented to limit unauthorized access to accounts and submitted documents. Never share your sign-in codes.

Withdrawing an application ends its processing as an active application. It does not automatically erase the account, documents or records needed to follow up the application.

Requests concerning your personal data are handled in accordance with applicable provisions.
"""

PRIVACY_FR = """La plateforme ASC 2026 recueille les informations nécessaires à la gestion des demandes de participation : coordonnées, informations d’identité, situation professionnelle, photographie et pièces justificatives demandées dans le formulaire.

Ces informations servent à traiter votre demande, vérifier les éléments déclarés, communiquer avec vous et assurer le suivi administratif de votre inscription. Selon votre situation et les services activés, la vérification de votre identité peut comprendre un contrôle des pièces justificatives ou une vérification auprès du service compétent.

L’accès aux données est limité aux personnes habilitées et aux services techniques intervenant dans le fonctionnement de la plateforme, selon leurs missions et leurs autorisations.

Les champs obligatoires sont signalés dans le formulaire. Leur absence peut empêcher la finalisation de votre demande. Les informations facultatives sont identifiées comme telles.

Les informations relatives aux besoins d’accessibilité sont facultatives et font l’objet d’un consentement explicite et distinct. Elles sont destinées aux personnes habilitées à organiser le soutien demandé. Vous pouvez retirer ce consentement depuis votre espace personnel.

Des mesures de protection sont mises en œuvre pour limiter les accès non autorisés aux comptes et aux documents transmis. Ne communiquez jamais vos codes de connexion.

Le retrait d’une demande met fin à son traitement comme demande active. Il n’entraîne pas automatiquement l’effacement du compte, des documents ou des traces nécessaires au suivi du dossier.

Les demandes relatives à vos données personnelles sont traitées conformément aux dispositions applicables.
"""

PRIVACY_AR = """تجمع منصة ASC 2026 المعلومات اللازمة لإدارة طلبات المشاركة، بما فيها معلومات الاتصال والهوية والوضع المهني والصورة الشخصية والوثائق المطلوبة في الاستمارة.

تُستخدم هذه المعلومات لمعالجة طلبك والتحقق من البيانات المقدمة والتواصل معك ومتابعة تسجيلك إداريًا. وبحسب وضعك والخدمات المفعّلة، قد يشمل التحقق من الهوية فحص الوثائق أو التحقق لدى المصلحة المختصة.

يقتصر الوصول إلى البيانات على الموظفين المخوّلين والخدمات التقنية المساهمة في تشغيل المنصة، وفقًا لمهامهم وصلاحياتهم.

تُبيَّن الحقول الإلزامية في الاستمارة، وقد يمنع عدم استكمالها إنهاء طلبك. وتُحدَّد المعلومات الاختيارية بوضوح.

المعلومات المتعلقة باحتياجات دعم إمكانية الوصول اختيارية وتتطلب موافقة صريحة ومنفصلة. وهي مخصصة للموظفين المخوّلين بتنظيم الدعم المطلوب. ويمكنك سحب هذه الموافقة من مساحتك الشخصية.

تُطبَّق تدابير حماية للحد من الوصول غير المصرح به إلى الحسابات والوثائق المقدمة. لا تشارك رموز الدخول مع أي شخص.

يؤدي سحب الطلب إلى إنهاء معالجته بوصفه طلبًا نشطًا، ولا يؤدي تلقائيًا إلى حذف الحساب أو الوثائق أو السجلات اللازمة لمتابعة الملف.

تُعالَج الطلبات المتعلقة ببياناتك الشخصية وفقًا للأحكام المطبقة.
"""

TERMS_EN = """Registration on the ASC 2026 platform allows you to submit a participation application. Submitting an application does not confirm admission.

You must provide accurate information and legible documents corresponding to your identity. Required fields and documents are identified in the form. Incomplete or inconsistent information may require correction or further verification.

Your email address is used to access your personal workspace and receive communications about your application. Keep access to it and do not share your sign-in codes.

Applications are reviewed by authorized personnel. Additional information may be requested. You can check the status of your application in your personal workspace.

You can withdraw an active application from your personal workspace. A new registration remains subject to the registration channel being open and the conditions applicable to your application.

You agree not to use the platform to submit forged documents, malicious content or information concerning another person without authorization.

Personal data is handled as described in the Privacy Notice.
"""

TERMS_FR = """L’inscription sur la plateforme ASC 2026 permet de soumettre une demande de participation. Le dépôt d’une demande ne constitue pas une confirmation d’admission.

Vous devez fournir des informations exactes et des documents lisibles correspondant à votre identité. Les champs et pièces obligatoires sont indiqués dans le formulaire. Toute information incomplète ou incohérente peut nécessiter une correction ou une vérification complémentaire.

Votre adresse électronique sert à accéder à votre espace personnel et à recevoir les communications relatives à votre demande. Veillez à conserver son accès et à ne pas communiquer vos codes de connexion.

Les demandes sont examinées par les personnes habilitées. Des informations complémentaires peuvent vous être demandées. Vous pouvez consulter l’état de votre demande dans votre espace personnel.

Vous pouvez retirer une demande active depuis votre espace personnel. Une nouvelle inscription reste soumise à l’ouverture du canal d’inscription et aux conditions applicables à votre demande.

Vous vous engagez à ne pas utiliser la plateforme pour transmettre des documents falsifiés, des contenus malveillants ou des informations concernant une autre personne sans autorisation.

Les données personnelles sont traitées comme décrit dans la notice de confidentialité.
"""

TERMS_AR = """يتيح التسجيل في منصة ASC 2026 تقديم طلب مشاركة. ولا يعني إرسال الطلب تأكيد قبول المشاركة.

يجب تقديم معلومات صحيحة ووثائق واضحة مطابقة لهويتك. وتُبيَّن الحقول والوثائق الإلزامية في الاستمارة. وقد تتطلب المعلومات الناقصة أو غير المتناسقة تصحيحًا أو تحققًا إضافيًا.

يُستخدم بريدك الإلكتروني للوصول إلى مساحتك الشخصية وتلقي المراسلات المتعلقة بطلبك. احرص على الاحتفاظ بإمكانية الوصول إليه ولا تشارك رموز الدخول.

يراجع الموظفون المخوّلون الطلبات، وقد يطلبون معلومات إضافية. ويمكنك متابعة حالة طلبك من مساحتك الشخصية.

يمكنك سحب طلب نشط من مساحتك الشخصية. ويظل التسجيل من جديد مرتبطًا بفتح قناة التسجيل والشروط المطبقة على طلبك.

تلتزم بعدم استخدام المنصة لتقديم وثائق مزوّرة أو محتويات ضارة أو معلومات تخص شخصًا آخر دون إذن.

تُعالَج البيانات الشخصية وفقًا لإشعار الخصوصية.
"""

CONTENT = {
    ("PRIVACY_NOTICE", "en"): PRIVACY_EN,
    ("PRIVACY_NOTICE", "fr"): PRIVACY_FR,
    ("PRIVACY_NOTICE", "ar"): PRIVACY_AR,
    ("TERMS", "en"): TERMS_EN,
    ("TERMS", "fr"): TERMS_FR,
    ("TERMS", "ar"): TERMS_AR,
}


class MigrationRefused(RuntimeError):
    """A guarded step refused to run; the migration transaction rolls back."""


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def forward(apps, schema_editor):
    """Guarded forward, the `privacy.0004` pattern.

    Every document and language is checked before anything changes: an
    existing `v3` version is a collision whose ownership cannot be established,
    and more than one PUBLISHED version is ambiguous; either refuses. Then the
    single published predecessor, if any, is RETIRED (status and end date only)
    and the v3 version is created, both from one `now`.
    """
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    LegalDocumentVersion = apps.get_model("privacy", "LegalDocumentVersion")
    now = timezone.now()
    plan = []
    for (code, language), content in CONTENT.items():
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions = LegalDocumentVersion.objects.filter(legal_document=document, language=language)
        if versions.filter(version_label=VERSION_LABEL).exists():
            raise MigrationRefused(
                f"privacy.0005 refused: {code}/{language} already has a {VERSION_LABEL} "
                "version whose ownership cannot be established."
            )
        published = list(versions.filter(status="PUBLISHED"))
        if len(published) > 1:
            raise MigrationRefused(
                f"privacy.0005 refused: {code}/{language} has several PUBLISHED versions."
            )
        plan.append((document, language, content, published[0] if published else None))
    for document, language, content, predecessor in plan:
        if predecessor is not None:
            # Status and end date only; the content and every acceptance or
            # consent record that references it stay unchanged.
            LegalDocumentVersion.objects.filter(pk=predecessor.pk).update(
                status="RETIRED", effective_until=now
            )
        LegalDocumentVersion.objects.create(
            legal_document=document,
            language=language,
            version_label=VERSION_LABEL,
            content=content,
            content_hash=_content_hash(content),
            effective_from=now,
            status="PUBLISHED",
        )


def backward(apps, schema_editor):
    """Guarded fail-safe reverse, the `privacy.0004` pattern: an intentionally
    partial reversal that removes a v3 version only while it is exactly as
    created, PUBLISHED without an end date, referenced by no acceptance or
    consent record, the only published version of its document and language,
    and paired with exactly one RETIRED predecessor ending when it began (or no
    retired version at all). That predecessor is restored. Any failed check
    refuses the whole reverse before any change; recovery is by roll-forward."""
    LegalDocument = apps.get_model("privacy", "LegalDocument")
    LegalDocumentVersion = apps.get_model("privacy", "LegalDocumentVersion")
    AcceptanceRecord = apps.get_model("privacy", "AcceptanceRecord")
    ConsentRecord = apps.get_model("privacy", "ConsentRecord")
    plan = []
    for (code, language), content in CONTENT.items():
        document = LegalDocument.objects.filter(code=code).first()
        if document is None:
            continue
        versions = LegalDocumentVersion.objects.filter(legal_document=document, language=language)
        target = versions.filter(version_label=VERSION_LABEL).first()
        if target is None:
            continue
        where = f"{code}/{language}"
        if (
            target.content != content
            or target.content_hash != _content_hash(content)
            or target.status != "PUBLISHED"
            or target.effective_until is not None
        ):
            raise MigrationRefused(f"privacy.0005 reverse refused: {where} v3 was altered.")
        if (
            AcceptanceRecord.objects.filter(legal_document_version=target).exists()
            or ConsentRecord.objects.filter(legal_document_version=target).exists()
        ):
            raise MigrationRefused(f"privacy.0005 reverse refused: {where} v3 is referenced.")
        if versions.filter(status="PUBLISHED").exclude(pk=target.pk).exists():
            raise MigrationRefused(
                f"privacy.0005 reverse refused: {where} has another PUBLISHED version."
            )
        paired = list(versions.filter(status="RETIRED", effective_until=target.effective_from))
        if len(paired) > 1:
            raise MigrationRefused(f"privacy.0005 reverse refused: {where} predecessor ambiguous.")
        if not paired and versions.filter(status="RETIRED").exists():
            raise MigrationRefused(
                f"privacy.0005 reverse refused: {where} predecessor cannot be established."
            )
        plan.append((target.pk, paired[0].pk if paired else None))
    for target_pk, predecessor_pk in plan:
        LegalDocumentVersion.objects.filter(pk=target_pk).delete()
        if predecessor_pk is not None:
            LegalDocumentVersion.objects.filter(pk=predecessor_pk).update(
                status="PUBLISHED", effective_until=None
            )


class Migration(migrations.Migration):
    dependencies = [("privacy", "0004_ux3_consent_purposes_and_v2_notices")]

    operations = [migrations.RunPython(forward, backward)]
