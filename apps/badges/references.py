"""Participant fallback reference: Crockford Base32 with a check symbol.

Phase 3 Prompt 2. The fallback reference is a **locator**, never an
authenticator and never an admission decision. An operator who types it
resolves a credential series; the full server-side verification decision
still runs afterwards, and a successful lookup by itself admits nobody and
writes no entry event.

Crockford Base32 is used deliberately rather than an invented encoding:
its payload alphabet excludes `I`, `L`, `O`, and `U`, which removes the
`1`/`I`/`L` and `0`/`O` transcription errors that matter when a tired
operator retypes a reference at a gate, and it specifies a documented
mod-37 check symbol so a single mistyped character is caught before any
database lookup happens.

Exact sizes (kept accurate here because the format documentation must not
drift from the implementation):

* payload characters:     8   (40 bits, from the 32-symbol payload alphabet)
* check characters:       1   (from the 37-symbol check alphabet)
* stored/normalized form: 9   characters (payload + check, uppercase)
* displayed form:        15   characters, `ASC-XXXX-XXXX-C`
                              (3 prefix + 3 hyphens + 8 payload + 1 check)
* accepted input:        any casing, with or without the `ASC` prefix,
                         hyphens, and spaces; normalized to the 9-character
                         form before validation.

Uniqueness is guaranteed by the database constraint on
`PassCredentialSeries.fallback_reference` plus a bounded retry in the
allocating service -- not by an assumption about collision probability.
With 40 bits the chance of a collision is small but finite, and the code
treats it as an ordinary, handled event.
"""

from __future__ import annotations

import secrets

#: Crockford Base32 payload alphabet: 32 symbols, no I, L, O, or U.
PAYLOAD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Crockford check-symbol alphabet: the 32 payload symbols plus 5 more,
#: giving the 37 values needed for the mod-37 check.
CHECK_ALPHABET = PAYLOAD_ALPHABET + "*~$=U"

PAYLOAD_CHARACTERS = 8
CHECK_CHARACTERS = 1
NORMALIZED_LENGTH = PAYLOAD_CHARACTERS + CHECK_CHARACTERS
DISPLAY_PREFIX = "ASC"
DISPLAY_LENGTH = 15
PAYLOAD_BITS = PAYLOAD_CHARACTERS * 5
_PAYLOAD_MAX_EXCLUSIVE = 1 << PAYLOAD_BITS
_CHECK_MODULUS = 37

#: Input aliasing defined by Crockford: these are typing mistakes, not
#: distinct values. Applied to the PAYLOAD portion only -- `U` is not a
#: payload symbol but IS a legitimate check symbol, so aliasing it in the
#: check position would corrupt a valid reference.
_PAYLOAD_ALIASES = {
    "I": "1",
    "L": "1",
    "O": "0",
}

_IGNORED_INPUT_CHARACTERS = frozenset("- \t ")


class InvalidFallbackReferenceError(ValueError):
    """Raised when a supplied reference is malformed or fails its check symbol.

    Carries no detail about which credential, if any, the reference might
    have addressed -- a malformed reference is rejected before any lookup.
    """


def _encode_payload(value: int) -> str:
    """Encode a 40-bit integer as exactly 8 Crockford Base32 characters."""
    if not 0 <= value < _PAYLOAD_MAX_EXCLUSIVE:
        raise ValueError("Fallback reference payload is out of range.")
    characters = []
    for shift in range(PAYLOAD_CHARACTERS - 1, -1, -1):
        characters.append(PAYLOAD_ALPHABET[(value >> (shift * 5)) & 0x1F])
    return "".join(characters)


def _decode_payload(payload: str) -> int:
    """Decode 8 Crockford Base32 characters back to a 40-bit integer."""
    value = 0
    for character in payload:
        index = PAYLOAD_ALPHABET.find(character)
        if index < 0:
            raise InvalidFallbackReferenceError("Fallback reference contains an invalid character.")
        value = (value << 5) | index
    return value


def _check_symbol(payload_value: int) -> str:
    """The Crockford mod-37 check symbol for a decoded payload value."""
    return CHECK_ALPHABET[payload_value % _CHECK_MODULUS]


def generate_fallback_reference() -> str:
    """Return a fresh, random 9-character normalized fallback reference.

    Drawn from `secrets` (CSPRNG). The 40-bit width is a transcription and
    casual-enumeration control and is NOT secret. Security rests on
    exact-match-only lookup, rate limiting, auditing, operator permission,
    and the full server-side verification decision.
    """
    payload_value = secrets.randbelow(_PAYLOAD_MAX_EXCLUSIVE)
    payload = _encode_payload(payload_value)
    return payload + _check_symbol(payload_value)


def normalize_fallback_reference(raw: object) -> str:
    """Normalize operator input to the stored 9-character form.

    Accepts any casing, an optional `ASC` prefix, and any arrangement of
    hyphens and spaces. Applies Crockford input aliasing to the payload
    portion only. Raises `InvalidFallbackReferenceError` for anything that
    cannot be a reference -- including the wrong length -- so a malformed
    value never reaches the database.
    """
    if not isinstance(raw, str):
        raise InvalidFallbackReferenceError("Fallback reference must be text.")
    if len(raw) > 64:
        raise InvalidFallbackReferenceError("Fallback reference is too long.")

    compact = "".join(
        character for character in raw.strip().upper() if character not in _IGNORED_INPUT_CHARACTERS
    )
    if compact.startswith(DISPLAY_PREFIX):
        compact = compact[len(DISPLAY_PREFIX) :]

    if len(compact) != NORMALIZED_LENGTH:
        raise InvalidFallbackReferenceError("Fallback reference has the wrong length.")

    payload = "".join(_PAYLOAD_ALIASES.get(character, character) for character in compact[:-1])
    check = compact[-1]
    if check not in CHECK_ALPHABET:
        raise InvalidFallbackReferenceError("Fallback reference check symbol is invalid.")
    return payload + check


def validate_fallback_reference(normalized: str) -> str:
    """Validate a normalized reference's check symbol, returning it unchanged.

    Called BEFORE any database access, so a single mistyped character costs
    no query and consumes no lookup budget beyond the attempt itself.
    """
    if len(normalized) != NORMALIZED_LENGTH:
        raise InvalidFallbackReferenceError("Fallback reference has the wrong length.")
    payload, check = normalized[:-1], normalized[-1]
    payload_value = _decode_payload(payload)
    if _check_symbol(payload_value) != check:
        raise InvalidFallbackReferenceError("Fallback reference check symbol does not match.")
    return normalized


def parse_fallback_reference(raw: object) -> str:
    """Normalize and validate in one step. The entry-lookup entry point."""
    return validate_fallback_reference(normalize_fallback_reference(raw))


def format_fallback_reference(normalized: str) -> str:
    """Render the stored form for display: `ASC-XXXX-XXXX-C` (15 characters)."""
    if len(normalized) != NORMALIZED_LENGTH:
        raise InvalidFallbackReferenceError("Fallback reference has the wrong length.")
    return f"{DISPLAY_PREFIX}-{normalized[0:4]}-{normalized[4:8]}-{normalized[8]}"


def mask_fallback_reference(normalized: str) -> str:
    """Mask a reference for audit summaries and logs.

    Keeps only the leading group so an auditor can correlate an event
    without the log itself becoming a usable lookup term.
    """
    if not normalized or len(normalized) != NORMALIZED_LENGTH:
        return "****"
    return f"{DISPLAY_PREFIX}-{normalized[0:4]}-****-*"
