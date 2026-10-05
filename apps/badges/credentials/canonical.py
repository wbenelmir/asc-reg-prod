"""Canonical, byte-deterministic serialization for the Digital Entry Pass QR.

Phase 3 Prompt 2 / ADR-0019. This module is the single definition of what
a pass credential looks like on the wire. Both the issuing adapter and the
verifier import from here, so the producer and the consumer cannot drift.

Two canonical representations, each with exactly one permitted byte form:

* the protected header -- exactly `alg`, `kid`, `typ`, sorted, no
  whitespace;
* the payload -- exactly the eleven approved claims, sorted, no
  insignificant whitespace, integer timestamps.

Everything here is fail-closed. An unexpected member, a missing member, a
wrongly typed value, an out-of-range length, a character outside the
declared alphabet, a duplicate JSON key, a padded base64url segment, or a
non-canonical base64url encoding is rejected outright, and the caller
receives a stable machine-readable code rather than a parser detail.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from typing import Any

#: The only permitted `typ` value. Distinct from `JWT` on purpose: this is
#: not a JWT and must not be mistaken for one by a generic consumer.
PASS_TYP = "ASC-PASS"  # noqa: S105 - a JWS media type, not a credential

#: The only permitted `alg` value, repeated here so the header allowlist is
#: self-contained.
PASS_ALG = "ES256"  # noqa: S105 - an algorithm name, not a credential

#: Exactly the protected-header members permitted, in canonical (sorted)
#: order. Anything else -- including `crit`, `jwk`, `jku`, `b64`, `cty` --
#: is rejected.
HEADER_MEMBERS: tuple[str, ...] = ("alg", "kid", "typ")

#: Exactly the payload claims permitted, in canonical (sorted) order.
#: There is no extension mechanism: adding a claim here is a deliberate,
#: reviewed change that a test will force you to make consciously.
PAYLOAD_CLAIMS: tuple[str, ...] = (
    "apc",  # Access Profile code
    "bai",  # Badge assignment public reference
    "btc",  # Badge Type code
    "cv",  # Credential version
    "eid",  # Event edition public code
    "exp",  # Expiry, epoch seconds
    "jti",  # Per-version credential identifier
    "n",  # Nonce
    "nbf",  # Not before, epoch seconds
    "pid",  # Event-scoped participant pseudonym
    "v",  # Payload schema version
)

#: Opaque 128-bit identifiers, base64url without padding.
OPAQUE_ID_LENGTH = 22
#: Opaque 96-bit nonce, base64url without padding.
NONCE_LENGTH = 16

_OPAQUE_ID_PATTERN = re.compile(rf"^[A-Za-z0-9_-]{{{OPAQUE_ID_LENGTH}}}$")
_NONCE_PATTERN = re.compile(rf"^[A-Za-z0-9_-]{{{NONCE_LENGTH}}}$")
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_KEY_ID_PATTERN = re.compile(r"^v[1-9][0-9]{0,3}$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

MAX_CODE_LENGTH = 64
MAX_EVENT_CODE_LENGTH = 32
MAX_KEY_ID_LENGTH = 8
#: Bounded to keep `cv`, `v`, `nbf`, and `exp` far away from unbounded
#: integers that could stress the JSON parser or a downstream consumer.
MAX_INTEGER_CLAIM = 1 << 62


class CanonicalFormatError(ValueError):
    """Raised for any structurally invalid header, payload, or encoding.

    The message is developer-facing. It is never rendered to an operator or
    a participant, and it never contains credential material.
    """


# ---------------------------------------------------------------------------
# Strict base64url
# ---------------------------------------------------------------------------


def b64url_encode(raw: bytes) -> str:
    """Base64url-encode without padding (RFC 7515 s2)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(segment: str) -> bytes:
    """Strictly decode one unpadded base64url segment.

    Rejects padding characters, any character outside the base64url
    alphabet, impossible segment lengths, and non-canonical encodings whose
    unused trailing bits are not zero (which would let two distinct strings
    decode to identical bytes and defeat byte-exact comparison).
    """
    if not isinstance(segment, str):
        raise CanonicalFormatError("Base64url segment must be text.")
    if segment == "":
        raise CanonicalFormatError("Base64url segment is empty.")
    if "=" in segment:
        raise CanonicalFormatError("Base64url segment must not be padded.")
    # `fullmatch`, never `match`: a `$`-anchored `match` also accepts a value
    # ending in a newline, which is outside the declared alphabet (P8-04).
    if not _BASE64URL_PATTERN.fullmatch(segment):
        raise CanonicalFormatError("Base64url segment contains an invalid character.")
    remainder = len(segment) % 4
    if remainder == 1:
        raise CanonicalFormatError("Base64url segment has an impossible length.")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise CanonicalFormatError("Base64url segment could not be decoded.") from exc
    # Canonicity: re-encoding must reproduce the input exactly.
    if b64url_encode(decoded) != segment:
        raise CanonicalFormatError("Base64url segment is not canonically encoded.")
    return decoded


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise CanonicalFormatError("JSON object contains a duplicate member.")
        seen[key] = value
    return seen


def loads_strict(raw: bytes) -> dict[str, Any]:
    """Parse a JSON object, rejecting duplicate members and non-objects."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalFormatError("JSON segment is not valid UTF-8.") from exc
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_members)
    except json.JSONDecodeError as exc:
        raise CanonicalFormatError("JSON segment is malformed.") from exc
    if not isinstance(parsed, dict):
        raise CanonicalFormatError("JSON segment is not an object.")
    return parsed


def dumps_canonical(payload: dict[str, Any]) -> bytes:
    """Serialize deterministically: sorted keys, no insignificant whitespace.

    `ensure_ascii=False` is pinned for byte determinism. Every value this
    project places in a header or payload is validated to be ASCII first,
    so the flag changes no byte today -- it is fixed here so a future value
    cannot silently alter the encoding.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------


def _require_text(value: Any, *, label: str, pattern: re.Pattern[str], max_length: int) -> str:
    if not isinstance(value, str):
        raise CanonicalFormatError(f"Claim {label} must be a string.")
    if len(value) > max_length:
        raise CanonicalFormatError(f"Claim {label} exceeds its maximum length.")
    # Whole-string validation (P8-04). A `$`-anchored `pattern.match` would
    # accept a value ending in a newline -- a character outside every
    # declared alphabet -- so `fullmatch` is the only permitted check.
    if not pattern.fullmatch(value):
        raise CanonicalFormatError(f"Claim {label} contains an invalid character.")
    return value


def _require_integer(value: Any, *, label: str, minimum: int) -> int:
    # `bool` is a subclass of `int` in Python; a boolean must never be
    # silently accepted where an integer claim is required.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalFormatError(f"Claim {label} must be an integer.")
    if value < minimum or value > MAX_INTEGER_CLAIM:
        raise CanonicalFormatError(f"Claim {label} is out of range.")
    return value


def validate_header(header: dict[str, Any]) -> dict[str, Any]:
    """Validate a protected header against the exact three-member allowlist."""
    members = tuple(sorted(header))
    if members != HEADER_MEMBERS:
        missing = [name for name in HEADER_MEMBERS if name not in header]
        if missing:
            raise CanonicalFormatError("Protected header is missing a required member.")
        raise CanonicalFormatError("Protected header contains an unsupported member.")
    if header["alg"] != PASS_ALG:
        raise CanonicalFormatError("Protected header declares an unsupported algorithm.")
    if header["typ"] != PASS_TYP:
        raise CanonicalFormatError("Protected header declares an unsupported type.")
    _require_text(header["kid"], label="kid", pattern=_KEY_ID_PATTERN, max_length=MAX_KEY_ID_LENGTH)
    return header


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a payload against the exact eleven-claim allowlist."""
    claims = tuple(sorted(payload))
    if claims != PAYLOAD_CLAIMS:
        missing = [name for name in PAYLOAD_CLAIMS if name not in payload]
        if missing:
            raise CanonicalFormatError("Payload is missing a required claim.")
        raise CanonicalFormatError("Payload contains an unsupported claim.")

    _require_text(payload["apc"], label="apc", pattern=_CODE_PATTERN, max_length=MAX_CODE_LENGTH)
    _require_text(payload["btc"], label="btc", pattern=_CODE_PATTERN, max_length=MAX_CODE_LENGTH)
    _require_text(
        payload["eid"], label="eid", pattern=_CODE_PATTERN, max_length=MAX_EVENT_CODE_LENGTH
    )
    _require_text(
        payload["bai"], label="bai", pattern=_OPAQUE_ID_PATTERN, max_length=OPAQUE_ID_LENGTH
    )
    _require_text(
        payload["jti"], label="jti", pattern=_OPAQUE_ID_PATTERN, max_length=OPAQUE_ID_LENGTH
    )
    _require_text(
        payload["pid"], label="pid", pattern=_OPAQUE_ID_PATTERN, max_length=OPAQUE_ID_LENGTH
    )
    _require_text(payload["n"], label="n", pattern=_NONCE_PATTERN, max_length=NONCE_LENGTH)

    _require_integer(payload["v"], label="v", minimum=1)
    _require_integer(payload["cv"], label="cv", minimum=1)
    nbf = _require_integer(payload["nbf"], label="nbf", minimum=1)
    exp = _require_integer(payload["exp"], label="exp", minimum=1)
    if exp <= nbf:
        raise CanonicalFormatError("Payload validity window is inverted.")
    return payload


# ---------------------------------------------------------------------------
# Canonical construction
# ---------------------------------------------------------------------------


def build_header(*, key_id: str) -> dict[str, Any]:
    """Build the canonical protected header for one signing key."""
    header = {"alg": PASS_ALG, "kid": key_id, "typ": PASS_TYP}
    return validate_header(header)


def canonical_header_bytes(*, key_id: str) -> bytes:
    """The exact protected-header bytes: `{"alg":"ES256","kid":...,"typ":"ASC-PASS"}`."""
    return dumps_canonical(build_header(key_id=key_id))


def canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """The exact payload bytes for a validated claim set."""
    return dumps_canonical(validate_payload(payload))


def signing_input(*, header_bytes: bytes, payload_bytes: bytes) -> bytes:
    """`BASE64URL(header) || '.' || BASE64URL(payload)` (RFC 7515 s5.1)."""
    return (
        b64url_encode(header_bytes).encode("ascii")
        + b"."
        + b64url_encode(payload_bytes).encode("ascii")
    )


def payload_hash(signing_input_bytes: bytes) -> str:
    """SHA-256 of the signing input, stored as regeneration evidence."""
    return hashlib.sha256(signing_input_bytes).hexdigest()
