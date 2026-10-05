"""Bounded, allowlist-only template rendering (Phase 2 Prompt 5 §4.1).

Rendering never executes arbitrary template code -- there is no expression
evaluation, no attribute lookup, and no access to anything not explicitly
passed in `context`. Only `{{name}}` placeholders whose `name` appears in
the template version's own `allowed_variables` list are ever substituted;
every other placeholder is left untouched and every other context key is
silently ignored. This is what prevents accidental secret exposure: a
caller cannot leak a value into a rendered message merely by having it
present in a broader context dict, and a template cannot request a
variable nobody explicitly approved for it.
"""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.communications.models import (
    CommunicationChannel,
    MessageTemplate,
    MessageTemplateVersion,
    MessageTemplateVersionStatus,
)

DEFAULT_FALLBACK_LANGUAGE = "en"


def resolve_template_version(
    *, purpose_code: str, language: str, channel: str = CommunicationChannel.EMAIL
) -> MessageTemplateVersion | None:
    """Return the PUBLISHED version for `purpose_code`/`language`, falling back
    to `DEFAULT_FALLBACK_LANGUAGE` deterministically when no published version
    exists in the requested language. Returns `None` if neither exists -- the
    caller must treat this as "cannot deliver", never invent a placeholder.

    Ties (more than one PUBLISHED version in the same language) are broken by
    the most recent `effective_from`, mirroring `apps.registrations.confirmation`.
    """
    template = MessageTemplate.objects.filter(purpose_code=purpose_code, channel=channel).first()
    if template is None:
        return None
    now = timezone.now()
    effective_versions = template.versions.filter(
        status=MessageTemplateVersionStatus.PUBLISHED,
        effective_from__lte=now,
    ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=now))
    version = effective_versions.filter(language=language).order_by("-effective_from").first()
    if version is not None:
        return version
    if language == DEFAULT_FALLBACK_LANGUAGE:
        return None
    return (
        effective_versions.filter(language=DEFAULT_FALLBACK_LANGUAGE)
        .order_by("-effective_from")
        .first()
    )


def render_message(version: MessageTemplateVersion, context: dict[str, object]) -> tuple[str, str]:
    """Render `version.subject`/`version.body` against `context`, substituting
    ONLY the keys BOTH declared in `version.allowed_variables` AND present in
    `context`. An allowed variable missing from `context` is left as a literal
    `{{name}}` placeholder rather than guessed at or rendered as empty text --
    a caller building `context` incompletely gets a visibly wrong message, not
    a silently wrong one.
    """
    allowed = set(version.allowed_variables or [])
    subject, body = version.subject, version.body
    for key in allowed:
        if key not in context:
            continue
        placeholder = "{{" + key + "}}"
        value = str(context[key])
        subject = subject.replace(placeholder, value)
        body = body.replace(placeholder, value)
    return subject, body
