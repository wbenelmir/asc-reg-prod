"""Enrolled-device PWA shell, service worker, manifest, and offline
preparation administration (Phase 4 Prompt 2, ADR-0023).

Ordinary Django views (binding decision P2-A): the device API that needs
DRF lives in `apps.entry.api`.

The shell (`/entry/offline/`) is deliberately STATIC per language: it holds
no participant data, no device identifier, no user-specific content and no
CSRF token in its HTML, so the service worker may keep a copy of it for use
without a network. Everything dynamic -- device status, package state,
counts -- comes from the device API and the encrypted local store at run
time.

The service worker (`/entry/sw.js`) is served from `/entry/`, so its scope
can never exceed `/entry/` (TRD §7.3). When offline continuity is disabled
the same URL serves a self-removing worker that clears only this
application's caches and unregisters itself; it never touches IndexedDB.
"""

from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import get_language, get_language_bidi
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.policies import has_scoped_permission, operational_permission_required
from apps.entry.forms import (
    DeviceLifecycleForm,
    EmergencyWipeForm,
    OfflineEventSettingForm,
)
from apps.entry.models import (
    DeviceKeyStatus,
    EntryDevice,
    EntryDeviceKey,
    OfflineCriticalDelta,
    OfflinePackage,
    OfflineSensitivity,
)
from apps.entry.offline_contract import band_at, client_config
from apps.entry.presentation import offline_error_message, service_error_message
from apps.entry.services import EntryConcurrencyError, EntryPermissionError, EntryServiceError

_SIGN_IN = "accounts:operational-sign-in"
CACHE_PREFIX = "asc-entry-shell-"

#: Static assets the offline shell needs, precached by the service worker.
#: The shell is a standalone page (no htmx, no Bootstrap JS), so this is
#: its complete dependency set.
SHELL_STATIC_ASSETS: tuple[str, ...] = (
    "vendor/bootstrap/bootstrap.min.css",
    "vendor/bootstrap/bootstrap.rtl.min.css",
    "vendor/thmanyah/thmanyahsans-Regular.woff2",
    "vendor/thmanyah/thmanyahsans-Medium.woff2",
    "vendor/thmanyah/thmanyahsans-Bold.woff2",
    "css/app.css",
    "css/asc-ui.css",
    "js/entry-offline.js",
    "img/brand/asc-logo.svg",
    "img/icons.svg",
)


def _api_urls() -> dict:
    return {
        name: reverse(f"entry:api-{name}")
        for name in (
            "heartbeat",
            "provision",
            "package",
            "delta",
            "self-test",
            "grant",
            "wipe-report",
            # Phase 4 Prompt 3: synchronization.
            "sync",
            "sync-quarantine",
        )
    }


def _precache_urls() -> list[str]:
    return [reverse("entry:offline-shell"), *[static(path) for path in SHELL_STATIC_ASSETS]]


def _cache_version() -> str:
    """Version the shell even when local static URLs are not fingerprinted."""
    digest = hashlib.sha256(
        ("phase4-prompt3-v1\n" + "\n".join(_precache_urls())).encode("utf-8")
    ).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# PWA: shell, service worker, manifest
# ---------------------------------------------------------------------------


def shell_strings() -> dict:
    """Every visible string the shell script renders (I18N-001: nothing is
    hard-coded in JavaScript)."""
    from apps.entry.models import EntryReasonCode, OfflineSensitivity
    from apps.entry.presentation import offline_error_codes

    errors = {f"error.{code}": offline_error_message(code) for code in offline_error_codes()}
    # Phase 4 Prompt 3: the offline verification vocabulary is the ONLINE
    # one (the same labels as the checkpoint result screen).
    reasons = {f"reason.{value}": str(label) for value, label in EntryReasonCode.choices if value}
    sensitivities = {
        f"sensitivity.{value}": str(label) for value, label in OfflineSensitivity.choices
    }
    return (
        errors
        | reasons
        | sensitivities
        | offline_verification_strings()
        | {
            "state.ONLINE": _("Online"),
            "state.OFFLINE_READY": _("Offline ready"),
            "state.OFFLINE_ACTIVE": _("Offline active"),
            "state.SYNCING": _("Syncing"),
            "state.STALE": _("Stale offline data"),
            "state.EXPIRED": _("Offline data expired"),
            "state.BLOCKED": _("Blocked"),
            "explain.ONLINE": _("Connected. Verification uses the live system."),
            "explain.OFFLINE_READY": _(
                "Connected. This device holds fresh offline data and can continue if the "
                "connection is lost."
            ),
            "explain.OFFLINE_ACTIVE": _(
                "The connection is lost. Offline verification of signed QR passes is permitted."
            ),
            "explain.SYNCING": _(
                "The connection is back. New verifications use the live system while local "
                "records are prepared for upload."
            ),
            "explain.STALE": _(
                "The offline data is stale. Do not admit on it: use Manual Review or Do Not Admit."
            ),
            "explain.EXPIRED": _(
                "The offline data has expired. Nobody can be admitted on it: follow the manual "
                "procedure."
            ),
            "explain.BLOCKED": _(
                "Verification is not possible on this device. Follow the manual procedure and "
                "call your supervisor."
            ),
            "sub.UNSTABLE": _("Connection unstable"),
            "sub.DEGRADED": _("Online — degraded"),
            "sub.OPERATOR_LOCKED": _("Operator actions locked after inactivity"),
            "sub.NO_GRANT": _("No operator is authorized for offline work on this device"),
            "sub.CLOCK": _("The device clock changed unexpectedly"),
            "sub.STORAGE": _("Local storage is unavailable or full"),
            "sub.NO_PACKAGE": _("No valid offline data on this device"),
            "sub.INTEGRITY": _("The offline data failed its integrity check"),
            "sub.AGING": _("Offline data is ageing — refresh when possible"),
            "sub.NOT_ENROLLED": _("This browser is not an enrolled entry device"),
            "sub.DIRECTIVE": _("The server has blocked this device"),
            "sub.DISABLED": _("Offline continuity is not enabled for this device"),
            "sub.REBUILD": _("Newer offline data is required"),
            "sub.WIPE_PENDING": _("Emergency wipe in progress"),
            "sub.WIPE_BLOCKED": _("Emergency wipe waiting for the other entry tabs on this device"),
            "sub.WIPE_FAILED": _("Emergency wipe not completed yet: retrying"),
            "sub.WIPED": _("Local data wiped by an emergency order"),
            "band.FRESH": _("Fresh"),
            "band.AGING": _("Ageing"),
            "band.STALE": _("Stale"),
            "band.EXPIRED": _("Expired"),
            "band.NONE": _("None"),
            "action.permitted": _("Permitted"),
            "action.forbidden": _("Not permitted"),
            "action.VERIFY_ONLINE": _("Verify online"),
            "action.RECORD_ONLINE_DECISION": _("Record an online decision"),
            "action.VERIFY_OFFLINE_QR": _("Verify a QR pass offline"),
            "action.ADMIT_OFFLINE": _("Admit offline"),
            "action.DO_NOT_ADMIT_OFFLINE": _("Do not admit (offline)"),
            "action.MANUAL_REVIEW_OFFLINE": _("Send to manual review (offline)"),
            "action.OVERRIDE_OFFLINE": _("Supervisor override (offline)"),
            "action.PREPARE_DEVICE": _("Prepare this device"),
            "action.REFRESH_PACKAGE": _("Refresh offline data"),
            "action.UPLOAD_OPERATIONS": _("Upload local records"),
            "action.MANUAL_PROCEDURE": _("Follow the manual procedure"),
            "value.none": _("—"),
            "value.yes": _("Yes"),
            "value.no": _("No"),
            "value.minutes": _("%(n)s min"),
            "value.hours": _("%(h)s h %(m)s min"),
            "value.in": _("in %(t)s"),
            "value.ago": _("%(t)s ago"),
            "msg.preparing": _("Preparing this device…"),
            "msg.prepared": _("Device keys registered. Downloading offline data…"),
            "msg.downloaded": _("Offline data downloaded and verified."),
            "msg.self_test_passed": _("Self-test passed. This device is offline ready."),
            "msg.self_test_failed": _("Self-test failed. The device is not offline ready."),
            "msg.refreshed": _("Offline data refreshed."),
            "msg.up_to_date": _("Offline data is already up to date."),
            "msg.refresh_failed": _("Refresh failed. The last valid offline data is kept."),
            "msg.rolled_back": _(
                "The new offline data failed verification. The previous valid data is kept."
            ),
            "msg.wiped": _("This device's local data was wiped by an emergency order."),
            "msg.wipe_blocked": _(
                "The emergency wipe is waiting for the other entry tabs on this device to close. "
                "Close them now. Nothing is reported as wiped until the wipe has finished."
            ),
            "msg.wipe_failed": _(
                "The emergency wipe did not complete. It is retried automatically."
            ),
            "msg.wiped_elsewhere": _(
                "Another tab on this device is carrying out an emergency wipe. "
                "This tab has closed its local data."
            ),
            "msg.building": _("The server is preparing this device's offline data…"),
            "msg.error": _("The request could not be completed."),
            "msg.unsupported": _(
                "This browser does not support the secure storage required for offline use."
            ),
            "msg.state_changed": _("Status changed: %(state)s"),
        }
    )


def offline_verification_strings() -> dict:
    """Strings of the offline verification and synchronization views (Phase 4
    Prompt 3). The result headings are the checkpoint's own."""
    from apps.entry.views import RESULT_PRESENTATION

    headings = {
        f"result.{result}": str(heading)
        for result, (_t, _i, heading) in RESULT_PRESENTATION.items()
    }
    return headings | {
        "msg.result_cleared": _(
            "Participant details cleared after inactivity. Verify again if needed."
        ),
        "msg.result_state_changed": _(
            "The device's state changed while this result was shown. Verify again."
        ),
        "msg.prior_admission": _("Previously admitted at %(t)s."),
        "msg.offline_verified_meta": _(
            "Verified on this device offline · offline data version %(v)s · last "
            "synchronization %(t)s"
        ),
        "msg.admit_recorded": _(
            "Admission recorded on this device. It is not synchronized yet: it will be "
            "uploaded when the connection returns."
        ),
        "msg.decision_recorded": _(
            "Decision recorded on this device. It will be uploaded when the connection returns."
        ),
        "msg.referral_recorded": _(
            "Referral recorded on this device. Follow the manual procedure with your supervisor."
        ),
        "msg.override_note_required": _("This override reason requires a note."),
        "msg.uploading": _("Uploading local records…"),
        "msg.upload_busy": _("Another tab of this device is already uploading."),
        "msg.upload_partial": _(
            "Some records are not confirmed by the server yet. They are kept and uploaded again."
        ),
        "msg.upload_done": _("Every local record is confirmed by the server."),
        "sync.idle": _("No local record"),
        "sync.pending": _("Waiting for upload"),
        "sync.waiting_offline": _("Waiting for the connection to return"),
        "sync.retrying": _("Upload failed — retrying automatically"),
        "sync.done": _("Synchronized"),
    }


@never_cache
@require_http_methods(["GET"])
def offline_shell(request):
    """The installable device shell (static per language)."""
    get_token(request)  # the CSRF cookie the shell script sends back; never in the HTML
    enabled = bool(settings.ENTRY_OFFLINE_ENABLED)
    config = {
        **client_config(),
        "enabled": enabled,
        "language": get_language() or "en",
        "rtl": bool(get_language_bidi()),
        "api": _api_urls(),
        "service_worker": reverse("entry:service-worker"),
        "scope": "/entry/",
        "checkpoint_url": reverse("entry:home"),
        "csrf_cookie": settings.CSRF_COOKIE_NAME,
        "vector": _self_test_vector(),
    }
    response = render(
        request,
        "entry/offline_shell.html",
        {"offline_config": config, "offline_strings": shell_strings(), "enabled": enabled},
    )
    response["Cache-Control"] = "no-cache, private"
    return response


def _self_test_vector() -> dict:
    """A committed PUBLIC ES256 test vector the self-test verifies with
    WebCrypto (proves the browser verifies raw `r || s` ES256 correctly).
    Synthetic; no private key exists anywhere in the repository."""
    from pathlib import Path

    path = Path(settings.BASE_DIR) / "tests" / "assets" / "offline_vectors" / "es256_self_test.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    # Parenthesized on purpose so the module parses on every Python 3 grammar;
    # the py314 formatter would otherwise drop the parentheses (PEP 758).
    except (OSError, ValueError):  # fmt: skip
        return {}


@require_http_methods(["GET"])
def service_worker(request):
    """The service worker, scoped to `/entry/` by where it is served."""
    if settings.ENTRY_OFFLINE_ENABLED:
        body = render(
            request,
            "entry/service_worker.js",
            {
                "cache_name": CACHE_PREFIX + _cache_version(),
                "cache_prefix": CACHE_PREFIX,
                "precache": json.dumps(_precache_urls()),
                "shell_url": json.dumps(reverse("entry:offline-shell")),
            },
            content_type="application/javascript",
        )
    else:
        body = render(
            request,
            "entry/service_worker_disabled.js",
            {"cache_prefix": CACHE_PREFIX},
            content_type="application/javascript",
        )
    body["Cache-Control"] = "no-cache"
    return body


@require_http_methods(["GET"])
def web_manifest(request):
    language = get_language() or "en"
    manifest = {
        "name": _("ASC 2026 Entry & Security"),
        "short_name": _("ASC Entry"),
        "lang": language,
        "dir": "rtl" if get_language_bidi() else "ltr",
        "start_url": reverse("entry:offline-shell"),
        "scope": "/entry/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#0b2545",
        "icons": [
            {
                "src": static("img/brand/asc-logo.svg"),
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any",
            }
        ],
    }
    response = JsonResponse(manifest, json_dumps_params={"ensure_ascii": False})
    response["Content-Type"] = "application/manifest+json"
    response["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Administration context (device list and device detail)
# ---------------------------------------------------------------------------


def event_offline_context(request, event) -> dict:
    from apps.entry.services.offline_devices import event_offline_setting, offline_enabled_for_event

    setting = event_offline_setting(event)
    return {
        "offline_globally_enabled": bool(settings.ENTRY_OFFLINE_ENABLED),
        "offline_event_enabled": bool(setting and setting.enabled),
        "offline_effective": offline_enabled_for_event(event),
        "may_toggle_offline": has_scoped_permission(
            request.user, "entry.enable_offline_entry", event_edition_id=event.pk
        ),
        "offline_setting_form": OfflineEventSettingForm(
            initial={"enabled": "0" if setting and setting.enabled else "1"}
        ),
    }


def device_offline_context(request, device, scope) -> dict:
    """Readiness facts for device administration: counts, versions, times
    and hashes only -- never package content or an operation body."""
    from apps.entry.services.offline_devices import (
        may_order_emergency_wipe,
        offline_unavailability,
        pending_wipe_order,
    )

    now = timezone.now()
    package = OfflinePackage.objects.filter(device=device).order_by("-package_version").first()
    delta = OfflineCriticalDelta.objects.filter(device=device).order_by("-issued_at").first()
    unavailable = offline_unavailability(device, scope=scope, now=now)
    keys = list(
        EntryDeviceKey.objects.filter(device=device, status=DeviceKeyStatus.ACTIVE).order_by(
            "purpose"
        )
    )
    package_info = None
    if package is not None:
        package_info = {
            "version": package.package_version,
            "status": package.get_status_display(),
            "status_code": package.status,
            "data_cutoff_at": package.data_cutoff_at,
            "expires_at": package.expires_at,
            "band": band_at(
                now,
                aging_at=package.aging_at,
                stale_at=package.stale_at,
                expires_at=package.expires_at,
            ),
            "entries": package.entry_count,
            "downloads": package.download_count,
            "sensitivity": package.get_sensitivity_display(),
        }
    return {
        "offline": {
            "enabled_for_event": not unavailable
            or unavailable
            not in (
                "OFFLINE_DISABLED",
                "EVENT_CLOSED",
            ),
            "unavailable_code": unavailable,
            "unavailable_label": offline_error_message(unavailable) if unavailable else "",
            "scope_capable": bool(scope and scope.offline_capable),
            "sensitivity": (OfflineSensitivity(scope.offline_sensitivity).label if scope else ""),
            "prepared_at": device.offline_prepared_at,
            "self_test_at": device.offline_self_test_at,
            "blocked_at": device.offline_blocked_at,
            "keys": [
                {
                    "purpose": key.get_purpose_display(),
                    "version": key.key_version,
                    "fingerprint": key.fingerprint[:16],
                }
                for key in keys
            ],
            "package": package_info,
            "delta": (
                {"version": delta.delta_version, "cutoff": delta.critical_delta_cutoff_at}
                if delta is not None
                else None
            ),
            "reported": {
                "state": device.reported_state,
                "pending": device.reported_pending_operations,
                "locked": device.reported_locked_operations,
                "sequence_high": device.reported_sequence_high,
                "chain_head": (device.reported_chain_head or "")[:16],
                "at": device.reported_at,
            },
            "wipe_order": pending_wipe_order(device),
            "sync": _device_sync_summary(request, device),
        },
        "block_offline_form": DeviceLifecycleForm(
            initial={"expected_version": device.version}, auto_id="block_offline_%s"
        ),
        "may_order_emergency_wipe": may_order_emergency_wipe(
            request.user, event_edition_id=device.event_edition_id
        ),
    }


def _device_sync_summary(request, device) -> dict:
    """Phase 4 Prompt 3: the device's synchronization facts -- last sync, the
    OFF-005 counts and its open reconciliation cases (counts only)."""
    from apps.entry.models import ReconciliationCase, ReconciliationStatus, SyncOperation
    from apps.entry.services.reconciliation import may_open_queue, recovery_counts

    return {
        "last_sync_at": device.last_sync_at,
        "counts": recovery_counts(SyncOperation.objects.filter(device=device)),
        "open_cases": ReconciliationCase.objects.filter(
            device=device, status=ReconciliationStatus.OPEN
        ).count(),
        "may_open_queue": may_open_queue(request.user, event_edition=device.event_edition),
    }


# ---------------------------------------------------------------------------
# Administration commands
# ---------------------------------------------------------------------------


@operational_permission_required("entry.enable_offline_entry", login_url=_SIGN_IN)
@require_http_methods(["POST"])
def event_offline_setting(request, event_pk):
    from apps.entry.services.offline_devices import set_event_offline_enabled
    from apps.events.models import EventEdition

    event = get_object_or_404(EventEdition, pk=event_pk)
    if not has_scoped_permission(
        request.user, "entry.enable_offline_entry", event_edition_id=event.pk
    ):
        raise Http404("Not found.")
    form = OfflineEventSettingForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Invalid request. Reload the page and try again."))
        return redirect("entry:device-list", event_pk=event.pk)
    try:
        setting = set_event_offline_enabled(
            event_edition=event,
            enabled=form.cleaned_data["enabled"],
            actor=request.user,
            reason_code=form.cleaned_data["reason_code"],
        )
    except EntryServiceError as exc:
        messages.error(
            request, offline_error_message(exc.code) if exc.code else service_error_message(exc)
        )
    else:
        messages.success(
            request,
            _("Offline continuity is now enabled for this event.")
            if setting.enabled
            else _(
                "Offline continuity is now disabled for this event. "
                "Prepared devices were withdrawn."
            ),
        )
    return redirect("entry:device-list", event_pk=event.pk)


def _device_for(request, public_id) -> EntryDevice:
    from apps.entry.selectors import devices_visible_to

    return get_object_or_404(devices_visible_to(request.user), public_id=public_id)


@operational_permission_required("entry.manage_entrydevice", login_url=_SIGN_IN)
@require_http_methods(["POST"])
def device_block_offline(request, public_id):
    from apps.entry.services.offline_devices import block_offline_use

    device = _device_for(request, public_id)
    form = DeviceLifecycleForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Invalid request. Reload the page and try again."))
        return redirect("entry:device-detail", public_id=device.public_id)
    try:
        block_offline_use(
            device=device,
            actor=request.user,
            expected_version=form.cleaned_data["expected_version"],
            reason_code=form.cleaned_data["reason_code"],
        )
    except EntryConcurrencyError:
        messages.error(request, _("This device changed since you loaded it. Reload and retry."))
    except EntryPermissionError:
        messages.error(request, _("You are not authorized to manage entry devices."))
    except EntryServiceError as exc:
        messages.error(request, service_error_message(exc))
    else:
        messages.success(
            request,
            _(
                "Offline use is blocked on this device. Its offline data was revoked; "
                "local records on the device are kept, locked and upload-only."
            ),
        )
    return redirect("entry:device-detail", public_id=device.public_id)


def _wipe_device(request, public_id) -> EntryDevice:
    from apps.entry.services.offline_devices import may_order_emergency_wipe

    device = get_object_or_404(
        EntryDevice.objects.select_related("event_edition"), public_id=public_id
    )
    if not may_order_emergency_wipe(request.user, event_edition_id=device.event_edition_id):
        raise Http404("Not found.")
    return device


@login_required(login_url=_SIGN_IN)
@never_cache
@require_http_methods(["GET", "POST"])
def device_emergency_wipe(request, public_id):
    """The distinct, destructive emergency-wipe order (binding decision P2-F)."""
    from apps.entry.services.offline_devices import expected_evidence_loss, order_emergency_wipe

    device = _wipe_device(request, public_id)
    form = EmergencyWipeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            order = order_emergency_wipe(
                device=device,
                actor=request.user,
                reason_code=form.cleaned_data["reason_code"],
                note=form.cleaned_data.get("note") or "",
                confirmed=form.cleaned_data["confirm_evidence_loss"],
                mfa_response=form.cleaned_data["mfa_response"],
            )
        except EntryServiceError as exc:
            messages.error(request, offline_error_message(exc.code))
        else:
            messages.success(
                request,
                _(
                    "Emergency wipe ordered (reference %(ref)s). "
                    "The device will wipe at its next contact."
                )
                % {"ref": order.public_id},
            )
            return redirect("entry:device-emergency-wipe", public_id=device.public_id)
    response = render(
        request,
        "entry/device_emergency_wipe.html",
        {
            "device": device,
            "form": EmergencyWipeForm() if request.method == "POST" else form,
            "expected_loss": expected_evidence_loss(device),
            "mfa_configured": bool(getattr(settings, "MFA_BACKEND", None)),
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response


def shell_config_json(config: dict) -> str:  # pragma: no cover - template helper
    return json.dumps(config, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Supervisor reconciliation queue (Phase 4 Prompt 3, ADR-0024)
# ---------------------------------------------------------------------------

_QUEUE_PAGE_SIZE = 25


def _labelled(choices, code):
    return dict(choices).get(code, code) if code else ""


def _state_rows(state: dict) -> list[tuple]:
    """A case's device/server state as (label, value, is_time) rows. Codes
    are shown with their localized labels where one exists; values are
    codes, counts and times only (the service never stores anything else)."""
    from datetime import UTC, datetime

    from apps.entry.models import EntryReasonCode, EntryResult

    labels = {
        "result": _("Result"),
        "reason": _("Reason"),
        "blockers": _("Blocking reasons"),
        "advisories": _("Advisories"),
        "band": _("Freshness"),
        "band_at_occurrence": _("Freshness at the admission"),
        "state": _("Device state"),
        "decision": _("Decision"),
        "decision_reason": _("Decision reason"),
        "override": _("Override reason"),
        "package_version": _("Package version"),
        "delta_version": _("Critical update version"),
        "pass_status_at_occurrence": _("Pass status at the admission"),
        "pass_status_now": _("Pass status now"),
        "pass_status_changed_at": _("Pass status changed"),
        "knowledge_at": _("Device knowledge up to"),
        "occurred_at": _("Occurred"),
        "received_at": _("Received"),
        "reentry_policy": _("Re-entry rule"),
        "package_status": _("Package status"),
        "grant_revoked_at": _("Operator authorization withdrawn"),
        "outcome": _("Outcome"),
        "flag": _("Anomaly"),
        "sequence": _("Sequence"),
    }
    results = dict(EntryResult.choices)
    reasons = dict(EntryReasonCode.choices)
    rows = []
    flat = dict(state or {})
    local = flat.pop("local", None)
    if isinstance(local, dict):
        for key in ("result", "reason", "blockers", "advisories"):
            flat.setdefault(key, local.get(key))
    for key, label in labels.items():
        if key not in flat or flat[key] in (None, "", []):
            continue
        value = flat[key]
        if key.endswith("_at") and isinstance(value, int) and not isinstance(value, bool):
            rows.append((str(label), datetime.fromtimestamp(value, tz=UTC), True))
            continue
        if key == "result":
            text = str(results.get(value, value))
        elif key == "reason":
            text = str(reasons.get(value, value))
        elif isinstance(value, list):
            text = ", ".join(str(reasons.get(item, item)) for item in value)
        else:
            text = str(value)
        rows.append((str(label), text, False))
    return rows


def _queue_denied(request):
    from apps.accounts.policies import deny_operational_access

    return deny_operational_access(request, login_url=_SIGN_IN)


@never_cache
@require_http_methods(["GET"])
def reconciliation_queue(request, event_pk):
    """The event's reconciliation cases in the actor's scope (event, venue
    or gate), with the OFF-005 recovery counts for that same scope."""
    from django.core.paginator import Paginator

    from apps.entry.models import (
        ReconciliationCaseType,
        ReconciliationSeverity,
        ReconciliationStatus,
    )
    from apps.entry.services.reconciliation import (
        cases_visible_to,
        may_open_queue,
        operations_visible_to,
        recovery_counts,
    )
    from apps.events.models import EventEdition

    event = get_object_or_404(EventEdition, pk=event_pk)
    if not may_open_queue(request.user, event_edition=event):
        return _queue_denied(request)
    cases = cases_visible_to(request.user, event_edition=event)
    status = request.GET.get("status", "OPEN")
    if status in ReconciliationStatus.values:
        cases = cases.filter(status=status)
    else:
        status = ""
    case_type = request.GET.get("case_type", "")
    if case_type in ReconciliationCaseType.values:
        cases = cases.filter(case_type=case_type)
    else:
        case_type = ""
    severity = request.GET.get("severity", "")
    if severity in ReconciliationSeverity.values:
        cases = cases.filter(severity=severity)
    else:
        severity = ""
    page = Paginator(cases.order_by("status", "-opened_at"), _QUEUE_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    query = {k: v for k, v in (("status", status), ("case_type", case_type)) if v}
    if severity:
        query["severity"] = severity
    from urllib.parse import urlencode

    all_cases = cases_visible_to(request.user, event_edition=event)
    response = render(
        request,
        "entry/reconciliation_queue.html",
        {
            "event": event,
            "page": page,
            "querystring": urlencode(query),
            "status": status,
            "case_type": case_type,
            "severity": severity,
            "statuses": ReconciliationStatus.choices,
            "case_types": ReconciliationCaseType.choices,
            "severities": ReconciliationSeverity.choices,
            "open_count": all_cases.filter(status=ReconciliationStatus.OPEN).count(),
            "counts": recovery_counts(operations_visible_to(request.user, event_edition=event)),
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response


def _visible_case_or_none(request, public_id):
    from apps.entry.models import ReconciliationCase
    from apps.entry.services.reconciliation import cases_visible_to, record_denied_access

    case = cases_visible_to(request.user).filter(public_id=public_id).first()
    if case is None:
        existing = (
            ReconciliationCase.objects.filter(public_id=public_id)
            .values_list("event_edition_id", flat=True)
            .first()
        )
        if existing is not None and request.user.is_authenticated:
            record_denied_access(
                request.user, target_public_id=public_id, event_edition_id=existing
            )
    return case


@never_cache
@require_http_methods(["GET"])
def reconciliation_case(request, public_id):
    """One case: what the device knew, what the server knew, what happened,
    every linked operation, the append-only actions, and the review form."""
    from apps.entry.forms import ReconciliationActionForm
    from apps.entry.models import (
        ReconciliationActionType,
        ReconciliationCaseType,
        ReconciliationSeverity,
        SyncOperationStatus,
    )
    from apps.entry.services.reconciliation import reconciliation_scopes

    if not request.user.is_authenticated or not (
        request.user.is_superuser or reconciliation_scopes(request.user)
    ):
        return _queue_denied(request)
    case = _visible_case_or_none(request, public_id)
    if case is None:
        raise Http404("Not found.")
    links = [
        {
            "public_id": link.sync_operation.public_id,
            "sequence": link.sync_operation.device_sequence,
            "status": _labelled(SyncOperationStatus.choices, link.sync_operation.status),
            "status_code": link.sync_operation.status,
            "outcome": link.sync_operation.outcome_code,
            "occurred_at": link.sync_operation.occurred_at,
            "received_at": link.sync_operation.received_at,
            "duplicates": link.sync_operation.duplicate_submissions,
        }
        for link in case.links.select_related("sync_operation").order_by(
            "sync_operation__device_sequence"
        )
    ]
    actions = list(case.actions.select_related("actor").order_by("created_at"))
    participant = None
    if case.registration is not None:
        person = case.registration.person
        participant = {
            "display_name": person.display_name if person is not None else "",
            "registration_reference": case.registration.public_reference,
        }
    operator = None
    if case.entry_event is not None:
        operator = case.entry_event.operator_user.display_name
    elif case.sync_operation is not None and case.sync_operation.operator_user is not None:
        operator = case.sync_operation.operator_user.display_name
    form = ReconciliationActionForm(
        initial={"expected_version": case.version, "action": ReconciliationActionType.NOTE}
    )
    response = render(
        request,
        "entry/reconciliation_case.html",
        {
            "case": case,
            "case_type_label": _labelled(ReconciliationCaseType.choices, case.case_type),
            "severity_label": _labelled(ReconciliationSeverity.choices, case.severity),
            "device_rows": _state_rows(case.device_known_state),
            "server_rows": _state_rows(case.server_known_state),
            "fact_rows": _state_rows(case.operational_fact),
            "links": links,
            "actions": actions,
            "participant": participant,
            "operator": operator,
            "form": form,
        },
    )
    response["Cache-Control"] = "no-store, private"
    return response


@never_cache
@require_http_methods(["POST"])
def reconciliation_action(request, public_id):
    from apps.entry.forms import ReconciliationActionForm
    from apps.entry.models import ReconciliationActionType
    from apps.entry.services.reconciliation import reconciliation_scopes, record_action

    if not request.user.is_authenticated or not (
        request.user.is_superuser or reconciliation_scopes(request.user)
    ):
        return _queue_denied(request)
    case = _visible_case_or_none(request, public_id)
    if case is None:
        raise Http404("Not found.")
    form = ReconciliationActionForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Enter a review note and choose an action."))
        return redirect("entry:reconciliation-case", public_id=case.public_id)
    try:
        record_action(
            case=case,
            actor=request.user,
            action=form.cleaned_data["action"],
            note=form.cleaned_data["note"],
            expected_version=form.cleaned_data["expected_version"],
        )
    except EntryConcurrencyError:
        messages.error(request, _("This case changed since you loaded it. Reload and retry."))
    except EntryPermissionError:
        messages.error(request, _("You are not authorized to reconcile this case."))
    except EntryServiceError as exc:
        messages.error(request, offline_error_message(exc.code))
    else:
        messages.success(
            request,
            _("The case is closed. The original evidence is unchanged.")
            if form.cleaned_data["action"] == ReconciliationActionType.CLOSE
            else _("Your note was added to the case."),
        )
    return redirect("entry:reconciliation-case", public_id=case.public_id)
