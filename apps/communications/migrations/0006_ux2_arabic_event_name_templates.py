"""UX-2 (M04 follow-up, D-03, delegated decision): Arabic e-mails name the event in full.

Three seeded templates wrote "ASC 2026" literally in Arabic. A `v2-draft`
Arabic version with the full name is published for each, and the Arabic
`v1-draft` it replaces is RETIRED (status only; subject and body stay
unchanged, and every sent message keeps its exact version reference). English
and French versions are untouched. Templates that use `{{event_name}}` are
covered by the localized event name instead.

UX-C2 (UX-F04, owner decision R1, edited while the migration was unapplied on
every known persistent database): the forward refuses target collisions and
competing published versions before any change, and stamps the retired v1
with an end date equal to the v2 start. The reverse is a guarded,
intentionally partial reversal that refuses atomically when it cannot
establish exact safety. See `forward` and `backward`.
"""

from __future__ import annotations

import hashlib

from django.db import migrations
from django.utils import timezone

FULL = "المؤتمر الإفريقي للمؤسسات الناشئة ASC 2026"

ARABIC_V2 = {
    "REGISTRATION_CONFIRMATION": (
        f"تم تأكيد تسجيلك في {FULL}",
        "شكرًا لتسجيلك في {{event_name}}. رقم مرجع تسجيلك هو {{public_reference}}. "
        "يمكنك متابعة حالتك في أي وقت من مساحة المشارك الخاصة بك.",
        ["event_name", "public_reference"],
    ),
    "DELEGATION_CLAIM": (
        f"أكمل تسجيلك في {FULL}",
        f"تمت دعوتك لإكمال تسجيل في {FULL}. يرجى استخدام هذا الرابط للمتابعة: {{{{claim_url}}}}",
        ["claim_url"],
    ),
    "ACCOUNT_ACCESS": (
        "تم إنشاء حساب تشغيلي لك",
        f"تم إنشاء حساب تشغيلي ({{{{display_name}}}}) لك على منصة {FULL}. يرجى "
        "التواصل مع منسق الفعالية للحصول على تعليمات تسجيل الدخول.",
        ["display_name"],
    ),
}


LANGUAGE = "ar"
PREVIOUS_LABEL = "v1-draft"
VERSION_LABEL = "v2-draft"


class MigrationRefused(RuntimeError):
    """A guarded step refused to run; the migration transaction rolls back."""


def _content_hash(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()


def forward(apps, schema_editor):
    """Guarded forward (UX-C2, UX-F04 option R1), limited to the Arabic
    versions of the three named templates.

    Phase 1 checks every template before anything changes:

    * A `v2-draft` Arabic version that already exists is a collision whose
      ownership cannot be established, so the migration refuses.
    * Several PUBLISHED Arabic versions are ambiguous, so it refuses too.
    * A template whose single published Arabic version is not the seeded
      `v1-draft` (an owner's own version, for example) is left untouched.

    Phase 2 retires that `v1-draft` (status and end date only; the subject and
    body stay unchanged) and publishes the v2 version from one `now`. The v1
    `effective_until` is the v2 `effective_from`, the pairing the reverse uses.
    """
    MessageTemplate = apps.get_model("communications", "MessageTemplate")
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")
    now = timezone.now()
    plan = []
    for code, (subject, body, variables) in ARABIC_V2.items():
        template = MessageTemplate.objects.filter(code=code).first()
        if template is None:
            continue
        versions = MessageTemplateVersion.objects.filter(template=template, language=LANGUAGE)
        if versions.filter(version_label=VERSION_LABEL).exists():
            raise MigrationRefused(
                f"communications.0006 refused: {code}/{LANGUAGE} already has a "
                f"{VERSION_LABEL} version whose ownership cannot be established."
            )
        published = list(versions.filter(status="PUBLISHED"))
        if len(published) > 1:
            raise MigrationRefused(
                f"communications.0006 refused: {code}/{LANGUAGE} has several PUBLISHED versions."
            )
        if not published or published[0].version_label != PREVIOUS_LABEL:
            continue
        previous = published[0]
        plan.append(
            (template, previous, subject, body, list(previous.allowed_variables or variables))
        )
    for template, previous, subject, body, allowed in plan:
        MessageTemplateVersion.objects.filter(pk=previous.pk).update(
            status="RETIRED", effective_until=now
        )
        MessageTemplateVersion.objects.create(
            template=template,
            language=LANGUAGE,
            version_label=VERSION_LABEL,
            subject=subject,
            body=body,
            allowed_variables=allowed,
            status="PUBLISHED",
            effective_from=now,
            content_hash=_content_hash(subject, body),
        )


def backward(apps, schema_editor):
    """Guarded fail-safe reverse (UX-C2, UX-F04 option R1): an intentionally
    partial reversal, limited to the Arabic versions of the three templates.

    For each template, the reverse removes the migration's own v2 version and
    republishes its `v1-draft` predecessor only when every one of these holds:

    * the v2 subject, body and stored hash still equal the migration's text,
      and it is PUBLISHED with no end date;
    * no sent message references it;
    * no other Arabic version is PUBLISHED;
    * exactly one RETIRED `v1-draft` has `effective_until` equal to the v2
      `effective_from` (the pairing the guarded forward wrote).

    If any template fails a check, the whole reverse refuses BEFORE any change
    and the migration transaction rolls back: no partial deletion, no two
    published versions, and message history is never touched. English and
    French versions and other templates are never touched. Recovery from a
    refused reverse is by roll-forward.
    """
    MessageTemplateVersion = apps.get_model("communications", "MessageTemplateVersion")
    CommunicationMessage = apps.get_model("communications", "CommunicationMessage")
    plan = []
    for code, (subject, body, _variables) in ARABIC_V2.items():
        versions = MessageTemplateVersion.objects.filter(template__code=code, language=LANGUAGE)
        target = versions.filter(version_label=VERSION_LABEL).first()
        if target is None:
            continue
        where = f"{code}/{LANGUAGE}"
        if (
            target.subject != subject
            or target.body != body
            or target.content_hash != _content_hash(subject, body)
            or target.status != "PUBLISHED"
            or target.effective_until is not None
        ):
            raise MigrationRefused(f"communications.0006 reverse refused: {where} v2 was altered.")
        if CommunicationMessage.objects.filter(template_version=target).exists():
            raise MigrationRefused(
                f"communications.0006 reverse refused: {where} v2 was used by sent messages."
            )
        if versions.filter(status="PUBLISHED").exclude(pk=target.pk).exists():
            raise MigrationRefused(
                f"communications.0006 reverse refused: {where} has another PUBLISHED version."
            )
        paired = list(
            versions.filter(
                status="RETIRED",
                version_label=PREVIOUS_LABEL,
                effective_until=target.effective_from,
            )
        )
        if len(paired) != 1:
            raise MigrationRefused(
                f"communications.0006 reverse refused: {where} predecessor cannot be established."
            )
        plan.append((target.pk, paired[0].pk))
    for target_pk, predecessor_pk in plan:
        MessageTemplateVersion.objects.filter(pk=target_pk).delete()
        MessageTemplateVersion.objects.filter(pk=predecessor_pk).update(
            status="PUBLISHED", effective_until=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("communications", "0005_communicationmessage_rendered_body_encrypted_and_more")
    ]

    operations = [migrations.RunPython(forward, backward)]
