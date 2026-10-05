"""Server-side input rules for UX-2 (gate §7: L-NAME, L-TEXT, NIN, URLs).

Pure functions, standard library only. Each `normalize_*` returns the value
to store or raises `TextRuleError` with a stable `code`. Forms map the code
to a localized message; services call the same functions, so a caller that
bypasses a form meets the same rule. None of them transliterates, guesses or
rewrites what a person typed beyond the documented normalization.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .normalization import normalize_digit_code


class TextRuleError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_APOSTROPHES = {"’": "'", "ʼ": "'"}
_NAME_PUNCTUATION = frozenset(" -'")
_TEXT_PUNCTUATION = frozenset(" .,&/()+#:;'-\"")


def _is_latin_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L") and unicodedata.name(char, "").startswith(
        "LATIN "
    )


def _prepare(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "")
    for source, target in _APOSTROPHES.items():
        text = text.replace(source, target)
    return " ".join(text.split())


def normalize_latin_name(value: str, *, max_length: int = 100) -> str:
    """L-NAME: Unicode Latin letters with their combining marks, and single
    inner spaces, hyphens and apostrophes. Not ASCII-only: "Zoé", "Nuñez",
    "Ǧamal" and "O'Neil" pass; Arabic, Cyrillic, digits and emoji fail."""
    text = _prepare(value)
    if not text:
        raise TextRuleError("required")
    if len(text) > max_length:
        raise TextRuleError("too_long")
    previous = ""
    letters = 0
    for index, char in enumerate(text):
        if _is_latin_letter(char):
            letters += 1
        elif unicodedata.category(char) == "Mn" and previous and _is_latin_letter(previous):
            pass
        elif (
            unicodedata.category(char) == "Mn"
            and previous
            and unicodedata.category(previous) == "Mn"
        ):
            pass
        elif char in _NAME_PUNCTUATION:
            if index == 0 or index == len(text) - 1 or previous in _NAME_PUNCTUATION:
                raise TextRuleError("not_latin")
        else:
            raise TextRuleError("not_latin")
        previous = char
    if not letters:
        raise TextRuleError("not_latin")
    return text


def normalize_latin_text(value: str, *, max_length: int) -> str:
    """L-TEXT: the L-NAME letters plus ASCII digits and `. , & / ( ) + # : ; ' - "`.
    At least one Latin letter. Used for organization name, job title and
    department."""
    text = _prepare(value)
    if not text:
        raise TextRuleError("required")
    if len(text) > max_length:
        raise TextRuleError("too_long")
    letters = 0
    previous = ""
    for char in text:
        if _is_latin_letter(char):
            letters += 1
        elif (
            unicodedata.category(char) == "Mn"
            and previous
            and (_is_latin_letter(previous) or unicodedata.category(previous) == "Mn")
        ):
            pass
        elif char.isascii() and char.isdigit():
            pass
        elif char in _TEXT_PUNCTUATION:
            pass
        else:
            raise TextRuleError("not_latin")
        previous = char
    if not letters:
        raise TextRuleError("not_latin")
    return text


_NIN_PATTERN = re.compile(r"[0-9]{18}")


def normalize_nin(value: str) -> str:
    """S-03: drop spaces, no-break and thin spaces and hyphens, read
    Arabic-Indic digits as ASCII, then require exactly 18 digits. Kept as a
    string, so leading zeros survive; no checksum is invented."""
    code = normalize_digit_code(value or "")
    if not _NIN_PATTERN.fullmatch(code):
        raise TextRuleError("nin_format")
    return code


URL_MAX_LENGTH = 300
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def normalize_optional_url(value: str) -> str:
    """S-11: "" stays "". Otherwise trim, add `https://` when no scheme is
    given, accept only http and https, lowercase the scheme and host, encode an
    international host with IDNA, keep path and query unchanged, and refuse
    credentials, ports other than 80 and 443, and anything over 300
    characters. The URL is never fetched."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > URL_MAX_LENGTH:
        raise TextRuleError("url_too_long")
    if "://" not in text:
        if ":" in text.split("/", 1)[0] and not re.match(r"^[^:/]+:\d+(/|$)", text):
            raise TextRuleError("url_scheme")  # javascript:, data:, mailto:, ...
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise TextRuleError("url_invalid") from None
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise TextRuleError("url_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise TextRuleError("url_credentials")
    try:
        port = parts.port
    except ValueError:
        raise TextRuleError("url_invalid") from None
    if port not in (None, 80, 443):
        raise TextRuleError("url_port")
    host = (parts.hostname or "").rstrip(".")
    if not host or " " in host:
        raise TextRuleError("url_invalid")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise TextRuleError("url_invalid") from None
    labels = ascii_host.split(".")
    if len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        raise TextRuleError("url_invalid")
    netloc = ascii_host if port is None else f"{ascii_host}:{port}"
    result = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    if len(result) > URL_MAX_LENGTH:
        raise TextRuleError("url_too_long")
    return result
