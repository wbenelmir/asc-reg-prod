"""Organization management forms (UPD-06). Validation only: the services in
`apps.organizations.services` write and audit."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.forms import LocalizedOrderModelChoiceField
from apps.core.models import selectable_countries
from apps.core.widgets import SearchableSelect
from apps.organizations.models import NEW_SELECTION_ORGANIZATION_TYPES, OrganizationType
from apps.registrations.forms import BootstrapFormMixin


class OrganizationForm(BootstrapFormMixin, forms.Form):
    official_name = forms.CharField(label=_("Official name"), max_length=300)
    organization_type = forms.ChoiceField(label=_("Organization type"))
    country_code = LocalizedOrderModelChoiceField(
        label=_("Country"),
        queryset=selectable_countries(),
        required=False,
        widget=SearchableSelect,
    )
    #: Shown only when organizations with the same normalized name exist.
    confirm_duplicate = forms.BooleanField(
        label=_("Create it anyway: this is a different organization."), required=False
    )

    def __init__(self, *args, current_type: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        labels = dict(OrganizationType.choices)
        codes = list(NEW_SELECTION_ORGANIZATION_TYPES)
        if current_type and current_type not in codes:
            codes.append(current_type)  # a legacy type stays selectable on its own record
        self.fields["organization_type"].choices = [(code, labels[code]) for code in codes]
        self.fields["country_code"].label_from_instance = lambda country: country.localized_name

    def clean_official_name(self):
        name = " ".join((self.cleaned_data.get("official_name") or "").split())
        if len(name) < 2:
            raise forms.ValidationError(_("Enter the organization's official name."))
        return name


class OrganizationFilterForm(forms.Form):
    """The organizations list filters (GET)."""

    q = forms.CharField(required=False, max_length=100)
    status = forms.ChoiceField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.organizations.models import OrganizationStatus

        self.fields["status"].choices = [("", ""), *OrganizationStatus.choices]
