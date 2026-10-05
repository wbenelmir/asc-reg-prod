"""Validation-only forms for accreditation (UI/UX Completion Gate F8).

No business transition happens here: a valid form's `cleaned_data` is
handed to `apps.accreditation.services`, and the view keeps every scoped
permission check it already performed.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import BulkAssignmentKind


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
