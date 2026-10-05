"""`StructuredFormatter` -- the final structured JSON log line (Prompt 2 correction §3).

Proves that a secret occurring ONLY inside an exception message or
traceback is still redacted in the final rendered output, and that the
exception is never replaced with an unredacted `repr()`.
"""

from __future__ import annotations

import json
import logging
import sys

from apps.core.logging_config import StructuredFormatter
from apps.core.redaction import MASK


def _make_record(msg: str = "ordinary message", exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="asc2026",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def test_ordinary_message_remains_readable_in_the_final_json() -> None:
    record = _make_record("registration submitted for event ASC2026")
    payload = json.loads(StructuredFormatter().format(record))
    assert payload["message"] == "registration submitted for event ASC2026"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "asc2026"


def test_secret_in_exception_message_is_redacted_in_the_final_json(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "traceback-embedded-secret")
    try:
        raise RuntimeError("connection failed with password traceback-embedded-secret")
    except RuntimeError:
        record = _make_record("db connection attempt", exc_info=sys.exc_info())

    rendered = StructuredFormatter().format(record)
    payload = json.loads(rendered)

    assert "traceback-embedded-secret" not in rendered
    assert MASK in payload["exc_info"]


def test_secret_in_a_chained_exception_traceback_is_redacted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "chained-cause-secret")
    try:
        try:
            raise ValueError("root cause holds chained-cause-secret")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError:
        record = _make_record("wrapped failure", exc_info=sys.exc_info())

    rendered = StructuredFormatter().format(record)

    assert "chained-cause-secret" not in rendered


def test_exception_is_not_replaced_by_an_unredacted_repr(monkeypatch) -> None:
    """The formatter must not fall back to `repr(exc)` -- that would bypass
    the traceback-text redaction path and could reintroduce the secret."""
    monkeypatch.setenv("DATABASE_PASSWORD", "repr-bypass-secret")

    class LoudException(Exception):
        def __repr__(self) -> str:  # pragma: no cover - defensive shape only
            return "LoudException(password='repr-bypass-secret')"  # secret-scan: allow

    try:
        raise LoudException("message also has repr-bypass-secret")
    except LoudException:
        record = _make_record("loud failure", exc_info=sys.exc_info())

    rendered = StructuredFormatter().format(record)

    assert "repr-bypass-secret" not in rendered


def test_structured_extra_fields_are_included_and_redacted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "structured-field-secret")
    record = _make_record("operational action")
    record.actor_id = "operational-user-123"
    record.detail = "used structured-field-secret during the attempt"

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["actor_id"] == "operational-user-123"
    assert "structured-field-secret" not in payload["detail"]


def test_stack_info_is_redacted_when_present(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "stack-info-secret")
    record = _make_record("diagnostic dump")
    record.stack_info = "Stack (most recent call last):\n  password=stack-info-secret\n"

    rendered = StructuredFormatter().format(record)

    assert "stack-info-secret" not in rendered


def test_message_is_redacted_even_when_the_filter_never_ran(monkeypatch) -> None:
    """The formatter must not depend on `RedactingLogFilter` having run first.

    A record constructed directly (as every test here does, and as a
    differently wired logger could in production) never passes through
    the filter -- `record.msg` still holds the raw, unredacted text.
    """
    monkeypatch.setenv("DATABASE_PASSWORD", "filter-bypassed-secret")
    record = _make_record("connecting with password filter-bypassed-secret")

    rendered = StructuredFormatter().format(record)
    payload = json.loads(rendered)

    assert "filter-bypassed-secret" not in rendered
    assert MASK in payload["message"]


def test_nested_extra_dict_is_recursively_redacted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "nested-dict-secret")
    record = _make_record("operational action")
    record.context = {"attempt": {"note": "used nested-dict-secret to proceed"}, "count": 3}

    payload = json.loads(StructuredFormatter().format(record))

    assert "nested-dict-secret" not in json.dumps(payload)
    assert payload["context"]["count"] == 3


def test_nested_extra_list_of_dicts_is_recursively_redacted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "list-item-secret")
    record = _make_record("operational action")
    record.attempts = [
        {"detail": "first try, no secret here"},
        {"detail": "second try used list-item-secret"},
    ]

    payload = json.loads(StructuredFormatter().format(record))

    assert "list-item-secret" not in json.dumps(payload)
    assert payload["attempts"][0]["detail"] == "first try, no secret here"


def test_deeply_nested_extra_structure_does_not_crash_or_hang(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "deeply-nested-secret")
    nested: dict[str, object] = {"leaf": "holds deeply-nested-secret here"}
    for _ in range(50):
        nested = {"child": nested}
    record = _make_record("operational action")
    record.tree = nested

    rendered = StructuredFormatter().format(record)

    assert "deeply-nested-secret" not in rendered


def test_self_referential_extra_structure_does_not_recurse_forever(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "cyclic-secret")
    cyclic: dict[str, object] = {"note": "holds cyclic-secret"}
    cyclic["self"] = cyclic
    record = _make_record("operational action")
    record.cyclic = cyclic

    rendered = StructuredFormatter().format(record)

    assert "cyclic-secret" not in rendered


def test_unsupported_extra_object_type_is_stringified_and_redacted(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "object-repr-secret")

    class ArbitraryValue:
        def __str__(self) -> str:  # pragma: no cover - defensive shape only
            return "ArbitraryValue(password=object-repr-secret)"  # secret-scan: allow

    record = _make_record("operational action")
    record.odd_value = ArbitraryValue()

    rendered = StructuredFormatter().format(record)

    assert "object-repr-secret" not in rendered


def test_correlation_id_field_does_not_bypass_redaction(monkeypatch) -> None:
    """Adversarial self-review: the correlation-ID field is rendered
    through the same `redact()` call as everything else, so even a
    corrupted/injected value there cannot slip a credential-shaped string
    into the final JSON line unmasked."""
    monkeypatch.setenv("DATABASE_PASSWORD", "correlation-field-secret")
    record = _make_record("operational action")
    record.correlation_id = "req-id password=correlation-field-secret"  # secret-scan: allow

    payload = json.loads(StructuredFormatter().format(record))

    assert "correlation-field-secret" not in payload["correlation_id"]
    assert MASK in payload["correlation_id"]
