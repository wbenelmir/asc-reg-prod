"""Phase 3 Prompt 8 (P8-03): exact-value redaction of ES256 signing keys.

`QR_SIGNING_KEY_V<n>` holds a PEM-encoded P-256 PRIVATE key. Before this
correction the name-based assignment redaction knew the family, but the
exact-value layer (the "strong guarantee": mask the configured value
wherever it appears) did not, so the key text appearing on its own in a log
line survived untouched.

Every value here is SYNTHETIC. The PEM markers are composed at runtime
because the repository credential scanner rejects a literal private-key
block anywhere in the tree. Assertions never interpolate the synthetic key
into a failure message: `_assert_absent` computes the membership first, so
a failing run reports only that material leaked, not the material itself.
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from apps.core import credential_shapes, redaction
from apps.core.credential_shapes import normalize_pem_env_value
from apps.core.logging_config import StructuredFormatter
from apps.core.redaction import MASK, RedactingLogFilter, extra_fields, redact

_BEGIN = "-----BEGIN EC {} KEY-----".format("PRIVATE")
_END = "-----END EC {} KEY-----".format("PRIVATE")


def _synthetic_env_value(body: str) -> str:
    """A single-line env value exactly as `.env` carries it: literal `\\n`."""
    return f"  {_BEGIN}\\n{body}\\n{body[::-1]}\\n{_END}\\n  "


def _assert_absent(material: str, text: str) -> None:
    leaked = material in text
    assert not leaked, "synthetic signing-key material reached the output"


def _body_lines(env_value: str) -> list[str]:
    """Every non-marker line of the normalized PEM -- none may survive."""
    return [
        line
        for line in normalize_pem_env_value(env_value).splitlines()
        if line and not line.startswith("-----")
    ]


@pytest.fixture
def qr_keys(monkeypatch):
    """Three active synthetic key versions, deliberately non-contiguous."""
    values = {
        version: _synthetic_env_value(f"SYNTHETICv{version}BODYqrSigning{version * 7919}xyz")
        for version in (1, 2, 17)
    }
    for version, value in values.items():
        monkeypatch.setenv(f"QR_SIGNING_KEY_V{version}", value)
    return values


def _record(msg: str, *, args=(), exc_info=None, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="asc2026.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# One shared family definition
# ---------------------------------------------------------------------------


def test_redaction_uses_the_single_shared_key_family_definition():
    assert redaction._VERSIONED_SECRET_ENV_PATTERNS is credential_shapes.VERSIONED_KEY_NAME_PATTERNS
    assert credential_shapes.QR_SIGNING_KEY_NAME_PATTERN in redaction._VERSIONED_SECRET_ENV_PATTERNS


def test_the_signing_provider_loads_exactly_the_shared_normalized_form(qr_keys):
    from apps.core.crypto.signing import _normalize_pem

    for value in qr_keys.values():
        assert _normalize_pem(value) == normalize_pem_env_value(value).encode("utf-8")
        assert "\n" in normalize_pem_env_value(value)
        assert "\\n" not in normalize_pem_env_value(value)


# ---------------------------------------------------------------------------
# Exact-value redaction
# ---------------------------------------------------------------------------


def test_the_raw_environment_value_is_redacted_when_it_appears_alone(qr_keys):
    for value in qr_keys.values():
        output = redact(f"provider loaded {value} and continued")
        _assert_absent(value.strip(), output)
        assert MASK in output


def test_the_normalized_multiline_pem_is_redacted_when_it_appears_alone(qr_keys):
    for value in qr_keys.values():
        normalized = normalize_pem_env_value(value)
        output = redact(f"key text was:\n{normalized}\nend of dump")
        _assert_absent(normalized, output)
        for line in _body_lines(value):
            _assert_absent(line, output)
        assert MASK in output


def test_assignment_shaped_occurrences_are_redacted(qr_keys):
    for version, value in qr_keys.items():
        for text in (
            f"QR_SIGNING_KEY_V{version}={value}",
            f'QR_SIGNING_KEY_V{version}: "{value}"',
            f"config dump: QR_SIGNING_KEY_V{version} = {normalize_pem_env_value(value)}",
        ):
            output = redact(text)
            for line in _body_lines(value):
                _assert_absent(line, output)


def test_every_active_version_is_covered_without_a_code_change(qr_keys, monkeypatch):
    far_future = _synthetic_env_value("SYNTHETICfarFutureBODY")
    monkeypatch.setenv("QR_SIGNING_KEY_V9999", far_future)
    for value in [*qr_keys.values(), far_future]:
        output = redact(f"a={normalize_pem_env_value(value)} b={value}")
        for line in _body_lines(value):
            _assert_absent(line, output)


def test_a_non_matching_name_is_not_treated_as_a_signing_key(monkeypatch):
    monkeypatch.setenv("QR_SIGNING_KEY_VX", "not-a-versioned-signing-key-value")
    assert redact("value not-a-versioned-signing-key-value") == (
        "value not-a-versioned-signing-key-value"
    )


# ---------------------------------------------------------------------------
# Every logging path: message, args, structured fields, exception text
# ---------------------------------------------------------------------------


def test_the_logging_filter_redacts_a_key_passed_as_an_argument(qr_keys):
    value = qr_keys[2]
    record = _record("loaded %s", args=(normalize_pem_env_value(value),))
    RedactingLogFilter().filter(record)
    for line in _body_lines(value):
        _assert_absent(line, record.getMessage())


def test_structured_fields_are_redacted(qr_keys):
    value = qr_keys[17]
    record = _record(
        "provider state",
        provider={"key": normalize_pem_env_value(value), "raw": [value]},
    )
    rendered = json.dumps(extra_fields(record), default=str)
    for line in _body_lines(value):
        _assert_absent(line, rendered)


def test_exception_text_and_the_final_formatted_line_are_redacted(qr_keys):
    value = qr_keys[1]
    try:
        raise RuntimeError(f"could not use {normalize_pem_env_value(value)}")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = _record(
        "failure while loading %s",
        args=(value,),
        exc_info=exc_info,
        detail=normalize_pem_env_value(value),
    )
    RedactingLogFilter().filter(record)
    line = StructuredFormatter().format(record)
    parsed = json.loads(line)
    for text in (line, parsed["message"], parsed["exc_info"], str(parsed["detail"])):
        for body_line in _body_lines(value):
            _assert_absent(body_line, text)
    assert MASK in parsed["exc_info"]
