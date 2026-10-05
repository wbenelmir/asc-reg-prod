"""Universal registration wizard forms (AF-REG-02..AF-REG-06).

No field here selects a Participant Role, Badge Type or Access Profile
(fixed product rule; UI/UX §6.7: "Omit all internal role, Badge
Type and Access Profile values").
"""

from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.forms import LocalizedOrderModelChoiceField
from apps.core.models import Country, Sector, selectable_countries
from apps.core.text_rules import (
    TextRuleError,
    normalize_latin_name,
    normalize_latin_text,
    normalize_nin,
    normalize_optional_url,
)
from apps.core.widgets import DayMonthYearWidget, SearchableSelect
from apps.organizations.models import OrganizationType

IDENTITY_PATH_CHOICES = [
    ("NIN", _("Algerian national identity number (NIN)")),
    ("PASSPORT", _("Foreign passport")),
]
#: Owner decision IDV-Q3: offered ONLY while the registration team holds an
#: active NIN exemption for this draft.
NIN_EXEMPTION_PATH_CHOICE = (
    "NIN_EXEMPTION",
    _("Another official Algerian document (no NIN)"),
)
EXEMPTION_DOCUMENT_KIND_CHOICES = [
    ("NATIONAL_ID_CARD", _("Algerian national identity card")),
    ("PASSPORT", _("Algerian passport")),
]
#: The evidence document type each declared document kind needs.
EXEMPTION_EVIDENCE_TYPES = {
    "NATIONAL_ID_CARD": "NATIONAL_ID_CARD",
    "PASSPORT": "PASSPORT_IDENTITY_PAGE",
}

ALGERIA_COUNTRY_CODE = "DZ"


class BootstrapFormMixin:
    """Adds Bootstrap 5 form-control/form-select classes and wires `aria-describedby`
    to each field's help text, without changing validation behavior.

    Deliberately never touches `self.errors` here: that forces an immediate
    `full_clean()` (and therefore `self.clean()`) at the end of `__init__`,
    before a subclass's OWN `__init__` (further down the MRO chain, whose
    `super().__init__()` call lands here) has had a chance to set any
    instance state its `clean()` depends on. Per-field error text is still
    rendered next to each field by `components/field.html`, and the
    error summary links to every invalid field by id -- both satisfy WCAG
    2.2 error identification without this coupling.
    """

    # Fields safe to restore into this form after a language-switch reload
    # (Prompt 5 correction pass §1): an explicit, per-form allowlist, never
    # inferred from "every field that isn't excluded". Empty by default --
    # a subclass must opt a field in by name. Authentication, OTP, and
    # operational forms never set this, so they are never eligible no
    # matter what fields they contain.
    language_preserve_fields: frozenset[str] = frozenset()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            # A readable, translated prompt instead of Django's "---------"
            # (UI/UX Completion Gate, Checkpoint 1). Label text only: the
            # empty option still submits "" and validation is unchanged.
            if isinstance(field, forms.ModelChoiceField) and field.empty_label == "---------":
                field.empty_label = _("Select…")
            widget = field.widget
            existing = widget.attrs.get("class", "")
            # RadioSelect (and its subclass CheckboxSelectMultiple) renders one
            # input per option, so each option is a check input -- never a
            # `form-control` text box (UI/UX Completion Gate F7).
            if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
                widget.attrs["class"] = (existing + " form-check-input").strip()
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs["class"] = (existing + " form-select").strip()
            else:
                widget.attrs["class"] = (existing + " form-control").strip()
            if field.help_text:
                widget.attrs["aria-describedby"] = f"id_{name}_help"
            if field.required:
                widget.attrs["aria-required"] = "true"
            if name in self.language_preserve_fields:
                widget.attrs["data-language-preserve-field"] = "true"

    def add_error(self, field, error):
        """Also set `aria-invalid="true"` on the offending widget (WCAG 2.2 §4.1.2/§3.3.1) --
        applied here rather than in the template so it is always in sync with what
        `self.errors` actually reports, including non-field cross-validation in `clean()`."""
        super().add_error(field, error)
        if field is not None and field in self.fields:
            widget = self.fields[field].widget
            widget.attrs["aria-invalid"] = "true"
            error_id = f"id_{field}_error"
            described_by = widget.attrs.get("aria-describedby", "").split()
            if error_id not in described_by:
                described_by.append(error_id)
            widget.attrs["aria-describedby"] = " ".join(described_by)


def _country_label(country: Country) -> str:
    """Localized Country label with an explicit English fallback, never the raw
    code (Prompt 5 correction pass §5)."""
    return country.localized_name


def _sector_label(sector: Sector) -> str:
    """Localized Sector label with an explicit English fallback, never the raw
    code (Prompt 5 correction pass §5)."""
    return sector.localized_name


ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES = {"image/jpeg", "image/png"}

_NAME_MESSAGES = {
    "required": _("This field is required."),
    "too_long": _("Use 100 characters or fewer."),
    "not_latin": _("Use Latin letters only (accents are allowed)."),
}
_TEXT_MESSAGES = {
    "required": _("This field is required."),
    "too_long": _("This text is too long."),
    "not_latin": _("Use Latin letters, digits and common punctuation only (accents are allowed)."),
}
_URL_MESSAGE = _("Enter a valid web address, for example https://www.example.org.")


def _name_or_error(value: str) -> str:
    try:
        return normalize_latin_name(value)
    except TextRuleError as error:
        raise forms.ValidationError(_NAME_MESSAGES[error.code], code=error.code) from None


def _text_or_error(value: str, *, max_length: int, required: bool) -> str:
    if not required and not (value or "").strip():
        return ""
    try:
        return normalize_latin_text(value, max_length=max_length)
    except TextRuleError as error:
        raise forms.ValidationError(_TEXT_MESSAGES[error.code], code=error.code) from None


def _url_or_error(value: str) -> str:
    try:
        return normalize_optional_url(value)
    except TextRuleError as error:
        raise forms.ValidationError(_URL_MESSAGE, code=error.code) from None


_AGE_MESSAGES = {
    "future": _("Enter a date of birth in the past."),
    "too_young": _("You must be at least %(age)s years old on %(date)s to register."),
    "too_old": _("Enter a real date of birth."),
    "no_event_date": _("Registration is not available: the event date is not configured."),
    "missing": _("Enter your date of birth."),
}


class IdentityStepForm(BootstrapFormMixin, forms.Form):
    # NIN/passport fields are deliberately never opted in here, even though
    # this is the same form -- the field-level allowlist, not the field's
    # position in the form, decides eligibility (Prompt 5 correction pass §1).
    language_preserve_fields = frozenset(
        {"given_names", "family_name", "date_of_birth", "nationality_code", "country_of_residence"}
    )

    # UX-2 (M11, D-02): Latin letters in every language, accents allowed; no
    # transliteration. The stored value is NFC with single spaces.
    given_names = forms.CharField(
        label=_("Given name(s)"),
        max_length=200,
        help_text=_(
            "In Latin letters, exactly as in your passport or on the Latin side of your "
            "identity card."
        ),
        widget=forms.TextInput(
            attrs={"placeholder": _("e.g. Amina"), "dir": "auto", "autocomplete": "given-name"}
        ),
    )
    family_name = forms.CharField(
        label=_("Family name"),
        max_length=200,
        help_text=_(
            "In Latin letters, exactly as in your passport or on the Latin side of your "
            "identity card."
        ),
        widget=forms.TextInput(
            attrs={"placeholder": _("e.g. Benali"), "dir": "auto", "autocomplete": "family-name"}
        ),
    )
    date_of_birth = forms.DateField(
        label=_("Date of birth"),
        widget=DayMonthYearWidget(autocomplete_prefix="bday"),
        input_formats=["%Y-%m-%d"],
        help_text=_("Day, month and year, for example 07 03 1990."),
        error_messages={"invalid": _("Enter a real date.")},
    )
    nationality_code = LocalizedOrderModelChoiceField(
        label=_("Nationality"),
        queryset=selectable_countries(),
        widget=SearchableSelect,
    )
    country_of_residence = LocalizedOrderModelChoiceField(
        label=_("Country of residence"),
        queryset=selectable_countries(),
        widget=SearchableSelect,
    )
    identity_path = forms.ChoiceField(
        label=_("Identity document"), choices=IDENTITY_PATH_CHOICES, widget=forms.RadioSelect
    )
    nin_value = forms.CharField(
        label=_("National identity number (18 digits)"),
        # Longer than 18 so a pasted number with spaces or hyphens arrives
        # whole; the server keeps exactly 18 digits (S-03).
        max_length=40,
        required=False,
        help_text=_("18 digits, as printed on your national identity card."),
        widget=forms.TextInput(
            attrs={
                "placeholder": _("18 digits"),
                "inputmode": "numeric",
                "autocomplete": "off",
                "spellcheck": "false",
                "data-asc-digit-counter": "18",
                "data-complete-text": _("18 of 18 digits entered."),
                "data-over-text": _("More than 18 digits entered."),
            }
        ),
    )
    passport_number = forms.CharField(
        label=_("Passport number"),
        max_length=40,
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("e.g. AB1234567"),
                "autocomplete": "off",
                "spellcheck": "false",
                "autocapitalize": "characters",
            }
        ),
    )
    passport_country_code = LocalizedOrderModelChoiceField(
        label=_("Issuing country"),
        queryset=selectable_countries(),
        required=False,
        widget=SearchableSelect,
    )
    passport_expires_at = forms.DateField(
        label=_("Passport expiry date"),
        required=False,
        widget=DayMonthYearWidget(),
        input_formats=["%Y-%m-%d"],
        help_text=_("Day, month and year, for example 07 03 2032."),
        error_messages={"invalid": _("Enter a real date.")},
    )
    # IDV-Q3: the documentary route. Removed in __init__ unless an exemption
    # is active for this draft, so nobody else ever sees or submits it.
    exemption_document_kind = forms.ChoiceField(
        label=_("Document"),
        choices=EXEMPTION_DOCUMENT_KIND_CHOICES,
        required=False,
        widget=forms.RadioSelect,
    )
    exemption_document_number = forms.CharField(
        label=_("Document number"),
        max_length=40,
        required=False,
        help_text=_("The number printed on the document itself. Never a NIN."),
        widget=forms.TextInput(
            attrs={"autocomplete": "off", "spellcheck": "false", "autocapitalize": "characters"}
        ),
    )
    exemption_document_expires_at = forms.DateField(
        label=_("Document expiry date"),
        required=False,
        widget=DayMonthYearWidget(),
        input_formats=["%Y-%m-%d"],
        help_text=_("Required for a passport. Day, month and year, for example 07 03 2032."),
        error_messages={"invalid": _("Enter a real date.")},
    )
    exemption_document_image = forms.ImageField(
        label=_("Photo of the document"),
        required=False,
        help_text=_(
            "A photo or scan of the side showing your names, date of birth and the document "
            "number -- JPEG or PNG. Uploading a new file replaces any previous one."
        ),
    )

    def __init__(
        self,
        *args,
        event_edition=None,
        has_passport_identity_page: bool = False,
        nin_exemption_active: bool = False,
        exemption_evidence_on_file=(),
        **kwargs,
    ):
        # The event whose age rule applies (D-09); set before super().__init__.
        self.event_edition = event_edition
        # IDV-3 (A13-02): whether a clean identity page is already on file, so
        # a re-save of the step does not ask for it again.
        self.has_passport_identity_page = has_passport_identity_page
        # IDV-Q3: whether staff hold an ACTIVE NIN exemption for this draft,
        # and which reviewable evidence types are already on file.
        self.nin_exemption_active = nin_exemption_active
        self.exemption_evidence_on_file = set(exemption_evidence_on_file)
        super().__init__(*args, **kwargs)
        if nin_exemption_active:
            self.fields["identity_path"].choices = [
                *IDENTITY_PATH_CHOICES,
                NIN_EXEMPTION_PATH_CHOICE,
            ]
        else:
            for name in (
                "exemption_document_kind",
                "exemption_document_number",
                "exemption_document_expires_at",
                "exemption_document_image",
            ):
                del self.fields[name]
        self.fields["nationality_code"].label_from_instance = _country_label
        self.fields["country_of_residence"].label_from_instance = _country_label
        self.fields["passport_country_code"].label_from_instance = _country_label
        # IDV-3 (amendment A-13, IDV-08): the identity page is required from every
        # foreign participant, so the field is always part of the passport path.
        # The former per-event switch no longer applies. Requiredness is decided
        # in `clean()`, because it depends on the path and on a file on record.
        self.fields["passport_identity_page"] = forms.ImageField(
            label=_("Passport identity page"),
            required=False,
            help_text=_(
                "Required. A photo or scan of the passport identity page only -- JPEG or PNG, "
                "up to the size shown below. Never a full passport, visa, or other document. "
                "Uploading a new file replaces any previous one."
            ),
        )

    def clean_given_names(self):
        return _name_or_error(self.cleaned_data["given_names"])

    def clean_family_name(self):
        return _name_or_error(self.cleaned_data["family_name"])

    def clean_passport_number(self):
        return "".join((self.cleaned_data.get("passport_number") or "").split()).upper()

    def clean_date_of_birth(self):
        value = self.cleaned_data["date_of_birth"]
        if value >= timezone.now().date():
            raise forms.ValidationError(_("Enter a date of birth in the past."))
        if self.event_edition is not None:
            from zoneinfo import ZoneInfo

            from django.utils.formats import date_format

            from apps.registrations.services import age_problem

            problem = age_problem(value, self.event_edition)
            if problem is not None:
                reference = self.event_edition.starts_at.astimezone(
                    ZoneInfo(self.event_edition.timezone)
                ).date()
                raise forms.ValidationError(
                    _AGE_MESSAGES[problem]
                    % {
                        "age": self.event_edition.minimum_participant_age,
                        "date": date_format(reference, "DATE_FORMAT"),
                    },
                    code=problem,
                )
        return value

    def clean(self):
        cleaned = super().clean()
        path = cleaned.get("identity_path")
        nationality = cleaned.get("nationality_code")
        if path == "NIN":
            if nationality is not None and nationality.pk != ALGERIA_COUNTRY_CODE:
                self.add_error(
                    "identity_path",
                    _("The NIN path is only available for Algerian nationals."),
                )
            try:
                cleaned["nin_value"] = normalize_nin(cleaned.get("nin_value") or "")
            except TextRuleError:
                self.add_error("nin_value", _("Enter exactly 18 digits."))
            if cleaned.get("passport_identity_page"):
                # Never requested on the NIN path (Prompt 5 correction pass
                # §2/§6) -- a value here can only come from a hidden/
                # disabled control being forced or a direct POST bypassing
                # the UI; reject rather than silently accept it. This check
                # MUST live inside the `path == "NIN"` branch itself: an
                # `elif path == "NIN" and ...` positioned after this branch
                # is unreachable dead code, since the `if path == "NIN":`
                # above it already consumes every NIN-path request (a real
                # bug found and fixed in this correction pass).
                self.add_error(
                    "passport_identity_page",
                    _("The passport identity page is only requested on the passport path."),
                )
        elif path == "PASSPORT":
            if nationality is not None and nationality.pk == ALGERIA_COUNTRY_CODE:
                # A13-01 (SCOPE-01): Algerian nationals use the NIN path; there is
                # no self-service passport route for them.
                self.add_error(
                    "identity_path",
                    _("Algerian nationals use the national identity number path."),
                )
            if not cleaned.get("passport_identity_page") and not self.has_passport_identity_page:
                self.add_error(
                    "passport_identity_page",
                    _("Upload a photo or scan of the passport identity page."),
                )
            if not (cleaned.get("passport_number") or "").strip():
                self.add_error("passport_number", _("Enter your passport number."))
            if not cleaned.get("passport_country_code"):
                self.add_error("passport_country_code", _("Select the issuing country."))
            expires_at = cleaned.get("passport_expires_at")
            if not expires_at:
                self.add_error("passport_expires_at", _("Enter the passport expiry date."))
            elif expires_at <= timezone.now().date():
                self.add_error(
                    "passport_expires_at", _("Enter a passport expiry date in the future.")
                )
        elif path == "NIN_EXEMPTION" and self.nin_exemption_active:
            self._clean_exemption_path(cleaned, nationality)
        photo = cleaned.get("passport_identity_page")
        if photo is not None and photo.content_type not in (
            ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES
        ):
            self.add_error("passport_identity_page", _("Upload a JPEG or PNG image."))
        return cleaned

    def _clean_exemption_path(self, cleaned, nationality) -> None:
        """IDV-Q3: the documentary route's own rules (the service checks
        them again, with the grant, under the registration lock)."""
        from django.core.exceptions import ValidationError

        from apps.people.services.nin_exemption import normalize_document_number

        if nationality is not None and nationality.pk != ALGERIA_COUNTRY_CODE:
            self.add_error("identity_path", _("This route is only for Algerian nationals."))
        if cleaned.get("passport_identity_page"):
            self.add_error(
                "passport_identity_page",
                _("The passport identity page is only requested on the passport path."),
            )
        kind = cleaned.get("exemption_document_kind")
        if not kind:
            self.add_error("exemption_document_kind", _("Choose the document."))
        try:
            cleaned["exemption_document_number"] = normalize_document_number(
                cleaned.get("exemption_document_number") or ""
            )
        except ValidationError:
            self.add_error(
                "exemption_document_number",
                _("Enter the document number as printed, in Latin letters and digits."),
            )
        expires_at = cleaned.get("exemption_document_expires_at")
        if kind == "PASSPORT" and not expires_at:
            self.add_error("exemption_document_expires_at", _("Enter the passport expiry date."))
        elif expires_at and expires_at <= timezone.now().date():
            self.add_error(
                "exemption_document_expires_at",
                _("Enter an expiry date in the future."),
            )
        image = cleaned.get("exemption_document_image")
        if image is None and EXEMPTION_EVIDENCE_TYPES.get(kind) not in (
            self.exemption_evidence_on_file
        ):
            self.add_error("exemption_document_image", _("Upload a photo or scan of the document."))
        if image is not None and image.content_type not in (
            ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES
        ):
            self.add_error("exemption_document_image", _("Upload a JPEG or PNG image."))


class ContactStepForm(BootstrapFormMixin, forms.Form):
    mobile_country_code = LocalizedOrderModelChoiceField(
        label=_("Country calling code"),
        queryset=selectable_countries(),
        required=False,
        widget=SearchableSelect,
    )
    mobile_number = forms.CharField(
        label=_("Mobile number"),
        max_length=30,
        required=False,
        help_text=_(
            "Do not type the country code; it is added from your selection. You can also paste "
            "a full number starting with +."
        ),
        widget=forms.TextInput(
            attrs={
                "inputmode": "tel",
                "autocomplete": "tel-national",
                "spellcheck": "false",
            }
        ),
    )

    def __init__(self, *args, mobile_required: bool = True, **kwargs):
        # Set BEFORE calling super().__init__(): BootstrapFormMixin's own
        # __init__ (next in the MRO) reads `self.errors`, which forces
        # `full_clean()` -> `self.clean()` immediately -- `clean()` below
        # depends on `self.mobile_required` already being set.
        self.mobile_required = mobile_required
        super().__init__(*args, **kwargs)
        import phonenumbers

        # The approved catalog (C-01) includes territories without a telephone
        # calling code (for example AQ, BV, HM); no number can be validated for
        # them, so this one list offers only regions that have a code. A
        # pasted "+..." number decides its own region, as before.
        field = self.fields["mobile_country_code"]
        field.queryset = field.queryset.filter(
            code__in=[
                code
                for code in field.queryset.values_list("code", flat=True)
                if phonenumbers.country_code_for_region(code)
            ]
        )
        from apps.people.services import calling_code_label

        # "Algeria (+213)" (S-10).
        self.fields["mobile_country_code"].label_from_instance = calling_code_label
        self._add_mobile_placeholder_examples()
        if mobile_required:
            self.fields["mobile_number"].required = True
            self.fields["mobile_country_code"].required = True
            self.fields["mobile_number"].widget.attrs["aria-required"] = "true"

    def _add_mobile_placeholder_examples(self) -> None:
        """The placeholder of the number field is the national mobile example of
        the selected region (from `phonenumbers` metadata, so it is synthetic
        and never invented here). The script re-reads the map when the country
        selection changes. Presentation only: no validation depends on it."""
        import json

        from apps.people.services import example_mobile_national

        examples = {}
        for code in self.fields["mobile_country_code"].queryset.values_list("pk", flat=True):
            example = example_mobile_national(code)
            if example:
                examples[code] = example
        widget = self.fields["mobile_number"].widget
        widget.attrs["data-region-examples"] = json.dumps(examples, sort_keys=True)
        widget.attrs["data-region-source"] = "mobile_country_code"
        # UX-2 (S-10): calling code per selectable region, so a pasted "+..."
        # number can switch the selector visibly. The server decides anyway.
        import phonenumbers

        calling_codes = {
            code: phonenumbers.country_code_for_region(code)
            for code in self.fields["mobile_country_code"].queryset.values_list("pk", flat=True)
        }
        widget.attrs["data-calling-codes"] = json.dumps(
            {code: value for code, value in calling_codes.items() if value}, sort_keys=True
        )
        widget.attrs["data-switched-text"] = _("Country calling code changed to %(country)s.")
        selected = self.data.get("mobile_country_code") if self.is_bound else None
        selected = selected or self.initial.get("mobile_country_code")
        if selected in examples:
            widget.attrs["placeholder"] = examples[selected]

    def clean(self):
        cleaned = super().clean()
        mobile_number = (cleaned.get("mobile_number") or "").strip()
        country = cleaned.get("mobile_country_code")
        if self.mobile_required and not mobile_number:
            self.add_error("mobile_number", _("Enter your mobile number."))
            return cleaned
        if mobile_number:
            from apps.people.services import PhoneValidationError, parse_mobile_number

            international = mobile_number.startswith(("+", "00"))
            if not country and not international:
                self.add_error("mobile_country_code", _("Select the country calling code."))
                return cleaned
            try:
                _e164, region = parse_mobile_number(mobile_number, country.pk if country else "")
            except PhoneValidationError:
                self.add_error(
                    "mobile_number", _("Enter a valid mobile number for the selected country.")
                )
                return cleaned
            # A pasted international number decides the region (S-10); it
            # must be a selectable country.
            parsed_country = selectable_countries().filter(pk=region).first()
            if parsed_country is None:
                self.add_error("mobile_number", _("This country is not available in the list."))
                return cleaned
            cleaned["mobile_country_code"] = parsed_country
        return cleaned


class ProfessionalStepForm(BootstrapFormMixin, forms.Form):
    """UX-2 (M14-M19, D-06, D-07, D-08): Latin-script organization, job title
    and department; the approved organization types; headquarters country
    plus operating scope; optional canonical links; an optional biography of
    up to 500 characters in any language."""

    # `profile_photo` is a file input and is never eligible regardless of
    # this list (file inputs are rejected structurally on the client).
    language_preserve_fields = frozenset(
        {
            "organization_name",
            "organization_type",
            "job_title",
            "department",
            "sector",
            "country_code",
            "operating_scope",
            "organization_website",
            "professional_profile_url",
            "biography",
        }
    )

    organization_name = forms.CharField(
        label=_("Professional organization"),
        max_length=300,
        help_text=_("Use the organization's official Latin-script name (French or English)."),
        widget=forms.TextInput(
            attrs={
                "placeholder": _("e.g. Example Technologies SARL"),
                "dir": "auto",
                "autocomplete": "organization",
            }
        ),
    )
    organization_type = forms.ChoiceField(
        label=_("Organization type"), choices=[], widget=SearchableSelect
    )
    job_title = forms.CharField(
        label=_("Job title"),
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("e.g. Product Manager"),
                "dir": "auto",
                "autocomplete": "organization-title",
            }
        ),
    )
    department = forms.CharField(
        label=_("Department"),
        max_length=200,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": _("e.g. Research and Development"), "dir": "auto"}
        ),
    )
    sector = forms.ModelChoiceField(
        label=_("Sector"),
        queryset=Sector.objects.filter(is_active=True).order_by("name"),
        widget=SearchableSelect,
    )
    country_code = LocalizedOrderModelChoiceField(
        label=_("Headquarters country"),
        queryset=selectable_countries(),
        widget=SearchableSelect,
        help_text=_("The country where the organization has its main office."),
    )
    operating_scope = forms.ChoiceField(
        label=_("Where does the organization operate?"),
        choices=[],
        widget=forms.RadioSelect,
    )
    organization_website = forms.CharField(
        label=_("Organization website"),
        max_length=400,
        required=False,
        widget=forms.URLInput(
            attrs={"placeholder": _("https://www.example.org"), "autocomplete": "url"}
        ),
    )
    professional_profile_url = forms.CharField(
        label=_("Professional profile URL"),
        max_length=400,
        required=False,
        help_text=_("For example, a LinkedIn profile or personal professional page."),
        widget=forms.URLInput(attrs={"placeholder": _("https://www.example.org")}),
    )
    biography = forms.CharField(
        label=_("Short professional biography"),
        max_length=4000,
        required=False,
        help_text=_("Optional, up to 500 characters, in the language of your choice."),
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _("Two or three sentences about your work and experience."),
                "data-asc-char-counter": "500",
                # Free language (UXR-C1, UXR-F04): the text sets its own direction.
                "dir": "auto",
            }
        ),
    )
    # Required for every new submission (owner correction, 2026-10-04): the
    # field itself stays `required=False` only so that a photograph already on
    # file satisfies it without a new upload; `clean()` decides.
    profile_photo = forms.ImageField(
        label=_("Profile photograph"),
        required=False,
        help_text=_(
            "Required. JPEG or PNG only. Used only for your professional profile, never public."
        ),
    )

    def __init__(self, *args, has_profile_photo: bool = False, **kwargs):
        # Whether an eligible photograph is already on file for this draft, so
        # a re-save of the step does not ask for it again.
        self.has_profile_photo = has_profile_photo
        super().__init__(*args, **kwargs)
        if not has_profile_photo:
            self.fields["profile_photo"].widget.attrs["aria-required"] = "true"
        from apps.organizations.models import (
            NEW_SELECTION_ORGANIZATION_TYPES,
            OperatingScope,
        )

        labels = dict(OrganizationType.choices)
        codes = list(NEW_SELECTION_ORGANIZATION_TYPES)
        # A legacy value (for example INSTITUTION) stays selectable only for
        # the record that already holds it (S-14); it is never offered to new
        # input.
        current = self.initial.get("organization_type")
        if current and current in labels and current not in codes:
            codes.insert(0, current)
        self.fields["organization_type"].choices = [("", _("Select…"))] + [
            (code, labels[code]) for code in codes
        ]
        self.fields["operating_scope"].choices = list(OperatingScope.choices)
        self.fields["sector"].label_from_instance = _sector_label
        self.fields["country_code"].label_from_instance = _country_label

    # The limits are the ones the step service and the submission guard
    # enforce (UX-C1, UX-F03), imported so the three cannot drift apart.

    def clean_organization_name(self):
        from apps.registrations.services import ORGANIZATION_NAME_MAX_LENGTH

        return _text_or_error(
            self.cleaned_data.get("organization_name", ""),
            max_length=ORGANIZATION_NAME_MAX_LENGTH,
            required=True,
        )

    def clean_job_title(self):
        from apps.registrations.services import JOB_TITLE_MAX_LENGTH

        return _text_or_error(
            self.cleaned_data.get("job_title", ""), max_length=JOB_TITLE_MAX_LENGTH, required=True
        )

    def clean_department(self):
        from apps.registrations.services import DEPARTMENT_MAX_LENGTH

        return _text_or_error(
            self.cleaned_data.get("department", ""),
            max_length=DEPARTMENT_MAX_LENGTH,
            required=False,
        )

    def clean_organization_website(self):
        return _url_or_error(self.cleaned_data.get("organization_website", ""))

    def clean_professional_profile_url(self):
        return _url_or_error(self.cleaned_data.get("professional_profile_url", ""))

    def clean_biography(self):
        from apps.registrations.services import BIOGRAPHY_MAX_LENGTH, normalize_long_text

        text = normalize_long_text(self.cleaned_data.get("biography", ""))
        if len(text) > BIOGRAPHY_MAX_LENGTH:
            raise forms.ValidationError(
                _("Use %(limit)s characters or fewer.") % {"limit": BIOGRAPHY_MAX_LENGTH},
                code="too_long",
            )
        return text

    def clean_profile_photo(self):
        photo = self.cleaned_data.get("profile_photo")
        if photo is None:
            if not self.has_profile_photo:
                raise forms.ValidationError(
                    _("Upload a profile photograph. It is required."), code="required"
                )
            return photo
        if photo.content_type not in {"image/jpeg", "image/png"}:
            raise forms.ValidationError(_("Upload a JPEG or PNG image."))
        return photo


class InterestsStepForm(BootstrapFormMixin, forms.Form):
    # UX-3 (S-18): the accommodation fields are deliberately NOT listed, so a
    # language switch never copies them into `sessionStorage`.
    language_preserve_fields = frozenset({"interest_topics", "objectives_text"})

    interest_topics = forms.ModelMultipleChoiceField(
        label=_("Areas of interest"),
        queryset=None,
        required=True,
        widget=forms.CheckboxSelectMultiple,
    )
    objectives_text = forms.CharField(
        label=_("What do you hope to achieve at this event?"),
        # Longer than the limit so the rule below counts the normalized text,
        # exactly as the service and the guard do (UXR-C1, UXR-F06).
        max_length=4000,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _("A short description of what you hope to achieve."),
                "dir": "auto",
            }
        ),
    )
    # UX-3 (M21, D-10): a voluntary question with practical support
    # categories and a short optional note; no diagnosis, certificate or
    # medical history. Declining has no effect on the registration.
    accommodation_answer = forms.ChoiceField(
        label=_("Do you need accessibility support to take part?"),
        required=False,
        choices=[],
        widget=forms.RadioSelect,
        help_text=_(
            "Optional. Your answer is used only to arrange support, never to decide on "
            "your registration."
        ),
    )
    accommodation_categories = forms.MultipleChoiceField(
        label=_("What support would help you?"),
        required=False,
        choices=[],
        widget=forms.CheckboxSelectMultiple,
    )
    accommodation_note = forms.CharField(
        label=_("Anything else the support team should know"),
        required=False,
        max_length=4000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "data-asc-char-counter": "300",
                "autocomplete": "off",
                "dir": "auto",
            }
        ),
        help_text=_("Optional, up to 300 characters. Do not include medical details or diagnoses."),
    )

    def __init__(self, *args, interest_topic_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.registrations.models import InterestTopic

        queryset = (
            interest_topic_queryset
            if interest_topic_queryset is not None
            else InterestTopic.objects.none()
        )
        # UX-2 (M20): grouped display order; validation is unchanged.
        self.fields["interest_topics"].queryset = queryset.order_by("group_code", "code")
        # Show each topic's label in the active language (with its English
        # fallback) -- the default `label_from_instance` would otherwise
        # fall back to `InterestTopic.__str__`, which is the internal
        # `code`, not a participant-facing label (Prompt 4 final closure
        # pass §9).
        self.fields["interest_topics"].label_from_instance = lambda topic: topic.localized_label
        from apps.registrations.models import AccommodationAnswer, AccommodationCategory

        self.fields["accommodation_answer"].choices = list(AccommodationAnswer.choices)
        self.fields["accommodation_categories"].choices = list(AccommodationCategory.choices)

    def clean_objectives_text(self):
        from apps.registrations.services import OBJECTIVES_MAX_LENGTH, normalize_long_text

        text = normalize_long_text(self.cleaned_data.get("objectives_text", ""))
        if not text:
            raise forms.ValidationError(_("This field is required."), code="required")
        if len(text) > OBJECTIVES_MAX_LENGTH:
            raise forms.ValidationError(
                _("Use %(limit)s characters or fewer.") % {"limit": OBJECTIVES_MAX_LENGTH},
                code="too_long",
            )
        return text

    def clean_accommodation_note(self):
        from apps.registrations.services import (
            ACCOMMODATION_NOTE_MAX_LENGTH,
            normalize_long_text,
        )

        note = normalize_long_text(self.cleaned_data.get("accommodation_note", ""))
        if len(note) > ACCOMMODATION_NOTE_MAX_LENGTH:
            raise forms.ValidationError(
                _("Use %(limit)s characters or fewer.") % {"limit": ACCOMMODATION_NOTE_MAX_LENGTH}
            )
        return note

    def clean(self):
        cleaned = super().clean()
        answer = cleaned.get("accommodation_answer")
        if answer != "YES":
            # Details belong only to a YES answer; anything else is dropped,
            # never stored.
            cleaned["accommodation_categories"] = []
            cleaned["accommodation_note"] = ""
        return cleaned


class NoticesForm(BootstrapFormMixin, forms.Form):
    language_preserve_fields = frozenset(
        {"accept_privacy_notice", "accept_terms", "marketing_consent"}
    )

    accept_privacy_notice = forms.BooleanField(label=_("I acknowledge the Privacy Notice."))
    accept_terms = forms.BooleanField(label=_("I accept the Registration Terms."))
    # UX-3 (D-11): explicit consent to the processing described in the notice,
    # a separate statement from the two above.
    accept_data_processing = forms.BooleanField(
        label=_(
            "I consent to the processing of my personal data for my registration and "
            "participation, as described in the Privacy Notice."
        )
    )
    marketing_consent = forms.BooleanField(
        label=_("I would like to receive optional communications about future editions."),
        required=False,
    )
    # The versions this page displayed (owner correction, 2026-10-04): the
    # acceptance is recorded only for those exact versions. When a newer one
    # was published meanwhile, the view shows the page again instead.
    privacy_version_id = forms.CharField(required=False, widget=forms.HiddenInput)
    terms_version_id = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(
        self,
        *args,
        requires_sensitive_consent: bool = False,
        offers_optional_sensitive_consent: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        # UX-C2 (G): a YES answer without details may be shared with the
        # support team only with a voluntary consent. It is optional, never
        # pre-checked, and declining it has no effect on the registration.
        if offers_optional_sensitive_consent and not requires_sensitive_consent:
            self.fields["accept_sensitive_data"] = forms.BooleanField(
                required=False,
                label=_(
                    "I consent to the support team seeing that I asked for accessibility "
                    "support, only to arrange support at the event."
                ),
                help_text=_(
                    "Optional. Without this consent the support team does not see your request. "
                    "You can withdraw it later from your workspace."
                ),
            )
            self.order_fields(
                [
                    "accept_privacy_notice",
                    "accept_terms",
                    "accept_data_processing",
                    "accept_sensitive_data",
                    "marketing_consent",
                ]
            )
        # A fourth, separate consent appears ONLY when accommodation
        # categories or a note were given (D-11). It is never pre-checked.
        if requires_sensitive_consent:
            self.fields["accept_sensitive_data"] = forms.BooleanField(
                label=_(
                    "I explicitly consent to the processing of the accommodation information I "
                    "gave, only to arrange support at the event."
                ),
                help_text=_(
                    "Required only because you described support needs. You can instead go back "
                    "and remove them. You can withdraw this consent later from your workspace."
                ),
            )
            self.order_fields(
                [
                    "accept_privacy_notice",
                    "accept_terms",
                    "accept_data_processing",
                    "accept_sensitive_data",
                    "marketing_consent",
                ]
            )
