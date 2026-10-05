"""Validation-only forms for identity review and correction (IDV-3).

No transition happens here: a valid form's data is handed to
`apps.people.services.identity_review`, which re-checks everything.
"""

from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.forms import LocalizedOrderModelChoiceField
from apps.core.models import selectable_countries
from apps.core.widgets import DayMonthYearWidget, SearchableSelect
from apps.people.services.identity_review import (
    EXCEPTION_REASONS,
    MANUAL_VERIFY_REASONS,
    MAX_NOTE_LENGTH,
    MIN_EXPLANATION_LENGTH,
    NIN_CORRECTION_REASONS,
    RECHECK_REASONS,
    REJECT_REASONS,
    RETURN_ITEMS,
)


def _latin_name(value: str) -> str:
    from apps.core.text_rules import TextRuleError, normalize_latin_name

    try:
        return normalize_latin_name(value)
    except TextRuleError:
        raise forms.ValidationError(
            _("Use Latin letters, exactly as on your identity document.")
        ) from None


def _choices(catalogue: dict) -> list[tuple[str, str]]:
    return list(catalogue.items())


class _CaseForm(forms.Form):
    """The concurrency token and the queue position every action carries."""

    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    after_changed_at = forms.CharField(widget=forms.HiddenInput, required=False, max_length=64)
    after_pk = forms.UUIDField(widget=forms.HiddenInput, required=False)


def _note_field(*, required: bool, label, help_text=""):
    return forms.CharField(
        label=label,
        required=required,
        max_length=MAX_NOTE_LENGTH,
        min_length=MIN_EXPLANATION_LENGTH if required else None,
        help_text=help_text,
        widget=forms.Textarea(attrs={"rows": 2, "dir": "auto"}),
    )


class VerifyIdentityForm(_CaseForm):
    evidence_document = forms.ChoiceField(label=_("Evidence reviewed"), widget=forms.RadioSelect)
    reason_code = forms.ChoiceField(label=_("Reason"), choices=_choices(MANUAL_VERIFY_REASONS))
    note = _note_field(required=False, label=_("Internal note (optional)"))

    def __init__(self, *args, evidence_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence_document"].choices = list(evidence_choices)
        if len(self.fields["evidence_document"].choices) == 1:
            self.fields["evidence_document"].initial = self.fields["evidence_document"].choices[0][
                0
            ]


class ExceptionVerifyForm(VerifyIdentityForm):
    reason_code = forms.ChoiceField(
        label=_("Exception reason"), choices=_choices(EXCEPTION_REASONS)
    )
    note = _note_field(
        required=True,
        label=_("Explanation (required)"),
        help_text=_("Why the NIN route cannot be used, and which document confirms the identity."),
    )


class ReturnForCorrectionForm(_CaseForm):
    items = forms.MultipleChoiceField(
        label=_("Ask the participant to"), widget=forms.CheckboxSelectMultiple
    )
    note = _note_field(required=False, label=_("Internal note (optional, never shown)"))

    def __init__(self, *args, route: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["items"].choices = _choices(RETURN_ITEMS[route])


class CorrectNinForm(_CaseForm):
    new_nin = forms.CharField(
        label=_("NIN to check (18 digits)"),
        max_length=40,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "spellcheck": "false"}
        ),
        help_text=_("Enter the same NIN to recheck it without a change."),
    )
    reason_code = forms.ChoiceField(
        label=_("Reason"),
        choices=[*_choices(NIN_CORRECTION_REASONS), *_choices(RECHECK_REASONS)],
    )
    evidence_document = forms.ChoiceField(
        label=_("Evidence reviewed (required for a changed NIN)"),
        required=False,
        widget=forms.RadioSelect,
    )
    confirmed = forms.BooleanField(
        label=_("I confirm that the corrected NIN matches the identity document."),
        required=False,
    )
    note = _note_field(required=False, label=_("Internal note (optional)"))

    def __init__(self, *args, evidence_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence_document"].choices = list(evidence_choices)


class RejectIdentityForm(_CaseForm):
    reason_code = forms.ChoiceField(label=_("Rejection reason"), choices=_choices(REJECT_REASONS))
    note = _note_field(required=True, label=_("Explanation (required)"))
    confirmed = forms.BooleanField(
        label=_("I understand that this is a final identity rejection."), required=True
    )


class GrantNinExemptionForm(forms.Form):
    """Staff grant of the NIN exemption route for one draft (IDV-Q3)."""

    reason_code = forms.ChoiceField(label=_("Reason"))
    explanation = _note_field(
        required=True,
        label=_("Explanation (required)"),
        help_text=_(
            "Why the participant cannot supply a usable NIN, and how this was established. "
            "Internal; never shown to the participant."
        ),
    )
    confirmed = forms.BooleanField(
        label=_(
            "I confirm that this Algerian participant cannot supply a usable NIN and that the "
            "identity will be verified manually from an official Algerian document."
        ),
        required=True,
    )

    def __init__(self, *args, **kwargs):
        from apps.people.models import NinExemptionReason

        super().__init__(*args, **kwargs)
        self.fields["reason_code"].choices = NinExemptionReason.choices


class RevokeNinExemptionForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    note = _note_field(required=True, label=_("Why the exemption is withdrawn (required)"))


class IdentityCorrectionForm(forms.Form):
    """The participant's correction of a returned identity (same registration).

    The same controls and rules as the wizard's identity step (Latin names,
    Day/Month/Year date entry, searchable country list), so a correction looks
    and validates like the original entry. Nationality is not editable here:
    the identity route follows it.
    """

    expected_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    given_names = forms.CharField(
        label=_("Given name(s)"),
        max_length=200,
        widget=forms.TextInput(attrs={"dir": "auto", "autocomplete": "given-name"}),
    )
    family_name = forms.CharField(
        label=_("Family name"),
        max_length=200,
        widget=forms.TextInput(attrs={"dir": "auto", "autocomplete": "family-name"}),
    )
    date_of_birth = forms.DateField(
        label=_("Date of birth"),
        input_formats=["%Y-%m-%d"],
        widget=DayMonthYearWidget(autocomplete_prefix="bday"),
        error_messages={"invalid": _("Enter a real date.")},
    )
    nin_value = forms.CharField(
        label=_("National identity number (18 digits)"),
        max_length=40,
        required=False,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "spellcheck": "false"}
        ),
    )
    passport_number = forms.CharField(label=_("Passport number"), max_length=40, required=False)
    passport_country_code = LocalizedOrderModelChoiceField(
        label=_("Issuing country"),
        queryset=selectable_countries(),
        required=False,
        widget=SearchableSelect,
    )
    passport_expires_at = forms.DateField(
        label=_("Passport expiry date"),
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=DayMonthYearWidget(),
        error_messages={"invalid": _("Enter a real date.")},
    )
    national_id_card = forms.ImageField(
        label=_("National identity card"),
        required=False,
        help_text=_("A photo of the side showing your NIN, names and date of birth. JPEG or PNG."),
    )
    passport_identity_page = forms.ImageField(
        label=_("Passport identity page"),
        required=False,
        help_text=_("A photo or scan of the passport identity page only. JPEG or PNG."),
    )
    # IDV-Q3: the declared Algerian document of the NIN exemption route.
    document_number = forms.CharField(
        label=_("Document number"),
        max_length=40,
        required=False,
        help_text=_("The number printed on the document itself. Never a NIN."),
    )
    document_expires_at = forms.DateField(
        label=_("Document expiry date"),
        required=False,
        input_formats=["%Y-%m-%d"],
        widget=DayMonthYearWidget(),
        error_messages={"invalid": _("Enter a real date.")},
    )
    document_image = forms.ImageField(
        label=_("Photo of the document"),
        required=False,
        help_text=_("The side showing your names, date of birth and the number. JPEG or PNG."),
    )

    _PASSPORT_FIELDS = ("passport_number", "passport_country_code", "passport_expires_at")
    _DOCUMENT_FIELDS = ("document_number", "document_expires_at", "document_image")

    def __init__(self, *args, route: str, document_kind: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.route = route
        self.document_kind = document_kind
        self.fields["passport_country_code"].label_from_instance = lambda c: c.localized_name
        if route == "NIN":
            self.fields["nin_value"].required = True
            for name in (*self._PASSPORT_FIELDS, *self._DOCUMENT_FIELDS):
                del self.fields[name]
        elif route == "NIN_EXEMPTION":
            for name in (
                "nin_value",
                "national_id_card",
                "passport_identity_page",
                *self._PASSPORT_FIELDS,
            ):
                del self.fields[name]
            self.fields["document_number"].required = True
            self.fields["document_expires_at"].required = document_kind == "PASSPORT"
        else:
            del self.fields["nin_value"]
            del self.fields["national_id_card"]
            for name in self._DOCUMENT_FIELDS:
                del self.fields[name]
            for name in self._PASSPORT_FIELDS:
                self.fields[name].required = True

    def clean_given_names(self):
        return _latin_name(self.cleaned_data["given_names"])

    def clean_family_name(self):
        return _latin_name(self.cleaned_data["family_name"])

    def clean_passport_number(self):
        return "".join((self.cleaned_data.get("passport_number") or "").split()).upper()

    def clean_date_of_birth(self):
        value = self.cleaned_data["date_of_birth"]
        if value >= timezone.now().date():
            raise forms.ValidationError(_("Enter a date of birth in the past."))
        return value
