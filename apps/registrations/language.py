"""The language a participant registered in (UPD-03).

Staff who write to a participant (the identity correction note, for example)
need the language of the participant's own submission, independently of the
language of the staff interface. `submission_language_for` answers it with
its basis, so the screen can say how sure the answer is:

* `RECORDED` -- stored at the final submission
  (`Registration.submission_language`, recorded since UPD-03);
* `ACCEPTED_NOTICE` -- an older registration: the language of the privacy
  notice version the participant accepted with the initial submission, read
  from the immutable submission snapshot. The notices step shows the notice
  in the interface language, so this is the language the participant read
  and submitted in;
* `UNKNOWN` -- neither is available. Nothing is inferred from nationality,
  country or the staff interface; the caller must ask staff to choose the
  notification language explicitly (`explicit_fallback_language` proposes
  the registration's preferred language, shown as a proposal, never as the
  submission language).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils.translation import get_language

RECORDED = "RECORDED"
ACCEPTED_NOTICE = "ACCEPTED_NOTICE"
UNKNOWN = "UNKNOWN"
#: The basis stored on a notification when staff chose the language because
#: the submission language was unknown.
STAFF_SELECTED = "STAFF_SELECTED"


def supported_language_codes() -> tuple[str, ...]:
    return tuple(code for code, _label in settings.LANGUAGES)


def clean_language(value) -> str:
    """A supported language code, or "" (never guessed)."""
    code = str(value or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in supported_language_codes() else ""


def normalize_language(value) -> str:
    """The supported language for `value`; the active language when `value`
    is empty or unsupported, and the default language as the last resort."""
    return clean_language(value) or clean_language(get_language()) or settings.LANGUAGE_CODE[:2]


@dataclass(frozen=True)
class SubmissionLanguage:
    code: str  # "" when unknown
    basis: str  # RECORDED, ACCEPTED_NOTICE or UNKNOWN

    @property
    def known(self) -> bool:
        return bool(self.code)


def submission_language_for(registration) -> SubmissionLanguage:
    """The registration's submission language and its basis (module docstring).

    Reads one submission row at most; never decrypts anything."""
    recorded = clean_language(getattr(registration, "submission_language", ""))
    if recorded:
        return SubmissionLanguage(recorded, RECORDED)
    from apps.registrations.models import RegistrationSubmission, RegistrationSubmissionKind

    snapshot = (
        RegistrationSubmission.objects.filter(
            registration_id=registration.pk,
            submission_kind=RegistrationSubmissionKind.INITIAL,
        )
        .values_list("snapshot_json", flat=True)
        .first()
    )
    accepted = (
        ((snapshot or {}).get("legal_acceptance") or {}).get("privacy_notice") or {}
        if isinstance(snapshot, dict)
        else {}
    )
    derived = clean_language(accepted.get("language") if isinstance(accepted, dict) else "")
    if derived:
        return SubmissionLanguage(derived, ACCEPTED_NOTICE)
    return SubmissionLanguage("", UNKNOWN)


def language_display(code: str) -> str:
    """ "French (FR)": the language name in the active language, and its code."""
    from apps.core.display_labels import code_label

    code = clean_language(code)
    if not code:
        return ""
    return f"{code_label('language', code)} ({code.upper()})"


def explicit_fallback_language(registration) -> str:
    """The language PROPOSED to staff when the submission language is
    unknown: the registration's preferred language (the language its other
    messages already use), else the default language."""
    return (
        clean_language(getattr(registration, "preferred_language", ""))
        or (settings.LANGUAGE_CODE[:2])
    )
