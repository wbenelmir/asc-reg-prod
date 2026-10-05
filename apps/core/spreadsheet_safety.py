"""Shared, FIELD-AWARE spreadsheet formula-injection detection and
neutralization (SEC-005, Phase 2 Prompt 2 correction pass).

Three operations:

* `is_formula_shaped` -- the STRICT, general-text-field check. True if a
  value would be interpreted as a formula by a spreadsheet application
  after skipping leading whitespace/control/invisible characters and at
  most one leading literal quote. Deliberately has NO "starts with a
  digit after the sign" carve-out: a general text field never
  legitimately needs one, and that exact carve-out previously let
  genuinely dangerous values like `-2+3` or `+1-2` through (Phase 2
  Prompt 2 correction pass).
* `is_value_safe_for_field` -- the field-aware entry point every caller
  should actually use. A value is safe ONLY when the field itself has an
  explicitly approved COMPLETE grammar (telephone or numeric, below) that
  the ENTIRE value matches, or when it is not formula-shaped at all. A
  field with no declared grammar gets no carve-out whatsoever.
* `neutralize_for_spreadsheet` -- for values that must still be shown in a
  downloadable/preview representation, prefixes a value Excel/LibreOffice
  would otherwise interpret as a formula with a leading apostrophe.
  Applied ONLY to the spreadsheet-output representation; NEVER mutates the
  canonical stored/matched value.
"""

from __future__ import annotations

import re

# Characters treated as leading "invisible" content to skip before checking
# a value's first meaningful character: everything `str.isspace()` already
# recognizes (this covers EVERY Unicode whitespace code point, not just
# ASCII -- Phase 2 Prompt 2 correction pass "Unicode whitespace" bypass
# tests), every C0/C1 control character (`ord < 0x20` or `0x7f <= ord <
# 0xa0`), and a short list of zero-width/invisible formatting characters
# that are neither whitespace nor control characters by Python's own
# classification but are routinely used to defeat naive "first character"
# filters.
_INVISIBLE_FORMATTING_CHARS = frozenset(
    {
        "﻿",  # BOM / zero-width no-break space
        "​",  # zero-width space
        "‌",  # zero-width non-joiner
        "‍",  # zero-width joiner
        "⁠",  # word joiner
    }
)


def _is_leading_invisible(char: str) -> bool:
    if char.isspace():
        return True
    if char in _INVISIBLE_FORMATTING_CHARS:
        return True
    codepoint = ord(char)
    if codepoint < 0x20:
        return True
    if 0x7F <= codepoint < 0xA0:
        return True
    return False


def _strip_leading_invisible(value: str) -> str:
    index = 0
    while index < len(value) and _is_leading_invisible(value[index]):
        index += 1
    return value[index:]


# A literal quote character (DATA content, not CSV-syntax quoting, which
# the CSV parser has already removed by the time this function runs) is a
# known "quoted formula form" bypass: an attacker prepends it hoping a
# naive check for only `=+-@` misses the formula trigger that follows.
# Stripped at most once before the real check.
_LEADING_QUOTE_CHARS = "\"'"

# Every one of these, as the first meaningful character of a GENERAL text
# field, marks the value as formula-shaped -- unconditionally. No
# "followed by a digit" exception exists here at all (that exception is
# exactly the defect this correction pass closes): a general text field
# never legitimately begins with any of these four characters.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def is_formula_shaped(value: str) -> bool:
    """Strict, general-text-field formula-shape check (no field-specific
    grammar carve-out). Use `is_value_safe_for_field` instead of calling
    this directly unless the field is genuinely unstructured free text."""
    if not isinstance(value, str):
        return False
    stripped = _strip_leading_invisible(value)
    if not stripped:
        return False
    if stripped[0] in _LEADING_QUOTE_CHARS:
        stripped = _strip_leading_invisible(stripped[1:])
        if not stripped:
            return False
    return stripped[0] in _FORMULA_TRIGGER_CHARS


# ---------------------------------------------------------------------------
# Explicitly approved COMPLETE grammars. A value is safe under one of these
# ONLY when the ENTIRE (stripped) value matches -- never merely its first
# character -- so `-2+3` still never matches either grammar below.
# ---------------------------------------------------------------------------

_TELEPHONE_ALLOWED_CHARS = re.compile(r"^\+?[0-9 ()-]+$")
_TELEPHONE_MIN_DIGITS = 6
_TELEPHONE_MAX_DIGITS = 15
_NUMERIC_GRAMMAR = re.compile(r"^[+-]?\d+(\.\d+)?$")


def matches_telephone_grammar(value: str) -> bool:
    """True only if `value`, in full, is a plausible telephone number:
    an optional leading `+`, only digits/spaces/hyphens/parentheses as
    separators, ending in a digit, with a realistic total digit count.

    The digit-count bound is deliberate, not cosmetic: without it, a short
    signed expression like `+1-2` structurally matches "digits and
    hyphens only" just as well as a real number does, and would otherwise
    slip through as a false "telephone" carve-out (Phase 2 Prompt 2
    correction pass).
    """
    stripped = value.strip()
    if not stripped or not _TELEPHONE_ALLOWED_CHARS.match(stripped):
        return False
    if not stripped[-1].isdigit():
        return False
    digit_count = sum(1 for char in stripped if char.isdigit())
    return _TELEPHONE_MIN_DIGITS <= digit_count <= _TELEPHONE_MAX_DIGITS


def matches_numeric_grammar(value: str) -> bool:
    """True only if `value`, in full, is a single signed integer or
    decimal number -- never a multi-operator expression like `-2+3`."""
    return bool(_NUMERIC_GRAMMAR.match(value.strip()))


_FIELD_GRAMMARS = {
    "telephone": matches_telephone_grammar,
    "numeric": matches_numeric_grammar,
}


def is_value_safe_for_field(value: str, *, field_kind: str | None = None) -> bool:
    """Field-aware safety check -- the entry point every caller should use.

    `field_kind` names an explicitly approved COMPLETE grammar
    ("telephone" or "numeric") for THIS field. The value is safe when
    EITHER the entire value matches that grammar, OR (regardless of
    `field_kind`) the value is not formula-shaped at all. A field with no
    declared grammar, or an unrecognized `field_kind`, gets no carve-out:
    it is safe only when `is_formula_shaped` is False.
    """
    if field_kind is not None:
        grammar = _FIELD_GRAMMARS.get(field_kind)
        if grammar is not None and grammar(value):
            return True
    return not is_formula_shaped(value)


def neutralize_for_spreadsheet(value: str) -> str:
    """Return a spreadsheet-safe PREVIEW/OUTPUT representation of `value`.

    Never mutates the canonical value a caller stores, matches, or
    persists -- only the representation handed to a downloadable CSV or an
    HTML preview. HTML previews still require their own normal
    auto-escaping in addition to this (this function addresses spreadsheet
    formula evaluation, not HTML injection).
    """
    if is_formula_shaped(value):
        return "'" + value
    return value
