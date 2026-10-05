"""Entry & Security interface space (TRD §5.2 item 4; Phase 3 Prompt 4).

Views coordinate HTTP only: every state change lives in `apps.entry.services`.

Two audiences, two URL spaces:

* `/ops/entry/...` -- device administration (enroll, rescope, suspend,
  revoke), gated by `entry.view_entrydevice` / `entry.manage_entrydevice`;
* `/entry/...` -- the checkpoint itself, reachable only from an enrolled
  device (HttpOnly device-credential cookie) by a signed-in operator with a
  live checkpoint session. `checkpoint_required` re-validates the whole
  chain on every request (`apps.entry.services.sessions.resolve_checkpoint`).

Every checkpoint response is `never_cache`: result screens carry
participant details and must not survive in a browser cache or history.
The browser only ever receives random handles (operation id, candidate
selection token, photo nonce); registration and credential identifiers stay
in the server-side session (`apps.entry.session_state`).
"""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.audit import action_codes
from apps.badges.services import OperationConflictError
from apps.entry import session_state
from apps.entry.forms import (
    CandidateSelectForm,
    CheckpointSetupForm,
    DecisionForm,
    DeviceActivationForm,
    DeviceLifecycleForm,
    DeviceRegisterForm,
    DeviceScopeForm,
    DeviceVersionForm,
    IdentityLookupForm,
    ManualSearchForm,
    OverrideForm,
    QrVerifyForm,
    ReferenceLookupForm,
)
from apps.entry.models import (
    VERIFICATION_METHOD_ORDER,
    EntryDecision,
    EntryDecisionReason,
    EntryDevice,
    EntryDeviceSession,
    EntryReasonCode,
    EntryResult,
    VerificationMethod,
)
from apps.entry.observability import degraded_notice_for, gate_health
from apps.entry.policies import checkpoint_permission, may_manage_devices, method_is_permitted
from apps.entry.presentation import service_error_message
from apps.entry.selectors import (
    candidate_projection,
    devices_visible_to,
    labelled_attempts,
    recent_anomaly_signals,
    recent_denied_attempts,
    recent_entry_events,
    result_clear_seconds,
    result_projection,
)
from apps.entry.services import (
    EntryConcurrencyError,
    EntryPermissionError,
    EntryServiceError,
    EntryStateError,
    audit,
)
from apps.entry.services.decisions import (
    OverrideNotPermittedError,
    StaleVerificationError,
    available_override_reasons,
    pending_from_assessment,
    record_entry_decision,
    record_override,
)
from apps.entry.services.devices import (
    DeviceConfigurationError,
    DeviceEnrollmentError,
    authenticate_device,
    change_device_scope,
    current_scope,
    enroll_device_with_code,
    format_activation_code,
    issue_activation_code,
    register_device,
    resume_device,
    revoke_device,
    suspend_device,
)
from apps.entry.services.limits import KIND_INVALID_SCANS, EntryLookupThrottled
from apps.entry.services.sessions import (
    CheckpointSetupError,
    CheckpointUnavailable,
    end_device_sessions,
    join_checkpoint_session,
    resolve_checkpoint,
    start_checkpoint_session,
)
from apps.entry.services.verification import (
    assess_selected_candidate,
    lookup_identity,
    lookup_reference,
    manual_search,
    verify_qr,
)
from apps.events.models import EventEdition, Gate, Zone

_SIGN_IN = "accounts:operational-sign-in"

CHECKPOINT_UNAVAILABLE_MESSAGES = {
    "DEVICE_NOT_ENROLLED": gettext_lazy("This browser is not an enrolled, active entry device."),
    "DEVICE_NOT_SCOPED": gettext_lazy("This device has no checkpoint scope."),
    "NO_CHECKPOINT_SESSION": gettext_lazy("Set up the checkpoint to start verifying."),
    "DEVICE_SESSION_ENDED": gettext_lazy("The checkpoint session has ended. Set it up again."),
    "DEVICE_SCOPE_CHANGED": gettext_lazy(
        "This device's scope was changed. Set up the checkpoint again."
    ),
    "OPERATOR_SESSION_ENDED": gettext_lazy("Your checkpoint session has ended. Start it again."),
    "OPERATOR_NOT_AUTHORIZED": gettext_lazy("You are no longer authorized at this checkpoint."),
}

#: Tone, icon (from static/img/icons.svg), and localized heading per
#: result. Text AND an icon accompany every colour (UI/UX §9.5, §12.2); the
#: template never relies on colour alone, and each result has a distinct
#: icon so two results never look alike to a colour-blind operator.
RESULT_PRESENTATION = {
    EntryResult.ALLOWED: ("success", "check-circle", gettext_lazy("Verified")),
    EntryResult.ALLOWED_WITH_ADVISORY: ("info", "info", gettext_lazy("Verified — advisory")),
    EntryResult.MANUAL_REVIEW: (
        "warning",
        "user-check",
        gettext_lazy("Manual verification required"),
    ),
    EntryResult.STALE: ("warning", "refresh", gettext_lazy("Outdated credential or verification")),
    EntryResult.DENIED: ("danger", "x-circle", gettext_lazy("Do not admit")),
    EntryResult.UNSUPPORTED: ("danger", "ban", gettext_lazy("Unsupported credential")),
    EntryResult.TECHNICAL_ERROR: (
        "neutral",
        "alert-octagon",
        gettext_lazy("Technical error — verify again"),
    ),
}

#: Secondary lookup tabs on the verification screen, in the approved
#: lookup-priority order (Flow §10.4): form key, icon, label.
_LOOKUP_TABS = (
    ("identity", "id-card", gettext_lazy("NIN or passport")),
    ("reference", "hash", gettext_lazy("Registration reference")),
    ("manual", "search", gettext_lazy("Manual search")),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_from_request(request) -> EntryDevice | None:
    return authenticate_device(request.COOKIES.get(settings.ENTRY_DEVICE_COOKIE_NAME))


def _checkpoint_unavailable_message(reason: str) -> str:
    """The localized message for a checkpoint-unavailable reason; an unknown
    reason gets the generic localized fallback, never the raw code (P8-01)."""
    from apps.core.service_errors import GENERIC_SERVICE_ERROR

    return str(CHECKPOINT_UNAVAILABLE_MESSAGES.get(reason, GENERIC_SERVICE_ERROR))


def _unavailable_redirect(request, reason: str) -> HttpResponse:
    messages.info(request, _checkpoint_unavailable_message(reason))
    return redirect("entry:home")


def checkpoint_required(view_func):
    """Resolve and re-validate the checkpoint chain for every request."""

    @login_required(login_url=_SIGN_IN)
    @never_cache
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        device_session_id, operator_session_id = session_state.checkpoint_ids(request)
        try:
            checkpoint = resolve_checkpoint(
                device=_device_from_request(request),
                device_session_id=device_session_id,
                operator_session_id=operator_session_id,
                user=request.user,
            )
        except CheckpointUnavailable as exc:
            session_state.clear_entry_session_state(request, reason=exc.reason)
            return _unavailable_redirect(request, exc.reason)
        request.checkpoint = checkpoint
        return view_func(request, *args, **kwargs)

    return wrapped


def _checkpoint_context(request, *, nav: str = "") -> dict:
    """Context every checkpoint page shares: identity, device scope, mode.

    Configuration data only; never a participant value.
    """
    checkpoint = request.checkpoint
    return {
        "checkpoint": checkpoint,
        "mode_label": _("Online"),
        "entry_nav": nav,
        "may_monitor": checkpoint_permission(request.user, "view_entryevent", checkpoint),
        "scope_method_labels": [
            VerificationMethod(method).label
            for method in VERIFICATION_METHOD_ORDER
            if method in checkpoint.verification_methods
        ],
        "connection_poll_seconds": settings.ENTRY_CONNECTION_POLL_SECONDS,
        # Phase 4 Prompt 2 (OFF-001): repeated checks before "offline".
        "connection_failures_to_offline": settings.ENTRY_OFFLINE_HEALTH["failures_to_offline"],
        "connection_failure_span_seconds": settings.ENTRY_OFFLINE_HEALTH["failure_span_seconds"],
        "connection_retry_seconds": settings.ENTRY_OFFLINE_HEALTH_RETRY_SECONDS,
    }


def _conflict(request, message: str, status: int = 409) -> HttpResponse:
    return render(request, "entry/conflict.html", {"message": message}, status=status)


# ---------------------------------------------------------------------------
# Checkpoint: set-up, verification, decisions
# ---------------------------------------------------------------------------


@login_required(login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET", "POST"])
def checkpoint_home(request):
    """Checkpoint set-up (Flow §10.3): confirm gate, choose zone, start shift."""
    device = _device_from_request(request)
    if device is None:
        return render(
            request,
            "entry/not_enrolled.html",
            {"may_activate": has_scoped_permission(request.user, "entry.manage_entrydevice")},
            status=403,
        )
    device_session_id, operator_session_id = session_state.checkpoint_ids(request)
    if device_session_id and operator_session_id:
        try:
            resolve_checkpoint(
                device=device,
                device_session_id=device_session_id,
                operator_session_id=operator_session_id,
                user=request.user,
            )
            return redirect("entry:verify")
        except CheckpointUnavailable:
            session_state.clear_entry_session_state(request, reason="REPLACED")

    scope = current_scope(device)
    zones = list(scope.permitted_zones.filter(is_active=True).order_by("code")) if scope else []
    now = timezone.now()
    open_session = (
        EntryDeviceSession.objects.select_related("gate", "zone")
        .filter(device=device, ended_at__isnull=True, expires_at__gt=now)
        .first()
    )
    form = CheckpointSetupForm(request.POST or None, zones=zones)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "join" and open_session is not None:
                operator_session = join_checkpoint_session(
                    device_session=open_session, user=request.user
                )
                session_state.set_checkpoint_ids(
                    request,
                    device_session_id=open_session.pk,
                    operator_session_id=operator_session.pk,
                )
                return redirect("entry:verify")
            if action == "start" and form.is_valid() and scope is not None:
                device_session, operator_session = start_checkpoint_session(
                    device=device,
                    user=request.user,
                    gate_id=scope.gate_id,
                    zone_id=form.cleaned_data["zone_id"],
                )
                session_state.set_checkpoint_ids(
                    request,
                    device_session_id=device_session.pk,
                    operator_session_id=operator_session.pk,
                )
                return redirect("entry:verify")
        except CheckpointUnavailable as exc:
            messages.error(request, _checkpoint_unavailable_message(exc.reason))
        except (CheckpointSetupError, EntryPermissionError) as exc:  # fmt: skip
            messages.error(request, service_error_message(exc))
    return render(
        request,
        "entry/checkpoint_setup.html",
        {
            "device": device,
            "scope": scope,
            "form": form,
            "open_session": open_session,
        },
        status=200,
    )


def _render_verify(
    request,
    *,
    bound_form=None,
    bound_key: str = "",
    throttled: EntryLookupThrottled | None = None,
    status: int = 200,
):
    """Render the verification screen, optionally re-showing one bound form
    with its field errors. Sensitive inputs never echo their submitted value
    (`NonEchoTextInput`), so a re-render cannot leak a token or number."""
    checkpoint = request.checkpoint
    user = request.user
    permitted = {m: method_is_permitted(user, m, checkpoint) for m in VerificationMethod.values}
    identity_methods = [
        m for m in (VerificationMethod.NIN, VerificationMethod.PASSPORT) if permitted[m]
    ]
    forms_by_key = {
        "qr": QrVerifyForm() if permitted[VerificationMethod.QR] else None,
        "identity": IdentityLookupForm(methods=identity_methods) if identity_methods else None,
        "reference": ReferenceLookupForm() if permitted[VerificationMethod.REFERENCE] else None,
        "manual": ManualSearchForm() if permitted[VerificationMethod.MANUAL] else None,
    }
    if bound_form is not None and forms_by_key.get(bound_key) is not None:
        if bound_key == "identity":
            # Offer only the permitted document types on the re-render too.
            bound_form.fields["method"].choices = forms_by_key["identity"].fields["method"].choices
        forms_by_key[bound_key] = bound_form
    lookup_tabs = [
        {"key": key, "icon": icon, "label": label}
        for key, icon, label in _LOOKUP_TABS
        if forms_by_key[key] is not None
    ]
    tab_keys = [tab["key"] for tab in lookup_tabs]
    context = {
        **_checkpoint_context(request, nav="verify"),
        "qr_form": forms_by_key["qr"],
        "identity_form": forms_by_key["identity"],
        "reference_form": forms_by_key["reference"],
        "manual_form": forms_by_key["manual"],
        "lookup_tabs": lookup_tabs,
        "active_tab": bound_key if bound_key in tab_keys else (tab_keys[0] if tab_keys else ""),
        "active_form": bound_key if bound_form is not None else "",
        "degraded_notice": degraded_notice_for(checkpoint),
        "throttle_notice": _throttle_notice(throttled) if throttled is not None else "",
        "inactivity_minutes": max(1, settings.ENTRY_OPERATOR_INACTIVITY_SECONDS // 60),
        "manual_search_min_chars": settings.ENTRY_MANUAL_SEARCH_MIN_CHARS,
        "manual_search_max_results": settings.ENTRY_MANUAL_SEARCH_MAX_RESULTS,
    }
    return render(request, "entry/verify.html", context, status=status)


def _throttle_notice(exc: EntryLookupThrottled) -> str:
    minutes = max(1, round(exc.retry_after_seconds / 60))
    if exc.kind == KIND_INVALID_SCANS:
        return ngettext(
            "Too many invalid codes were scanned from your session. QR verification is paused "
            "for up to %(minutes)d minute. Call your supervisor.",
            "Too many invalid codes were scanned from your session. QR verification is paused "
            "for up to %(minutes)d minutes. Call your supervisor.",
            minutes,
        ) % {"minutes": minutes}
    return ngettext(
        "You have reached the limit for identity and reference lookups. Try again in up to "
        "%(minutes)d minute, or call your supervisor.",
        "You have reached the limit for identity and reference lookups. Try again in up to "
        "%(minutes)d minutes, or call your supervisor.",
        minutes,
    ) % {"minutes": minutes}


@checkpoint_required
@require_http_methods(["GET"])
def verify(request):
    """The verification screen, optimized for repeated scanning."""
    return _render_verify(request)


def _render_outcome(request, outcome) -> HttpResponse:
    checkpoint = request.checkpoint
    if outcome.candidates:
        token = session_state.store_candidates(
            request,
            method=outcome.method,
            registration_ids=[c.pk for c in outcome.candidates],
            verified_ids=outcome.extra.get("verified_registration_ids", ()),
        )
        return render(
            request,
            "entry/candidates.html",
            {
                **_checkpoint_context(request),
                "selection_token": token,
                "candidates": [candidate_projection(c) for c in outcome.candidates],
                "truncated": outcome.truncated,
            },
        )

    projection = result_projection(outcome=outcome, user=request.user, checkpoint=checkpoint)
    tone, icon, heading = RESULT_PRESENTATION[outcome.result]
    context = {
        **_checkpoint_context(request, nav="verify"),
        "projection": projection,
        "tone": tone,
        "icon": icon,
        "heading": heading,
        "method_label": VerificationMethod(outcome.method).label,
        "decision_window_seconds": settings.ENTRY_DECISION_TICKET_SECONDS,
        "clear_after_seconds": result_clear_seconds(),
        "pending_operation_id": None,
        "can_admit": False,
        "override_reasons": [],
        "photo_nonce": None,
        "decision_reasons": [
            (value, label)
            for value, label in EntryDecisionReason.choices
            if value != EntryDecisionReason.FOLLOWS_RESULT
        ],
    }
    assessment = outcome.assessment
    # A decision may be recorded only for a context in THIS edition.
    if assessment is not None and assessment.reason_code != EntryReasonCode.WRONG_EVENT:
        pending = pending_from_assessment(
            assessment=assessment,
            checkpoint=checkpoint,
            method=outcome.method,
            identity_verified=outcome.identity_verified,
        )
        session_state.store_pending(request, pending)
        context["pending_operation_id"] = pending.operation_id
        context["can_admit"] = assessment.is_admittable
        context["override_reasons"] = available_override_reasons(
            checkpoint=checkpoint, assessment=assessment
        )
        participant = projection.get("participant")
        if participant and participant.get("has_photo"):
            context["photo_nonce"] = session_state.grant_photo(request, assessment.registration.pk)
    return render(request, "entry/result.html", context)


def _lookup_command(request, form_class, form_key: str, runner) -> HttpResponse:
    form = form_class(request.POST)
    if not form.is_valid():
        # Re-show the same form with its field errors (never the value).
        return _render_verify(request, bound_form=form, bound_key=form_key, status=400)
    try:
        outcome = runner(form.cleaned_data)
    except EntryPermissionError:
        return _conflict(request, _("This lookup method is not permitted here."), status=403)
    except EntryLookupThrottled as exc:
        response = _render_verify(request, throttled=exc, status=429)
        response["Retry-After"] = str(exc.retry_after_seconds)
        return response
    return _render_outcome(request, outcome)


@checkpoint_required
@require_http_methods(["POST"])
def verify_qr_view(request):
    return _lookup_command(
        request,
        QrVerifyForm,
        "qr",
        lambda data: verify_qr(checkpoint=request.checkpoint, raw_token=data["token"]),
    )


@checkpoint_required
@require_http_methods(["POST"])
def lookup_identity_view(request):
    return _lookup_command(
        request,
        IdentityLookupForm,
        "identity",
        lambda data: lookup_identity(
            checkpoint=request.checkpoint,
            method=data["method"],
            raw_value=data["value"],
            country_code=data.get("country_code") or "",
        ),
    )


@checkpoint_required
@require_http_methods(["POST"])
def lookup_reference_view(request):
    return _lookup_command(
        request,
        ReferenceLookupForm,
        "reference",
        lambda data: lookup_reference(
            checkpoint=request.checkpoint, raw_reference=data["reference"]
        ),
    )


@checkpoint_required
@require_http_methods(["POST"])
def manual_search_view(request):
    return _lookup_command(
        request,
        ManualSearchForm,
        "manual",
        lambda data: manual_search(checkpoint=request.checkpoint, raw_query=data["query"]),
    )


@checkpoint_required
@require_http_methods(["POST"])
def select_candidate(request):
    form = CandidateSelectForm(request.POST)
    if not form.is_valid():
        return _conflict(request, _("Invalid selection. Verify again."))
    selected = session_state.take_candidate(
        request, form.cleaned_data["selection"], form.cleaned_data["index"]
    )
    if selected is None:
        messages.warning(request, _("This selection has expired. Verify again."))
        return redirect("entry:verify")
    method, registration_id, identity_verified = selected
    try:
        outcome = assess_selected_candidate(
            checkpoint=request.checkpoint,
            method=method,
            registration_id=registration_id,
            identity_verified=identity_verified,
        )
    except EntryPermissionError:
        return _conflict(request, _("This lookup method is not permitted here."), status=403)
    return _render_outcome(request, outcome)


_DECISION_MESSAGES = {
    EntryDecision.ADMIT: gettext_lazy("Entry recorded: admitted."),
    EntryDecision.DO_NOT_ADMIT: gettext_lazy("Entry recorded: not admitted."),
    EntryDecision.REDIRECTED: gettext_lazy("Entry recorded: redirected."),
}


@checkpoint_required
@require_http_methods(["POST"])
def record_decision(request):
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return _conflict(request, _("Invalid request. Verify again."))
    pending = session_state.get_pending(request, form.cleaned_data["operation_id"])
    if pending is None:
        messages.warning(request, _("This verification has expired. Verify again."))
        return redirect("entry:verify")
    try:
        outcome = record_entry_decision(
            checkpoint=request.checkpoint,
            pending=pending,
            decision=form.cleaned_data["decision"],
            decision_reason=form.cleaned_data.get("decision_reason") or "",
        )
    except StaleVerificationError:
        messages.warning(
            request, _("The situation changed or the verification expired. Verify again.")
        )
        return redirect("entry:verify")
    except OperationConflictError:
        return _conflict(request, _("This verification was already used for a different decision."))
    except EntryPermissionError:
        return _conflict(request, _("You are not authorized to record entry here."), status=403)
    except EntryStateError:
        messages.error(request, _("This result does not permit admission."))
        return redirect("entry:verify")
    session_state.clear_participant_state(request)
    if outcome.replayed:
        messages.info(request, _("This decision was already recorded."))
    else:
        messages.success(request, _DECISION_MESSAGES[outcome.entry_event.decision])
    return redirect("entry:verify")


@checkpoint_required
@require_http_methods(["POST"])
def record_override_view(request):
    form = OverrideForm(request.POST)
    if not form.is_valid():
        return _conflict(request, _("Invalid request. Verify again."))
    pending = session_state.get_pending(request, form.cleaned_data["operation_id"])
    if pending is None:
        messages.warning(request, _("This verification has expired. Verify again."))
        return redirect("entry:verify")
    try:
        outcome = record_override(
            checkpoint=request.checkpoint,
            pending=pending,
            override_reason_id=form.cleaned_data["override_reason_id"],
            note=form.cleaned_data.get("note") or "",
        )
    except StaleVerificationError:
        messages.warning(
            request, _("The situation changed or the verification expired. Verify again.")
        )
        return redirect("entry:verify")
    except OperationConflictError:
        return _conflict(request, _("This verification was already used for a different decision."))
    except EntryPermissionError:
        return _conflict(request, _("You are not authorized to override here."), status=403)
    except OverrideNotPermittedError:
        messages.error(request, _("This result cannot be overridden with that reason."))
        return redirect("entry:verify")
    session_state.clear_participant_state(request)
    if outcome.replayed:
        messages.info(request, _("This decision was already recorded."))
    else:
        messages.success(request, _("Override recorded: admitted."))
    return redirect("entry:verify")


@checkpoint_required
@require_http_methods(["GET"])
def participant_photo(request, nonce):
    """The profile photograph of the context just verified -- and only that.

    Addressed by a short-lived, session-bound nonce, never by a document or
    registration id; streamed inline, never cached, and audited.
    """
    from apps.documents.services import active_profile_photo
    from apps.documents.storage import ObjectNotFound, PrivateStorageError, get_private_storage
    from apps.registrations.models import Registration

    registration_id = session_state.photo_registration_id(request, nonce)
    if registration_id is None:
        raise Http404("Not found.")
    registration = Registration.objects.filter(
        pk=registration_id, event_edition=request.checkpoint.event_edition
    ).first()
    document = active_profile_photo(registration) if registration is not None else None
    if document is None:
        raise Http404("Not found.")
    stored = document.stored_object
    try:
        stream = get_private_storage().open(stored.storage_key)
    except (ObjectNotFound, PrivateStorageError):  # fmt: skip
        raise Http404("Not found.") from None
    audit(
        action_code=action_codes.ENTRY_PHOTO_VIEWED,
        actor=request.user,
        target_type="Registration",
        target_uuid=registration.pk,
        event_edition_id=registration.event_edition_id,
        after_summary={"gate": request.checkpoint.gate.code},
    )
    response = FileResponse(stream, content_type=stored.content_type)
    response["Content-Disposition"] = "inline"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@never_cache
@require_http_methods(["GET"])
def checkpoint_status(request):
    """Passive connection check for an open checkpoint page (UI/UX §9.7).

    Re-validates the whole checkpoint chain WITHOUT refreshing any
    inactivity clock (`resolve_checkpoint(touch=False)`, and the path is in
    `OPERATIONAL_PASSIVE_PATHS`), so an unattended page can never keep a
    session alive. Returns only a state word -- never a participant value,
    identifier, or count.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"state": "session-ended"}, status=401)
    device_session_id, operator_session_id = session_state.checkpoint_ids(request)
    try:
        checkpoint = resolve_checkpoint(
            device=_device_from_request(request),
            device_session_id=device_session_id,
            operator_session_id=operator_session_id,
            user=request.user,
            touch=False,
        )
    except CheckpointUnavailable:
        return JsonResponse({"state": "session-ended"}, status=403)
    state = "degraded" if gate_health(checkpoint).degraded else "online"
    return JsonResponse({"state": state})


@checkpoint_required
@require_http_methods(["GET"])
def checkpoint_monitor(request):
    """Supervisor view: recent entries and denied attempts at this gate."""
    checkpoint = request.checkpoint
    if not checkpoint_permission(request.user, "view_entryevent", checkpoint):
        return _conflict(request, _("You are not authorized to view this monitor."), status=403)
    return render(
        request,
        "entry/monitor.html",
        {
            **_checkpoint_context(request, nav="monitor"),
            "events": recent_entry_events(checkpoint),
            "denied_attempts": labelled_attempts(recent_denied_attempts(checkpoint)),
            "anomalies": recent_anomaly_signals(checkpoint),
            # Phase 4 Prompt 3: the gate's offline reconciliation queue.
            "may_reconcile": checkpoint_permission(
                request.user, "reconcile_offline_operation", checkpoint
            ),
        },
    )


@login_required(login_url=_SIGN_IN)
@never_cache
@require_http_methods(["POST"])
def end_checkpoint_session(request):
    """End this operator's checkpoint session; optionally close the checkpoint."""
    device = _device_from_request(request)
    device_session_id, _operator_session_id = session_state.checkpoint_ids(request)
    close_checkpoint = request.POST.get("close") == "1"
    session_state.clear_entry_session_state(request, reason="SIGNED_OUT")
    if close_checkpoint and device is not None and device_session_id:
        session = EntryDeviceSession.objects.filter(
            pk=device_session_id, device=device, ended_at__isnull=True
        ).first()
        if session is not None and session.started_by_id == request.user.pk:
            end_device_sessions(device=device, reason="CLOSED")
    messages.info(request, _("Your checkpoint session has ended."))
    return redirect("entry:home")


# ---------------------------------------------------------------------------
# Device activation (on the device itself)
# ---------------------------------------------------------------------------


@login_required(login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET", "POST"])
def device_activate(request):
    if not has_scoped_permission(request.user, "entry.manage_entrydevice"):
        return _conflict(request, _("You are not authorized to enroll entry devices."), status=403)
    form = DeviceActivationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            enrollment = enroll_device_with_code(
                raw_code=form.cleaned_data["code"],
                actor=request.user,
                platform_summary=request.META.get("HTTP_USER_AGENT", "")[:200],
            )
        except DeviceEnrollmentError:
            messages.error(request, _("This activation code cannot be used."))
            return render(request, "entry/device_activate.html", {"form": DeviceActivationForm()})
        response = redirect("entry:home")
        max_age = int((enrollment.device.expires_at - timezone.now()).total_seconds())
        response.set_cookie(
            settings.ENTRY_DEVICE_COOKIE_NAME,
            enrollment.device_secret,
            max_age=max(max_age, 0),
            path="/entry/",
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Strict",
        )
        messages.success(
            request,
            _("This browser is now enrolled as entry device “%(name)s”. Sign out now.")
            % {"name": enrollment.device.public_name},
        )
        return response
    return render(request, "entry/device_activate.html", {"form": DeviceActivationForm()})


# ---------------------------------------------------------------------------
# Device administration (operations back office)
# ---------------------------------------------------------------------------


def _scoped_event_or_404(request, pk) -> EventEdition:
    event = get_object_or_404(EventEdition, pk=pk)
    if not has_scoped_permission(request.user, "entry.view_entrydevice", event_edition_id=event.pk):
        raise Http404("Not found.")
    return event


def _device_or_404(request, public_id) -> EntryDevice:
    return get_object_or_404(devices_visible_to(request.user), public_id=public_id)


def _event_gates_and_zones(event):
    gates = (
        Gate.objects.select_related("venue")
        .filter(venue__event_edition=event, is_active=True, supports_entry=True)
        .order_by("venue__code", "code")
    )
    zones = (
        Zone.objects.select_related("venue")
        .filter(venue__event_edition=event, is_active=True)
        .order_by("venue__code", "code")
    )
    return list(gates), list(zones)


def _resolve_scope_inputs(event, cleaned):
    gate = get_object_or_404(
        Gate.objects.select_related("venue"), pk=cleaned["gate_id"], venue__event_edition=event
    )
    zones = list(Zone.objects.filter(pk__in=cleaned["zone_ids"], venue__event_edition=event))
    if len(zones) != len(set(cleaned["zone_ids"])):
        raise Http404("Not found.")
    return gate, zones


def _activation_code_response(request, device, code) -> HttpResponse:
    response = render(
        request,
        "entry/device_activation_code.html",
        {
            "device": device,
            "code": format_activation_code(code),
            "valid_minutes": settings.ENTRY_DEVICE_ACTIVATION_CODE_SECONDS // 60,
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response


@operational_permission_required("entry.view_entrydevice", login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET", "POST"])
def device_list(request, event_pk):
    event = _scoped_event_or_404(request, event_pk)
    can_manage = may_manage_devices(request.user, event_edition_id=event.pk)
    gates, zones = _event_gates_and_zones(event)
    form = DeviceRegisterForm(request.POST or None, gates=gates, zones=zones)
    if request.method == "POST":
        if not can_manage:
            return _conflict(request, _("You are not authorized to manage entry devices."), 403)
        if form.is_valid():
            gate, selected_zones = _resolve_scope_inputs(event, form.cleaned_data)
            try:
                registration = register_device(
                    event_edition=event,
                    public_name=form.cleaned_data["public_name"],
                    expires_at=form.cleaned_data["expires_at"],
                    venue=gate.venue,
                    gate=gate,
                    zones=selected_zones,
                    verification_methods=form.cleaned_data["verification_methods"],
                    actor=request.user,
                    reason=form.cleaned_data.get("reason") or "",
                    offline_capable=form.cleaned_data.get("offline_capable") or False,
                    offline_sensitivity=form.cleaned_data.get("offline_sensitivity") or "STANDARD",
                )
            except (DeviceConfigurationError, EntryPermissionError) as exc:  # fmt: skip
                messages.error(request, service_error_message(exc))
            else:
                return _activation_code_response(
                    request, registration.device, registration.activation_code
                )
    devices = devices_visible_to(request.user).filter(event_edition=event).order_by("public_name")
    from apps.entry.offline_views import event_offline_context

    return render(
        request,
        "entry/device_list.html",
        {
            **event_offline_context(request, event),
            "event": event,
            "devices": devices,
            "form": form,
            "can_manage": can_manage,
            "may_view_observability": has_scoped_permission(
                request.user, "entry.view_entry_observability", event_edition_id=event.pk
            ),
        },
    )


@operational_permission_required("entry.view_entrydevice", login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET"])
def device_detail(request, public_id):
    device = _device_or_404(request, public_id)
    scope = current_scope(device)
    gates, zones = _event_gates_and_zones(device.event_edition)
    initial = {}
    if scope is not None:
        initial = {
            "gate_id": str(scope.gate_id),
            "zone_ids": [str(z.pk) for z in scope.permitted_zones.all()],
            "verification_methods": list(scope.verification_methods),
            "expected_version": device.version,
            "offline_capable": scope.offline_capable,
            "offline_sensitivity": scope.offline_sensitivity,
        }
    from apps.entry.offline_views import device_offline_context

    return render(
        request,
        "entry/device_detail.html",
        {
            **device_offline_context(request, device, scope),
            "device": device,
            "scope": scope,
            "scope_method_labels": [
                VerificationMethod(method).label
                for method in VERIFICATION_METHOD_ORDER
                if scope is not None and method in scope.verification_methods
            ],
            "can_manage": may_manage_devices(
                request.user, event_edition_id=device.event_edition_id
            ),
            "scope_form": DeviceScopeForm(initial=initial, gates=gates, zones=zones),
            **{
                f"{action}_form": DeviceLifecycleForm(
                    initial={"expected_version": device.version}, auto_id=f"{action}_%s"
                )
                for action in ("suspend", "resume", "revoke")
            },
            "open_sessions": EntryDeviceSession.objects.select_related("gate", "zone", "started_by")
            .filter(device=device, ended_at__isnull=True)
            .order_by("-started_at"),
        },
    )


def _device_command(request, public_id, runner, success_message):
    device = _device_or_404(request, public_id)
    try:
        runner(device)
    except EntryConcurrencyError:
        return _conflict(request, _("This device changed since you loaded it. Reload and retry."))
    except EntryPermissionError:
        return _conflict(request, _("You are not authorized to manage entry devices."), status=403)
    except (EntryStateError, DeviceConfigurationError, EntryServiceError) as exc:  # fmt: skip
        messages.error(request, service_error_message(exc))
    else:
        messages.success(request, success_message)
    return redirect("entry:device-detail", public_id=device.public_id)


def _lifecycle_view(service, success_message):
    @operational_permission_required("entry.manage_entrydevice", login_url=_SIGN_IN)
    @require_http_methods(["POST"])
    def view(request, public_id):
        form = DeviceLifecycleForm(request.POST)
        if not form.is_valid():
            return _conflict(request, _("Invalid request. Reload the page and try again."))
        return _device_command(
            request,
            public_id,
            lambda device: service(
                device=device,
                actor=request.user,
                expected_version=form.cleaned_data["expected_version"],
                reason_code=form.cleaned_data["reason_code"],
                reason_text=form.cleaned_data.get("reason_text") or "",
            ),
            success_message,
        )

    return view


device_suspend = _lifecycle_view(suspend_device, gettext_lazy("Device suspended."))
device_resume = _lifecycle_view(resume_device, gettext_lazy("Device resumed."))
device_revoke = _lifecycle_view(revoke_device, gettext_lazy("Device revoked."))


@operational_permission_required("entry.manage_entrydevice", login_url=_SIGN_IN)
@require_http_methods(["POST"])
def device_rescope(request, public_id):
    device = _device_or_404(request, public_id)
    gates, zones = _event_gates_and_zones(device.event_edition)
    form = DeviceScopeForm(request.POST, gates=gates, zones=zones)
    if not form.is_valid():
        return _conflict(request, _("Invalid request. Reload the page and try again."))
    gate, selected_zones = _resolve_scope_inputs(device.event_edition, form.cleaned_data)
    return _device_command(
        request,
        public_id,
        lambda locked: change_device_scope(
            device=locked,
            venue=gate.venue,
            gate=gate,
            zones=selected_zones,
            verification_methods=form.cleaned_data["verification_methods"],
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
            reason=form.cleaned_data.get("reason") or "",
            offline_capable=form.cleaned_data.get("offline_capable") or False,
            offline_sensitivity=form.cleaned_data.get("offline_sensitivity") or "STANDARD",
        ),
        gettext_lazy("Device scope updated. Its open checkpoint sessions were ended."),
    )


@operational_permission_required("entry.manage_entrydevice", login_url=_SIGN_IN)
@never_cache
@require_http_methods(["POST"])
def device_activation_code(request, public_id):
    device = _device_or_404(request, public_id)
    form = DeviceVersionForm(request.POST)
    if not form.is_valid():
        return _conflict(request, _("Invalid request. Reload the page and try again."))
    try:
        code = issue_activation_code(
            device=device,
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
        )
    except EntryConcurrencyError:
        return _conflict(request, _("This device changed since you loaded it. Reload and retry."))
    except EntryPermissionError:
        return _conflict(request, _("You are not authorized to manage entry devices."), status=403)
    except EntryStateError as exc:
        messages.error(request, service_error_message(exc))
        return redirect("entry:device-detail", public_id=device.public_id)
    device.refresh_from_db()
    return _activation_code_response(request, device, code)


# ---------------------------------------------------------------------------
# Entry observability (Phase 3 Prompt 5, ADR-0022)
# ---------------------------------------------------------------------------

#: Look-back windows offered on the dashboard, in seconds.
OBSERVABILITY_WINDOWS = (15 * 60, 60 * 60, 6 * 60 * 60, 24 * 60 * 60)


@operational_permission_required("entry.view_entry_observability", login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET"])
def observability_dashboard(request, event_pk):
    """Identifier-free operational metrics for one event edition.

    Verification latency and result codes, pass failures, device health,
    outbox queue depth, stock exceptions and anomaly signals -- aggregates
    and checkpoint coordinates only (`apps.entry.observability`).
    """
    from apps.entry.observability import snapshot

    event = get_object_or_404(EventEdition, pk=event_pk)
    if not has_scoped_permission(
        request.user, "entry.view_entry_observability", event_edition_id=event.pk
    ):
        raise Http404("Not found.")
    try:
        window = int(request.GET.get("window", ""))
    except ValueError:
        window = settings.ENTRY_METRICS_DEFAULT_WINDOW_SECONDS
    if window not in OBSERVABILITY_WINDOWS:
        window = settings.ENTRY_METRICS_DEFAULT_WINDOW_SECONDS
    return render(
        request,
        "entry/observability.html",
        {
            "event": event,
            "data": snapshot(event_edition=event, window_seconds=window),
            "window": window,
            "windows": [(seconds, seconds // 60) for seconds in OBSERVABILITY_WINDOWS],
        },
    )
