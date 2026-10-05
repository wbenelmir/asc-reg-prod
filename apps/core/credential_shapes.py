"""Shared credential-NAME shape recognition.

Used by BOTH the log-redaction layer (`apps/core/redaction.py`) and the
standalone credential scanner (`scripts/check.py`) so the two can never
drift apart on what counts as a credential-shaped identifier (Prompt 2
corrections §2 and §5 explicitly require the same suffix coverage in both).

This module recognizes NAMES only -- e.g. `"DATABASE_PASSWORD"`,
`"secret_key"`, `"S3_STORAGE_SECRET_ACCESS_KEY"`. It says nothing about
values; pairing a recognized name with an actual value, and deciding
whether that value looks like a hard-coded secret versus a safe
environment lookup, is the caller's job.
"""

from __future__ import annotations

import re

# Names that are themselves credential-shaped in full, not merely by
# suffix (either because they are the bare word, or because the suffix
# rule below would not otherwise catch them -- e.g. `S3_STORAGE_
# ACCESS_KEY_ID` ends in `_ID`, not `_ACCESS_KEY`).
EXACT_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {
        "PASSWORD",
        "SECRET",
        "TOKEN",
        "API_KEY",
        "ACCESS_KEY",
        "SECRET_KEY",
        "SECRET_ACCESS_KEY",
        "DATABASE_PASSWORD",
        "DATABASE_MIGRATION_PASSWORD",
        "DJANGO_SECRET_KEY",
        "S3_STORAGE_ACCESS_KEY_ID",
        "S3_STORAGE_SECRET_ACCESS_KEY",
        "EMAIL_PROVIDER_API_KEY",
        "JWT_SIGNING_SECRET",
    }
)

# A normalized name ENDING WITH any of these is credential-shaped
# regardless of its prefix (accepted plan, Prompt 2 correction §2). Order
# here is for readability only -- `str.endswith()` checks below are each
# independent of ordering.
CREDENTIAL_NAME_SUFFIXES: tuple[str, ...] = (
    "_SECRET_ACCESS_KEY",
    "_SECRET_KEY",
    "_SECRET",
    "_PASSWORD",
    "_TOKEN",
    "_API_KEY",
    "_ACCESS_KEY",
)

# RATE_LIMIT_HMAC_KEY_V1, _V2, ... _V<any positive integer> -- the version
# is dynamic (accepted plan §5.5 key rotation), so this is a pattern, not
# a fixed set of exact names. Prompt 3 adds two further versioned-key
# families on the same rotation model (ADR-0006): field-encryption keys
# and identity blind-index HMAC keys. All three are matched the same way.
RATE_LIMIT_HMAC_KEY_NAME_PATTERN = re.compile(r"^RATE_LIMIT_HMAC_KEY_V\d+$")
IDENTITY_ENCRYPTION_KEY_NAME_PATTERN = re.compile(r"^IDENTITY_ENCRYPTION_KEY_V\d+$")
IDENTITY_BLIND_INDEX_HMAC_KEY_NAME_PATTERN = re.compile(r"^IDENTITY_BLIND_INDEX_HMAC_KEY_V\d+$")
# Phase 3 Prompt 2 (ADR-0019) adds a fourth versioned-key family on the same
# rotation model: the Digital Entry Pass ES256 signing keys. Registering the
# pattern here is what makes BOTH the log-redaction filter and the
# standalone credential scanner recognize `QR_SIGNING_KEY_V<n>` from a
# single change -- which is the entire reason this module exists.
QR_SIGNING_KEY_NAME_PATTERN = re.compile(r"^QR_SIGNING_KEY_V\d+$")
# Phase 4 Prompt 2 (binding decision P2-D, ADR-0023) adds a fifth family:
# the Offline Package ES256 signing keys, deliberately separate from the QR
# keys. Like the QR family, its values are PEM private keys.
OFFLINE_PACKAGE_SIGNING_KEY_NAME_PATTERN = re.compile(r"^OFFLINE_PACKAGE_SIGNING_KEY_V\d+$")
VERSIONED_KEY_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    RATE_LIMIT_HMAC_KEY_NAME_PATTERN,
    IDENTITY_ENCRYPTION_KEY_NAME_PATTERN,
    IDENTITY_BLIND_INDEX_HMAC_KEY_NAME_PATTERN,
    QR_SIGNING_KEY_NAME_PATTERN,
    OFFLINE_PACKAGE_SIGNING_KEY_NAME_PATTERN,
)
#: The versioned families whose values are PEM text, so redaction must also
#: mask the normalized multi-line form the signing providers load.
PEM_KEY_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    QR_SIGNING_KEY_NAME_PATTERN,
    OFFLINE_PACKAGE_SIGNING_KEY_NAME_PATTERN,
)


def normalize_pem_env_value(raw: str) -> str:
    r"""The PEM text a signing provider actually consumes from an env value.

    `.env` values cannot span lines, so a literal two-character `\n`
    sequence is folded to a real newline and surrounding whitespace is
    stripped. Defined HERE, once, because two consumers must agree on it
    byte for byte: `apps.core.crypto.signing` loads exactly this form, and
    `apps.core.redaction` must mask exactly this form too (Phase 3 Prompt 8,
    P8-03) -- a second copy of the rule could drift and leave the loaded
    key text unredacted.
    """
    return raw.replace("\\n", "\n").strip()


# The explicit, single-source-of-truth ceiling on a credential NAME's
# total length once separators are folded (accepted plan: "a safe bounded
# total length, such as 128 characters").
_MAX_CREDENTIAL_NAME_LENGTH = 128

# A credential-name candidate as it may appear in source, structured config,
# or a log message. The sole syntactic bound is the same 128-character total
# ceiling enforced semantically by `is_credential_shaped_name()`: there is no
# independent word-count or per-segment limit that can create a blind spot.
#
# Each repeated branch consumes exactly one character and the quantifier is
# explicitly bounded, avoiding catastrophic backtracking. Separators are an
# underscore, hyphen, or literal space and must be followed by an alphanumeric
# character, so trailing assignment whitespace is not consumed as part of the
# candidate name.
NAME_TOKEN_PATTERN = (
    rf"[A-Za-z](?:[A-Za-z0-9]|[_\- ](?=[A-Za-z0-9])){{0,{_MAX_CREDENTIAL_NAME_LENGTH - 1}}}"  # noqa: S105
)


def normalize_credential_name(candidate: str) -> str:
    """Fold separators (underscore, hyphen, whitespace) to `_`, uppercase.

    `"secret key"`, `"secret-key"`, and `"secret_key"` all normalize to
    `"SECRET_KEY"`, so one comparison covers every separator variant the
    accepted plan requires.
    """
    normalized = re.sub(r"[-\s]+", "_", candidate.strip()).upper()
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def is_credential_shaped_name(candidate: str) -> bool:
    """True if `candidate` (after normalization) names a credential.

    Covers every identifier the credential scanner and the log-redaction
    layer must both recognize: the fixed exact names, every suffix in
    `CREDENTIAL_NAME_SUFFIXES`, and every `RATE_LIMIT_HMAC_KEY_V<n>` --
    with no arbitrary segment-count limit, only the explicit
    `_MAX_CREDENTIAL_NAME_LENGTH` total-length ceiling.
    """
    if not 1 <= len(candidate) <= _MAX_CREDENTIAL_NAME_LENGTH:
        return False
    name = normalize_credential_name(candidate)
    if not name:
        return False
    if name in EXACT_CREDENTIAL_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in CREDENTIAL_NAME_SUFFIXES):
        return True
    if any(pattern.match(name) for pattern in VERSIONED_KEY_NAME_PATTERNS):
        return True
    return False
