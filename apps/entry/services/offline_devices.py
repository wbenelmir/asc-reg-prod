"""Enrolled-device offline PREPARATION (Phase 4 Prompt 2, ADR-0023).

Every command here is deliberate, online and authorized:

* per-event enablement (`entry.enable_offline_entry`), off by default;
* provisioning on the physical device by a signed-in device administrator:
  registration of the device's two non-extractable WebCrypto PUBLIC keys,
  proven by a signature made with the new signing key, and return of the
  package-verification keys the device PINS as its only trust roots
  (binding decision P2-D);
* the self-test report that alone can move a device to `OFFLINE_READY`;
* "Block offline use", which withdraws readiness without touching online
  operation or any local evidence (binding decision P2-F);
* the destructive emergency wipe ORDER, a distinct security authority with
  MFA step-up, explicit confirmation, reason code and evidence-loss record;
* the heartbeat: device-authenticated, passive, returning server time, a
  single-use nonce and the directives (block, purge package data, lock
  operations, revoke grants, emergency wipe) the device must obey.

Nothing here ever logs, audits or returns a device private key, a data key,
package content, a nonce, a signature, or an operation body.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    OPERATIONAL_DEVICE_STATUSES,
    DeviceEvidenceReport,
    DeviceKeyPurpose,
    DeviceKeyStatus,
    DeviceWipeOrder,
    EmergencyWipeReason,
    EntryDevice,
    EntryDeviceKey,
    EntryDeviceStatus,
    ExpectedEvidenceLoss,
    OfflineEventSetting,
    OfflinePackage,
    OfflineRequestNonce,
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
from apps.entry.services.offline_crypto import (
    OfflineCryptoError,
    b64url_decode,
    b64url_encode,
    key_fingerprint,
    load_device_public_key,
    pem_to_spki_b64,
    sha256_hex,
    verify_device_signature,
)

#: Event statuses in which offline continuity can never apply.
CLOSED_EVENT_STATUSES: tuple[str, ...] = ("COMPLETED", "ARCHIVED")

#: The self-test checks that must ALL pass before `OFFLINE_READY`. Durable
#: storage (`storage_persisted`) is reported but not required: browsers
#: grant it only by their own heuristics, and refusing readiness for it
#: would not make the device safer (ADR-0023 "Browser limitations").
REQUIRED_SELF_TEST_CHECKS: tuple[str, ...] = (
    "webcrypto_es256",
    "trust_pinned",
    "package_signature",
    "package_decrypt",
    "package_allow_list",
    "storage_roundtrip",
    "opstore_encryption",
    "storage_quota",
    "service_worker",
)
OPTIONAL_SELF_TEST_CHECKS: tuple[str, ...] = ("storage_persisted",)

_NONCE_SALT = "asc2026.entry.offline.nonce"
_PROOF_PREFIX = "ASC-DEVICE-PROOF-v1"
_MAX_REPORTED_COUNT = 10_000_000


class OfflineUnavailable(EntryServiceError):
    """Offline preparation is not possible right now; `code` says why."""


class OfflineRequestRejected(EntryServiceError):
    """A device request failed its proof, nonce, or shape checks."""


class DeviceNotAuthenticated(EntryServiceError):
    """No device matches the presented credential (unknown or revoked)."""

    code = "DEVICE_NOT_AUTHENTICATED"


class MfaStepUpRequired(EntryServiceError):
    code = "MFA_STEP_UP_FAILED"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def event_offline_setting(event_edition) -> OfflineEventSetting | None:
    return OfflineEventSetting.objects.filter(event_edition=event_edition).first()


def offline_enabled_for_event(event_edition) -> bool:
    """Global flag AND per-event enablement AND an open event."""
    if not settings.ENTRY_OFFLINE_ENABLED:
        return False
    if event_edition.status in CLOSED_EVENT_STATUSES:
        return False
    setting = event_offline_setting(event_edition)
    return bool(setting is not None and setting.enabled)


def offline_unavailability(device: EntryDevice, *, scope=None, now=None) -> str:
    """The first reason this device cannot hold offline data, or "".

    Order matters: the most fundamental reason is reported first.
    """
    from apps.entry.services.devices import current_scope

    now = now or timezone.now()
    if not settings.ENTRY_OFFLINE_ENABLED:
        return "OFFLINE_DISABLED"
    if device.event_edition.status in CLOSED_EVENT_STATUSES:
        return "EVENT_CLOSED"
    if not offline_enabled_for_event(device.event_edition):
        return "OFFLINE_DISABLED"
    if device.status not in OPERATIONAL_DEVICE_STATUSES or device.expires_at <= now:
        return "DEVICE_NOT_OPERATIONAL"
    if device.data_wipe_requested_at is not None:
        return "DEVICE_WIPE_ORDERED"
    if device.offline_blocked_at is not None:
        return "DEVICE_OFFLINE_BLOCKED"
    scope = scope if scope is not None else current_scope(device)
    if scope is None or not scope.offline_capable:
        return "SCOPE_NOT_OFFLINE"
    return ""


def active_device_key(device: EntryDevice, purpose: str) -> EntryDeviceKey | None:
    return EntryDeviceKey.objects.filter(
        device=device, purpose=purpose, status=DeviceKeyStatus.ACTIVE
    ).first()


def trust_anchors() -> list[dict]:
    """The package-verification public keys a device pins at provisioning.

    Every ACTIVE package-signing key of the separate package family, as
    canonical DER SubjectPublicKeyInfo. Never a QR key.
    """
    from apps.core.crypto.package_signing import get_package_signing_key_provider

    provider = get_package_signing_key_provider()
    return [
        {"kid": key_id, "spki": pem_to_spki_b64(provider.public_key_pem(key_id))}
        for key_id in provider.active_signing_key_ids()
    ]


# ---------------------------------------------------------------------------
# Single-use nonces and device proofs
# ---------------------------------------------------------------------------


def issue_nonce(device: EntryDevice) -> str:
    """A short-lived, device-bound, single-use nonce for the next signed call."""
    return signing.TimestampSigner(salt=_NONCE_SALT).sign(
        f"{device.public_id}.{secrets.token_urlsafe(18)}"
    )


def proof_message(*, purpose: str, nonce: str, body: bytes) -> bytes:
    """Exactly what the device signs: purpose, nonce and the body digest."""
    return f"{_PROOF_PREFIX}\n{purpose}\n{nonce}\n{sha256_hex(body)}".encode()


def verify_device_proof(
    device: EntryDevice,
    *,
    purpose: str,
    nonce: object,
    signature: object,
    body: bytes,
    public_key=None,
    now=None,
) -> None:
    """Verify the device's signature over (purpose, nonce, body) and consume
    the nonce. Raise `OfflineRequestRejected` on any failure.

    `public_key` defaults to the device's ACTIVE operation-signing key; the
    provisioning call passes the NEW key being registered, which is how the
    device proves possession of it.
    """
    now = now or timezone.now()
    if not isinstance(nonce, str) or not isinstance(signature, str) or len(nonce) > 200:
        raise OfflineRequestRejected("Missing device proof.", code="PROOF_MISSING")
    try:
        value = signing.TimestampSigner(salt=_NONCE_SALT).unsign(
            nonce, max_age=settings.ENTRY_OFFLINE_NONCE_SECONDS
        )
    except signing.BadSignature as exc:  # includes SignatureExpired
        raise OfflineRequestRejected("Invalid or expired nonce.", code="NONCE_INVALID") from exc
    if value.split(".", 1)[0] != device.public_id:
        raise OfflineRequestRejected("Nonce issued to another device.", code="NONCE_INVALID")
    if public_key is None:
        key = active_device_key(device, DeviceKeyPurpose.OPERATION_SIGNING)
        if key is None:
            raise OfflineRequestRejected("Device is not provisioned.", code="NOT_PROVISIONED")
        public_key = load_device_public_key(key.public_key_spki)
    try:
        raw_signature = b64url_decode(signature)
    except OfflineCryptoError as exc:
        raise OfflineRequestRejected("Malformed signature.", code="PROOF_INVALID") from exc
    if not verify_device_signature(
        public_key, proof_message(purpose=purpose, nonce=nonce, body=body), raw_signature
    ):
        raise OfflineRequestRejected("Invalid device signature.", code="PROOF_INVALID")
    try:
        with transaction.atomic():
            OfflineRequestNonce.objects.create(
                nonce_digest=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                device=device,
                purpose=purpose[:24],
                used_at=now,
            )
    except IntegrityError as exc:
        raise OfflineRequestRejected("Nonce already used.", code="NONCE_REPLAYED") from exc


def record_request_rejection(device, *, purpose: str, code: str) -> None:
    with transaction.atomic():
        audit(
            action_code=action_codes.OFFLINE_REQUEST_REJECTED,
            target_type="EntryDevice",
            target_uuid=getattr(device, "pk", None),
            event_edition_id=getattr(device, "event_edition_id", None),
            result="DENIED",
            reason_code=code,
            after_summary={"purpose": purpose},
            actor_type="SYSTEM",
        )


# ---------------------------------------------------------------------------
# Device authentication for offline calls (any status, so directives reach it)
# ---------------------------------------------------------------------------


def authenticate_device_any_status(raw_credential: object) -> EntryDevice | None:
    """Resolve the device presenting `raw_credential` in ANY status.

    Used only by the offline API so that a suspended, expired or wipe-ordered
    device still receives the directives it must obey. A revoked device has
    no credential digest any more and resolves to None (the device then
    locks its evidence and purges package data on its own).
    """
    if not isinstance(raw_credential, str) or not 20 <= len(raw_credential) <= 128:
        return None
    return (
        EntryDevice.objects.select_related("event_edition")
        .filter(credential_hash=secret_digest(raw_credential))
        .exclude(credential_hash="")
        .first()
    )


# ---------------------------------------------------------------------------
# Readiness withdrawal (shared by every lifecycle path)
# ---------------------------------------------------------------------------


def withdraw_offline_readiness(
    locked: EntryDevice, *, reason: str, now, retire_keys: bool = False, actor=None
) -> None:
    """Revoke READY packages and active grants; OFFLINE_READY -> ENROLLED.

    Caller holds the device row lock and saves the device. Never touches a
    device's local evidence: that stays on the device, locked and
    upload-only, until durable server acknowledgement (Prompt 3).
    """
    from apps.entry.services.offline_grants import revoke_device_grants
    from apps.entry.services.offline_packages import revoke_device_packages

    was_ready = locked.status == EntryDeviceStatus.OFFLINE_READY
    revoked = revoke_device_packages(locked, reason=reason, now=now)
    revoke_device_grants(locked, reason=reason, now=now)
    retired = 0
    if retire_keys:
        retired = EntryDeviceKey.objects.filter(
            device=locked, status=DeviceKeyStatus.ACTIVE
        ).update(status=DeviceKeyStatus.RETIRED, retired_at=now)
    if was_ready:
        locked.status = EntryDeviceStatus.ENROLLED
        locked.status_changed_at = now
    if was_ready or revoked or retired:
        audit(
            action_code=action_codes.DEVICE_OFFLINE_READINESS_WITHDRAWN,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=reason,
            after_summary={
                "public_id": locked.public_id,
                "was_offline_ready": was_ready,
                "packages_revoked": revoked,
                "keys_retired": retired,
            },
        )


# ---------------------------------------------------------------------------
# Per-event enablement
# ---------------------------------------------------------------------------


def set_event_offline_enabled(*, event_edition, enabled: bool, actor, reason_code: str):
    """Enable or disable offline continuity for one event (binding decision P2-E)."""
    from apps.accounts.policies import has_scoped_permission

    if not has_scoped_permission(
        actor, "entry.enable_offline_entry", event_edition_id=event_edition.pk
    ):
        raise EntryPermissionError("You are not authorized to change offline continuity.")
    if not reason_code:
        raise EntryStateError("A reason is required.", code="REASON_REQUIRED")
    if enabled and event_edition.status in CLOSED_EVENT_STATUSES:
        raise EntryStateError("A closed event cannot use offline continuity.", code="EVENT_CLOSED")
    now = timezone.now()
    with transaction.atomic():
        setting, _ = OfflineEventSetting.objects.select_for_update().get_or_create(
            event_edition=event_edition,
            defaults={"enabled": False, "changed_by": actor, "changed_at": now},
        )
        setting.enabled = bool(enabled)
        setting.changed_by = actor
        setting.changed_at = now
        setting.reason_code = reason_code[:32]
        setting.version += 1
        setting.save()
        if not enabled:
            for device in EntryDevice.objects.select_for_update().filter(
                event_edition=event_edition
            ):
                withdraw_offline_readiness(device, reason="OFFLINE_DISABLED", now=now, actor=actor)
                device.save()
        audit(
            action_code=(
                action_codes.OFFLINE_EVENT_ENABLED
                if enabled
                else action_codes.OFFLINE_EVENT_DISABLED
            ),
            actor=actor,
            target_type="EventEdition",
            target_uuid=event_edition.pk,
            event_edition_id=event_edition.pk,
            reason_code=reason_code,
            after_summary={"enabled": bool(enabled)},
        )
    return setting


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provisioning:
    device: EntryDevice
    trust_anchors: list


def provision_device(
    *,
    device: EntryDevice,
    actor,
    signing_key_spki: object,
    unwrap_key_spki: object,
    nonce: object,
    signature: object,
    body: bytes,
) -> Provisioning:
    """Register the device's two new public keys and return the trust roots.

    Requires a signed-in device administrator ON the device (the caller
    presents both the operational session and the device cookie), offline
    availability, and a proof signed by the NEW signing key. Re-provisioning
    retires the previous keys and revokes packages encrypted to them.
    """
    from apps.entry.policies import may_manage_devices

    if not may_manage_devices(actor, event_edition_id=device.event_edition_id):
        raise EntryPermissionError("You are not authorized to prepare entry devices.")
    now = timezone.now()
    reason = offline_unavailability_for_provisioning(device, now=now)
    if reason:
        raise OfflineUnavailable("Offline preparation is not available.", code=reason)
    try:
        signing_key = load_device_public_key(signing_key_spki)
        load_device_public_key(unwrap_key_spki)
    except OfflineCryptoError as exc:
        raise OfflineRequestRejected("Invalid device public key.", code="KEY_INVALID") from exc
    if signing_key_spki == unwrap_key_spki:
        raise OfflineRequestRejected("The two device keys must differ.", code="KEY_INVALID")
    verify_device_proof(
        device,
        purpose="provision",
        nonce=nonce,
        signature=signature,
        body=body,
        public_key=signing_key,
        now=now,
    )
    anchors = trust_anchors()
    try:
        with transaction.atomic():
            locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
            withdraw_offline_readiness(
                locked, reason="REPROVISIONED", now=now, retire_keys=True, actor=actor
            )
            for purpose, spki in (
                (DeviceKeyPurpose.OPERATION_SIGNING, signing_key_spki),
                (DeviceKeyPurpose.PACKAGE_UNWRAP, unwrap_key_spki),
            ):
                previous = (
                    EntryDeviceKey.objects.filter(device=locked, purpose=purpose)
                    .order_by("-key_version")
                    .values_list("key_version", flat=True)
                    .first()
                )
                EntryDeviceKey.objects.create(
                    device=locked,
                    purpose=purpose,
                    key_version=(previous or 0) + 1,
                    public_key_spki=spki,
                    fingerprint=key_fingerprint(spki),
                    created_at=now,
                    created_by=actor,
                )
            locked.offline_prepared_at = now
            locked.offline_prepared_by = actor
            locked.offline_self_test_at = None
            locked.offline_blocked_at = None
            locked.version += 1
            locked.save()
            audit(
                action_code=action_codes.DEVICE_OFFLINE_PROVISIONED,
                actor=actor,
                target_type="EntryDevice",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                after_summary={
                    "public_id": locked.public_id,
                    "signing_key_fp": key_fingerprint(signing_key_spki)[:16],
                    "unwrap_key_fp": key_fingerprint(unwrap_key_spki)[:16],
                    "trust_key_ids": [a["kid"] for a in anchors],
                },
            )
    except IntegrityError as exc:
        # A public key already registered to any device is refused.
        raise OfflineRequestRejected("Device key already registered.", code="KEY_REUSED") from exc
    return Provisioning(device=locked, trust_anchors=anchors)


def offline_unavailability_for_provisioning(device: EntryDevice, *, now) -> str:
    """Like `offline_unavailability`, except that "Block offline use" is what
    re-provisioning lifts, so a blocked device may be prepared again."""
    reason = offline_unavailability(device, now=now)
    return "" if reason == "DEVICE_OFFLINE_BLOCKED" else reason


# ---------------------------------------------------------------------------
# Self-test and readiness
# ---------------------------------------------------------------------------


def record_self_test(
    *,
    device: EntryDevice,
    actor,
    package_public_id: object,
    checks: object,
    nonce: object,
    signature: object,
    body: bytes,
) -> bool:
    """Record a self-test report; move to OFFLINE_READY only if EVERY
    required check passed on the CURRENT package. Returns True if ready."""
    from apps.entry.policies import may_manage_devices
    from apps.entry.services.offline_packages import current_package

    if not may_manage_devices(actor, event_edition_id=device.event_edition_id):
        raise EntryPermissionError("You are not authorized to prepare entry devices.")
    now = timezone.now()
    reason = offline_unavailability(device, now=now)
    if reason:
        raise OfflineUnavailable("Offline preparation is not available.", code=reason)
    verify_device_proof(
        device, purpose="self-test", nonce=nonce, signature=signature, body=body, now=now
    )
    if not isinstance(checks, dict) or not all(isinstance(v, bool) for v in checks.values()):
        raise OfflineRequestRejected("Malformed self-test report.", code="REPORT_INVALID")
    unknown = set(checks) - set(REQUIRED_SELF_TEST_CHECKS) - set(OPTIONAL_SELF_TEST_CHECKS)
    if unknown:
        raise OfflineRequestRejected("Unknown self-test check.", code="REPORT_INVALID")
    failed = sorted(name for name in REQUIRED_SELF_TEST_CHECKS if checks.get(name) is not True)
    package = current_package(device, now=now)
    if package is None or package.public_id != package_public_id:
        failed.append("current_package")
    with transaction.atomic():
        locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
        summary = {
            "public_id": locked.public_id,
            "failed": failed,
            "optional": {name: bool(checks.get(name)) for name in OPTIONAL_SELF_TEST_CHECKS},
            "package_version": getattr(package, "package_version", None),
        }
        if failed:
            audit(
                action_code=action_codes.DEVICE_OFFLINE_SELF_TEST_FAILED,
                actor=actor,
                target_type="EntryDevice",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="FAILURE",
                after_summary=summary,
            )
            return False
        locked.offline_self_test_at = now
        was_ready = locked.status == EntryDeviceStatus.OFFLINE_READY
        locked.status = EntryDeviceStatus.OFFLINE_READY
        locked.status_changed_at = now
        locked.version += 1
        locked.save()
        OfflinePackage.objects.filter(pk=package.pk, activation_reported_at__isnull=True).update(
            activation_reported_at=now
        )
        audit(
            action_code=action_codes.DEVICE_OFFLINE_SELF_TEST_PASSED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            after_summary=summary,
        )
        if not was_ready:
            audit(
                action_code=action_codes.DEVICE_OFFLINE_READY,
                actor=actor,
                target_type="EntryDevice",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                after_summary={"public_id": locked.public_id},
            )
    return True


# ---------------------------------------------------------------------------
# Block offline use (ordinary, non-destructive; binding decision P2-F)
# ---------------------------------------------------------------------------


def block_offline_use(*, device: EntryDevice, actor, expected_version: int, reason_code: str):
    """Withdraw offline readiness and prevent further offline admission.

    Revokes the device's packages and grants and requires re-preparation.
    Online operation is unaffected, and nothing on the device is deleted:
    its unacknowledged operations become locked and upload-only.
    """
    from apps.entry.policies import may_manage_devices

    if not may_manage_devices(actor, event_edition_id=device.event_edition_id):
        raise EntryPermissionError("You are not authorized to manage entry devices.")
    if not reason_code:
        raise EntryStateError("A reason is required.", code="REASON_REQUIRED")
    now = timezone.now()
    with transaction.atomic():
        locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
        if locked.version != expected_version:
            raise EntryConcurrencyError("This device changed since you loaded it.")
        if locked.status in (EntryDeviceStatus.REVOKED, EntryDeviceStatus.EXPIRED):
            raise EntryStateError("This device can no longer be changed.")
        withdraw_offline_readiness(locked, reason="OFFLINE_BLOCKED", now=now, actor=actor)
        locked.offline_blocked_at = now
        locked.version += 1
        locked.save()
        audit(
            action_code=action_codes.DEVICE_OFFLINE_BLOCKED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=reason_code,
            after_summary={"public_id": locked.public_id},
        )
    return locked


# ---------------------------------------------------------------------------
# Destructive emergency wipe (distinct authority; binding decision P2-F)
# ---------------------------------------------------------------------------


def may_order_emergency_wipe(user, *, event_edition_id) -> bool:
    from apps.accounts.policies import has_scoped_permission

    return has_scoped_permission(
        user, "entry.emergency_wipe_device", event_edition_id=event_edition_id
    )


def expected_evidence_loss(device: EntryDevice) -> str:
    if device.reported_at is None:
        return ExpectedEvidenceLoss.UNKNOWN
    pending = (device.reported_pending_operations or 0) + (device.reported_locked_operations or 0)
    return (
        ExpectedEvidenceLoss.OPERATIONS_AT_RISK if pending else ExpectedEvidenceLoss.NONE_REPORTED
    )


def order_emergency_wipe(
    *,
    device: EntryDevice,
    actor,
    reason_code: str,
    note: str,
    confirmed: bool,
    mfa_response: object,
) -> DeviceWipeOrder:
    """Order a destructive wipe of a device's local store.

    Requires `entry.emergency_wipe_device` (Security Restriction Managers),
    an MFA step-up through the `apps.accounts.mfa` boundary (fails closed
    where no provider exists), explicit confirmation, and a reason code.
    Records the evidence-loss scope known now. Also withdraws readiness and
    stops the device online (`data_wipe_requested_at`).
    """
    from apps.accounts.mfa import MfaStepUpUnavailable, verify_step_up
    from apps.core.crypto.package_signing import get_package_signing_key_provider

    event_id = device.event_edition_id
    if not may_order_emergency_wipe(actor, event_edition_id=event_id):
        raise EntryPermissionError("You are not authorized to order an emergency wipe.")

    def refuse(code: str, message: str, error_class=EntryStateError):
        with transaction.atomic():
            audit(
                action_code=action_codes.DEVICE_EMERGENCY_WIPE_REFUSED,
                actor=actor,
                target_type="EntryDevice",
                target_uuid=device.pk,
                event_edition_id=event_id,
                result="DENIED",
                reason_code=code,
            )
        raise error_class(message, code=code)

    if reason_code not in EmergencyWipeReason.values:
        refuse("REASON_REQUIRED", "A reason is required.")
    if confirmed is not True:
        refuse("CONFIRMATION_REQUIRED", "The evidence-loss confirmation is required.")
    note = (note or "").strip()
    if len(note) > 300:
        refuse("NOTE_TOO_LONG", "The note is too long.")
    try:
        verified = verify_step_up(user=actor, purpose="ENTRY_EMERGENCY_WIPE", response=mfa_response)
    except MfaStepUpUnavailable:
        refuse("MFA_UNAVAILABLE", "MFA step-up is not available.", MfaStepUpRequired)
    if not verified:
        refuse("MFA_STEP_UP_FAILED", "The MFA step-up failed.", MfaStepUpRequired)
    now = timezone.now()
    provider = get_package_signing_key_provider()
    with transaction.atomic():
        locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
        if locked.status == EntryDeviceStatus.REVOKED and not locked.credential_hash:
            # Still recorded: the order is delivered if the device ever
            # presents a credential again (it cannot) -- the order documents
            # the authority's decision and the known evidence scope.
            pass
        order = DeviceWipeOrder.objects.create(
            public_id=new_public_id(),
            device=locked,
            event_edition_id=event_id,
            ordered_by=actor,
            reason_code=reason_code,
            note_encrypted=note,
            confirmed=True,
            mfa_verified_at=now,
            signing_key_id=provider.current_signing_key_id(),
            known_pending_operations=locked.reported_pending_operations,
            known_locked_operations=locked.reported_locked_operations,
            known_sequence_high=locked.reported_sequence_high,
            known_chain_head=locked.reported_chain_head,
            known_reported_at=locked.reported_at,
            expected_evidence_loss=expected_evidence_loss(locked),
            created_at=now,
        )
        withdraw_offline_readiness(locked, reason="EMERGENCY_WIPE", now=now, actor=actor)
        if locked.data_wipe_requested_at is None:
            locked.data_wipe_requested_at = now
        locked.version += 1
        locked.save()
        from apps.entry.services.sessions import end_device_sessions

        end_device_sessions(device=locked, reason="EMERGENCY_WIPE", now=now)
        audit(
            action_code=action_codes.DEVICE_EMERGENCY_WIPE_ORDERED,
            actor=actor,
            target_type="EntryDevice",
            target_uuid=locked.pk,
            event_edition_id=event_id,
            reason_code=reason_code,
            after_summary={
                "public_id": locked.public_id,
                "order": order.public_id,
                "note_present": bool(note),
                "known_pending": order.known_pending_operations,
                "known_locked": order.known_locked_operations,
                "known_sequence_high": order.known_sequence_high,
                "expected_evidence_loss": order.expected_evidence_loss,
            },
        )
    return order


def pending_wipe_order(device: EntryDevice) -> DeviceWipeOrder | None:
    """The wipe order the device must still obey: one issued for its CURRENT
    enrollment (a re-enrollment by an administrator starts afresh)."""
    orders = DeviceWipeOrder.objects.filter(device=device)
    if device.enrolled_at is not None:
        orders = orders.filter(created_at__gte=device.enrolled_at)
    return orders.order_by("-created_at").first()


def signed_wipe_order(order: DeviceWipeOrder) -> str:
    """The wipe directive, signed with the package family so the device can
    verify it against its PINNED keys before destroying anything."""
    from apps.core.crypto.package_signing import get_package_signing_key_provider
    from apps.entry.offline_contract import TYP_WIPE_ORDER, WIPE_ORDER_SCHEMA_VERSION
    from apps.entry.services.offline_crypto import jws_sign

    compact, _kid = jws_sign(
        {
            "typ": TYP_WIPE_ORDER,
            "schema_version": WIPE_ORDER_SCHEMA_VERSION,
            "order_id": order.public_id,
            "device": order.device.public_id,
            "reason_code": order.reason_code,
            "issued_at": int(order.created_at.timestamp()),
        },
        typ=TYP_WIPE_ORDER,
        provider=get_package_signing_key_provider(),
    )
    return compact


def record_evidence_report(
    *, device: EntryDevice, report: object, nonce: object, signature: object, body: bytes
) -> DeviceEvidenceReport:
    """Store the metadata a device reports right before an emergency wipe."""
    now = timezone.now()
    verify_device_proof(
        device, purpose="wipe-report", nonce=nonce, signature=signature, body=body, now=now
    )
    if not isinstance(report, dict):
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    order = DeviceWipeOrder.objects.filter(device=device, public_id=report.get("order_id")).first()
    if order is None:
        raise OfflineRequestRejected("Unknown wipe order.", code="REPORT_INVALID")
    counts = _reported_counts(report)
    with transaction.atomic():
        evidence = DeviceEvidenceReport.objects.create(
            device=device,
            wipe_order=order,
            pending_operations=counts["pending"],
            locked_operations=counts["locked"],
            sequence_low=counts["sequence_low"],
            sequence_high=counts["sequence_high"],
            chain_head=counts["chain_head"],
            received_at=now,
        )
        audit(
            action_code=action_codes.DEVICE_EVIDENCE_REPORTED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            actor_type="SYSTEM",
            after_summary={
                "order": order.public_id,
                "pending": counts["pending"],
                "locked": counts["locked"],
                "sequence_low": counts["sequence_low"],
                "sequence_high": counts["sequence_high"],
            },
        )
    return evidence


def _bounded_int(value, *, allow_none=True):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**62:
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    return value


def _reported_counts(report: dict) -> dict:
    pending = _bounded_int(report.get("pending", 0), allow_none=False)
    locked = _bounded_int(report.get("locked", 0), allow_none=False)
    if pending > _MAX_REPORTED_COUNT or locked > _MAX_REPORTED_COUNT:
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    chain_head = report.get("chain_head") or ""
    if not isinstance(chain_head, str) or (
        chain_head
        and (len(chain_head) != 64 or any(c not in "0123456789abcdef" for c in chain_head))
    ):
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    return {
        "pending": pending,
        "locked": locked,
        "sequence_low": _bounded_int(report.get("sequence_low")),
        "sequence_high": _bounded_int(report.get("sequence_high")),
        "chain_head": chain_head,
    }


# ---------------------------------------------------------------------------
# Heartbeat: status, nonce, directives (passive)
# ---------------------------------------------------------------------------

_REPORTABLE_STATES = frozenset(
    {"ONLINE", "OFFLINE_READY", "OFFLINE_ACTIVE", "SYNCING", "STALE", "EXPIRED", "BLOCKED"}
)


def heartbeat(
    *, device: EntryDevice, report: object, nonce: object, signature: object, body: bytes
) -> dict:
    """Directives, server time and the next nonce for one device.

    A report is stored only when it carries a valid proof from the device's
    registered signing key; an unsigned heartbeat still receives directives.
    """
    from apps.entry.services.devices import current_scope
    from apps.entry.services.offline_grants import recently_revoked_grant_ids
    from apps.entry.services.offline_packages import current_package

    now = timezone.now()
    report_accepted = False
    if report is not None and nonce is not None:
        try:
            verify_device_proof(
                device, purpose="heartbeat", nonce=nonce, signature=signature, body=body, now=now
            )
            _store_report(device, report, now=now)
            report_accepted = True
        except OfflineRequestRejected as exc:
            record_request_rejection(device, purpose="heartbeat", code=exc.code)
    if device.last_seen_at is None or (now - device.last_seen_at).total_seconds() >= 60:
        EntryDevice.objects.filter(pk=device.pk).update(last_seen_at=now)

    scope = current_scope(device)
    unavailable = offline_unavailability(device, scope=scope, now=now)
    directives: list[dict] = []
    order = pending_wipe_order(device)
    if order is not None:
        directives.append({"type": "EMERGENCY_WIPE", "order": signed_wipe_order(order)})
    blocking = {
        EntryDeviceStatus.SUSPENDED: "DEVICE_SUSPENDED",
        EntryDeviceStatus.EXPIRED: "DEVICE_EXPIRED",
        EntryDeviceStatus.REVOKED: "DEVICE_REVOKED",
        EntryDeviceStatus.PENDING_ENROLLMENT: "DEVICE_NOT_ENROLLED",
    }.get(device.status)
    if blocking is None and device.expires_at <= now:
        blocking = "DEVICE_EXPIRED"
    if blocking is None and device.event_edition.status in CLOSED_EVENT_STATUSES:
        blocking = "EVENT_CLOSED"
    if blocking is None and order is not None:
        blocking = "DEVICE_WIPE_ORDERED"
    if blocking is not None:
        directives.append({"type": "BLOCK", "reason": blocking})
    if blocking is not None or unavailable:
        reason = blocking or unavailable
        directives.append({"type": "PURGE_PACKAGE", "reason": reason})
        directives.append({"type": "LOCK_OPERATIONS", "reason": reason})
    revoked_grants = recently_revoked_grant_ids(device, now=now)
    if revoked_grants:
        directives.append({"type": "REVOKE_GRANTS", "grants": revoked_grants})
    package = current_package(device, now=now) if not unavailable else None
    return {
        "server_time": int(now.timestamp()),
        "nonce": issue_nonce(device),
        "report_accepted": report_accepted,
        "device": {
            "public_id": device.public_id,
            "name": device.public_name,
            "status": device.status,
            "expires_at": int(device.expires_at.timestamp()),
            "event": device.event_edition.code,
            "gate": scope.gate.code if scope is not None else "",
        },
        "offline": {
            "available": not unavailable,
            "unavailable_reason": unavailable,
            "scope_version": scope.scope_version if scope is not None else None,
            "sensitivity": scope.offline_sensitivity if scope is not None else "",
            "prepared": device.offline_prepared_at is not None,
            "prepared_at": _epoch(device.offline_prepared_at),
            "self_tested_at": _epoch(device.offline_self_test_at),
            "ready": device.status == EntryDeviceStatus.OFFLINE_READY,
            "latest_package_version": getattr(package, "package_version", None),
        },
        "directives": directives,
    }


def _epoch(value):
    return int(value.timestamp()) if value is not None else None


def _store_report(device: EntryDevice, report: object, *, now) -> None:
    if not isinstance(report, dict):
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    state = report.get("state")
    if state not in _REPORTABLE_STATES:
        raise OfflineRequestRejected("Malformed report.", code="REPORT_INVALID")
    counts = _reported_counts(report)
    EntryDevice.objects.filter(pk=device.pk).update(
        reported_state=state,
        reported_pending_operations=counts["pending"],
        reported_locked_operations=counts["locked"],
        reported_sequence_high=counts["sequence_high"],
        reported_chain_head=counts["chain_head"],
        reported_package_version=_bounded_int(report.get("package_version")),
        reported_delta_version=_bounded_int(report.get("delta_version")),
        reported_at=now,
    )


def next_nonce_header(device) -> dict:
    return {"X-ASC-Next-Nonce": issue_nonce(device)}


__all__ = [
    "CLOSED_EVENT_STATUSES",
    "DeviceNotAuthenticated",
    "OfflineRequestRejected",
    "OfflineUnavailable",
    "b64url_encode",
]
