"""Legal-document version read selectors (Schema §14.1, Prompt 4 final closure pass §7).

A version is "effective" only when it is PUBLISHED, in the requested
language, and the given instant falls inside its
`[effective_from, effective_until)` window. Never falls back silently to a
different language here -- callers that want an English fallback (e.g. the
registration wizard, the confirmation email) ask for it explicitly and are
responsible for recording the LANGUAGE OF THE VERSION THEY ACTUALLY USED,
never the participant's ambient preference (Prompt 4 final closure pass §7:
"Do not silently accept an English version while recording French or
Arabic").
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from apps.privacy.models import (
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
    LegalHold,
)


def effective_published_version(
    document_code: str, language: str, *, at: datetime | None = None
) -> LegalDocumentVersion | None:
    """Return the single PUBLISHED, effective version of `document_code` in `language`.

    Returns `None` when no such version exists -- callers decide whether
    that means "fall back to English" or "fail the request"; this selector
    never guesses.
    """
    moment = at or timezone.now()
    document = LegalDocument.objects.filter(code=document_code).first()
    if document is None:
        return None
    return (
        document.versions.filter(
            language=language,
            status=LegalDocumentVersionStatus.PUBLISHED,
            effective_from__lte=moment,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=moment))
        .order_by("-effective_from")
        .first()
    )


def effective_published_version_with_fallback(
    document_code: str, language: str, *, at: datetime | None = None, fallback_language: str = "en"
) -> LegalDocumentVersion | None:
    """Same as `effective_published_version`, falling back to `fallback_language`.

    The caller MUST read `.language` off the returned version -- it may
    differ from the requested `language` -- and record THAT value as the
    accepted/rendered language, never the originally requested one.
    """
    version = effective_published_version(document_code, language, at=at)
    if version is not None:
        return version
    if language == fallback_language:
        return None
    return effective_published_version(document_code, fallback_language, at=at)


def is_under_legal_hold(registration) -> bool:
    """True if `registration` currently has an ACTIVE (unreleased) Legal
    Hold (Phase 2 Prompt 5 §4.6). Any future retention/deletion job MUST
    consult this before treating a record as eligible for routine deletion
    -- no such job exists yet in this prompt's scope."""
    return LegalHold.objects.filter(registration=registration, released_at__isnull=True).exists()
