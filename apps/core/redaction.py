"""Redaction of sensitive values from application logs.

Accepted plan, Prompt 2 CP 2g: redaction foundations for identity values,
OTPs, tokens, document paths/URLs, database credentials and connection
URLs. Two layers:

1. EXACT-VALUE redaction of the secrets this project's settings know about
   (database password, migration-owner password, Django secret key,
   object-storage credentials, rate-limit HMAC keys) -- any occurrence of
   the literal configured value in a log message is masked, regardless of
   context. This is the strong guarantee: it is not a guess about what a
   secret looks like, it is the actual configured value.
2. SHAPE-based redaction of connection-string and key=value secret
   patterns that might appear incidentally (e.g. inside a driver's own
   exception message), as defence in depth.

`redact()` is applied at every point a secret could reach the final
structured JSON log line (Prompt 2 correction §3): the formatted message
(positional and mapping args already folded in), project-attached
`extra=` structured fields (`extra_fields()`), and formatted exception
messages, tracebacks, and stack traces (applied by
`apps/core/logging_config.StructuredFormatter`, since that text does not
exist yet at filter time). Every currently-active
`RATE_LIMIT_HMAC_KEY_V<version>` is covered dynamically, not just V1.

Neither layer is a substitute for never constructing a log message that
contains a secret in the first place. Domain-specific redaction (OTP
values, NIN/passport plaintext) is enforced at the point those values exist,
starting in Prompt 3 -- this module is the last line of defence, not the
first.
"""

from __future__ import annotations

import logging
import os
import re

from apps.core.credential_shapes import (
    NAME_TOKEN_PATTERN,
    PEM_KEY_NAME_PATTERNS,
    VERSIONED_KEY_NAME_PATTERNS,
    is_credential_shaped_name,
    normalize_pem_env_value,
)

MASK = "***REDACTED***"

# Environment variable NAMES whose current values are redacted verbatim
# wherever they appear in a log message. Deliberately does not include
# non-secret configuration such as DATABASE_HOST/PORT/NAME/USER.
SENSITIVE_ENV_VARS: tuple[str, ...] = (
    "DATABASE_PASSWORD",
    "DATABASE_MIGRATION_PASSWORD",
    "DJANGO_SECRET_KEY",
    "S3_STORAGE_ACCESS_KEY_ID",
    "S3_STORAGE_SECRET_ACCESS_KEY",
    "EMAIL_PROVIDER_API_KEY",
    "EMAIL_HOST_PASSWORD",
    # IDV-2: the ministry NIN service credentials (ADR-0026).
    "MINISTRY_NIN_API_USERNAME",
    "MINISTRY_NIN_API_PASSWORD",
)

# Every RATE_LIMIT_HMAC_KEY_V<version> currently present in the environment
# is a secret, for whichever versions are active (accepted plan §5.5) --
# not only V1. Matched dynamically rather than hardcoded so a rotation to
# V2, V3, ... is covered automatically without a code change here. Prompt 3
# adds two further versioned-key families on the same rotation model
# (ADR-0006): field-encryption keys and identity blind-index HMAC keys.
# Phase 3 Prompt 2 adds the ES256 Digital Entry Pass signing keys
# (`QR_SIGNING_KEY_V<n>`). Phase 4 Prompt 2 adds the separate Offline Package
# signing keys (`OFFLINE_PACKAGE_SIGNING_KEY_V<n>`, ADR-0023).
#
# The family list is NOT redefined here: it is the single shared
# `apps.core.credential_shapes.VERSIONED_KEY_NAME_PATTERNS` that the
# credential scanner also uses. A private copy here is exactly how the QR
# signing-key family was once missing from exact-value redaction while the
# scanner already knew it (Phase 3 Prompt 8, P8-03).
_VERSIONED_SECRET_ENV_PATTERNS: tuple[re.Pattern[str], ...] = VERSIONED_KEY_NAME_PATTERNS


def _sensitive_env_values() -> list[str]:
    """Every currently-configured secret value that must never reach a log.

    Combines the fixed `SENSITIVE_ENV_VARS` list with every environment
    variable matching one of `_VERSIONED_SECRET_ENV_PATTERNS`, however many
    versions are currently active for any of the four key families.

    For a PEM-valued family (`QR_SIGNING_KEY_V<n>`,
    `OFFLINE_PACKAGE_SIGNING_KEY_V<n>`), BOTH text forms are covered: the raw
    environment value (single line, literal `\\n` sequences) and the
    normalized multi-line PEM the signing provider actually loads
    (`normalize_pem_env_value`). Longer values are returned first so a
    shorter secret that happens to be a substring of a longer one can never
    leave a fragment of the longer one unmasked.
    """
    values = [value for name in SENSITIVE_ENV_VARS if (value := os.environ.get(name))]
    for name, value in os.environ.items():
        if not value or not any(pattern.match(name) for pattern in _VERSIONED_SECRET_ENV_PATTERNS):
            continue
        values.append(value)
        if any(pattern.match(name) for pattern in PEM_KEY_NAME_PATTERNS):
            normalized = normalize_pem_env_value(value)
            if normalized and normalized != value:
                values.append(normalized)
    return sorted(set(values), key=len, reverse=True)


_SHAPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # postgres(ql)://user:password@host:port/db -- mask the password only
    (
        re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)([^@/\s]+)(@)"),
        r"\1" + MASK + r"\3",
    ),
    # redis(s)://[:password@]host:port -- mask the password only
    (
        re.compile(r"(rediss?://(?:[^:@/\s]*:)?)([^@/\s]+)(@)"),
        r"\1" + MASK + r"\3",
    ),
    # Authorization: Bearer <token>
    (
        re.compile(r"(?i)(Authorization:\s*Bearer\s+)([A-Za-z0-9\-_.]+)"),
        r"\1" + MASK,
    ),
    # IDV-2 (addendum §6): the ministry lookup path carries the NIN, so any
    # `/api/get/<value>` segment is masked wherever it surfaces (an HTTP client
    # error, a proxy line, a traceback), and so is any bare run of exactly 18
    # digits, the NIN shape.
    (re.compile(r"(/api/get/)[^\s/?#\"']+"), r"\1" + MASK),
    (re.compile(r"(?<![0-9])[0-9]{18}(?![0-9])"), MASK),
]

# Generic key=value / key: "value" secret-shaped assignment, e.g. one that
# might appear incidentally inside a driver's own exception message. The
# NAME is matched permissively (any word-token shape) and then validated
# SEMANTICALLY by `is_credential_shaped_name()` -- the same shared
# recognizer the standalone credential scanner uses (`scripts/check.py`,
# `apps/core/credential_shapes.py`) -- so this covers every prefixed or
# suffixed identifier the project uses (`DATABASE_PASSWORD`,
# `S3_STORAGE_SECRET_ACCESS_KEY`, `secret_key`, `secret-key`, `secret key`,
# `RATE_LIMIT_HMAC_KEY_V<n>`, ...), not just the bare words the previous
# fixed alternation recognized (Prompt 2 correction §5).
#
# The value alternates between a QUOTED form (bounded by its own closing
# quote) and an UNQUOTED form bounded only by a structural delimiter: comma,
# semicolon, YAML comment, or end of line. Security deliberately wins over
# preserving ambiguous trailing prose: once a credential assignment begins,
# the complete value is masked rather than leaking a seventh or later word.
_GENERIC_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    rf"""(?<![A-Za-z0-9_])({NAME_TOKEN_PATTERN})(\s*[=:]\s*)"""
    r"""(?:"[^"]+"|'[^']+'|[^"',;#\r\n]+)"""
)


def _redact_generic_credential_assignments(text: str) -> str:
    """Mask every credential-shaped `name = value` / `name: value` pair in `text`.

    Deliberately NOT a plain `pattern.sub()`: the name is matched
    permissively (any word-token shape, including free-text words like
    "dump" or "config"), so a rejected candidate -- one whose name is not
    actually credential-shaped -- must not let the regex engine's normal
    non-overlapping match advancement swallow a REAL credential that
    immediately follows it past a colon (e.g. "config dump: password=..."
    would otherwise match "dump" as a false name whose greedy "value"
    consumes the real `password=` text, hiding it from ever being matched
    on its own). On a rejection this only skips past the rejected NAME
    token itself and retries from there, so a genuine credential
    immediately after is still found.
    """
    parts: list[str] = []
    pos = 0
    text_length = len(text)
    while pos <= text_length:
        match = _GENERIC_CREDENTIAL_ASSIGNMENT_PATTERN.search(text, pos)
        if match is None:
            parts.append(text[pos:])
            break
        name, separator = match.group(1), match.group(2)
        if is_credential_shaped_name(name):
            parts.append(text[pos : match.start()])
            parts.append(f"{name}{separator}{MASK}")
            pos = match.end()
        else:
            parts.append(text[pos : match.end(1)])
            pos = match.end(1)
    return "".join(parts)


def redact(text: str) -> str:
    """Return `text` with known secret material masked out.

    Applied to: the formatted log message, positional and mapping log
    arguments (both folded into the message by `record.getMessage()` before
    this function ever sees them), every structured field this project
    attaches via `extra=`, and -- critically -- formatted exception
    messages, tracebacks, and stack traces, via `StructuredFormatter`
    (`apps/core/logging_config.py`), which calls this same function on
    `exc_info`/`stack_info` text before it is ever written out. There is
    exactly one redaction implementation; nothing downstream re-derives or
    duplicates it, so a secret cannot slip through a second, unredacted
    rendering path (including a `repr()` of the exception).
    """
    if not text:
        return text
    result = text
    for value in _sensitive_env_values():
        result = result.replace(value, MASK)
    for pattern, replacement in _SHAPE_PATTERNS:
        result = pattern.sub(replacement, result)
    result = _redact_generic_credential_assignments(result)
    return result


# LogRecord attributes that are never treated as a project-controlled
# "structured field" -- either they are the stdlib's own bookkeeping
# (rendered/redacted separately, or irrelevant to secrets) or they are
# already surfaced under their own top-level JSON key by
# `StructuredFormatter` (accepted plan, Prompt 2 CP 2g; correction §3).
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "correlation_id",
    "asctime",
}


# Hard ceiling on nested-structure depth while sanitizing an `extra=`
# value. Defence in depth against a pathological or hostile deeply-nested
# structure ever hanging or crashing the logging call itself -- logging
# must never be the thing that brings a request down (Prompt 2
# correction §5).
_MAX_SANITIZE_DEPTH = 10


def _sanitize_value(value: object, _seen: frozenset[int] = frozenset(), _depth: int = 0) -> object:
    """Recursively redact a value that may be a nested dict/list/tuple/set.

    String values are redacted with `redact()`. Plain scalars (`int`,
    `float`, `bool`, `None`) pass through unchanged. Containers (`dict`,
    `list`, `tuple`, `set`, `frozenset`) are walked recursively, keys
    included (a secret could be attached as a mapping key, not only a
    value). Any other object type is never trusted to have a secret-free
    `repr()`/`str()` (for example, a driver exception object can embed a
    connection URL) -- it is converted to text and redacted the same way
    a string would be, rather than passed through as an opaque object.

    Two independent safety limits, since this runs on every log call and
    must never itself become a hang or a crash: `_seen` tracks container
    `id()`s already on the current path, so a structure that contains
    itself (directly or through a child) renders as a marker instead of
    recursing forever; `_depth` stops at `_MAX_SANITIZE_DEPTH` regardless,
    covering any other kind of unbounded or extremely deep nesting.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if _depth >= _MAX_SANITIZE_DEPTH:
        return "<max-depth-reached>"
    if isinstance(value, dict):
        container_id = id(value)
        if container_id in _seen:
            return "<circular-reference>"
        child_seen = _seen | {container_id}
        return {
            _sanitize_value(key, child_seen, _depth + 1): _sanitize_value(
                item, child_seen, _depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        container_id = id(value)
        if container_id in _seen:
            return "<circular-reference>"
        child_seen = _seen | {container_id}
        sanitized_items = [_sanitize_value(item, child_seen, _depth + 1) for item in value]
        if isinstance(value, tuple):
            return tuple(sanitized_items)
        return sanitized_items
    try:
        text = str(value)
    except Exception:  # pragma: no cover - defensive, logging must never raise
        return "<unrepresentable>"
    return redact(text)


_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "access_key",
        "client_network_address",
        "database_password",
        "document_url",
        "download_url",
        "identity_identifier",
        "nin",
        "otp",
        "otp_value",
        "passport",
        "passport_number",
        "password",
        "private_key",
        "recipient_value",
        "secret",
        "secret_key",
        "storage_key",
        "token",
        "value_encrypted",
    }
)


def sanitize_persistent_payload(value: object, _depth: int = 0) -> object:
    """Return a JSON-safe, redacted value for audit and outbox persistence.

    Persistence boundaries need a stronger rule than log redaction: the
    configured secret value may be unknown here, while a caller can still
    accidentally submit a raw OTP, identifier, document URL, or key under a
    recognisably sensitive field name. Such values are therefore masked by
    key as well as by the normal exact-value and assignment-shape rules.
    """
    if _depth >= _MAX_SANITIZE_DEPTH:
        return "<max-depth-reached>"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in value.items():
            key = redact(str(raw_key))
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            result[key] = (
                MASK
                if normalized_key in _SENSITIVE_PAYLOAD_KEYS
                or is_credential_shaped_name(normalized_key)
                else sanitize_persistent_payload(item, _depth + 1)
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_persistent_payload(item, _depth + 1) for item in value]
    return _sanitize_value(value, _depth=_depth)


def extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return the project-attached `extra=` fields on `record`, redacted.

    Anything passed as `logger.info(..., extra={"actor_id": ...})` lands as
    a plain attribute on the `LogRecord`, entirely outside `record.msg`/
    `record.args` -- `RedactingLogFilter` alone would never see it. Every
    value is sanitized with `_sanitize_value()`, which recurses into
    nested dicts/lists/tuples/sets (Prompt 2 correction §5) rather than
    redacting only a top-level string and passing nested structures
    through untouched.
    """
    fields: dict[str, object] = {}
    for key, value in vars(record).items():
        if key in _RESERVED_LOGRECORD_ATTRS:
            continue
        fields[key] = _sanitize_value(value)
    return fields


class RedactingLogFilter(logging.Filter):
    """Logging filter applying `redact()` to the formatted record message.

    Only the message (and its args, already folded in by `getMessage()`) is
    handled here -- `exc_info`/`stack_info` text does not exist as a string
    yet at filter time (it is rendered lazily, later, by the formatter), so
    `StructuredFormatter` applies `redact()` to that text itself. Both paths
    call this module's one `redact()` function; there is no second
    implementation to fall out of sync.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive, never hide the record
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
