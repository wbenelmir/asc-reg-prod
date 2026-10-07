"""Validation-only forms of the staff account administration area and the
credential setup page. Business rules (scope delegation, self-change,
last administrator, validity) live in `apps.accounts.administration`."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts import roles
from apps.accounts.administration import CREATABLE_ACCOUNT_TYPES
from apps.accounts.models import OperationalUserAccountType, OperationalUserStatus

#: The explicit "every value" choice. A blank value is never accepted.
EVERY = "*"

_DATETIME = {"type": "datetime-local"}
_DATETIME_FORMAT = "%Y-%m-%dT%H:%M"
_REASON_HELP = _("Short and factual. Do not include personal data.")


def _datetime_field(label):
    return forms.DateTimeField(
        required=False,
        label=label,
        help_text=_("Coordinated Universal Time (UTC). Leave empty for no limit."),
        widget=forms.DateTimeInput(attrs=_DATETIME, format=_DATETIME_FORMAT),
        input_formats=[_DATETIME_FORMAT, "%Y-%m-%d %H:%M"],
    )


def _account_type_choices():
    labels = dict(OperationalUserAccountType.choices)
    names = {
        OperationalUserAccountType.INTERNAL: _("Internal staff"),
        OperationalUserAccountType.ORGANIZATION: _("Organization staff"),
        OperationalUserAccountType.SUPPORT: _("Support"),
        OperationalUserAccountType.EXTERNAL_SECURITY: _("External security (temporary)"),
    }
    return [(value, names.get(value, labels[value])) for value in CREATABLE_ACCOUNT_TYPES]


class StaffAccountCreateForm(forms.Form):
    email = forms.EmailField(
        label=_("Email address"), widget=forms.EmailInput(attrs={"autocomplete": "off"})
    )
    display_name = forms.CharField(max_length=200, label=_("Display name"))
    account_type = forms.ChoiceField(choices=_account_type_choices, label=_("Account type"))
    active_from = _datetime_field(_("Valid from"))
    active_until = _datetime_field(_("Valid until"))


class StaffAccountUpdateForm(forms.Form):
    display_name = forms.CharField(max_length=200, label=_("Display name"))
    active_from = _datetime_field(_("Valid from"))
    active_until = _datetime_field(_("Valid until"))
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )


class StaffAccountStatusForm(forms.Form):
    command = forms.ChoiceField(
        choices=[("activate", "activate"), ("suspend", "suspend"), ("disable", "disable")],
        widget=forms.HiddenInput,
    )
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )


class CredentialLinkForm(forms.Form):
    purpose = forms.ChoiceField(
        choices=[("INVITATION", "INVITATION"), ("RESET", "RESET")], widget=forms.HiddenInput
    )


class RoleGrantForm(forms.Form):
    """One role in one explicit scope. Event and organization are required
    selections: "every event" / "every organization" are explicit choices,
    never the result of an empty field."""

    role = forms.ChoiceField(label=_("Role"))
    event = forms.ChoiceField(label=_("Event edition"))
    organization = forms.ChoiceField(label=_("Organization"))
    gate = forms.ChoiceField(label=_("Checkpoint"), required=False)
    active_from = _datetime_field(_("Starts"))
    active_until = _datetime_field(_("Ends"))
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )
    confirm_broad_scope = forms.BooleanField(
        required=False,
        label=_("I confirm this role applies to every event edition."),
    )

    def __init__(self, *args, events=(), organizations=(), gates=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [("", _("Select a role"))] + [
            (role.key, role.label) for role in roles.STAFF_ROLES
        ]
        self.fields["event"].choices = (
            [("", _("Select an event edition"))]
            + [(str(event.pk), f"{event.code} — {event.name}") for event in events]
            + [(EVERY, _("Every event edition"))]
        )
        self.fields["organization"].choices = (
            [("", _("Select"))]
            + [(EVERY, _("Every organization"))]
            + [(str(org.pk), org.official_name) for org in organizations]
        )
        self.fields["gate"].choices = [("", _("Every checkpoint of the event"))] + [
            (str(gate.pk), f"{gate.venue.code} / {gate.code} — {gate.name}") for gate in gates
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("event") == EVERY and not cleaned.get("confirm_broad_scope"):
            self.add_error(
                "confirm_broad_scope",
                _("Confirm that this role applies to every event edition."),
            )
        return cleaned


class RoleRevokeForm(forms.Form):
    membership_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(
        min_length=3, max_length=300, label=_("Reason"), help_text=_REASON_HELP
    )


class StaffAccountFilterForm(forms.Form):
    q = forms.CharField(max_length=200, required=False, label=_("Search"))
    status = forms.ChoiceField(
        required=False,
        label=_("Status"),
        choices=[("", _("Every status"))] + list(OperationalUserStatus.choices),
    )
    role = forms.ChoiceField(required=False, label=_("Role"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = [("", _("Every role"))] + [
            (role.key, role.label) for role in roles.STAFF_ROLES
        ]


class CredentialSetupForm(forms.Form):
    """Choose a password through a single-use setup or reset link."""

    new_password1 = forms.CharField(
        label=_("New password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_("At least 12 characters. Avoid common words and your name or email."),
    )
    new_password2 = forms.CharField(
        label=_("Repeat the new password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        first, second = cleaned.get("new_password1"), cleaned.get("new_password2")
        if first and second and first != second:
            self.add_error("new_password2", _("The two passwords do not match."))
        return cleaned
