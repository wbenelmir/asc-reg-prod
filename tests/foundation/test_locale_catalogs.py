"""Translation-catalog completeness checks for the Prompt 4 participant UI."""

from __future__ import annotations

import gettext
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A conservative, dependency-free `.po` parser covering exactly the entry
# shape this project's catalogs actually use: a `msgid "..."` (optionally
# spread across `""`-continuation lines) followed immediately by a single
# `msgstr "..."` (also possibly continued). Plural (`msgid_plural`) and
# context (`msgctxt`) entries are deliberately skipped -- this project does
# not currently author any -- rather than mis-parsed.
_ENTRY_PATTERN = re.compile(
    r'^msgid((?:\s*"(?:[^"\\]|\\.)*")+)\s*\nmsgstr((?:\s*"(?:[^"\\]|\\.)*")+)\s*$',
    re.MULTILINE,
)
_STRING_PIECE_PATTERN = re.compile(r'"((?:[^"\\]|\\.)*)"')


_PO_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _decode_po_string(raw: str) -> str:
    """Join `"..."` continuation pieces and unescape `\\n`/`\\t`/`\\r`/`\\"`/`\\\\`
    character-by-character -- NEVER via an `encode()`/`decode("unicode_escape")`
    round trip, which mangles any real (already-correctly-decoded) non-ASCII
    character such as an accented French letter or Arabic script by
    reinterpreting its multi-byte UTF-8 encoding one raw byte at a time.
    """
    joined = "".join(_STRING_PIECE_PATTERN.findall(raw))
    result: list[str] = []
    index = 0
    while index < len(joined):
        char = joined[index]
        if char == "\\" and index + 1 < len(joined):
            mapped = _PO_ESCAPES.get(joined[index + 1])
            if mapped is not None:
                result.append(mapped)
                index += 2
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _parse_simple_po_entries(catalog_text: str) -> dict[str, str]:
    """Return {msgid: msgstr} for every simple (non-plural, non-context,
    non-fuzzy) entry with a non-empty msgid. The very first entry (the
    catalog header, msgid "") is skipped."""
    entries: dict[str, str] = {}
    for block in catalog_text.split("\n\n"):
        if "#, fuzzy" in block or "msgctxt" in block or "msgid_plural" in block:
            continue
        match = _ENTRY_PATTERN.search(block)
        if match is None:
            continue
        msgid = _decode_po_string(match.group(1))
        msgstr = _decode_po_string(match.group(2))
        if not msgid or not msgstr:
            continue
        entries[msgid] = msgstr
    return entries


@pytest.mark.parametrize("language", ["fr", "ar"])
def test_participant_catalog_has_no_fuzzy_entries(language: str) -> None:
    catalog = (PROJECT_ROOT / "locale" / language / "LC_MESSAGES" / "django.po").read_text(
        encoding="utf-8"
    )
    assert "#, fuzzy" not in catalog


@pytest.mark.parametrize("language", ["fr", "ar"])
def test_participant_catalog_has_no_empty_non_header_translation(language: str) -> None:
    catalog = (PROJECT_ROOT / "locale" / language / "LC_MESSAGES" / "django.po").read_text(
        encoding="utf-8"
    )
    entries = catalog.split("\n\n")
    untranslated = [
        entry
        for entry in entries
        if 'msgid ""' not in entry
        and 'msgstr ""' in entry
        and '\n"' not in entry.split('msgstr ""', 1)[1]
    ]
    assert untranslated == []


@pytest.mark.parametrize("language", ["fr", "ar"])
def test_compiled_catalog_matches_the_source_po_file(language: str) -> None:
    """Phase 2 Prompt 6 §5.L "compiled catalogs match source catalogs": the
    shipped `.mo` must actually reflect the current `.po` -- a translator
    edit committed without recompiling `django.mo` would otherwise silently
    keep serving stale (or missing) translations at runtime forever."""
    locale_dir = PROJECT_ROOT / "locale" / language / "LC_MESSAGES"
    po_entries = _parse_simple_po_entries((locale_dir / "django.po").read_text(encoding="utf-8"))
    assert po_entries, "the .po parser should find at least one simple entry to check"

    with (locale_dir / "django.mo").open("rb") as mo_file:
        translations = gettext.GNUTranslations(mo_file)

    mismatched = {
        msgid: (expected, translations.gettext(msgid))
        for msgid, expected in po_entries.items()
        if translations.gettext(msgid) != expected
    }
    assert mismatched == {}, (
        f"{language}/LC_MESSAGES/django.mo is stale relative to django.po "
        f"for {len(mismatched)} entries -- run compilemessages"
    )
