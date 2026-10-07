"""Bounded transport inputs for Digital Entry Pass lifecycle commands.

Forms here validate SHAPE only -- they never decide authorization and never
perform a state change. Every command carries three protected values:

* `operation_id` -- the caller's idempotency key;
* `expected_lock_version` -- optimistic-concurrency protection;
* `expected_jti` (replacement only) -- the exact credential the caller
  intends to supersede.

`expected_jti` is not redundant with `expected_lock_version`. A newly
created credential starts at lock version 1, so a stale lock version alone
could match a replacement row that a competing request just created.
Naming the credential closes that window.
"""

from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.badges.models import PassReasonCode, VerificationKeyReasonCode

_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_JTI_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


class _LifecycleCommandForm(forms.Form):
    """Shared shape for every POST-only lifecycle command."""

    operation_id = forms.CharField(max_length=64, widget=forms.HiddenInput)
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def clean_operation_id(self) -> str:
        value = self.cleaned_data["operation_id"]
        if not _OPERATION_ID_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid operation identifier."))
        return value


class GeneratePassForm(forms.Form):
    """Generation carries no expected version: no credential exists yet."""

    operation_id = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def clean_operation_id(self) -> str:
        value = self.cleaned_data["operation_id"]
        if not _OPERATION_ID_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid operation identifier."))
        return value


class ActivatePassForm(_LifecycleCommandForm):
    """Activation is always explicit; there is no automatic activation."""


#: Which controlled reasons each credential action may legitimately carry.
#: Hard-coding one hidden value per action -- as the first implementation did
#: -- meant the audit trail recorded a reason the operator never chose. These
#: are the server-side allowlists; the template renders exactly these, and the
#: form rejects anything outside them.
CREDENTIAL_REASON_CHOICES: dict[str, tuple[str, ...]] = {
    "suspend": (
        PassReasonCode.SECURITY_CONCERN,
        PassReasonCode.ADMINISTRATIVE_ERROR,
        PassReasonCode.PARTICIPANT_REQUEST,
        PassReasonCode.OTHER_APPROVED,
    ),
    "resume": (
        PassReasonCode.ADMINISTRATIVE_ERROR,
        PassReasonCode.PARTICIPANT_REQUEST,
        PassReasonCode.OTHER_APPROVED,
    ),
    "revoke": (
        PassReasonCode.LOST_OR_COMPROMISED,
        PassReasonCode.SECURITY_CONCERN,
        PassReasonCode.ADMINISTRATIVE_ERROR,
        PassReasonCode.REGISTRATION_CLOSED,
        PassReasonCode.PARTICIPANT_REQUEST,
        PassReasonCode.OTHER_APPROVED,
    ),
    "replace": (
        PassReasonCode.LOST_OR_COMPROMISED,
        PassReasonCode.ASSIGNMENT_CHANGED,
        PassReasonCode.ADMINISTRATIVE_ERROR,
        PassReasonCode.PARTICIPANT_REQUEST,
        PassReasonCode.OTHER_APPROVED,
    ),
}

_PASS_REASON_LABELS = dict(PassReasonCode.choices)


def credential_reason_choices(action: str) -> list[tuple[str, str]]:
    """Label/value pairs for one credential action's allowed reasons."""
    return [(code, _PASS_REASON_LABELS[code]) for code in CREDENTIAL_REASON_CHOICES[action]]


class ReasonedLifecycleForm(_LifecycleCommandForm):
    """Suspension, resumption, revocation, and replacement carry a reason.

    The permitted set is narrowed per action by `action`, so a revoke-only
    reason cannot be submitted to the resume endpoint.
    """

    reason_code = forms.ChoiceField(choices=PassReasonCode.choices)
    reason_text = forms.CharField(max_length=300, required=False)

    #: Subclasses and callers set this to narrow the allowed reason set.
    action: str | None = None

    def __init__(self, *args, action: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        chosen = action or self.action
        if chosen:
            self.fields["reason_code"].choices = credential_reason_choices(chosen)


class ReplacePassForm(ReasonedLifecycleForm):
    """Replacement additionally names the exact credential being superseded."""

    action = "replace"

    expected_jti = forms.CharField(max_length=22, widget=forms.HiddenInput)

    def clean_expected_jti(self) -> str:
        value = self.cleaned_data["expected_jti"]
        if not _JTI_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid credential identifier."))
        return value


class FallbackReferenceLookupForm(forms.Form):
    """Controlled, exact-match-only fallback reference lookup.

    Deliberately a single bounded field with no wildcard, prefix, range, or
    empty-term option: there is no participant browse surface here.
    """

    reference = forms.CharField(
        max_length=32,
        label=_("Fallback reference"),
        help_text=_("Enter the full reference exactly as shown, for example ASC-7K4M-9PQR-3."),
    )


class PublishVerificationKeyForm(forms.Form):
    """Publish a PUBLIC verification key. Private material is refused."""

    key_id = forms.RegexField(regex=r"^v[1-9][0-9]{0,3}$", max_length=8, label=_("Key identifier"))
    public_key_pem = forms.CharField(widget=forms.Textarea, label=_("Public key (PEM)"))


#: Retirement is a planned act; revocation is an emergency one. Offering the
#: same vocabulary to both would let a compromise be filed as a routine
#: rotation, which is exactly the distinction the audit trail must keep.
KEY_RETIREMENT_REASON_CODES: tuple[str, ...] = (
    VerificationKeyReasonCode.PLANNED_ROTATION,
    VerificationKeyReasonCode.ROTATION_SUPERSEDED,
    VerificationKeyReasonCode.PROVIDER_MIGRATION,
    VerificationKeyReasonCode.OPERATOR_ERROR,
)

#: Emergency revocation must state an emergency-appropriate reason. A
#: "planned rotation" revocation would misrepresent an incident.
KEY_REVOCATION_REASON_CODES: tuple[str, ...] = (
    VerificationKeyReasonCode.SUSPECTED_COMPROMISE,
    VerificationKeyReasonCode.CONFIRMED_COMPROMISE,
    VerificationKeyReasonCode.OPERATOR_ERROR,
    VerificationKeyReasonCode.OTHER_APPROVED,
)

_KEY_REASON_LABELS = dict(VerificationKeyReasonCode.choices)


def key_reason_choices(codes: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(code, _KEY_REASON_LABELS[code]) for code in codes]


class VerificationKeyLifecycleForm(forms.Form):
    """Shape for a promote or retire command on a verification key.

    Carries its own `operation_id` so each control on the page submits a
    distinct identifier -- sharing one across forms is exactly the replay hole
    the command fingerprint exists to close.
    """

    operation_id = forms.CharField(max_length=64, widget=forms.HiddenInput)
    reason_code = forms.ChoiceField(
        choices=key_reason_choices(KEY_RETIREMENT_REASON_CODES), required=False
    )
    reason_text = forms.CharField(max_length=300, required=False)

    def clean_operation_id(self) -> str:
        value = self.cleaned_data["operation_id"]
        if not _OPERATION_ID_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid operation identifier."))
        return value


class RevokeVerificationKeyForm(VerificationKeyLifecycleForm):
    """Emergency revocation requires an emergency-appropriate reason."""

    reason_code = forms.ChoiceField(
        choices=key_reason_choices(KEY_REVOCATION_REASON_CODES), required=True
    )


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
# ---------------------------------------------------------------------------

from apps.badges.models import (  # noqa: E402
    BadgeIssuanceReasonCode,
    PrintBatchStatus,
    StockAdjustmentReasonCode,
    StockAllocationPurpose,
    StockLocationType,
)

_LOCATION_TYPE_LABELS = dict(StockLocationType.choices)
_PRINT_BATCH_STATUS_LABELS = dict(PrintBatchStatus.choices)
_STOCK_ADJUSTMENT_REASON_LABELS = dict(StockAdjustmentReasonCode.choices)
_BADGE_ISSUANCE_REASON_LABELS = dict(BadgeIssuanceReasonCode.choices)

#: Controlled, per-action reason allowlists -- the same "no hidden fixed
#: reason" discipline the Prompt 2 correction pass established for
#: credential lifecycle reasons.
ISSUANCE_REPLACE_REASON_CODES: tuple[str, ...] = (
    BadgeIssuanceReasonCode.WRONG_BADGE_TYPE,
    BadgeIssuanceReasonCode.DAMAGED_BADGE,
    BadgeIssuanceReasonCode.ADMINISTRATIVE_ERROR,
    BadgeIssuanceReasonCode.OTHER_APPROVED,
)
ISSUANCE_RETURN_REASON_CODES: tuple[str, ...] = (
    BadgeIssuanceReasonCode.EVENT_CONCLUDED,
    BadgeIssuanceReasonCode.PARTICIPANT_REQUEST,
    BadgeIssuanceReasonCode.ADMINISTRATIVE_ERROR,
    BadgeIssuanceReasonCode.OTHER_APPROVED,
)


def issuance_reason_choices(codes: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(code, _BADGE_ISSUANCE_REASON_LABELS[code]) for code in codes]


def adjustment_reason_choices() -> list[tuple[str, str]]:
    return list(StockAdjustmentReasonCode.choices)


class _StockCommandForm(forms.Form):
    """Shared shape for every POST-only stock command."""

    operation_id = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def clean_operation_id(self) -> str:
        value = self.cleaned_data["operation_id"]
        if not _OPERATION_ID_PATTERN.match(value):
            raise forms.ValidationError(_("Invalid operation identifier."))
        return value


class CreateStockLocationForm(_StockCommandForm):
    code = forms.RegexField(regex=r"^[A-Za-z0-9_-]{1,32}$", label=_("Code"))
    name = forms.CharField(max_length=200, label=_("Name (English)"))
    name_fr = forms.CharField(max_length=200, required=False, label=_("Name (French)"))
    name_ar = forms.CharField(max_length=200, required=False, label=_("Name (Arabic)"))
    location_type = forms.ChoiceField(choices=StockLocationType.choices, label=_("Location type"))


class CreatePrintBatchForm(_StockCommandForm):
    planned_quantity = forms.IntegerField(min_value=1, label=_("Planned quantity"))
    artwork_version = forms.CharField(max_length=32, required=False, label=_("Artwork version"))
    supplier_reference = forms.CharField(
        max_length=200, required=False, label=_("Supplier reference")
    )
    destination_location_id = forms.UUIDField(
        required=False, widget=forms.HiddenInput, label=_("Destination location")
    )


class ChangeBatchStatusForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    target_status = forms.ChoiceField(choices=PrintBatchStatus.choices)


class ReceivePrintBatchForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    produced_quantity = forms.IntegerField(min_value=0, label=_("Produced quantity"))
    accepted_quantity = forms.IntegerField(min_value=0, label=_("Accepted quantity"))
    damaged_quantity = forms.IntegerField(min_value=0, label=_("Damaged quantity"))
    destination_location_id = forms.UUIDField(required=False, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        produced = cleaned.get("produced_quantity")
        accepted = cleaned.get("accepted_quantity")
        damaged = cleaned.get("damaged_quantity")
        if None not in (produced, accepted, damaged) and produced != accepted + damaged:
            raise forms.ValidationError(
                _("Produced quantity must equal accepted plus damaged quantity.")
            )
        return cleaned


class TransferStockForm(_StockCommandForm):
    destination_location_id = forms.UUIDField(widget=forms.HiddenInput)
    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    quantity = forms.IntegerField(min_value=1, label=_("Quantity"))
    note = forms.CharField(max_length=300, required=False, label=_("Note"))


def _attendance_choices():
    from apps.accreditation.models import AttendanceCategory

    return list(AttendanceCategory.choices)


class IssueBadgeForm(_StockCommandForm):
    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    location_id = forms.UUIDField(widget=forms.HiddenInput)
    allocation_id = forms.UUIDField(required=False)
    optional_serial_number = forms.CharField(
        max_length=64, required=False, label=_("Serial number (optional)")
    )
    # The attendance marking the operator confirms having applied to the badge
    # being handed over (blank: none confirmed yet; recorded later).
    attendance_marking = forms.ChoiceField(
        choices=[("", ""), *_attendance_choices()], required=False
    )


class RecordAttendanceMarkingForm(forms.Form):
    """Confirm the attendance marking applied to a handed-over badge."""

    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    attendance_marking = forms.ChoiceField(choices=_attendance_choices())


class ReplaceIssuanceForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    return_original = forms.BooleanField(
        required=False, label=_("The original badge is being returned to stock")
    )
    reason_code = forms.ChoiceField(choices=issuance_reason_choices(ISSUANCE_REPLACE_REASON_CODES))
    reason_text = forms.CharField(max_length=300, required=False)


class ReturnIssuanceForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason_code = forms.ChoiceField(choices=issuance_reason_choices(ISSUANCE_RETURN_REASON_CODES))
    reason_text = forms.CharField(max_length=300, required=False)


class MarkIssuanceLostForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason_text = forms.CharField(max_length=300, required=False)


class VoidIssuanceForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason_text = forms.CharField(max_length=300, required=False)


class RecordAdjustmentForm(_StockCommandForm):
    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    quantity_delta = forms.IntegerField(label=_("Quantity change"))
    reason_code = forms.ChoiceField(choices=adjustment_reason_choices())
    reason_text = forms.CharField(max_length=300, required=False)

    def clean_quantity_delta(self) -> int:
        value = self.cleaned_data["quantity_delta"]
        if value == 0:
            raise forms.ValidationError(_("The quantity change cannot be zero."))
        return value


class RecordReconciliationForm(_StockCommandForm):
    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    counted_quantity = forms.IntegerField(min_value=0, label=_("Counted quantity"))
    notes = forms.CharField(max_length=300, required=False)


def allocation_purpose_choices() -> list[tuple[str, str]]:
    return list(StockAllocationPurpose.choices)


class AllocateStockForm(_StockCommandForm):
    """Reserve a quantity at a location (TRD `PRINT-002` "reserved")."""

    badge_type_id = forms.UUIDField(widget=forms.HiddenInput)
    quantity = forms.IntegerField(min_value=1, label=_("Quantity to allocate"))
    purpose_code = forms.ChoiceField(
        choices=StockAllocationPurpose.choices, label=_("Allocation purpose")
    )
    purpose_text = forms.CharField(max_length=300, required=False, label=_("Notes (optional)"))


class ReleaseAllocationForm(_StockCommandForm):
    expected_lock_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    reason_text = forms.CharField(max_length=300, required=False)
