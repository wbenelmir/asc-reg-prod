"""Shared form widgets (UX-1: decisions S-01, S-07 and S-08).

Presentation and parsing only. Every widget keeps the server as the single
authority: the field that owns the widget still validates the parsed value,
and a widget never invents, widens or hides a validation rule.
"""

from __future__ import annotations

import datetime

from django import forms
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

from .normalization import normalize_digit_code, normalize_digits

OTP_CODE_LENGTH = 6


class OtpCodeInput(forms.TextInput):
    """The sign-in code: one logical text input, six visual cells (S-01).

    It stays one accessible control (`type=text`, numeric keyboard, one-time
    code autofill, LTR digits). `data-asc-otp` lets the shared script draw
    the six cells with CSS and sanitize typed or pasted text. `maxlength` is
    larger than six on purpose: a pasted code such as "123 456" must reach
    the script (or, with scripting off, the server) intact, and the server
    still requires exactly six ASCII digits after normalization.
    """

    def __init__(self, attrs=None):
        defaults = {
            "inputmode": "numeric",
            "autocomplete": "one-time-code",
            "pattern": "[0-9]{6}",
            "maxlength": "12",
            "spellcheck": "false",
            "autocapitalize": "off",
            "autocorrect": "off",
            "dir": "ltr",
            "autofocus": True,
            "data-asc-otp": str(OTP_CODE_LENGTH),
        }
        defaults.update(attrs or {})
        super().__init__(defaults)


class SearchableSelect(forms.Select):
    """A native select that the shared script upgrades to a search-and-select
    combobox when its list is long (S-07). With scripting off, the native
    select works unchanged, and the server validates the value against the
    same queryset either way."""

    def __init__(self, attrs=None, choices=(), *, threshold: int = 8):
        defaults = {
            "data-asc-combobox": "true",
            "data-combobox-threshold": str(threshold),
            "data-label-toggle": _("Show all choices"),
            "data-label-clear": _("Clear selection"),
            "data-label-empty": _("No matching choices"),
            "data-label-count": _("Choices shown:"),
            "data-label-hint": _("Type to filter the list"),
        }
        defaults.update(attrs or {})
        super().__init__(defaults, choices)


_PART_NAMES = ("day", "month", "year")
_PART_LABELS = {"day": _("Day"), "month": _("Month"), "year": _("Year")}
_PART_MAXLENGTH = {"day": 2, "month": 2, "year": 4}
_PART_WIDTH_CLASS = {
    "day": "asc-date-part-day",
    "month": "asc-date-part-month",
    "year": "asc-date-part-year",
}


class DayMonthYearWidget(forms.Widget):
    """Date entry as three labelled numeric fields: Day, Month, Year (S-08).

    The order is the same in English, French and Arabic. The layout follows
    the page direction, and each part keeps left-to-right digits. The widget
    parses the parts into an ISO `YYYY-MM-DD` string that `forms.DateField`
    validates with `input_formats=["%Y-%m-%d"]`, so it does not depend on the
    browser locale and storage stays ISO. A post that carries only the
    single `<name>` key (an API client or an older form) is passed through
    unchanged.

    Digits typed as Arabic-Indic or Extended Arabic-Indic are converted to
    ASCII (S-02). Anything that is not a plausible date is returned as
    `day/month/year` in the typed order, so it fails the ISO parse and the
    form shows the typed values again next to the error.
    """

    template_name = ""  # rendered by `render()`; no template is involved
    is_day_month_year = True
    input_type = "text"
    needs_multipart_form = False

    def __init__(self, attrs=None, *, autocomplete_prefix: str | None = None):
        super().__init__(attrs)
        self.autocomplete_prefix = autocomplete_prefix

    # -- ids -------------------------------------------------------------------
    def id_for_label(self, id_):
        """The field label points at the first part, so an error link that
        targets `#id_<field>_day` lands on a real control."""
        return f"{id_}_day" if id_ else id_

    # -- reading ---------------------------------------------------------------
    def value_omitted_from_data(self, data, files, name):
        return not any(f"{name}_{part}" in data for part in _PART_NAMES) and name not in data

    def value_from_datadict(self, data, files, name):
        keys = [f"{name}_{part}" for part in _PART_NAMES]
        if not any(key in data for key in keys):
            return data.get(name)
        day, month, year = (normalize_digit_code(data.get(key, "") or "") for key in keys)
        if not (day or month or year):
            return ""
        if (
            day.isascii()
            and month.isascii()
            and year.isascii()
            and day.isdigit()
            and month.isdigit()
            and year.isdigit()
            and 1 <= len(day) <= 2
            and 1 <= len(month) <= 2
            and len(year) == 4
        ):
            return f"{year}-{int(month):02d}-{int(day):02d}"
        return f"{day}/{month}/{year}"

    # -- writing ---------------------------------------------------------------
    @staticmethod
    def _split(value) -> tuple[str, str, str]:
        if isinstance(value, (datetime.date, datetime.datetime)):
            return f"{value.day:02d}", f"{value.month:02d}", f"{value.year:04d}"
        if isinstance(value, str) and value:
            iso = value.split("-")
            if len(iso) == 3 and all(piece.isdigit() for piece in iso):
                return f"{int(iso[2]):02d}", f"{int(iso[1]):02d}", iso[0]
            typed = value.split("/")
            if len(typed) == 3:
                return typed[0], typed[1], typed[2]
        return "", "", ""

    def format_value(self, value):
        return value

    def get_context(self, name, value, attrs):  # pragma: no cover - render() is used
        return super().get_context(name, value, attrs)

    def render(self, name, value, attrs=None, renderer=None):
        attrs = dict(self.build_attrs(self.attrs, attrs))
        base_id = attrs.pop("id", None) or f"id_{name}"
        attrs.pop("autofocus", None)
        css = (attrs.pop("class", "") + " asc-date-part-input").strip()
        described_by = attrs.pop("aria-describedby", "")
        invalid = attrs.pop("aria-invalid", "")
        required = attrs.pop("aria-required", "")
        direction = attrs.pop("dir", "ltr")
        parts = dict(zip(_PART_NAMES, self._split(value), strict=True))
        cells = []
        for part in _PART_NAMES:
            part_id = f"{base_id}_{part}"
            control_attrs = {
                "type": "text",
                "id": part_id,
                "name": f"{name}_{part}",
                "value": parts[part],
                "class": f"{css} {_PART_WIDTH_CLASS[part]}",
                "inputmode": "numeric",
                "maxlength": str(_PART_MAXLENGTH[part]),
                "autocomplete": f"{self.autocomplete_prefix}-{part}"
                if self.autocomplete_prefix
                else "off",
                "dir": direction,
                "spellcheck": "false",
            }
            if described_by:
                control_attrs["aria-describedby"] = described_by
            if invalid:
                control_attrs["aria-invalid"] = invalid
            if required:
                control_attrs["aria-required"] = required
            for key, extra in attrs.items():
                control_attrs.setdefault(key, extra)
            cells.append(
                format_html(
                    '<div class="asc-date-part"><label for="{}" class="asc-date-part-label">'
                    "{}</label><input {}></div>",
                    part_id,
                    _PART_LABELS[part],
                    format_html_join(
                        " ",
                        '{}="{}"',
                        ((key, val) for key, val in control_attrs.items() if val != ""),
                    ),
                )
            )
        return format_html(
            '<div class="asc-date-parts">{}</div>',
            format_html_join("", "{}", ((c,) for c in cells)),
        )


__all__ = [
    "DayMonthYearWidget",
    "OtpCodeInput",
    "SearchableSelect",
    "normalize_digit_code",
    "normalize_digits",
]
