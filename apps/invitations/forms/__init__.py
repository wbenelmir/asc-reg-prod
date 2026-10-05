"""Organization Workspace forms (Phase 2 Prompt 2)."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.registrations.forms import BootstrapFormMixin


class CampaignForm(BootstrapFormMixin, forms.Form):
    name = forms.CharField(label=_("Campaign name"), max_length=200)
    # Native date-time pickers (UI/UX Completion Gate); the accepted value
    # and its validation are unchanged (ISO 8601 date and time).
    valid_from = forms.DateTimeField(
        label=_("Valid from"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    valid_until = forms.DateTimeField(
        label=_("Valid until"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    capacity = forms.IntegerField(
        label=_("Capacity"),
        required=False,
        min_value=0,
        help_text=_("Leave empty for no limit."),
    )
    language = forms.ChoiceField(
        label=_("Default language"),
        choices=[("en", "English"), ("fr", "Français"), ("ar", "العربية")],
        initial="en",
    )

    def clean(self):
        cleaned = super().clean()
        valid_from = cleaned.get("valid_from")
        valid_until = cleaned.get("valid_until")
        if valid_from and valid_until and valid_until < valid_from:
            raise forms.ValidationError(_("The end date must be after the start date."))
        return cleaned


class DelegationUploadForm(BootstrapFormMixin, forms.Form):
    csv_file = forms.FileField(
        label=_("Delegation CSV file"),
        help_text=_(
            "A .csv file with the columns email, given_names, family_name and, optionally, notes."
        ),
        widget=forms.FileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def clean_csv_file(self):
        uploaded = self.cleaned_data["csv_file"]
        from django.conf import settings

        if uploaded.size is not None and uploaded.size > settings.DELEGATION_CSV_MAX_SIZE_BYTES:
            raise forms.ValidationError(_("The uploaded file is too large."))
        return uploaded


class OnBehalfDraftForm(BootstrapFormMixin, forms.Form):
    intended_email = forms.EmailField(label=_("Participant's email address"))
