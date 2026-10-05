"""Event operations forms (UX-4 registration channel control)."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.events.models import PublicRegistrationMode
from apps.events.services import CHANNEL_REASON_MAX_LENGTH, CHANNEL_REASON_MIN_LENGTH

_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"

MODE_CHOICES = [
    (PublicRegistrationMode.OPEN, _("Open: public registration and invitations")),
    (PublicRegistrationMode.INVITATION_ONLY, _("Invitation only: public registration closed")),
    (PublicRegistrationMode.CLOSED, _("Closed: no registration on any channel")),
]


class RegistrationChannelForm(forms.Form):
    """Mode, optional window and a mandatory reason.

    The window is typed in the EVENT's timezone (never the browser's) and
    stored as an aware UTC timestamp. The service re-validates everything.
    """

    mode = forms.ChoiceField(
        label=_("Registration mode"), choices=MODE_CHOICES, widget=forms.RadioSelect
    )
    opens_at = forms.DateTimeField(
        label=_("Opens at (event time)"),
        required=False,
        input_formats=[_LOCAL_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=_LOCAL_FORMAT),
        help_text=_("Optional. Public registration starts at this time."),
    )
    closes_at = forms.DateTimeField(
        label=_("Closes at (event time)"),
        required=False,
        input_formats=[_LOCAL_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=_LOCAL_FORMAT),
        help_text=_("Optional. Public registration stops at this time, even with a link."),
    )
    reason = forms.CharField(
        label=_("Reason for this change"),
        min_length=CHANNEL_REASON_MIN_LENGTH,
        max_length=CHANNEL_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Required and recorded in the audit trail. Do not include personal data."),
    )
    expected_settings_version = forms.IntegerField(widget=forms.HiddenInput, min_value=1)

    def __init__(self, *args, event_timezone: str, **kwargs):
        self.event_zone = ZoneInfo(event_timezone)
        super().__init__(*args, **kwargs)

    def _to_event_time(self, value):
        """A naive wall-clock time in the event timezone becomes an aware UTC value."""
        if value is None:
            return None
        naive = value.replace(tzinfo=None) if value.tzinfo is not None else value
        return naive.replace(tzinfo=self.event_zone).astimezone(datetime.UTC)

    def clean_opens_at(self):
        return self._to_event_time(self.cleaned_data.get("opens_at"))

    def clean_closes_at(self):
        return self._to_event_time(self.cleaned_data.get("closes_at"))

    def clean(self):
        cleaned = super().clean()
        opens_at, closes_at = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens_at is not None and closes_at is not None and closes_at <= opens_at:
            self.add_error("closes_at", _("The closing time must be after the opening time."))
        return cleaned


def event_local_value(value, event_timezone: str) -> str:
    """An aware timestamp as the `datetime-local` value in the event timezone."""
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(event_timezone)).strftime(_LOCAL_FORMAT)
