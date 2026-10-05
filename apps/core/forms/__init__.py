"""apps.core.forms -- shared, validation-only form building blocks."""

from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError


class ScopedModelChoiceField(forms.ModelChoiceField):
    """A choice among a permission-scoped queryset (UI/UX Completion Gate F8).

    The queryset is the caller's authorized scope, so a value outside it is
    rejected by Django's normal `invalid_choice` check. This subclass also
    maps a malformed value (for example a non-UUID string, which the model
    field itself rejects with a different error that repeats the value) to
    the same generic `invalid_choice` error, so every forged, stale,
    unknown, out-of-scope or malformed id is answered identically and the
    submitted text is never echoed.
    """

    def to_python(self, value):
        try:
            return super().to_python(value)
        except ValidationError as error:
            if getattr(error, "code", None) == "invalid_choice":
                raise
            raise ValidationError(
                self.error_messages["invalid_choice"], code="invalid_choice"
            ) from None


# ---------------------------------------------------------------------------
# UXR-C1 (UXR-F03): choices ordered by their localized label
# ---------------------------------------------------------------------------

#: Arabic letters that sort as their bare form (hamza and madda carriers).
_ARABIC_SORT_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
#: Unicode isolation controls used only for display (see `calling_code_label`).
_DISPLAY_CONTROLS = dict.fromkeys(map(ord, "⁦⁧⁨⁩‎‏"))


def localized_sort_key(label: str) -> str:
    """A deterministic, locale-neutral collation key for a displayed label.

    Standard library only (no ICU): display controls are dropped, the text is
    decomposed (NFKD), combining marks are removed, Arabic hamza/madda alef
    forms fold to bare alef, and the result is case-folded. So "Émirats"
    sorts with E, "Algérie" before "Allemagne", and "إسبانيا" with "ا". It is
    presentation only and never changes a stored value.
    """
    import unicodedata

    text = (label or "").translate(_DISPLAY_CONTROLS).translate(_ARABIC_SORT_FOLD)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().strip()


class LocalizedOrderChoiceIterator(forms.models.ModelChoiceIterator):
    """Yields the queryset's choices sorted by their label in the active
    language (ties broken by primary key), so the native select and the
    enhanced search-and-select list, which reads the native options, share
    one order. Membership and validation still come from the unchanged
    queryset."""

    def __iter__(self):
        if self.field.empty_label is not None:
            yield ("", self.field.empty_label)
        objects = list(self.queryset)
        labelled = [(self.field.label_from_instance(obj), obj) for obj in objects]
        labelled.sort(key=lambda pair: (localized_sort_key(str(pair[0])), str(pair[1].pk)))
        for _label, obj in labelled:
            yield self.choice(obj)


class LocalizedOrderModelChoiceField(forms.ModelChoiceField):
    """A ModelChoiceField whose choices are ordered by localized label (UXR-F03)."""

    iterator = LocalizedOrderChoiceIterator
