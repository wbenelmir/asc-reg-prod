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
