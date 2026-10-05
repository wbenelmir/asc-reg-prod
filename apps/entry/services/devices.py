"""Entry device registration, enrollment, scope versions, and lifecycle
(Schema §11.1, TRD §12.3, Flow §14.6, ADR-0021 §2).

Enrollment is a two-step, two-factor act:

1. an authorized administrator REGISTERS the device (name, expiry, scope)
   and receives a one-time activation code, shown once and stored only as
   a digest, valid for `ENTRY_DEVICE_ACTIVATION_CODE_SECONDS`;
2. on the physical device, an authorized administrator -- signed in there
   -- enters that code. The server then mints a 256-bit device secret,
   stores only its digest, and hands the secret to that browser in an
   HttpOnly, SameSite=Strict cookie.

A database read therefore never yields a usable device credential, a
leaked activation code alone never enrolls anything (a signed-in
administrator is also required), and re-enrollment rotates both the
secret and the public `device_key_id`.

Every lifecycle change ends the device's open sessions in the same
transaction, so a suspended, revoked, rescoped, or expired device stops
working on its very next request.

Phase 4 Prompt 2 (ADR-0023): `OFFLINE_READY` operates online exactly like
`ENROLLED`. Every lifecycle change also withdraws offline readiness
(revokes the device's packages and operator grants) in the same
transaction. None of them deletes anything on the device: its
unacknowledged operations become locked and upload-only.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    OPERATIONAL_DEVICE_STATUSES,
    VERIFICATION_METHOD_ORDER,
    DeviceScope,
    EntryDevice,
    EntryDeviceStatus,
    OfflineSensitivity,
)
from apps.entry.services import (
    EntryConcurrencyError,
    EntryPermissionError,
    EntryServiceError,
    EntryStateError,
    audit,
    new_public_id,
    secret_digest,
)

#: Crockford-style alphabet without ambiguous characters, for a code a
#: person reads from one screen and types on another.
_ACTIVATION_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_ACTIVATION_CODE_LENGTH = 12  # ~59 bits; single-use and short-lived

#: Device statuses from which a device can never return.
TERMINAL_DEVICE_STATUSES: tuple[str, ...] = (EntryDeviceStatus.REVOKED, EntryDeviceStatus.EXPIRED)


class DeviceConfigurationError(EntryServiceError):
    """Raised for an invalid device registration or scope."""


class DeviceEnrollmentError(EntryServiceError):
    """Raised when an activation code cannot enroll a device. Deliberately
    one message for every cause, so the code space cannot be probed."""


@dataclass(frozen=True)
class DeviceRegistration:
    device: EntryDevice
    activation_code: str


@dataclass(frozen=True)
class DeviceEnrollment:
    device: EntryDevice
    device_secret: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_manager(actor, event_edition) -> None:
    from apps.entry.policies import may_manage_devices

    if not may_manage_devices(actor, event_edition_id=event_edition.pk):
        raise EntryPermissionError("You are not authorized to manage entry devices.")


def generate_activation_code() -> str:
    return "".join(secrets.choice(_ACTIVATION_ALPHABET) for _ in range(_ACTIVATION_CODE_LENGTH))


def normalize_activation_code(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw.upper() if ch.isalnum())[:32]


def format_activation_code(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def _validate_expiry(event_edition, expires_at, *, now) -> None:
    if expires_at is None or expires_at <= now:
        raise DeviceConfigurationError("A device requires a future expiry.", code="EXPIRY_REQUIRED")
    ceiling = now + timedelta(days=settings.ENTRY_DEVICE_MAX_ENROLLMENT_DAYS)
    if expires_at > ceiling:
        raise DeviceConfigurationError(
            "The device expiry exceeds the maximum enrollment period.", code="EXPIRY_TOO_LONG"
        )
    if expires_at > event_edition.ends_at + timedelta(days=1):
        raise DeviceConfigurationError(
            "A device cannot outlive its event edition.", code="EXPIRY_AFTER_EVENT"
        )


def _validate_scope(*, event_edition, venue, gate, zones, verification_methods) -> list[str]:
    if venue.event_edition_id != event_edition.pk or not venue.is_active:
        raise DeviceConfigurationError(
            "The venue does not belong to this event edition.", code="INVALID_SCOPE"
        )
    if gate.venue_id != venue.pk or not gate.is_active or not gate.supports_entry:
        raise DeviceConfigurationError(
            "The gate is not an active entry gate of this venue.", code="INVALID_SCOPE"
        )
    zones = list(zones)
    if not zones:
        raise DeviceConfigurationError(
            "A device scope requires at least one permitted zone.", code="INVALID_SCOPE"
        )
    for zone in zones:
        if zone.venue_id != venue.pk or not zone.is_active:
            raise DeviceConfigurationError(
                "Every permitted zone must be an active venue zone.", code="INVALID_SCOPE"
            )
    methods = [m for m in VERIFICATION_METHOD_ORDER if m in set(verification_methods or [])]
    if not methods or len(methods) != len(set(verification_methods or [])):
        raise DeviceConfigurationError(
            "The verification methods are invalid.", code="INVALID_METHODS"
        )
    return methods


def _validate_offline_scope(*, zones, offline_capable: bool, offline_sensitivity: str) -> str:
    """Validate the offline part of a scope version (Phase 4 Prompt 2).

    A scope whose permitted zones include a RESTRICTED or HIGH_SECURITY zone
    can only be offline-capable under the SENSITIVE validity values -- the
    stricter approved values are never silently bypassed.
    """
    if offline_sensitivity not in OfflineSensitivity.values:
        raise DeviceConfigurationError(
            "The offline sensitivity is invalid.", code="INVALID_SENSITIVITY"
        )
    if (
        offline_capable
        and offline_sensitivity != OfflineSensitivity.SENSITIVE
        and any(getattr(zone, "sensitivity", "STANDARD") != "STANDARD" for zone in zones)
    ):
        raise DeviceConfigurationError(
            "A scope with a restricted zone must use the sensitive offline values.",
            code="SENSITIVITY_REQUIRED",
        )
    return offline_sensitivity


def _withdraw_offline(
    locked: EntryDevice, *, reason: str, now, retire_keys: bool = False, actor=None
):
    from apps.entry.services.offline_devices import withdraw_offline_readiness

    withdraw_offline_readiness(locked, reason=reason, now=now, retire_keys=retire_keys, actor=actor)


def _end_device_sessions(device: EntryDevice, *, reason: str, now) -> None:
    from apps.entry.services.sessions import end_device_sessions

    end_device_sessions(device=device, reason=reason, now=now)


def _lock_device(device: EntryDevice, *, expected_version: int | None) -> EntryDevice:
    locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
    if expected_version is not None and locked.version != expected_version:
        raise EntryConcurrencyError("This device changed since you loaded it. Reload and retry.")
    return locked


def _set_status(
    locked: EntryDevice, *, status: str, actor, reason_code: str, reason_text: str, now
):
    locked.status = status
    locked.status_changed_at = now
    locked.status_changed_by = actor if getattr(actor, "pk", None) else None
    locked.status_reason_code = reason_code[:32]
    locked.status_reason_text = (reason_text or "").strip()[:300]
    locked.version += 1


def device_summary(device: EntryDevice) -> dict:
    """Bounded audit summary -- never a secret, code, or credential digest."""
    return {
        "public_id": device.public_id,
        "status": device.status,
        "device_key_id": device.device_key_id,
        "expires_at": device.expires_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Registration and enrollment
# ---------------------------------------------------------------------------


def register_device(
    *,
    event_edition,
    public_name: str,
    expires_at,
    venue,
    gate,
    zones,
    verification_methods,
    actor,
    reason: str = "",
    offline_capable: bool = False,
    offline_sensitivity: str = OfflineSensitivity.STANDARD,
) -> DeviceRegistration:
    """Register a device in PENDING_ENROLLMENT with scope version 1."""
    _require_manager(actor, event_edition)
    now = timezone.now()
    public_name = (public_name or "").strip()[:100]
    if not public_name:
        raise DeviceConfigurationError(
            "A device requires an operational name.", code="NAME_REQUIRED"
        )
    _validate_expiry(event_edition, expires_at, now=now)
    methods = _validate_scope(
        event_edition=event_edition,
        venue=venue,
        gate=gate,
        zones=zones,
        verification_methods=verification_methods,
    )
    offline_sensitivity = _validate_offline_scope(
        zones=zones, offline_capable=offline_capable, offline_sensitivity=offline_sensitivity
    )
    code = generate_activation_code()
    with transaction.atomic():
        device = EntryDevice.objects.create(
            public_id=new_public_id(),
            event_edition=event_edition,
            public_name=public_name,
            expires_at=expires_at,
            activation_code_hash=secret_digest(code),
            activation_code_expires_at=now
            + timedelta(seconds=settings.ENTRY_DEVICE_ACTIVATION_CODE_SECONDS),
            created_by=actor,
        )
        scope = DeviceScope.objects.create(
            device=device,
            scope_version=1,
            event_edition=event_edition,
            venue=venue,
            gate=gate,
            verification_methods=methods,
            offline_capable=bool(offline_capable),
            offline_sensitivity=offline_sensitivity,
            reason=(reason or "").strip()[:300],
            created_by=actor,
        )
        scope.permitted_zones.set(zones)
        audit(
            action_code=action_codes.DEVICE_REGISTERED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=event_edition.pk,
            after_summary={
                **device_summary(device),
                "gate": gate.code,
                "zones": sorted(z.code for z in zones),
                "methods": methods,
                "offline_capable": bool(offline_capable),
                "offline_sensitivity": offline_sensitivity,
            },
        )
    return DeviceRegistration(device=device, activation_code=code)


def issue_activation_code(*, device: EntryDevice, actor, expected_version: int) -> str:
    """Issue a fresh activation code for first enrollment or re-enrollment."""
    _require_manager(actor, device.event_edition)
    now = timezone.now()
    code = generate_activation_code()
    with transaction.atomic():
        locked = _lock_device(device, expected_version=expected_version)
        if locked.status not in (
            EntryDeviceStatus.PENDING_ENROLLMENT,
            *OPERATIONAL_DEVICE_STATUSES,
        ):
            raise EntryStateError("This device cannot be enrolled in its current state.")
        if locked.expires_at <= now:
            raise EntryStateError("This device has expired.", code="DEVICE_EXPIRED")
        locked.activation_code_hash = secret_digest(code)
        locked.activation_code_expires_at = now + timedelta(
            seconds=settings.ENTRY_DEVICE_ACTIVATION_CODE_SECONDS
        )
        locked.version += 1
        locked.save()
        audit(
            action_code=action_codes.DEVICE_ACTIVATION_CODE_ISSUED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            after_summary=device_summary(locked),
        )
    return code


def enroll_device_with_code(
    *, raw_code: object, actor, app_version: str = "", platform_summary: str = ""
) -> DeviceEnrollment:
    """Consume an activation code on the physical device (step 2).

    The code is single-use: it is cleared in the same transaction that
    installs the new credential digest. Any failure -- unknown, expired,
    already used, wrong state, unauthorized actor -- raises the same
    `DeviceEnrollmentError`.
    """
    from apps.entry.policies import may_manage_devices

    now = timezone.now()
    code = normalize_activation_code(raw_code)
    locked = None
    if len(code) == _ACTIVATION_CODE_LENGTH:
        with transaction.atomic():
            locked = (
                EntryDevice.objects.select_for_update(of=("self",))
                .select_related("event_edition")
                .filter(activation_code_hash=secret_digest(code))
                .first()
            )
            usable = (
                locked is not None
                and locked.status
                in (EntryDeviceStatus.PENDING_ENROLLMENT, *OPERATIONAL_DEVICE_STATUSES)
                and locked.activation_code_expires_at is not None
                and locked.activation_code_expires_at > now
                and locked.expires_at > now
                and may_manage_devices(actor, event_edition_id=locked.event_edition_id)
            )
            if usable:
                secret = secrets.token_urlsafe(32)
                was_enrolled = locked.status in OPERATIONAL_DEVICE_STATUSES
                if was_enrolled:
                    _end_device_sessions(locked, reason="RE_ENROLLED", now=now)
                # A new credential voids the previous offline preparation:
                # packages and grants are revoked and the device keys are
                # retired (the new browser must be prepared again).
                _withdraw_offline(
                    locked, reason="RE_ENROLLED", now=now, retire_keys=True, actor=actor
                )
                locked.offline_prepared_at = None
                locked.offline_prepared_by = None
                locked.offline_self_test_at = None
                locked.offline_blocked_at = None
                locked.data_wipe_requested_at = None
                locked.credential_hash = secret_digest(secret)
                locked.device_key_id = secrets.token_hex(8)
                locked.activation_code_hash = ""
                locked.activation_code_expires_at = None
                locked.status = EntryDeviceStatus.ENROLLED
                locked.enrolled_at = now
                locked.last_seen_at = now
                locked.app_version = (app_version or "")[:64]
                locked.platform_summary = (platform_summary or "")[:200]
                locked.version += 1
                locked.save()
                audit(
                    action_code=action_codes.DEVICE_ENROLLED,
                    actor=actor,
                    target_type="EntryDevice",
                    target_uuid=locked.pk,
                    event_edition_id=locked.event_edition_id,
                    after_summary={**device_summary(locked), "re_enrollment": was_enrolled},
                )
                return DeviceEnrollment(device=locked, device_secret=secret)
    _record_enrollment_rejection(actor=actor, device=locked)
    raise DeviceEnrollmentError("This activation code cannot be used.")


def _record_enrollment_rejection(*, actor, device) -> None:
    with transaction.atomic():
        audit(
            action_code=action_codes.DEVICE_ENROLLMENT_REJECTED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=getattr(device, "pk", None),
            event_edition_id=getattr(device, "event_edition_id", None),
            result="DENIED",
        )


# ---------------------------------------------------------------------------
# Device authentication (every checkpoint request)
# ---------------------------------------------------------------------------

#: `last_seen_at` is refreshed at most this often, so a busy checkpoint does
#: not write the device row on every scan.
_LAST_SEEN_REFRESH_SECONDS = 60


def authenticate_device(raw_secret: object, *, now=None) -> EntryDevice | None:
    """Resolve the enrolled device presenting `raw_secret`, or None.

    Fails closed. An ENROLLED device past `expires_at` is treated as expired
    immediately -- correctness never depends on the expiry sweep having
    run -- and the expiry is persisted on the spot. `OFFLINE_READY` operates
    online like `ENROLLED`; a device under an emergency-wipe order does not
    operate at all (Phase 4 Prompt 2).
    """
    if not isinstance(raw_secret, str) or not 20 <= len(raw_secret) <= 128:
        return None
    now = now or timezone.now()
    device = (
        EntryDevice.objects.select_related("event_edition")
        .filter(credential_hash=secret_digest(raw_secret))
        .first()
    )
    if device is None or device.status not in OPERATIONAL_DEVICE_STATUSES:
        return None
    if device.data_wipe_requested_at is not None:
        return None
    if device.expires_at <= now:
        expire_device(device=device, now=now)
        return None
    if (
        device.last_seen_at is None
        or (now - device.last_seen_at).total_seconds() >= _LAST_SEEN_REFRESH_SECONDS
    ):
        EntryDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        device.last_seen_at = now
    return device


# ---------------------------------------------------------------------------
# Scope versions and lifecycle
# ---------------------------------------------------------------------------


def current_scope(device: EntryDevice) -> DeviceScope | None:
    return (
        DeviceScope.objects.select_related("venue", "gate", "event_edition")
        .prefetch_related("permitted_zones")
        .filter(device=device, is_current=True)
        .first()
    )


def change_device_scope(
    *,
    device: EntryDevice,
    venue,
    gate,
    zones,
    verification_methods,
    actor,
    expected_version: int,
    reason: str = "",
    offline_capable: bool = False,
    offline_sensitivity: str = OfflineSensitivity.STANDARD,
) -> DeviceScope:
    """Close the current scope and write the next version. Ends sessions
    and withdraws offline readiness (a package is bound to one scope
    version, so the device must be prepared again for the new one)."""
    _require_manager(actor, device.event_edition)
    methods = _validate_scope(
        event_edition=device.event_edition,
        venue=venue,
        gate=gate,
        zones=zones,
        verification_methods=verification_methods,
    )
    offline_sensitivity = _validate_offline_scope(
        zones=zones, offline_capable=offline_capable, offline_sensitivity=offline_sensitivity
    )
    now = timezone.now()
    with transaction.atomic():
        locked = _lock_device(device, expected_version=expected_version)
        if locked.status in TERMINAL_DEVICE_STATUSES:
            raise EntryStateError("A revoked or expired device cannot be rescoped.")
        previous = DeviceScope.objects.select_for_update().filter(device=locked, is_current=True)
        previous_version = 0
        for row in previous:
            previous_version = max(previous_version, row.scope_version)
            row.is_current = False
            row.save(update_fields=["is_current"])
        scope = DeviceScope.objects.create(
            device=locked,
            scope_version=previous_version + 1,
            event_edition=locked.event_edition,
            venue=venue,
            gate=gate,
            verification_methods=methods,
            offline_capable=bool(offline_capable),
            offline_sensitivity=offline_sensitivity,
            reason=(reason or "").strip()[:300],
            created_by=actor,
        )
        scope.permitted_zones.set(zones)
        _end_device_sessions(locked, reason="SCOPE_CHANGED", now=now)
        _withdraw_offline(locked, reason="SCOPE_CHANGED", now=now, actor=actor)
        locked.version += 1
        locked.save(update_fields=["version", "updated_at", "status", "status_changed_at"])
        audit(
            action_code=action_codes.DEVICE_SCOPE_CHANGED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            after_summary={
                "public_id": locked.public_id,
                "scope_version": scope.scope_version,
                "gate": gate.code,
                "zones": sorted(z.code for z in zones),
                "methods": methods,
                "offline_capable": bool(offline_capable),
                "offline_sensitivity": offline_sensitivity,
            },
        )
    return scope


def _lifecycle(
    *,
    device: EntryDevice,
    actor,
    expected_version: int,
    allowed_from: tuple[str, ...],
    target: str,
    action_code: str,
    reason_code: str,
    reason_text: str,
) -> EntryDevice:
    _require_manager(actor, device.event_edition)
    if not reason_code:
        raise DeviceConfigurationError("A reason is required.", code="REASON_REQUIRED")
    now = timezone.now()
    with transaction.atomic():
        locked = _lock_device(device, expected_version=expected_version)
        if locked.status not in allowed_from:
            raise EntryStateError("This change is not permitted in the device's current state.")
        if target == EntryDeviceStatus.ENROLLED and locked.expires_at <= now:
            raise EntryStateError("This device has expired.", code="DEVICE_EXPIRED")
        if target != EntryDeviceStatus.ENROLLED:
            _withdraw_offline(
                locked,
                reason=target,
                now=now,
                retire_keys=target == EntryDeviceStatus.REVOKED,
                actor=actor,
            )
        _set_status(
            locked,
            status=target,
            actor=actor,
            reason_code=reason_code,
            reason_text=reason_text,
            now=now,
        )
        if target == EntryDeviceStatus.REVOKED:
            # The cookie can never authenticate again, even if the status
            # were ever mis-set: there is no longer a digest to match.
            locked.credential_hash = ""
            locked.activation_code_hash = ""
            locked.activation_code_expires_at = None
        locked.save()
        if target != EntryDeviceStatus.ENROLLED:
            _end_device_sessions(locked, reason=target, now=now)
        audit(
            action_code=action_code,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=reason_code,
            after_summary=device_summary(locked),
        )
    return locked


def suspend_device(*, device, actor, expected_version, reason_code, reason_text=""):
    return _lifecycle(
        device=device,
        actor=actor,
        expected_version=expected_version,
        allowed_from=OPERATIONAL_DEVICE_STATUSES,
        target=EntryDeviceStatus.SUSPENDED,
        action_code=action_codes.DEVICE_SUSPENDED,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def resume_device(*, device, actor, expected_version, reason_code, reason_text=""):
    return _lifecycle(
        device=device,
        actor=actor,
        expected_version=expected_version,
        allowed_from=(EntryDeviceStatus.SUSPENDED,),
        target=EntryDeviceStatus.ENROLLED,
        action_code=action_codes.DEVICE_RESUMED,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def revoke_device(*, device, actor, expected_version, reason_code, reason_text=""):
    return _lifecycle(
        device=device,
        actor=actor,
        expected_version=expected_version,
        allowed_from=(
            EntryDeviceStatus.PENDING_ENROLLMENT,
            *OPERATIONAL_DEVICE_STATUSES,
            EntryDeviceStatus.SUSPENDED,
        ),
        target=EntryDeviceStatus.REVOKED,
        action_code=action_codes.DEVICE_REVOKED,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def expire_device(*, device: EntryDevice, now=None) -> bool:
    """Persist EXPIRED for one device whose `expires_at` has passed."""
    now = now or timezone.now()
    with transaction.atomic():
        locked = EntryDevice.objects.select_for_update().filter(pk=device.pk).first()
        if locked is None or locked.status in TERMINAL_DEVICE_STATUSES or locked.expires_at > now:
            return False
        _withdraw_offline(locked, reason="EXPIRED", now=now, retire_keys=True)
        _set_status(
            locked,
            status=EntryDeviceStatus.EXPIRED,
            actor=None,
            reason_code="VALIDITY_ENDED",
            reason_text="",
            now=now,
        )
        locked.activation_code_hash = ""
        locked.activation_code_expires_at = None
        locked.save()
        _end_device_sessions(locked, reason="EXPIRED", now=now)
        audit(
            action_code=action_codes.DEVICE_EXPIRED,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            after_summary=device_summary(locked),
        )
    return True


def expire_lapsed_devices(*, now=None, limit: int = 500) -> int:
    """Idempotent sweep persisting EXPIRED for every lapsed device."""
    now = now or timezone.now()
    ids = list(
        EntryDevice.objects.exclude(status__in=TERMINAL_DEVICE_STATUSES)
        .filter(expires_at__lte=now)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    return sum(1 for pk in ids if expire_device(device=EntryDevice(pk=pk), now=now))
