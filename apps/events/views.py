"""Registration channel control for authorized operators (UX-4, M24, D-12)."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import operational_permission_required
from apps.core.middleware.correlation import get_correlation_id

from .forms import MODE_CHOICES, RegistrationChannelForm, event_local_value
from .policies.registration_channels import ChannelState, effective_channel_state
from .selectors import events_with_channel_control
from .services import (
    RegistrationChannelChangeConflict,
    RegistrationChannelChangeDenied,
    RegistrationChannelChangeError,
    change_registration_channel,
)

_PERMISSION = "events.manage_registration_channels"
_LOGIN = "accounts:operational-sign-in"


def _state_label(state: str) -> str:
    return {
        ChannelState.PUBLIC_OPEN: _("Public registration open"),
        ChannelState.INVITATION_ONLY: _("Invitations only"),
        ChannelState.CLOSED: _("Registration closed on every channel"),
    }[state]


def _state_tone(state: str) -> str:
    return {
        ChannelState.PUBLIC_OPEN: "success",
        ChannelState.INVITATION_ONLY: "warning",
        ChannelState.CLOSED: "danger",
    }[state]


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
def channel_list(request):
    now = timezone.now()
    events = list(events_with_channel_control(request.user))
    for event in events:
        event.channel_state = effective_channel_state(event, now=now)
        event.channel_state_label = _state_label(event.channel_state)
        event.channel_state_tone = _state_tone(event.channel_state)
    return render(request, "events/channel_list.html", {"events": events})


@operational_permission_required(_PERMISSION, login_url=_LOGIN)
@require_http_methods(["GET", "POST"])
def channel_detail(request, pk):
    # Scope first: an event outside the operator's scope is a 404, the same
    # answer as an unknown id.
    event = events_with_channel_control(request.user).filter(pk=pk).first()
    if event is None:
        raise Http404("No event matches the given query.")
    initial = {
        "mode": event.public_registration_mode,
        "opens_at": event_local_value(event.registration_opens_at, event.timezone),
        "closes_at": event_local_value(event.registration_closes_at, event.timezone),
        "expected_settings_version": event.settings_version,
    }
    form = RegistrationChannelForm(
        request.POST or None, initial=initial, event_timezone=event.timezone
    )
    status = 200
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            result = change_registration_channel(
                event_edition_id=event.pk,
                actor=request.user,
                mode=data["mode"],
                opens_at=data["opens_at"],
                closes_at=data["closes_at"],
                reason=data["reason"],
                expected_settings_version=data["expected_settings_version"],
                correlation_id=get_correlation_id() or "",
            )
        except RegistrationChannelChangeDenied:
            raise Http404("No event matches the given query.") from None
        except RegistrationChannelChangeConflict:
            messages.error(
                request,
                _(
                    "The registration settings changed since you opened this page. "
                    "Review them and try again."
                ),
            )
            return redirect("events:channel-detail", pk=event.pk)
        except RegistrationChannelChangeError:
            form.add_error(
                None, _("This change could not be applied. Check the values and try again.")
            )
            status = 400
        else:
            if result.changed:
                messages.success(request, _("The registration channels were updated."))
            else:
                messages.info(
                    request, _("Nothing changed: the settings were already in this state.")
                )
            return redirect("events:channel-detail", pk=event.pk)
    elif request.method == "POST":
        status = 400
    now = timezone.now()
    state = effective_channel_state(event, now=now)
    return render(
        request,
        "events/channel_detail.html",
        {
            "event": event,
            "form": form,
            "state": state,
            "state_label": _state_label(state),
            "state_tone": _state_tone(state),
            "mode_label": dict(MODE_CHOICES).get(event.public_registration_mode, ""),
            "opens_at_local": event_local_value(event.registration_opens_at, event.timezone),
            "closes_at_local": event_local_value(event.registration_closes_at, event.timezone),
        },
        status=status,
    )
