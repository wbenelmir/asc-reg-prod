"""Participant accreditation settings pages (UPD-07).

`/ops/accreditation/settings/` lists, per event edition, the participant
roles, badge types, access profiles and access rules, with their current
assignment counts. Readers need `accreditation.view_participantrole` in the
event; creating needs `accreditation.add_<model>` and editing or
(de)activating `accreditation.change_<model>`, each checked for the setting's
own event on every request (`apps.accreditation.settings_admin`).
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from apps.accounts.policies import (
    deny_operational_access,
    has_scoped_permission,
    operational_permission_required,
)

from .forms import AccreditationSettingForm
from .selectors import events_with_any_scoped_permission
from .settings_admin import (
    SETTING_KINDS,
    SettingError,
    create_setting,
    current_assignment_counts,
    permission,
    set_setting_active,
    update_setting,
)

LOGIN = "accounts:operational-sign-in"
VIEW_PERMISSION = "accreditation.view_participantrole"

_ERRORS = {
    "code_format": gettext_lazy("Use 2 to 64 capital letters, digits, - or _ for the code."),
    "code_taken": gettext_lazy("This code is already used in this event."),
    "wrong_event": gettext_lazy("The profile, zone and gate must belong to the same event."),
    "gate_requires_zone": gettext_lazy("Choose the zone of the gate."),
    "validity_range": gettext_lazy("The end of the validity must be after its start."),
}


def _kind_or_404(slug: str):
    kind = SETTING_KINDS.get(slug)
    if kind is None:
        raise Http404
    return kind


def _event_or_404(request, raw_id):
    events = events_with_any_scoped_permission(request.user, VIEW_PERMISSION)
    event = events.filter(pk=raw_id).first() if raw_id else None
    if event is None:
        raise Http404
    return event


def _selected_event(request, events):
    from apps.events.selectors import NoOpenEventEdition, current_event_edition

    raw = request.GET.get("event") or ""
    if raw:
        chosen = next((event for event in events if str(event.pk) == raw), None)
        if chosen is not None:
            return chosen
    try:
        current = current_event_edition()
    except NoOpenEventEdition:
        current = None
    if current is not None and any(event.pk == current.pk for event in events):
        return current
    return events[0] if events else None


@operational_permission_required(VIEW_PERMISSION, login_url=LOGIN)
def settings_overview(request):
    events = list(events_with_any_scoped_permission(request.user, VIEW_PERMISSION))
    event = _selected_event(request, events)
    sections = []
    if event is not None:
        for kind in SETTING_KINDS.values():
            items = list(kind.model.objects.filter(event_edition=event).order_by("code"))
            counts = current_assignment_counts(kind, [item.pk for item in items])
            for item in items:
                item.current_assignments = counts.get(str(item.pk), 0)
            sections.append(
                {
                    "kind": kind,
                    "items": items,
                    "active_count": sum(1 for item in items if item.is_active),
                    "can_add": has_scoped_permission(
                        request.user, permission(kind, "add"), event_edition_id=event.pk
                    ),
                    "can_change": has_scoped_permission(
                        request.user, permission(kind, "change"), event_edition_id=event.pk
                    ),
                }
            )
    return render(
        request,
        "accreditation/settings_overview.html",
        {"events": events, "event": event, "sections": sections},
    )


def _render_form(request, kind, event, form, obj=None, status=200):
    return render(
        request,
        "accreditation/setting_form.html",
        {"kind": kind, "event": event, "form": form, "object": obj},
        status=status,
    )


def setting_create(request, kind_slug):
    kind = _kind_or_404(kind_slug)
    if not has_scoped_permission(request.user, permission(kind, "add")):
        return deny_operational_access(request, login_url=LOGIN)
    event = _event_or_404(request, request.GET.get("event") or request.POST.get("event"))
    if not has_scoped_permission(request.user, permission(kind, "add"), event_edition_id=event.pk):
        raise Http404
    form = AccreditationSettingForm(
        request.POST or None, kind_slug=kind.slug, event_edition=event, creating=True
    )
    if request.method == "POST" and form.is_valid():
        try:
            create_setting(
                kind,
                actor=request.user,
                event_edition=event,
                code=form.cleaned_data["code"],
                values=form.values(),
            )
        except SettingError as error:
            form.add_error(None, _ERRORS.get(error.code, _("Check the values entered.")))
            return _render_form(request, kind, event, form, status=400)
        messages.success(request, _("Setting created and active."))
        return redirect(f"{reverse('accreditation:settings')}?event={event.pk}")
    return _render_form(request, kind, event, form)


def _setting_or_404(request, kind, pk, action: str):
    obj = kind.model.objects.select_related("event_edition").filter(pk=pk).first()
    if obj is None or not has_scoped_permission(
        request.user, permission(kind, action), event_edition_id=obj.event_edition_id
    ):
        raise Http404
    return obj


def setting_edit(request, kind_slug, pk):
    kind = _kind_or_404(kind_slug)
    if not has_scoped_permission(request.user, permission(kind, "change")):
        return deny_operational_access(request, login_url=LOGIN)
    obj = _setting_or_404(request, kind, pk, "change")
    initial = {
        name: getattr(obj, name)
        for name in AccreditationSettingForm._BY_KIND[kind.slug]
        if hasattr(obj, name)
    }
    form = AccreditationSettingForm(
        request.POST or None,
        initial=initial,
        kind_slug=kind.slug,
        event_edition=obj.event_edition,
        creating=False,
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_setting(kind, obj, actor=request.user, values=form.values())
        except SettingError as error:
            form.add_error(None, _ERRORS.get(error.code, _("Check the values entered.")))
            return _render_form(request, kind, obj.event_edition, form, obj, status=400)
        messages.success(request, _("Setting updated."))
        return redirect(f"{reverse('accreditation:settings')}?event={obj.event_edition_id}")
    return _render_form(request, kind, obj.event_edition, form, obj)


@require_POST
def setting_status(request, kind_slug, pk):
    kind = _kind_or_404(kind_slug)
    if not has_scoped_permission(request.user, permission(kind, "change")):
        return deny_operational_access(request, login_url=LOGIN)
    obj = _setting_or_404(request, kind, pk, "change")
    active = request.POST.get("active") == "1"
    set_setting_active(kind, obj, actor=request.user, active=active)
    messages.success(
        request,
        _("Setting activated.")
        if active
        else _("Setting deactivated. Existing assignments are unchanged."),
    )
    return redirect(f"{reverse('accreditation:settings')}?event={obj.event_edition_id}")
