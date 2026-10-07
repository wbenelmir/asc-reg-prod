"""Attendance entitlement messages (`apps.accreditation.attendance`).

Seeds two EMAIL templates in English, French and Arabic (`v1`, PUBLISHED):

* `APPROVAL_ATTENDANCE` -- the approval decision, with the authorized days.
  It replaces the generic `DECISION_STATUS` message for an approval (that
  template stays in use for the other decisions).
* `ATTENDANCE_CHANGE` -- an earlier approval classified, or the days of an
  approval changed later. One message per new entitlement.

`{{attendance_statement}}` is the sentence the participant also reads in the
workspace, rendered by the service in the registration's language with the
real dates, for example "Your participation is approved for Sunday 6
December 2026 and Monday 7 December 2026. This approval does not include
the opening day, Saturday 5 December 2026." The templates name no reason, no
identifier and no internal note. Approval never says that an entry pass is
available: pass generation and activation stay separate steps.

The reverse removes a template only when no message was rendered from it (a
sent message keeps its exact version reference); otherwise it refuses.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

VERSION_LABEL = "v1"
VARIABLES = ["public_reference", "event_name", "attendance_statement"]

CONTENT = {
    "APPROVAL_ATTENDANCE": {
        "en": (
            "Your participation in {{event_name}} is approved",
            "Your registration ({{public_reference}}) for {{event_name}} is approved. "
            "{{attendance_statement}} Your participant workspace shows the same days. Approval "
            "does not mean that your entry pass is already available: it is prepared "
            "separately, and your workspace shows it when it is ready.",
        ),
        "fr": (
            "Votre participation à {{event_name}} est approuvée",
            "Votre inscription ({{public_reference}}) à {{event_name}} est approuvée. "
            "{{attendance_statement}} Votre espace participant indique les mêmes jours. "
            "L'approbation ne signifie pas que votre titre d'accès est déjà disponible : il est "
            "préparé séparément, et votre espace participant l'affiche dès qu'il est prêt.",
        ),
        "ar": (
            "تمت الموافقة على مشاركتك في {{event_name}}",
            "تمت الموافقة على تسجيلك ({{public_reference}}) في {{event_name}}. "
            "{{attendance_statement}} تعرض مساحة المشارك الخاصة بك الأيام نفسها. لا تعني "
            "الموافقة أن تصريح الدخول الخاص بك متاح بالفعل: يتم إعداده بشكل منفصل، وتعرضه "
            "مساحة المشارك عندما يصبح جاهزًا.",
        ),
    },
    "ATTENDANCE_CHANGE": {
        "en": (
            "Your attendance days for {{event_name}}",
            "The conference days covered by your approved registration ({{public_reference}}) "
            "for {{event_name}} have been confirmed or updated. {{attendance_statement}} This "
            "replaces any earlier information about your attendance days. Your participant "
            "workspace and your entry pass show the same days.",
        ),
        "fr": (
            "Vos jours de participation à {{event_name}}",
            "Les jours de conférence couverts par votre inscription approuvée "
            "({{public_reference}}) à {{event_name}} ont été confirmés ou mis à jour. "
            "{{attendance_statement}} Ceci remplace toute information antérieure sur vos jours "
            "de participation. Votre espace participant et votre titre d'accès indiquent les "
            "mêmes jours.",
        ),
        "ar": (
            "أيام حضورك في {{event_name}}",
            "تم تأكيد أو تحديث أيام المؤتمر التي يشملها تسجيلك المعتمد ({{public_reference}}) "
            "في {{event_name}}. {{attendance_statement}} يحل هذا محل أي معلومات سابقة عن أيام "
            "حضورك. تعرض مساحة المشارك الخاصة بك وتصريح الدخول الأيام نفسها.",
        ),
    },
}


class MigrationRefused(RuntimeError):
    """A guarded reverse refused to run; the migration transaction rolls back."""


def forward(apps, schema_editor):
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")
    now = timezone.now()
    for code, languages in CONTENT.items():
        template, _created = MessageTemplate.objects.get_or_create(
            code=code, defaults={"channel": "EMAIL", "purpose_code": code}
        )
        for language, (subject, body) in languages.items():
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
    for code in CONTENT:
        template = MessageTemplate.objects.filter(code=code).first()
        if template is None:
            continue
        if CommunicationMessage.objects.filter(template_version__template=template).exists():
            raise MigrationRefused(
                f"{code} messages exist; the template is kept for their history."
            )
        template.delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0007_idv_q2_identity_rejection_template")]

    operations = [migrations.RunPython(forward, backward)]
