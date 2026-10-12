"""Validation-only forms for accreditation (UI/UX Completion Gate F8).

No business transition happens here: a valid form's `cleaned_data` is
handed to `apps.accreditation.services`, and the view keeps every scoped
permission check it already performed.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AttendanceCategory, BulkAssignmentKind


class BulkPreviewForm(forms.Form):
    """Bulk assignment target selection.

    `reference_object_id` keeps its POST name. Its choices are the active
    objects of each kind that the caller may bulk-assign
    (`apps.accreditation.selectors.bulk_assignable_references`), grouped by
    kind; `clean` also requires the chosen object to belong to the chosen
    kind. A forged, stale, malformed or out-of-scope id therefore fails
    validation with one generic message, never a lookup or a 404.
    """

    kind = forms.ChoiceField(
        choices=[("", _("Select what to assign")), *BulkAssignmentKind.choices],
        label=_("Assignment type"),
        error_messages={
            "required": _("Select what to assign."),
            "invalid_choice": _("Select what to assign."),
        },
    )
    reference_object_id = forms.ChoiceField(
        label=_("Assign"),
        # asc-enhance.js narrows the list to the chosen type's group.
        widget=forms.Select(attrs={"data-group-follows": "id_kind"}),
        error_messages={
            "required": _("Select an item from the list."),
            "invalid_choice": _("Select an item from the list."),
        },
    )
    reason = forms.CharField(max_length=300, required=False, label=_("Reason"))

    def __init__(self, *args, references_by_kind: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.references_by_kind = references_by_kind or {}
        multi_event = (
            len(
                {
                    obj.event_edition_id
                    for objects in self.references_by_kind.values()
                    for obj in objects
                }
            )
            > 1
        )
        grouped: list = [("", _("Select an item"))]
        for kind, label in BulkAssignmentKind.choices:
            objects = self.references_by_kind.get(kind, [])
            if objects:
                grouped.append(
                    (
                        label,
                        [(str(obj.pk), self._label(obj, multi_event)) for obj in objects],
                    )
                )
        self.fields["reference_object_id"].choices = grouped

    @staticmethod
    def _label(obj, multi_event: bool) -> str:
        name = obj.localized_name
        return f"{name} · {obj.event_edition.name}" if multi_event else name

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        reference_id = cleaned.get("reference_object_id")
        if kind and reference_id:
            match = next(
                (
                    obj
                    for obj in self.references_by_kind.get(kind, [])
                    if str(obj.pk) == reference_id
                ),
                None,
            )
            if match is None:
                self.add_error("reference_object_id", _("Select an item of the chosen type."))
            else:
                cleaned["reference_obj"] = match
        return cleaned


# ---------------------------------------------------------------------------
# Attendance entitlements (apps.accreditation.attendance)
# ---------------------------------------------------------------------------

_REASON_HELP = _("Short and factual. Do not include personal data.")


class AttendancePolicyForm(forms.Form):
    """The three conference days (local dates in the event timezone) and the
    opening-day capacity. Validation of order, locks and capacity happens in
    `apps.accreditation.attendance.update_policy`."""

    opening_date = forms.DateField(
        required=False,
        label=_("Opening day"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    second_date = forms.DateField(
        required=False,
        label=_("Second conference day"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    third_date = forms.DateField(
        required=False,
        label=_("Third conference day"),
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    opening_day_capacity = forms.IntegerField(
        required=False,
        min_value=0,
        label=_("Opening-day capacity"),
        help_text=_(
            "The number of approved participants who may attend the opening day. It cannot be "
            "lower than the places already allocated."
        ),
    )
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason for the change"), help_text=_REASON_HELP
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput, required=False)


class AttendanceChangeForm(forms.Form):
    """Classify an earlier approval, or change the attendance days of one."""

    attendance_category = forms.ChoiceField(
        choices=AttendanceCategory.choices,
        widget=forms.RadioSelect,
        label=_("Attendance days"),
        error_messages={"required": _("Choose the attendance days.")},
    )
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )
    expected_entitlement_id = forms.CharField(
        required=False, max_length=36, widget=forms.HiddenInput
    )
    next = forms.CharField(required=False, max_length=500, widget=forms.HiddenInput)


class AttendanceActivationForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class AttendanceDeactivationForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )


# ---------------------------------------------------------------------------
# UPD-07: participant accreditation settings (apps.accreditation.settings_admin)
# ---------------------------------------------------------------------------


def _datetime_field(label):
    return forms.DateTimeField(
        label=label,
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )


class AccreditationSettingForm(forms.Form):
    """One participant role, badge type, access profile or access rule. The
    fields follow the kind; the code is asked only at creation."""

    code = forms.CharField(
        label=_("Code"),
        max_length=64,
        help_text=_(
            "Capital letters, digits, - and _ (for example SPEAKER). It cannot be changed later."
        ),
        widget=forms.TextInput(attrs={"dir": "ltr", "autocomplete": "off", "spellcheck": "false"}),
    )
    name = forms.CharField(label=_("Name (English)"), max_length=200)
    name_fr = forms.CharField(label=_("Name (French)"), max_length=200, required=False)
    name_ar = forms.CharField(
        label=_("Name (Arabic)"),
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"dir": "rtl", "lang": "ar"}),
    )
    participant_visible = forms.BooleanField(
        label=_("Show this badge type to the participant"), required=False
    )
    reentry_policy = forms.ChoiceField(label=_("Re-entry"), required=False)
    access_profile = forms.ModelChoiceField(
        label=_("Access profile (optional)"), queryset=None, required=False
    )
    zone = forms.ModelChoiceField(label=_("Zone (optional)"), queryset=None, required=False)
    gate = forms.ModelChoiceField(label=_("Gate (optional)"), queryset=None, required=False)
    event_type = forms.ChoiceField(label=_("Applies to"), required=False)
    effect = forms.ChoiceField(label=_("Effect"), required=False)
    valid_from = _datetime_field(_("Valid from (optional)"))
    valid_until = _datetime_field(_("Valid until (optional)"))

    _BY_KIND = {
        "role": ("name", "name_fr", "name_ar"),
        "badge-type": ("name", "name_fr", "name_ar", "participant_visible"),
        "access-profile": (
            "name",
            "name_fr",
            "name_ar",
            "reentry_policy",
            "valid_from",
            "valid_until",
        ),
        "access-rule": (
            "name",
            "name_fr",
            "name_ar",
            "access_profile",
            "zone",
            "gate",
            "event_type",
            "effect",
            "valid_from",
            "valid_until",
        ),
    }

    def __init__(self, *args, kind_slug: str, event_edition, creating: bool, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accreditation.models import (
            AccessProfile,
            AccessRuleEffect,
            AccessRuleEventType,
            ReentryPolicy,
        )
        from apps.events.models import Gate, Zone

        self.kind_slug = kind_slug
        keep = set(self._BY_KIND[kind_slug]) | ({"code"} if creating else set())
        for name in list(self.fields):
            if name not in keep:
                del self.fields[name]
        if "reentry_policy" in self.fields:
            self.fields["reentry_policy"].choices = ReentryPolicy.choices
            self.fields["reentry_policy"].required = True
        if "event_type" in self.fields:
            self.fields["event_type"].choices = AccessRuleEventType.choices
            self.fields["event_type"].required = True
            self.fields["effect"].choices = AccessRuleEffect.choices
            self.fields["effect"].required = True
            self.fields["access_profile"].queryset = AccessProfile.objects.filter(
                event_edition=event_edition
            ).order_by("code")
            self.fields["zone"].queryset = Zone.objects.filter(
                venue__event_edition=event_edition
            ).order_by("code")
            self.fields["gate"].queryset = Gate.objects.filter(
                venue__event_edition=event_edition
            ).order_by("code")
            for name in ("access_profile", "zone", "gate"):
                self.fields[name].label_from_instance = lambda obj: f"{obj.code} · {obj.name}"

    def values(self) -> dict:
        """The model values of a valid form (everything except the code)."""
        return {name: self.cleaned_data.get(name) for name in self._BY_KIND[self.kind_slug]}
