"""Attendance operations: edition settings, activation, classification
worklist and per-registration changes (`apps.accreditation.attendance`).

Views only coordinate HTTP. Every page and every command checks its own
permission on the server, in the exact scope of its object:

* edition-wide settings and the enforcement switch need
  `accreditation.view_attendancepolicy` / `manage_attendancepolicy` through a
  membership that is not narrowed to an organization, venue or gate;
* the worklist lists only registrations in the reader's
  `accreditation.view_attendanceentitlement` scope;
* a classification or change needs `accreditation.change_attendanceentitlement`
  in the registration's own event and organization scope.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.accounts.selectors import scope_filtered_queryset
from apps.core.middleware.correlation import get_correlation_id
from apps.registrations.models import Registration

from . import attendance
from .forms import (
    AttendanceActivationForm,
    AttendanceChangeForm,
    AttendanceDeactivationForm,
    AttendancePolicyForm,
)
from .models import AttendanceCategory, AttendanceEntitlement
from .presentation import attendance_error_message, readiness_label
from .selectors import events_with_any_scoped_permission, events_with_edition_wide_permission

_LOGIN = "accounts:operational-sign-in"
_VIEW_POLICY = "accreditation.view_attendancepolicy"
_MANAGE_POLICY = "accreditation.manage_attendancepolicy"
_VIEW_ENTITLEMENT = "accreditation.view_attendanceentitlement"
_CHANGE_ENTITLEMENT = "accreditation.change_attendanceentitlement"
PAGE_SIZE = 25

#: Worklist filters, in display order (the default lists what still needs work).
FILTER_UNCLASSIFIED = "unclassified"
FILTER_BADGES = "badges"
FILTER_ALL_DAYS = "all-days"
FILTER_FOLLOWING = "following"
FILTER_EVERY = "every"
_FILTERS = (FILTER_UNCLASSIFIED, FILTER_BADGES, FILTER_ALL_DAYS, FILTER_FOLLOWING, FILTER_EVERY)


def _correlation_id() -> str:
    return get_correlation_id() or ""


@operational_permission_required(_VIEW_ENTITLEMENT, login_url=_LOGIN)
def attendance_overview(request):
    """The editions whose attendance this user may open."""
    events = list(events_with_any_scoped_permission(request.user, _VIEW_ENTITLEMENT))
    policy_events = set(
        events_with_edition_wide_permission(request.user, _VIEW_POLICY).values_list("pk", flat=True)
    )
    rows = []
    for event in events:
        policy = attendance.policy_for(event.pk)
        rows.append(
            {
                "event": event,
                "policy": policy,
                "configured": attendance.is_configured(policy),
                "enforcement_active": bool(policy and policy.enforcement_active),
                "can_view_policy": event.pk in policy_events,
            }
        )
    return render(request, "accreditation/attendance_overview.html", {"rows": rows})


def _edition_or_404(user, event_pk, permission: str):
    event = events_with_edition_wide_permission(user, permission).filter(pk=event_pk).first()
    if event is None:
        raise Http404("No event matches the given query.")
    return event


def _policy_initial(event, policy) -> dict:
    if policy is not None and attendance.days_configured(policy):
        opening, second, third = attendance.conference_days(policy)
    else:
        opening = second = third = None
    return {
        "opening_date": opening,
        "second_date": second,
        "third_date": third,
        "opening_day_capacity": getattr(policy, "opening_day_capacity", None),
        "expected_version": getattr(policy, "version", None),
    }


@operational_permission_required(_VIEW_POLICY, login_url=_LOGIN)
@require_http_methods(["GET", "POST"])
def attendance_policy(request, event_pk):
    """Edition attendance settings, counts and the activation checklist."""
    event = _edition_or_404(request.user, event_pk, _VIEW_POLICY)
    can_manage = (
        events_with_edition_wide_permission(request.user, _MANAGE_POLICY)
        .filter(pk=event.pk)
        .exists()
    )
    policy = attendance.policy_for(event.pk)
    form = AttendancePolicyForm(request.POST or None, initial=_policy_initial(event, policy))
    status = 200
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if form.is_valid():
            data = form.cleaned_data
            try:
                _policy, changed = attendance.update_policy(
                    event_edition_id=event.pk,
                    actor=request.user,
                    opening_date=data["opening_date"],
                    second_date=data["second_date"],
                    third_date=data["third_date"],
                    opening_day_capacity=data["opening_day_capacity"],
                    expected_version=data["expected_version"],
                    reason=data["reason"],
                    correlation_id=_correlation_id(),
                )
            except attendance.AttendanceError as exc:
                form.add_error(None, attendance_error_message(exc))
                status = 400
            else:
                if changed:
                    messages.success(request, _("The attendance settings were saved."))
                else:
                    messages.info(request, _("Nothing changed: the settings were already these."))
                return redirect("accreditation:attendance-policy", event_pk=event.pk)
        else:
            status = 400
    counts = attendance.attendance_counts(event.pk, policy)
    readiness = attendance.readiness(event, policy)
    suggested = attendance.suggested_days(event) if not attendance.days_configured(policy) else None
    context = {
        "event": event,
        "policy": policy,
        "form": form,
        "can_manage": can_manage,
        "counts": counts,
        "readiness": readiness,
        "readiness_rows": [
            {"item": item, "label": readiness_label(item.code)} for item in readiness.items
        ],
        "days": (
            [attendance.format_day(day) for day in attendance.conference_days(policy)]
            if attendance.days_configured(policy)
            else []
        ),
        "suggested_days": [attendance.format_day(day) for day in suggested] if suggested else [],
        "activation_form": AttendanceActivationForm(
            initial={"expected_version": getattr(policy, "version", 1)}
        ),
        "deactivation_form": AttendanceDeactivationForm(
            initial={"expected_version": getattr(policy, "version", 1)}, auto_id="id_off_%s"
        ),
    }
    return render(request, "accreditation/attendance_policy.html", context, status=status)


@operational_permission_required(_MANAGE_POLICY, login_url=_LOGIN)
@require_http_methods(["POST"])
def attendance_activate(request, event_pk):
    event = _edition_or_404(request.user, event_pk, _MANAGE_POLICY)
    form = AttendanceActivationForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Invalid request. Reload the page and try again."))
        return redirect("accreditation:attendance-policy", event_pk=event.pk)
    try:
        attendance.activate_enforcement(
            event_edition=event,
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
            correlation_id=_correlation_id(),
        )
    except attendance.AttendanceError as exc:
        messages.error(request, attendance_error_message(exc))
    else:
        messages.success(
            request,
            _(
                "Attendance enforcement is active. Admission now checks the authorized days. "
                "Offline devices must download new offline data."
            ),
        )
    return redirect("accreditation:attendance-policy", event_pk=event.pk)


@operational_permission_required(_MANAGE_POLICY, login_url=_LOGIN)
@require_http_methods(["POST"])
def attendance_deactivate(request, event_pk):
    event = _edition_or_404(request.user, event_pk, _MANAGE_POLICY)
    form = AttendanceDeactivationForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Enter a reason to switch enforcement off."))
        return redirect("accreditation:attendance-policy", event_pk=event.pk)
    try:
        attendance.deactivate_enforcement(
            event_edition=event,
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
            reason=form.cleaned_data["reason"],
            correlation_id=_correlation_id(),
        )
    except attendance.AttendanceError as exc:
        messages.error(request, attendance_error_message(exc))
    else:
        messages.warning(
            request,
            _(
                "Attendance enforcement is off. Admission no longer checks the authorized days; "
                "the attendance days and the capacity are kept."
            ),
        )
    return redirect("accreditation:attendance-policy", event_pk=event.pk)


def _scoped_registrations(user, codename: str):
    return scope_filtered_queryset(
        user,
        Registration.objects,
        app_label="accreditation",
        codename=codename,
        organization_field="source_organization_id",
    )


class _ChangePermissionCache:
    """`change_attendanceentitlement` per (event, organization) scope, so a
    page of rows costs one check per distinct organization."""

    def __init__(self, user) -> None:
        self.user = user
        self._cache: dict[tuple, bool] = {}

    def __call__(self, registration) -> bool:
        key = (registration.event_edition_id, registration.source_organization_id)
        if key not in self._cache:
            self._cache[key] = has_scoped_permission(
                self.user,
                _CHANGE_ENTITLEMENT,
                event_edition_id=key[0],
                organization_id=key[1],
            )
        return self._cache[key]


@operational_permission_required(_VIEW_ENTITLEMENT, login_url=_LOGIN)
def attendance_worklist(request, event_pk):
    """Approved registrations of one edition with their attendance days,
    filtered to what still needs an operator by default (unclassified)."""
    event = (
        events_with_any_scoped_permission(request.user, _VIEW_ENTITLEMENT)
        .filter(pk=event_pk)
        .first()
    )
    if event is None:
        raise Http404("No event matches the given query.")
    base = (
        _scoped_registrations(request.user, "view_attendanceentitlement")
        .filter(event_edition=event)
        .filter(attendance.seat_holding_q())
        .annotate(attendance_category=attendance._current_category_subquery())
    )
    mismatched_ids = set(
        attendance.badge_marking_mismatches(event.pk)
        .filter(registration__in=base.values("pk"))
        .values_list("registration_id", flat=True)
    )
    shown = request.GET.get("show") or FILTER_UNCLASSIFIED
    if shown not in _FILTERS:
        shown = FILTER_UNCLASSIFIED
    queryset = base
    if shown == FILTER_UNCLASSIFIED:
        queryset = queryset.filter(attendance_category__isnull=True)
    elif shown == FILTER_ALL_DAYS:
        queryset = queryset.filter(attendance_category=AttendanceCategory.ALL_CONFERENCE_DAYS)
    elif shown == FILTER_FOLLOWING:
        queryset = queryset.filter(attendance_category=AttendanceCategory.FOLLOWING_TWO_DAYS)
    elif shown == FILTER_BADGES:
        queryset = queryset.filter(pk__in=mismatched_ids)
    reference = (request.GET.get("reference") or "").strip()[:32]
    if reference:
        queryset = queryset.filter(public_reference__iexact=reference)
    queryset = queryset.select_related("source_organization").order_by("public_reference")
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    can_change = _ChangePermissionCache(request.user)
    entitlements = {
        e.registration_id: e
        for e in AttendanceEntitlement.objects.filter(
            registration__in=[r.pk for r in page.object_list],
            status="CURRENT",
        )
    }
    for registration in page.object_list:
        registration.current_attendance = entitlements.get(registration.pk)
        registration.badge_marking_pending = registration.pk in mismatched_ids
        registration.can_change_attendance = can_change(registration)
    counts_rows = list(base.values_list("attendance_category", flat=True))
    scope_counts = {
        FILTER_UNCLASSIFIED: sum(1 for c in counts_rows if c is None),
        FILTER_ALL_DAYS: sum(1 for c in counts_rows if c == AttendanceCategory.ALL_CONFERENCE_DAYS),
        FILTER_FOLLOWING: sum(1 for c in counts_rows if c == AttendanceCategory.FOLLOWING_TWO_DAYS),
        FILTER_BADGES: len(mismatched_ids),
        FILTER_EVERY: len(counts_rows),
    }
    policy = attendance.policy_for(event.pk)
    querystring = request.GET.copy()
    querystring.pop("page", None)
    context = {
        "event": event,
        "policy": policy,
        "configured": attendance.is_configured(policy),
        "page": page,
        "querystring": querystring.urlencode(),
        "shown": shown,
        "reference": reference,
        "filters": [
            {"key": key, "label": _filter_label(key), "count": scope_counts[key]}
            for key in _FILTERS
        ],
        "counts": attendance.attendance_counts(event.pk, policy),
        "category_choices": AttendanceCategory.choices,
        "next_url": request.get_full_path(),
        "can_view_policy": events_with_edition_wide_permission(request.user, _VIEW_POLICY)
        .filter(pk=event.pk)
        .exists(),
    }
    return render(request, "accreditation/attendance_worklist.html", context)


def _filter_label(key: str) -> str:
    return {
        FILTER_UNCLASSIFIED: _("Not yet classified"),
        FILTER_BADGES: _("Badge marking to record"),
        FILTER_ALL_DAYS: _("All three days"),
        FILTER_FOLLOWING: _("Two following days"),
        FILTER_EVERY: _("All approved"),
    }[key]


def _safe_next(request, fallback: str) -> str:
    candidate = request.POST.get("next") or ""
    if (
        candidate.startswith("/ops/")
        and "\\" not in candidate
        and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return candidate
    return fallback


@operational_permission_required(_CHANGE_ENTITLEMENT, login_url=_LOGIN)
@require_http_methods(["POST"])
def attendance_change(request, pk):
    """Classify an earlier approval or change the attendance days of one."""
    registration = get_object_or_404(
        _scoped_registrations(request.user, "change_attendanceentitlement").select_related(
            "event_edition"
        ),
        pk=pk,
    )
    fallback = reverse("accreditation:registration-detail", kwargs={"pk": registration.pk})
    next_url = _safe_next(request, fallback)
    form = AttendanceChangeForm(request.POST)
    if not form.is_valid():
        errors = [str(error) for errors in form.errors.values() for error in errors]
        messages.error(request, " ".join(errors))
        return redirect(next_url)
    data = form.cleaned_data
    try:
        result = attendance.change_entitlement(
            registration=registration,
            category=data["attendance_category"],
            actor=request.user,
            reason=data["reason"],
            expected_entitlement_id=data["expected_entitlement_id"],
            correlation_id=_correlation_id(),
        )
    except attendance.AttendanceError as exc:
        messages.error(request, attendance_error_message(exc))
        return redirect(next_url)
    if not result.changed:
        messages.info(
            request, _("Nothing changed: these were already the registration's attendance days.")
        )
    else:
        messages.success(
            request,
            _(
                "Attendance days saved for %(reference)s: %(category)s. Admission follows them "
                "immediately and the participant is notified. If a physical badge was already "
                "handed over, record its new attendance marking on the badge page."
            )
            % {
                "reference": registration.public_reference,
                "category": result.entitlement.get_category_display(),
            },
        )
    return redirect(next_url)


def attendance_panel_context(registration, permissions: set[str]) -> dict | None:
    """The attendance section of the accreditation detail page, or None when
    the reader may not see attendance in this registration's scope.

    `permissions` are the reader's codenames in this registration's exact
    scope, already computed by the page. Nothing is queried for a
    registration that is not approved (attendance applies to approvals)."""
    if _VIEW_ENTITLEMENT not in permissions:
        return None
    if not attendance.is_seat_holding(registration):
        return {"approved": False}
    from apps.badges.models import BadgeIssuance, BadgeIssuanceStatus

    history = list(
        AttendanceEntitlement.objects.filter(registration=registration)
        .select_related("decided_by")
        .order_by("-effective_from")
    )
    current = next((entry for entry in history if entry.status == "CURRENT"), None)
    issuance = BadgeIssuance.objects.filter(
        registration=registration, status=BadgeIssuanceStatus.ISSUED
    ).first()
    return {
        "approved": True,
        "current": current,
        "history": history,
        "participant_view": attendance.participant_attendance(registration, entitlement=current),
        "can_change": _CHANGE_ENTITLEMENT in permissions,
        "category_choices": AttendanceCategory.choices,
        "issuance": issuance,
        "badge_marking_pending": issuance is not None
        and (current is None or issuance.attendance_marking != current.category),
    }
