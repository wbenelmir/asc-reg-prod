"""Owner decision IDV-Q2 (2026-10-02): the identity-rejection message.

Seeds one EMAIL template, `IDENTITY_REJECTION`, in English, French and Arabic
(`v1-draft`, PUBLISHED). It tells the participant that the registration was
rejected because the identity could not be verified, and how to register
again from the same account. It names no reason, provider result, identifier,
document or internal note; its only variables are the public reference and the
event name.

The reverse removes the template only when no message was rendered from it
(a sent message keeps its exact version reference); otherwise it refuses.

IDV-Q-C1 (2026-10-02) corrected the copy IN PLACE. This migration had not
been applied to any persistent database (it is unapplied on the development
database and nothing else exists), the precedent of UX-C2. The first text
told every participant to "choose to register again while registration is
open", which is not possible for an invitation registration whose invitation
can no longer be used. The copy now points to the workspace, which shows the
next step for the registration's own origin, and says that an invitation may
need to be renewed by the organization that sent it.

Status (owner decision COMM-IDV-01, 2026-10-02): the exact English, French and
Arabic wording below is APPROVED for real delivery, subject to the normal
staging configuration, delivery testing and release gates. Approving the
wording is not evidence that delivery works: real delivery stays unproven until
the staging UAT records it (docs/testing/phase_04_staging_uat_handoff.md). The
version label `v1-draft` is kept unchanged on purpose: it is the label the
earlier seeds share, it is not a status, and the copy and its content hash are
unchanged, so a message already rendered from this version keeps its exact
reference. After any database has applied this migration, a wording change
should be published as a new version, not edited in place.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

CODE = "IDENTITY_REJECTION"
VERSION_LABEL = "v1-draft"
VARIABLES = ["public_reference", "event_name"]

CONTENT = {
    "en": (
        "Your registration for {{event_name}} was rejected",
        "We could not verify your identity, so your registration ({{public_reference}}) for "
        "{{event_name}} was rejected. You can register again from the same account. Sign in "
        "to your participant workspace to see the next step for this registration. If you "
        "registered through an invitation, you may need a new invitation from the "
        "organization that sent it. Enter your names, date of birth and identity document "
        "details exactly as they appear on your official document.",
    ),
    "fr": (
        "Votre inscription à {{event_name}} a été rejetée",
        "Nous n'avons pas pu vérifier votre identité ; votre inscription "
        "({{public_reference}}) à {{event_name}} a donc été rejetée. Vous pouvez vous "
        "inscrire à nouveau depuis le même compte. Connectez-vous à votre espace participant "
        "pour voir l'étape suivante pour cette inscription. Si vous vous êtes inscrit(e) au "
        "moyen d'une invitation, une nouvelle invitation de l'organisation qui vous l'a "
        "envoyée peut être nécessaire. Saisissez vos noms, votre date de naissance et les "
        "informations de votre pièce d'identité exactement comme sur votre document officiel.",
    ),
    "ar": (
        "تم رفض تسجيلك في {{event_name}}",
        "لم نتمكن من التحقق من هويتك، لذلك تم رفض تسجيلك ({{public_reference}}) في "
        "{{event_name}}. يمكنك التسجيل من جديد من الحساب نفسه. سجّل الدخول إلى مساحة المشارك "
        "الخاصة بك لمعرفة الخطوة التالية لهذا التسجيل. إذا كنت قد سجّلت عن طريق دعوة، فقد "
        "تحتاج إلى دعوة جديدة من الجهة التي أرسلتها إليك. أدخل أسماءك وتاريخ ميلادك وبيانات "
        "وثيقة هويتك تمامًا كما تظهر في وثيقتك الرسمية.",
    ),
}


class MigrationRefused(RuntimeError):
    """A guarded reverse refused to run; the migration transaction rolls back."""


def forward(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")
    now = timezone.now()
    template, _created = MessageTemplate.objects.get_or_create(
        code=CODE, defaults={"channel": "EMAIL", "purpose_code": CODE}
    )
    for language, (subject, body) in CONTENT.items():
        if MessageTemplateVersion.objects.filter(
            template=template, language=language, version_label=VERSION_LABEL
        ).exists():
            continue
        MessageTemplateVersion.objects.create(
            template=template,
            language=language,
            version_label=VERSION_LABEL,
            subject=subject,
            body=body,
            allowed_variables=VARIABLES,
            status="PUBLISHED",
            effective_from=now,
            content_hash=hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest(),
        )


def backward(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    CommunicationMessage = apps.get_model("communications", "CommunicationMessage")
    template = MessageTemplate.objects.filter(code=CODE).first()
    if template is None:
        return
    if CommunicationMessage.objects.filter(template_version__template=template).exists():
        raise MigrationRefused(
            "IDENTITY_REJECTION messages exist; the template is kept for their history."
        )
    template.delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0006_ux2_arabic_event_name_templates")]

    operations = [migrations.RunPython(forward, backward)]
