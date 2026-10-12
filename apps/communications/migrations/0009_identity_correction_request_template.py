"""UPD-03: the identity correction request e-mail, with the team's note.

Seeds one EMAIL template, `IDENTITY_CORRECTION_REQUEST`, in English, French
and Arabic (`v1`, PUBLISHED). It replaces the generic `INFORMATION_REQUEST`
message for identity returns only; that template stays unchanged for the
review information requests.

`{{participant_note_section}}` is either empty or the block the service
renders in the participant's submission language: a short heading ("Message
from the registration team:") followed by the note exactly as staff wrote it,
line breaks kept. The e-mail is plain text. It names no reason code, no
provider result and no identifier.

Additive: the forward creates rows only when they do not exist (re-running is
a no-op). The reverse removes the template only when no message was rendered
from it (a sent message keeps its exact version reference); otherwise it
refuses.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

CODE = "IDENTITY_CORRECTION_REQUEST"
VERSION_LABEL = "v1"
VARIABLES = ["public_reference", "event_name", "participant_note_section"]

CONTENT = {
    "en": (
        "Please correct your identity information for {{event_name}}",
        "We need you to check and correct the identity information of your registration "
        "({{public_reference}}) for {{event_name}}. Please sign in to your participant "
        "workspace to see what to correct and to send your correction."
        "{{participant_note_section}}",
    ),
    "fr": (
        "Veuillez corriger vos informations d'identité pour {{event_name}}",
        "Nous avons besoin que vous vérifiiez et corrigiez les informations d'identité de "
        "votre inscription ({{public_reference}}) à {{event_name}}. Veuillez vous connecter à "
        "votre espace participant pour voir ce qui doit être corrigé et envoyer votre "
        "correction.{{participant_note_section}}",
    ),
    "ar": (
        "يرجى تصحيح معلومات هويتك في {{event_name}}",
        "نحتاج منك التحقق من معلومات الهوية في تسجيلك ({{public_reference}}) في "
        "{{event_name}} وتصحيحها. يرجى تسجيل الدخول إلى مساحة المشارك الخاصة بك للاطلاع "
        "على ما يجب تصحيحه وإرسال تصحيحك.{{participant_note_section}}",
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
        raise MigrationRefused(f"{CODE} messages exist; the template is kept for their history.")
    template.delete()


class Migration(migrations.Migration):
    dependencies = [("communications", "0008_attendance_templates")]

    operations = [migrations.RunPython(forward, backward)]
