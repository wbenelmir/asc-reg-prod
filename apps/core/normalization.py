"""Input normalization helpers shared by every form (UX-1, decisions S-02 and S-03).

Pure string functions, no Django imports: they are reused by widgets, forms
and services, and they never touch storage.
"""

from __future__ import annotations

# Arabic-Indic (U+0660..U+0669) and Extended Arabic-Indic (U+06F0..U+06F9)
# decimal digits map to their ASCII value. Only these two ranges are
# converted: any other Unicode decimal digit (Devanagari, fullwidth, ...) is
# left as it is, so the strict `[0-9]` checks that follow reject it instead of
# silently accepting a confusable character.
_DIGIT_TABLE = {
    code: ord("0") + offset for offset in range(10) for code in (0x0660 + offset, 0x06F0 + offset)
}

# Separators a person may type or paste inside a code: ordinary space,
# no-break space, thin space, narrow no-break space, and hyphen-minus.
CODE_SEPARATORS = "".join(chr(code) for code in (0x0020, 0x00A0, 0x2009, 0x202F)) + "-"
_SEPARATOR_TABLE = {ord(char): None for char in CODE_SEPARATORS}


def normalize_digits(value: str) -> str:
    """Return `value` with Arabic-Indic and Extended Arabic-Indic digits as ASCII."""
    return value.translate(_DIGIT_TABLE)


def strip_code_separators(value: str) -> str:
    """Remove the separators in `CODE_SEPARATORS` (never any other character)."""
    return value.translate(_SEPARATOR_TABLE)


def normalize_digit_code(value: str) -> str:
    """Digits-only code normalization: strip separators, then map digits to ASCII.

    The result is not validated. Callers still require the exact shape
    (for example `^[0-9]{6}$`), so any remaining non-digit character fails.
    """
    return normalize_digits(strip_code_separators(value or ""))
