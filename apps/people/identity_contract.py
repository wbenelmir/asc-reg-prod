"""Ministry NIN lookup contract and the conservative identity comparison (IDV-1, amendment A-13).

Pure functions only: no network, no database, no settings. The HTTP adapter
(`apps.people.nin_provider`) decodes the response and hands the decoded body to
`classify_lookup_body`; the verification worker then calls
`evaluate_identity_match`. Both are tested on their own, with synthetic data.

The observed contract (addendum §3, owner clarifications in A-13):

* `GET /api/get/{nin}` returns HTTP 200 with a JSON object holding `identite`.
* `identite: null` is NOT_FOUND. Any explanatory `result` text next to it is
  ignored: nationality or anything else is never inferred from its wording.
* A found identity is an object. Only `nin`, `nom_f`, `pren_f`, `d_nais` and
  `presume` are read. Every other field (Arabic names, parents, civil-status
  events) is ignored and never retained (A13-03).
* A missing `identite`, a wrong type, invalid JSON or a missing required
  matching fact is INVALID_RESPONSE. A malformed response is never repaired
  into a success (A13-10).

Matching (A13-04, A13-08, A13-09, A13-13):

* the returned NIN must equal the requested NIN exactly, as a string;
* names are compared after NFC normalization, whitespace collapsing and Unicode
  case folding only. Accents, spelling, transliteration, token order and
  missing names are never "corrected"; any such difference is a mismatch;
* `presume` true: the official birth date is ignored entirely and the
  participant's entered date is relied on (owner rule, A13-08);
* `presume` false: the official date must be DD/MM/YYYY (the only observed
  format) and a real calendar date; it is then compared with the entered date;
* any other `presume` encoding, or an absent flag, goes to manual review.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import dataclass, field

#: The existing NIN rule (S-03, `apps.core.text_rules.normalize_nin`): exactly 18
#: ASCII digits, kept as a string so leading zeros survive. No checksum.
NIN_PATTERN = re.compile(r"[0-9]{18}")

#: The only official date format observed in the contract (A13-13). ASCII digits
#: only: `\d` would also accept other Unicode digits.
_OFFICIAL_DATE_PATTERN = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")

#: Bounds on official text, so an oversized value is refused, never truncated.
MAX_OFFICIAL_NAME_LENGTH = 200
MAX_OFFICIAL_DATE_TEXT_LENGTH = 32

#: The owner policy that allows verification without an official date (A13-08).
PRESUMED_DATE_POLICY_BASIS = "OWNER_RULE_PRESUMED_BIRTH_DATE_2026_10_01"


class LookupKind:
    """How a lookup ended. Only FOUND carries official facts."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    AUTH_ERROR = "AUTH_ERROR"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class PresumeFlag:
    TRUE = "true"
    FALSE = "false"
    MISSING = "missing"
    UNRECOGNIZED = "unrecognized"


class Comparison:
    """Per-field comparison outcomes, stored as plain codes."""

    MATCH = "match"
    NORMALIZED_MATCH = "normalized_match"
    MISMATCH = "mismatch"
    SKIPPED_PRESUMED = "skipped_presumed"
    UNRECOGNIZED_FORMAT = "unrecognized_format"
    IMPOSSIBLE_DATE = "impossible_date"
    NOT_COMPARED = "not_compared"


class MatchReason:
    """Manual-review reasons produced by the comparison (a subset of
    `apps.people.models.IdentityReasonCode`)."""

    PROVIDER_IDENTITY_MISMATCH = "PROVIDER_IDENTITY_MISMATCH"
    DATA_MISMATCH = "DATA_MISMATCH"
    AMBIGUOUS_DATE = "AMBIGUOUS_DATE"
    PRESUME_FLAG_UNRECOGNIZED = "PRESUME_FLAG_UNRECOGNIZED"


def parse_presume(raw: object, *, present: bool) -> str:
    """Explicit parsing of the `presume` flag (A13-09).

    A real boolean is taken as it is. A string counts only when it is "True" or
    "False" after trimming surrounding whitespace and folding case. Generic
    truthiness is never used: the string "False" is not true because it is not
    empty. An absent key or a JSON null is MISSING; anything else (0, 1, "yes",
    "", a list) is UNRECOGNIZED.
    """
    if not present or raw is None:
        return PresumeFlag.MISSING
    if type(raw) is bool:
        return PresumeFlag.TRUE if raw else PresumeFlag.FALSE
    if isinstance(raw, str):
        text = raw.strip().casefold()
        if text == "true":
            return PresumeFlag.TRUE
        if text == "false":
            return PresumeFlag.FALSE
    return PresumeFlag.UNRECOGNIZED


@dataclass(frozen=True)
class ParsedOfficialDate:
    outcome: str  # "parsed", Comparison.UNRECOGNIZED_FORMAT or Comparison.IMPOSSIBLE_DATE
    value: datetime.date | None = None


def parse_official_birth_date(text: str) -> ParsedOfficialDate:
    """Parse an official birth date in DD/MM/YYYY, and nothing else (A13-13).

    The day and month must have two digits and the year four, exactly as
    observed. Surrounding whitespace, other separators, two-digit years and any
    other order are refused: their meaning is not established, so the order is
    never guessed. A well-formed but impossible date (31/02/1990) is refused as
    impossible. No date is ever invented (no 1 January for a missing day).
    """
    match = _OFFICIAL_DATE_PATTERN.fullmatch(text)
    if match is None:
        return ParsedOfficialDate(Comparison.UNRECOGNIZED_FORMAT)
    day, month, year = (int(part) for part in match.groups())
    try:
        return ParsedOfficialDate("parsed", datetime.date(year, month, day))
    except ValueError:
        return ParsedOfficialDate(Comparison.IMPOSSIBLE_DATE)


def normalize_name_for_comparison(value: str) -> str:
    """The only automatic normalization (A13-04): NFC, whitespace collapsed to
    single spaces and trimmed, then Unicode default case folding.

    NFC only merges canonically equivalent encodings of the same text (a
    precomposed "é" and "e" plus a combining accent); it removes no accent.
    "Zoé" and "Zoe" stay different.
    """
    text = unicodedata.normalize("NFC", value or "")
    return " ".join(text.split()).casefold()


def compare_name(submitted: str, official: str) -> str:
    """MATCH when identical, NORMALIZED_MATCH when only case or whitespace
    differs, MISMATCH otherwise (including a missing value on either side)."""
    if not (submitted or "").strip() or not (official or "").strip():
        return Comparison.MISMATCH
    if unicodedata.normalize("NFC", submitted) == unicodedata.normalize("NFC", official):
        return Comparison.MATCH
    if normalize_name_for_comparison(submitted) == normalize_name_for_comparison(official):
        return Comparison.NORMALIZED_MATCH
    return Comparison.MISMATCH


@dataclass(frozen=True)
class OfficialIdentityFacts:
    """The facts read from a found identity, already type-checked.

    `birth_date_text` is the raw `d_nais` value when it is a string, kept only
    in memory until `evaluate_identity_match` decides whether any of it may be
    retained.
    """

    nin: str
    family_name_latin: str
    given_names_latin: str
    presume: str
    birth_date_text: str | None = None


@dataclass(frozen=True)
class LookupClassification:
    kind: str  # LookupKind.FOUND, NOT_FOUND or INVALID_RESPONSE
    facts: OfficialIdentityFacts | None = None
    detail: str = ""


def _official_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_OFFICIAL_NAME_LENGTH or not text.isprintable():
        return None
    return text


def classify_lookup_body(body: object) -> LookupClassification:
    """Classify an already JSON-decoded lookup body (see the module docstring).

    `detail` is a fixed machine code, never provider text.
    """
    invalid = LookupKind.INVALID_RESPONSE
    if not isinstance(body, dict):
        return LookupClassification(invalid, detail="body_not_object")
    if "identite" not in body:
        return LookupClassification(invalid, detail="identite_missing")
    identity = body["identite"]
    if identity is None:
        return LookupClassification(LookupKind.NOT_FOUND)
    if not isinstance(identity, dict):
        return LookupClassification(invalid, detail="identite_wrong_type")

    nin = identity.get("nin")
    if not isinstance(nin, str) or NIN_PATTERN.fullmatch(nin) is None:
        # A number would already have lost any leading zero; a string of another
        # shape is not the documented identifier. Neither is repaired.
        return LookupClassification(invalid, detail="nin_invalid")
    family_name = _official_name(identity.get("nom_f"))
    given_names = _official_name(identity.get("pren_f"))
    if family_name is None or given_names is None:
        return LookupClassification(invalid, detail="names_missing_or_invalid")

    presume = parse_presume(identity.get("presume"), present="presume" in identity)
    raw_date = identity.get("d_nais")
    birth_date_text: str | None = None
    if presume == PresumeFlag.FALSE:
        # The official date is a required matching fact only when it is used.
        if raw_date is None or raw_date == "":
            return LookupClassification(invalid, detail="birth_date_missing")
        if not isinstance(raw_date, str):
            return LookupClassification(invalid, detail="birth_date_wrong_type")
        if len(raw_date) > MAX_OFFICIAL_DATE_TEXT_LENGTH or not raw_date.isprintable():
            return LookupClassification(invalid, detail="birth_date_wrong_type")
        birth_date_text = raw_date
    # presume TRUE: d_nais is ignored entirely, whatever it holds (A13-08).
    # presume MISSING or UNRECOGNIZED: the case goes to manual review; the date
    # is not used, so it is not read either.
    return LookupClassification(
        LookupKind.FOUND,
        facts=OfficialIdentityFacts(
            nin=nin,
            family_name_latin=family_name,
            given_names_latin=given_names,
            presume=presume,
            birth_date_text=birth_date_text,
        ),
    )


# ---------------------------------------------------------------------------
# UPD-04: identity details for the staff lookup tool (display only)
# ---------------------------------------------------------------------------

#: How the official birth date reads, for display.
DATE_COMPLETE = "complete"  # DD/MM/YYYY and a real date
DATE_PARTIAL = "partial"  # some parts unknown (00, XX or missing): never completed
DATE_UNREADABLE = "unreadable"  # another format, or an impossible date
DATE_ABSENT = "absent"

_PARTIAL_DMY = re.compile(r"([0-9X]{2}|\?\?|)[/.-]([0-9X]{2}|\?\?|)[/.-]([0-9]{4})", re.IGNORECASE)
_YEAR_ONLY = re.compile(r"[0-9]{4}")


@dataclass(frozen=True)
class DiagnosticBirthDate:
    kind: str
    text: str = ""  # exactly as returned (bounded, printable), "" when absent
    value: datetime.date | None = None  # only for a complete, real date
    day: int | None = None
    month: int | None = None
    year: int | None = None


@dataclass(frozen=True)
class DiagnosticIdentity:
    """The identity details the lookup tool shows to an authorized operator
    (UPD-04). Built from a decoded body, in memory, for one response; never
    stored, logged or audited. Only the requested display fields are read:
    the Latin and Arabic names, the birth date and the presume flag. The
    returned number itself is not kept, only whether it equals the number
    looked up. A field the service did not supply (or supplied in an
    unusable form) is None."""

    nin_matches_request: bool
    family_name_latin: str | None
    given_names_latin: str | None
    family_name_arabic: str | None
    given_names_arabic: str | None
    birth_date: DiagnosticBirthDate
    presume: str


def describe_official_birth_date(raw: object) -> DiagnosticBirthDate:
    """Describe an official birth date for display, without inventing any
    part of it. A complete DD/MM/YYYY real date is `complete`. A date whose
    day or month is unknown (00, XX, ?? or empty) or a year alone is
    `partial`, with only the known parts. Anything else is `unreadable` and
    is shown exactly as received."""
    if raw is None or raw == "":
        return DiagnosticBirthDate(DATE_ABSENT)
    if not isinstance(raw, str):
        return DiagnosticBirthDate(DATE_UNREADABLE)
    text = raw.strip()
    if len(text) > MAX_OFFICIAL_DATE_TEXT_LENGTH or not text.isprintable():
        return DiagnosticBirthDate(DATE_UNREADABLE)
    parsed = parse_official_birth_date(text)
    if parsed.outcome == "parsed":
        value = parsed.value
        return DiagnosticBirthDate(
            DATE_COMPLETE, text, value, day=value.day, month=value.month, year=value.year
        )
    if _YEAR_ONLY.fullmatch(text):
        return DiagnosticBirthDate(DATE_PARTIAL, text, year=int(text))
    match = _PARTIAL_DMY.fullmatch(text)
    if match is not None:
        day_text, month_text, year_text = match.groups()
        parts = []
        for value, upper in ((day_text, 31), (month_text, 12)):
            if value.upper() in ("", "00", "XX", "??"):
                parts.append(None)  # explicitly unknown
            elif value.isdigit() and 1 <= int(value) <= upper:
                parts.append(int(value))
            else:
                return DiagnosticBirthDate(DATE_UNREADABLE, text)
        day, month = parts
        if day is None or month is None:
            return DiagnosticBirthDate(
                DATE_PARTIAL, text, day=day, month=month, year=int(year_text)
            )
    return DiagnosticBirthDate(DATE_UNREADABLE, text)


def _display_name(value: object) -> str | None:
    """A bounded, printable official name, or None."""
    return _official_name(value)


def diagnostic_identity_from_body(body: object, *, requested_nin: str) -> DiagnosticIdentity | None:
    """The display details of a lookup body whose `identite` is an object,
    else None (no identity, or not an object). Works on bodies the matching
    contract refuses too, so an operator sees which fields are missing."""
    if not isinstance(body, dict):
        return None
    identity = body.get("identite")
    if not isinstance(identity, dict):
        return None
    returned = identity.get("nin")
    return DiagnosticIdentity(
        nin_matches_request=isinstance(returned, str) and returned == requested_nin,
        family_name_latin=_display_name(identity.get("nom_f")),
        given_names_latin=_display_name(identity.get("pren_f")),
        family_name_arabic=_display_name(identity.get("nom_a")),
        given_names_arabic=_display_name(identity.get("pren_a")),
        birth_date=describe_official_birth_date(identity.get("d_nais")),
        presume=parse_presume(identity.get("presume"), present="presume" in identity),
    )


@dataclass(frozen=True)
class MatchEvaluation:
    """The outcome of comparing official facts with the submitted identity.

    `retained_*` are the only official values that may be stored (A13-03). They
    are empty when the returned NIN belongs to someone else: another person's
    names are never kept.
    """

    verified: bool
    reason_code: str
    comparison: dict = field(default_factory=dict)
    presume: str = ""
    policy_basis: str = ""
    retained_family_name_latin: str = ""
    retained_given_names_latin: str = ""
    retained_birth_date: datetime.date | None = None
    retained_birth_date_text: str = ""


def evaluate_identity_match(
    *,
    requested_nin: str,
    submitted_given_names: str,
    submitted_family_name: str,
    submitted_birth_date: datetime.date,
    facts: OfficialIdentityFacts,
) -> MatchEvaluation:
    """Decide whether a found identity verifies the submitted identity.

    Existence alone never verifies. Verification needs the exact NIN, both names
    matching (exactly, or after case and whitespace normalization only), a
    recognized `presume` flag, and, when `presume` is false, an equal official
    birth date. The submitted values are never modified here.
    """
    if facts.nin != requested_nin:
        return MatchEvaluation(
            verified=False,
            reason_code=MatchReason.PROVIDER_IDENTITY_MISMATCH,
            comparison={"nin": Comparison.MISMATCH},
        )

    comparison = {
        "nin": Comparison.MATCH,
        "family_name": compare_name(submitted_family_name, facts.family_name_latin),
        "given_names": compare_name(submitted_given_names, facts.given_names_latin),
    }
    retained_date: datetime.date | None = None
    retained_date_text = ""
    policy_basis = ""
    if facts.presume == PresumeFlag.TRUE:
        comparison["birth_date"] = Comparison.SKIPPED_PRESUMED
        policy_basis = PRESUMED_DATE_POLICY_BASIS
    elif facts.presume == PresumeFlag.FALSE:
        parsed = parse_official_birth_date(facts.birth_date_text or "")
        if parsed.outcome == "parsed":
            retained_date = parsed.value
            comparison["birth_date"] = (
                Comparison.MATCH if parsed.value == submitted_birth_date else Comparison.MISMATCH
            )
        else:
            # Kept, restricted, only because a reviewer cannot judge it otherwise.
            comparison["birth_date"] = parsed.outcome
            retained_date_text = facts.birth_date_text or ""
    else:
        comparison["birth_date"] = Comparison.NOT_COMPARED

    names_ok = all(
        comparison[name] in (Comparison.MATCH, Comparison.NORMALIZED_MATCH)
        for name in ("family_name", "given_names")
    )
    if not names_ok:
        reason = MatchReason.DATA_MISMATCH
    elif facts.presume in (PresumeFlag.MISSING, PresumeFlag.UNRECOGNIZED):
        reason = MatchReason.PRESUME_FLAG_UNRECOGNIZED
    elif comparison["birth_date"] in (Comparison.UNRECOGNIZED_FORMAT, Comparison.IMPOSSIBLE_DATE):
        reason = MatchReason.AMBIGUOUS_DATE
    elif comparison["birth_date"] == Comparison.MISMATCH:
        reason = MatchReason.DATA_MISMATCH
    else:
        reason = ""
    return MatchEvaluation(
        verified=not reason,
        reason_code=reason,
        comparison=comparison,
        presume=facts.presume,
        policy_basis=policy_basis,
        retained_family_name_latin=facts.family_name_latin,
        retained_given_names_latin=facts.given_names_latin,
        retained_birth_date=retained_date,
        retained_birth_date_text=retained_date_text,
    )
