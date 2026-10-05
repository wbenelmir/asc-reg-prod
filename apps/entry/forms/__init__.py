"""Bounded transport inputs for the entry interfaces.

Shape validation only: no form here decides authorization or changes state.
Sensitive inputs (QR token, NIN, passport number, search text, activation
code) are rendered with `autocomplete="off"` and are never re-rendered back
into a page after submission: they use `NonEchoTextInput`, which omits the
submitted value even when a bound form is re-rendered to show a validation
error, and views build a fresh unbound form for every result screen.
"""

from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.entry.models import (
    VERIFICATION_METHOD_ORDER,
    EmergencyWipeReason,
    EntryDecision,
    EntryDecisionReason,
    OfflineSensitivity,
    ReconciliationActionType,
    VerificationMethod,
)

_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SELECTION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_SENSITIVE_INPUT_ATTRS = {
    "autocomplete": "off",
    "autocapitalize": "characters",
    "spellcheck": "false",
    "class": "form-control form-control-lg",
}


class NonEchoTextInput(forms.TextInput):
    """A text input that never renders a submitted value back into the page.

    Lets a checkpoint form be re-displayed with its field errors (Prompt 5:
    useful validation messages) without echoing a token, identity number,
    reference, or search string into the HTML, a cache, or a screenshot.
    """

    def format_value(self, value):
        return None


class AccessibleErrorsMixin:
    """Mark an invalid field `aria-invalid` and point its `aria-describedby`
    at the error text (WCAG 2.2 §3.3.1, §4.1.2) -- the same contract as
    `apps.registrations.forms.BootstrapFormMixin.add_error`, which Django's
    own field cleaning also routes through."""

    def add_error(self, field, error):
        super().add_error(field, error)
        if field is not None and field in self.fields:
            widget = self.fields[field].widget
            widget.attrs["aria-invalid"] = "true"
            error_id = f"id_{field}_error"
            described_by = widget.attrs.get("aria-describedby", "").split()
            if error_id not in described_by:
                described_by.append(error_id)
            widget.attrs["aria-describedby"] = " ".join(described_by)


def _operation_id_field():
    return forms.CharField(max_length=64, widget=forms.HiddenInput)


def _validate_operation_id(value: str) -> str:
    if not _OPERATION_ID_PATTERN.match(value or ""):
        raise forms.ValidationError(_("Invalid operation identifier."))
    return value


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


class CheckpointSetupForm(forms.Form):
    zone_id = forms.ChoiceField(label=_("Zone"))

    def __init__(self, *args, zones=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["zone_id"].choices = [(str(z.pk), z.localized_name) for z in zones]
        self.fields["zone_id"].widget.attrs["class"] = "form-select"


class QrVerifyForm(AccessibleErrorsMixin, forms.Form):
    """A USB 2D scanner "types" the token and presses Enter into this field;
    manual keyboard entry works the same way (UI/UX §12.2)."""

    token = forms.CharField(
        max_length=2048,
        strip=True,
        label=_("Scan the Digital Entry Pass QR code"),
        error_messages={"required": _("Scan the QR code, or type the code printed under it.")},
        # A pass code is inherently left-to-right, even on an Arabic page.
        widget=NonEchoTextInput(
            attrs={**_SENSITIVE_INPUT_ATTRS, "autofocus": "autofocus", "dir": "ltr"}
        ),
    )


class IdentityLookupForm(AccessibleErrorsMixin, forms.Form):
    method = forms.ChoiceField(
        label=_("Identity document"),
        choices=[
            (VerificationMethod.NIN, VerificationMethod.NIN.label),
            (VerificationMethod.PASSPORT, VerificationMethod.PASSPORT.label),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    value = forms.CharField(
        max_length=64,
        label=_("Number"),
        error_messages={"required": _("Enter the number shown on the identity document.")},
        widget=NonEchoTextInput(attrs=_SENSITIVE_INPUT_ATTRS),
    )
    country_code = forms.CharField(
        max_length=2,
        required=False,
        label=_("Passport issuing country (2-letter code)"),
        widget=forms.TextInput(attrs={**_SENSITIVE_INPUT_ATTRS, "class": "form-control"}),
    )

    def __init__(self, *args, methods=None, **kwargs):
        super().__init__(*args, **kwargs)
        if methods is not None:
            self.fields["method"].choices = [
                choice for choice in self.fields["method"].choices if choice[0] in methods
            ]

    def clean(self):
        from apps.entry.services.verification import normalize_issuing_country

        cleaned = super().clean()
        if cleaned.get("method") == VerificationMethod.PASSPORT:
            if not cleaned.get("country_code"):
                self.add_error("country_code", _("The issuing country is required for a passport."))
            else:
                # The same exact two-letter rule the lookup service applies
                # (P8-07), surfaced here as a localized field error.
                country = normalize_issuing_country(cleaned["country_code"])
                if not country:
                    self.add_error(
                        "country_code",
                        _("Enter the two-letter issuing country code, for example FR."),
                    )
                else:
                    cleaned["country_code"] = country
        return cleaned


class ReferenceLookupForm(AccessibleErrorsMixin, forms.Form):
    reference = forms.CharField(
        max_length=64,
        label=_("Registration reference or pass fallback reference"),
        error_messages={
            "required": _("Enter the registration reference or the pass fallback reference.")
        },
        widget=NonEchoTextInput(attrs=_SENSITIVE_INPUT_ATTRS),
    )


class ManualSearchForm(AccessibleErrorsMixin, forms.Form):
    query = forms.CharField(
        max_length=100,
        label=_("Name"),
        error_messages={"required": _("Enter part of the participant's name.")},
        widget=NonEchoTextInput(attrs={**_SENSITIVE_INPUT_ATTRS, "autocapitalize": "words"}),
    )


class CandidateSelectForm(forms.Form):
    selection = forms.CharField(max_length=64, widget=forms.HiddenInput)
    index = forms.IntegerField(min_value=0, max_value=50, widget=forms.HiddenInput)

    def clean_selection(self) -> str:
        value = self.cleaned_data["selection"]
        if not _SELECTION_TOKEN_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid selection."))
        return value


class DecisionForm(forms.Form):
    operation_id = _operation_id_field()
    decision = forms.ChoiceField(choices=EntryDecision.choices)
    decision_reason = forms.ChoiceField(
        choices=[("", "")] + list(EntryDecisionReason.choices), required=False
    )

    def clean_operation_id(self) -> str:
        return _validate_operation_id(self.cleaned_data["operation_id"])


class OverrideForm(forms.Form):
    operation_id = _operation_id_field()
    override_reason_id = forms.UUIDField(label=_("Override reason"))
    note = forms.CharField(
        max_length=500,
        required=False,
        label=_("Note (recorded encrypted)"),
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control", "autocomplete": "off"}),
    )

    def clean_operation_id(self) -> str:
        return _validate_operation_id(self.cleaned_data["operation_id"])


# ---------------------------------------------------------------------------
# Device administration
# ---------------------------------------------------------------------------

DEVICE_LIFECYCLE_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("LOST_OR_STOLEN", _("Lost or stolen")),
    ("SECURITY_CONCERN", _("Security concern")),
    ("MAINTENANCE", _("Maintenance")),
    ("REASSIGNED", _("Reassigned")),
    ("ADMINISTRATIVE_ERROR", _("Administrative error")),
    ("OTHER_APPROVED", _("Other approved reason")),
)


def _method_choices():
    labels = dict(VerificationMethod.choices)
    return [(m, labels[m]) for m in VERIFICATION_METHOD_ORDER]


class _DeviceScopeFields(forms.Form):
    gate_id = forms.ChoiceField(label=_("Gate"))
    zone_ids = forms.MultipleChoiceField(
        label=_("Permitted zones"), widget=forms.CheckboxSelectMultiple
    )
    verification_methods = forms.MultipleChoiceField(
        label=_("Permitted verification methods"),
        choices=_method_choices,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, gates=(), zones=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["gate_id"].choices = [
            (str(g.pk), f"{g.venue.code} / {g.code} — {g.localized_name}") for g in gates
        ]
        self.fields["gate_id"].widget.attrs["class"] = "form-select"
        self.fields["zone_ids"].choices = [
            (str(z.pk), f"{z.venue.code} / {z.code} — {z.localized_name}") for z in zones
        ]


class _OfflineScopeFields(forms.Form):
    """Offline continuity for one scope version (Phase 4 Prompt 2). Necessary,
    never sufficient: the global flag, the event enablement, preparation and
    the self-test are also required."""

    offline_capable = forms.BooleanField(
        required=False,
        label=_("Permit offline continuity for this scope (QR verification only)"),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    # Optional in the form (a scope that is not offline-capable needs none);
    # the service defaults it to STANDARD and refuses STANDARD for a scope
    # with a restricted zone.
    offline_sensitivity = forms.ChoiceField(
        choices=OfflineSensitivity.choices,
        initial=OfflineSensitivity.STANDARD,
        required=False,
        label=_("Offline validity class"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class DeviceRegisterForm(_DeviceScopeFields, _OfflineScopeFields):
    public_name = forms.CharField(
        max_length=100,
        label=_("Device name"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    expires_at = forms.DateTimeField(
        label=_("Enrollment expires at"),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}),
    )
    reason = forms.CharField(
        max_length=300,
        required=False,
        label=_("Reason"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )


class DeviceScopeForm(_DeviceScopeFields, _OfflineScopeFields):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    reason = forms.CharField(
        max_length=300,
        required=False,
        label=_("Reason"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )


class DeviceVersionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class DeviceLifecycleForm(DeviceVersionForm):
    reason_code = forms.ChoiceField(
        choices=DEVICE_LIFECYCLE_REASON_CODES,
        label=_("Reason"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason_text = forms.CharField(
        max_length=300,
        required=False,
        label=_("Details"),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )


class DeviceActivationForm(forms.Form):
    code = forms.CharField(
        max_length=32,
        label=_("Activation code"),
        widget=forms.TextInput(attrs=_SENSITIVE_INPUT_ATTRS),
    )


# ---------------------------------------------------------------------------
# Offline preparation administration (Phase 4 Prompt 2, ADR-0023)
# ---------------------------------------------------------------------------

OFFLINE_EVENT_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("OPERATIONAL_READINESS", _("Operational readiness")),
    ("REHEARSAL", _("Rehearsal")),
    ("INCIDENT", _("Incident")),
    ("EVENT_CLOSING", _("Event closing")),
    ("OTHER_APPROVED", _("Other approved reason")),
)


class OfflineEventSettingForm(forms.Form):
    enabled = forms.TypedChoiceField(
        choices=(("1", _("Enable")), ("0", _("Disable"))),
        coerce=lambda value: value == "1",
        widget=forms.HiddenInput,
    )
    reason_code = forms.ChoiceField(
        choices=OFFLINE_EVENT_REASON_CODES,
        label=_("Reason"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class EmergencyWipeForm(forms.Form):
    """Destructive emergency wipe (binding decision P2-F): reason code,
    explicit confirmation, optional bounded note, MFA step-up response."""

    reason_code = forms.ChoiceField(
        choices=EmergencyWipeReason.choices,
        label=_("Reason"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    note = forms.CharField(
        max_length=300,
        required=False,
        label=_("Note (optional, recorded encrypted)"),
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control", "autocomplete": "off"}),
    )
    confirm_evidence_loss = forms.BooleanField(
        required=True,
        label=_(
            "I understand that this wipe may destroy operations the device has not yet "
            "uploaded, and that this loss cannot be undone."
        ),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    mfa_response = forms.CharField(
        max_length=256,
        label=_("MFA step-up code"),
        widget=NonEchoTextInput(attrs={**_SENSITIVE_INPUT_ATTRS, "autocapitalize": "off"}),
    )


class ReconciliationActionForm(AccessibleErrorsMixin, forms.Form):
    """A supervisor's note on an offline reconciliation case, or its closure
    (Phase 4 Prompt 3). The note is required either way: it is the review
    evidence (recorded encrypted; the audit trail records only its presence)."""

    action = forms.ChoiceField(
        choices=ReconciliationActionType.choices,
        label=_("Action"),
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
    )
    note = forms.CharField(
        max_length=1000,
        label=_("Review note (recorded encrypted)"),
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control", "autocomplete": "off"}),
    )
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput())
