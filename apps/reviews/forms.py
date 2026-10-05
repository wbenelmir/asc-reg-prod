"""Validation-only forms for the review domain (Phase 2 Prompt 3 §4).

No business transition happens here -- a valid form's `cleaned_data` is
always handed to `apps.reviews.services`, never applied directly.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.display_labels import CODE_LABELS
from apps.core.forms import ScopedModelChoiceField

from .models import (
    ChecklistResultValue,
    InformationRequestPurpose,
    ReviewQueueCode,
)
from .services import ALLOWED_REQUEST_FIELD_CODES

# Lazily translated field names (UI/UX Completion Gate: never the raw field
# code); the submitted values are unchanged.
_FIELD_CODE_CHOICES = [
    (code, CODE_LABELS["request_field"].get(code, code))
    for code in sorted(ALLOWED_REQUEST_FIELD_CODES)
]


def _text_in(language: str, *, rows: int | None = None):
    """A text widget declaring the language (and direction) of its content,
    so an Arabic message is typed right-to-left even on an English page."""
    attrs = {"lang": language, "dir": "rtl" if language == "ar" else "ltr"}
    if rows is None:
        return forms.TextInput(attrs=attrs)
    return forms.Textarea(attrs={**attrs, "rows": rows})


_DOCUMENT_TYPE_CHOICES = [("REQUESTED_EVIDENCE", _("Requested evidence document"))]


class InformationRequestForm(forms.Form):
    purpose = forms.ChoiceField(choices=InformationRequestPurpose.choices, label=_("Purpose"))
    message_en = forms.CharField(
        max_length=2000, widget=_text_in("en", rows=3), label=_("Message (English)")
    )
    message_fr = forms.CharField(
        max_length=2000, required=False, widget=_text_in("fr", rows=3), label=_("Message (French)")
    )
    message_ar = forms.CharField(
        max_length=2000, required=False, widget=_text_in("ar", rows=3), label=_("Message (Arabic)")
    )
    deadline_at = forms.DateTimeField(
        required=False,
        label=_("Deadline"),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )


class RequestItemForm(forms.Form):
    # A blank first choice: an untouched extra item form is then "unchanged"
    # and skipped, instead of being validated as a half-filled item.
    kind = forms.ChoiceField(
        choices=[
            ("", _("Select a kind")),
            ("FIELD_CORRECTION", _("Field correction")),
            ("DOCUMENT_UPLOAD", _("Document upload")),
            ("CLARIFICATION", _("Clarification")),
        ],
        label=_("Kind"),
    )
    field_code = forms.ChoiceField(
        choices=[("", _("Select a field")), *_FIELD_CODE_CHOICES],
        required=False,
        label=_("Field"),
    )
    document_type = forms.ChoiceField(
        choices=[("", _("Select a document type")), *_DOCUMENT_TYPE_CHOICES],
        required=False,
        label=_("Document type"),
    )
    is_required = forms.BooleanField(required=False, initial=True, label=_("Required"))
    instructions_en = forms.CharField(
        max_length=500, required=False, widget=_text_in("en"), label=_("Instructions (English)")
    )
    instructions_fr = forms.CharField(
        max_length=500, required=False, widget=_text_in("fr"), label=_("Instructions (French)")
    )
    instructions_ar = forms.CharField(
        max_length=500, required=False, widget=_text_in("ar"), label=_("Instructions (Arabic)")
    )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if kind in ("FIELD_CORRECTION", "CLARIFICATION") and not cleaned.get("field_code"):
            self.add_error("field_code", _("A field is required for this item kind."))
        if kind == "DOCUMENT_UPLOAD" and not cleaned.get("document_type"):
            self.add_error("document_type", _("A document type is required for this item kind."))
        return cleaned


class _RequestItemBaseFormSet(forms.BaseFormSet):
    # A project message instead of Django's generic "Please submit at least
    # 1 form.", so the error is meaningful and translated.
    default_error_messages = {"too_few_forms": _("Add at least one requested item.")}


RequestItemFormSet = forms.formset_factory(
    RequestItemForm,
    formset=_RequestItemBaseFormSet,
    extra=1,
    min_num=1,
    validate_min=True,
)


class ChecklistResultForm(forms.Form):
    item_code = forms.CharField(max_length=64, label=_("Checklist item"))
    result = forms.ChoiceField(choices=ChecklistResultValue.choices, label=_("Result"))
    notes = forms.CharField(
        max_length=1000, required=False, widget=forms.Textarea, label=_("Notes")
    )


class InternalNoteForm(forms.Form):
    note_text = forms.CharField(max_length=4000, widget=forms.Textarea, label=_("Internal note"))


class AssigneeChoiceField(ScopedModelChoiceField):
    """A permission-scoped person choice (UI/UX Completion Gate F8).

    Shows the person's display name; the email is added only when two
    people would otherwise look the same (or a person has no display name),
    so staff can tell them apart without every option exposing an address."""

    def label_from_instance(self, user) -> str:
        if getattr(self, "_ambiguous_names", None) is None:
            names = [(u.display_name or "").strip().casefold() for u in self.queryset]
            self._ambiguous_names = {n for n in names if names.count(n) > 1}
        name = (user.display_name or "").strip()
        if not name or name == user.email_normalized:
            return user.email_normalized
        if name.casefold() in self._ambiguous_names:
            return f"{name} · {user.email_normalized}"
        return name


class AssignmentForm(forms.Form):
    """`assigned_user_id` keeps its POST name; its choices are exactly the
    people `apps.reviews.selectors.eligible_review_assignees` allows for the
    case, so a forged, stale or out-of-scope id fails validation."""

    assigned_user_id = AssigneeChoiceField(
        queryset=None,
        label=_("Assign to"),
        empty_label=_("Select a person"),
        help_text=_("Only people who may open this review case are listed."),
        error_messages={
            "required": _("Select a person to assign this case to."),
            "invalid_choice": _("Select a person from the list."),
        },
    )
    reason = forms.CharField(
        max_length=300, required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Reason")
    )

    def __init__(self, *args, assignees=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import OperationalUser

        self.fields["assigned_user_id"].queryset = (
            assignees if assignees is not None else OperationalUser.objects.none()
        )


class DuplicateCandidateChoiceField(ScopedModelChoiceField):
    """A candidate identity record, labelled by type, issuing country and
    its masked value only: never the raw identifier, never a person's name."""

    def label_from_instance(self, identifier) -> str:
        return " · ".join(
            (
                str(identifier.get_identifier_type_display()),
                identifier.country_code_id,
                identifier.masked_value,
            )
        )


class DuplicateResolutionForm(forms.Form):
    outcome = forms.ChoiceField(label=_("Outcome"))
    candidate_identifier_id = DuplicateCandidateChoiceField(
        queryset=None,
        required=False,
        label=_("Matching record"),
        empty_label=_("No specific record"),
        help_text=_("Optional. Only the candidate records found for this case are listed."),
        error_messages={"invalid_choice": _("Select a matching record from the list.")},
    )
    reason = forms.CharField(
        max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}), label=_("Reason")
    )

    def __init__(self, *args, outcome_choices=None, candidates=None, **kwargs):
        super().__init__(*args, **kwargs)
        if outcome_choices is not None:
            self.fields["outcome"].choices = outcome_choices
        from apps.people.models import IdentityIdentifier

        self.fields["candidate_identifier_id"].queryset = (
            candidates if candidates is not None else IdentityIdentifier.objects.none()
        )


class NotApprovedDecisionForm(forms.Form):
    internal_reason_code = forms.CharField(max_length=64, label=_("Internal reason code"))
    internal_note = forms.CharField(
        max_length=2000, required=False, widget=forms.Textarea, label=_("Internal note")
    )
    participant_reason_code = forms.CharField(
        max_length=64, required=False, label=_("Participant-facing reason code")
    )


class ApprovedDecisionForm(forms.Form):
    """Bounded transport input for the direct APPROVED command.

    Approval has no operator-authored or participant-facing reason field: the
    service records its fixed ``ELIGIBLE`` internal reason after verifying the
    three required assignments. Requiring the rendered version prevents a
    caller from omitting it and silently falling back to the latest database
    version, which would defeat the optimistic-concurrency contract.
    """

    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class ReasonForm(forms.Form):
    """Shared bounded-reason form for reopen/operational-cancellation."""

    reason = forms.CharField(max_length=500, widget=forms.Textarea, label=_("Reason"))


class QueueFilterForm(forms.Form):
    """Bounded, safe filters for the operations intake queue (Phase 2 Prompt 3 §6)."""

    q = forms.CharField(max_length=200, required=False, label=_("Search"))
    public_status = forms.CharField(max_length=32, required=False)
    internal_status = forms.CharField(max_length=32, required=False)
    queue_code = forms.ChoiceField(
        choices=[("", "")] + list(ReviewQueueCode.choices), required=False
    )
    case_type = forms.CharField(max_length=24, required=False)
    page = forms.IntegerField(required=False, min_value=1)
