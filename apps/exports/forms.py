"""Validation-only form for the controlled export workspace (UI/UX
Completion Gate F8).

Event edition and organization are chosen from the caller's own export
scope (`apps.exports.selectors.export_scope_choices`) instead of typed
database ids. The view still re-checks the exact scoped permission and the
service still re-derives scope and validates purpose and reason.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.display_labels import code_choices
from apps.core.forms import ScopedModelChoiceField

from .models import ALLOWED_EXPORT_PURPOSE_CODES

SCOPE_FIELDS = ("event_edition_id", "organization_id", "campaign_id")


class OrganizationChoiceField(ScopedModelChoiceField):
    """Official name; the organization type is added only when two
    organizations in the list share a name."""

    def label_from_instance(self, organization) -> str:
        if getattr(self, "_duplicate_names", None) is None:
            names = [org.official_name.casefold() for org in self.queryset]
            self._duplicate_names = {name for name in names if names.count(name) > 1}
        if organization.official_name.casefold() in self._duplicate_names:
            return f"{organization.official_name} · {organization.get_organization_type_display()}"
        return organization.official_name


class ExportRequestForm(forms.Form):
    event_edition_id = ScopedModelChoiceField(
        queryset=None,
        label=_("Event edition"),
        empty_label=_("Select an event"),
        error_messages={
            "required": _("Select an event."),
            "invalid_choice": _("Select an event from the list."),
        },
    )
    organization_id = OrganizationChoiceField(
        queryset=None,
        required=False,
        label=_("Organization"),
        empty_label=_("Every organization in your scope"),
        error_messages={"invalid_choice": _("Select an organization from the list.")},
    )
    # UPD-06: optionally, one invitation campaign's results.
    campaign_id = ScopedModelChoiceField(
        queryset=None,
        required=False,
        label=_("Invitation campaign"),
        empty_label=_("Every campaign"),
        error_messages={"invalid_choice": _("Select a campaign from the list.")},
    )
    purpose_code = forms.ChoiceField(
        label=_("Purpose"),
        error_messages={
            "required": _("Select an approved export purpose."),
            "invalid_choice": _("Select an approved export purpose."),
        },
    )
    reason = forms.CharField(
        max_length=500,
        label=_("Reason"),
        error_messages={"required": _("A reason is required to request an export.")},
    )

    def __init__(self, *args, events=None, organizations=None, campaigns=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.events.models import EventEdition
        from apps.organizations.models import Organization

        self.fields["event_edition_id"].queryset = (
            events if events is not None else EventEdition.objects.none()
        )
        self.fields["organization_id"].queryset = (
            organizations if organizations is not None else Organization.objects.none()
        )
        from apps.invitations.models import InvitationCampaign

        self.fields["campaign_id"].queryset = (
            campaigns if campaigns is not None else InvitationCampaign.objects.none()
        )
        self.fields["campaign_id"].label_from_instance = lambda campaign: (
            f"{campaign.name} ({campaign.public_reference})"
        )
        self.fields["purpose_code"].choices = [
            ("", _("Select a purpose")),
            *code_choices("export_purpose", sorted(ALLOWED_EXPORT_PURPOSE_CODES)),
        ]

    def clean(self):
        cleaned = super().clean()
        campaign = cleaned.get("campaign_id")
        event = cleaned.get("event_edition_id")
        organization = cleaned.get("organization_id")
        if campaign is not None and (
            (event is not None and campaign.event_edition_id != event.pk)
            or (organization is not None and campaign.organization_id != organization.pk)
        ):
            self.add_error(
                "campaign_id",
                forms.ValidationError(
                    _("This campaign belongs to another event or organization."),
                    code="mismatch",
                ),
            )
        return cleaned

    def has_scope_violation(self) -> bool:
        """A submitted event or organization outside the caller's scope, or
        a forged or malformed value (all reported as `invalid_choice` by
        `ScopedModelChoiceField`): treated as a scope failure, like a denied
        scope, never as an ordinary validation message."""
        return any(self.has_error(name, code="invalid_choice") for name in SCOPE_FIELDS)
