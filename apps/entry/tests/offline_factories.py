"""Synthetic builders for the offline-preparation suite (Phase 4 Prompt 2).

A `SyntheticDevice` plays the device browser's role in Python: it holds an
in-memory ECDSA key (operation signing) and an in-memory ECDH key (package
unwrapping), exactly as WebCrypto would -- generated per test and discarded.
It signs request proofs over (purpose, nonce, body digest) and can decrypt
what the server encrypted to it, which is how the tests prove the round
trip. No private key is ever written anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from django.utils import timezone

from apps.entry.models import OfflineEventSetting
from apps.entry.services.offline_crypto import (
    b64url_decode,
    b64url_encode,
    jws_verify,
    public_key_der,
    unwrap_and_decrypt_for_tests,
)
from apps.entry.services.offline_devices import issue_nonce, proof_message


def raw_sign(private_key, message: bytes) -> str:
    r, s = decode_dss_signature(private_key.sign(message, ec.ECDSA(hashes.SHA256())))
    size = (private_key.curve.key_size + 7) // 8
    return b64url_encode(r.to_bytes(size, "big") + s.to_bytes(size, "big"))


@dataclass
class SyntheticDevice:
    device: object
    sign_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    unwrap_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    trust_anchors: list = field(default_factory=list)

    @property
    def sign_spki(self) -> str:
        return b64url_encode(public_key_der(self.sign_key.public_key()))

    @property
    def unwrap_spki(self) -> str:
        return b64url_encode(public_key_der(self.unwrap_key.public_key()))

    def signed(self, purpose: str, payload: dict, *, key=None, nonce: str | None = None):
        """Return `(body_bytes, nonce, signature)` for one signed request."""
        body = json.dumps(payload).encode("utf-8")
        nonce = nonce or issue_nonce(self.device)
        signature = raw_sign(
            key or self.sign_key, proof_message(purpose=purpose, nonce=nonce, body=body)
        )
        return body, nonce, signature

    def trusted_keys_der(self) -> dict:
        return {anchor["kid"]: b64url_decode(anchor["spki"]) for anchor in self.trust_anchors}

    def open_response(self, raw: bytes, *, typ: str, kind: str) -> tuple[dict, dict]:
        """Verify a package or delta response with PINNED keys and decrypt it."""
        envelope = json.loads(raw)
        manifest = jws_verify(
            envelope["manifest"], trusted_keys_der=self.trusted_keys_der(), typ=typ
        )
        object_id = (
            manifest["package_id"]
            if kind == "OPKG"
            else f"{manifest['package_id']}.d{manifest['delta_version']}"
        )
        plaintext = unwrap_and_decrypt_for_tests(
            b64url_decode(envelope["ciphertext"]),
            enc=manifest["enc"],
            wrap=manifest["wrap"],
            recipient_private_key=self.unwrap_key,
            device_public_id=manifest["device"],
            object_id=object_id,
            kind=kind,
        )
        return manifest, json.loads(plaintext)


def timezone_with_day_left(*, hours: int = 9) -> str:
    """An event timezone in which at least `hours` of the day remain now, so
    every validity band (capped by the end of the event day) is reachable
    whatever the wall-clock time of the run."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    for name in (
        "UTC",
        "Pacific/Pago_Pago",
        "Pacific/Kiritimati",
        "America/New_York",
        "Asia/Tokyo",
    ):
        local = now.astimezone(ZoneInfo(name))
        if 24 - local.hour - local.minute / 60 >= hours:
            return name
    raise AssertionError("no timezone with enough day left")  # pragma: no cover


def enable_event(event, actor) -> OfflineEventSetting:
    setting, _ = OfflineEventSetting.objects.update_or_create(
        event_edition=event,
        defaults={"enabled": True, "changed_by": actor, "changed_at": timezone.now()},
    )
    return setting


def make_offline_capable(
    device, *, admin, layout, zones=None, sensitivity="STANDARD", methods=None
):
    from apps.entry.services.devices import change_device_scope
    from apps.entry.tests.factories import ALL_METHODS

    device.refresh_from_db()
    change_device_scope(
        device=device,
        venue=layout.venue,
        gate=layout.gate_a,
        zones=zones or [layout.main, layout.hall],
        verification_methods=methods or ALL_METHODS,
        actor=admin,
        expected_version=device.version,
        offline_capable=True,
        offline_sensitivity=sensitivity,
    )
    device.refresh_from_db()
    return device


def provision(synthetic: SyntheticDevice, *, admin):
    from apps.entry.services.offline_devices import provision_device

    payload = {"signing_key": synthetic.sign_spki, "unwrap_key": synthetic.unwrap_spki}
    body, nonce, signature = synthetic.signed("provision", payload)
    provisioning = provision_device(
        device=synthetic.device,
        actor=admin,
        signing_key_spki=synthetic.sign_spki,
        unwrap_key_spki=synthetic.unwrap_spki,
        nonce=nonce,
        signature=signature,
        body=body,
    )
    synthetic.trust_anchors = provisioning.trust_anchors
    synthetic.device.refresh_from_db()
    return provisioning


def download_result(synthetic: SyntheticDevice, *, have_version=None, now=None):
    """One signed package request; returns the service's `PackageDownload`."""
    from apps.entry.services.offline_packages import download_package

    body, nonce, signature = synthetic.signed("package", {"have_version": have_version})
    return download_package(
        device=synthetic.device,
        have_version=have_version,
        nonce=nonce,
        signature=signature,
        body=body,
        now=now,
    )


def run_pending_builds(*, now=None) -> list:
    """Run every open build in the caller, as the Celery worker would. The
    unit tests run inside one transaction, where `on_commit` never fires."""
    from apps.entry.models import OPEN_BUILD_STATUSES, OfflinePackageBuild
    from apps.entry.services.offline_packages import run_package_build

    return [
        run_package_build(build.pk, now=now)
        for build in OfflinePackageBuild.objects.filter(status__in=OPEN_BUILD_STATUSES)
    ]


def download(synthetic: SyntheticDevice, *, have_version=None, now=None):
    """Request the package; while the answer is BUILDING, run the build (as
    the worker would) and ask again. Returns `(package, raw_or_None)`."""
    for _ in range(3):
        result = download_result(synthetic, have_version=have_version, now=now)
        if result.status != "BUILDING":
            return result.package, result.raw
        run_pending_builds(now=now)
    raise AssertionError("the package never became ready")


def all_checks_passed() -> dict:
    from apps.entry.services.offline_devices import (
        OPTIONAL_SELF_TEST_CHECKS,
        REQUIRED_SELF_TEST_CHECKS,
    )

    return {name: True for name in (*REQUIRED_SELF_TEST_CHECKS, *OPTIONAL_SELF_TEST_CHECKS)}


ZERO_HASH = "0" * 64


@dataclass
class SyntheticStore:
    """The device's local operation store, in Python (Phase 4 Prompt 3): a
    store id, a sequence counter and a hash chain, exactly as the browser
    keeps them. Every operation is canonical JSON signed with the device's
    in-memory signing key; the chain head is SHA-256(prev || operation)."""

    synthetic: SyntheticDevice
    store_id: str = field(default_factory=lambda: _random_b64u(16))
    sequence: int = 0
    head: str = ZERO_HASH

    def next(self, op: dict) -> dict:
        import hashlib

        from apps.entry.services.offline_crypto import canonical_json

        self.sequence += 1
        op = {**op, "store": self.store_id, "sequence": self.sequence, "prev": self.head}
        raw = canonical_json(op)
        self.head = hashlib.sha256(self.head.encode("utf-8") + raw).hexdigest()
        return op

    def envelope(self, op: dict, *, key=None, note=None, raw: bytes | None = None) -> dict:
        from apps.entry.services.offline_crypto import canonical_json

        raw = raw if raw is not None else canonical_json(op)
        envelope = {
            "operation_id": op["operation_id"],
            "sequence": op["sequence"],
            "op": raw.decode("utf-8"),
            "signature": raw_sign(key or self.synthetic.sign_key, raw),
        }
        if note is not None:
            envelope["note"] = note
        return envelope


def _random_b64u(size: int) -> str:
    import secrets

    return b64url_encode(secrets.token_bytes(size))


def new_operation_id() -> str:
    import uuid

    return str(uuid.uuid4())


def base_operation(
    *,
    device,
    package,
    grant=None,
    zone_code=None,
    op_type="ENTRY_DECISION",
    credential=None,
    local=None,
    decision="ADMIT",
    decision_reason="",
    override=None,
    occurred_at=None,
    band="FRESH",
    state="OFFLINE_ACTIVE",
    delta=None,
    operation_id=None,
) -> dict:
    """One operation body as the offline runtime records it."""
    occurred = int((occurred_at or timezone.now()).timestamp())
    cutoff = int(package.data_cutoff_at.timestamp())
    return {
        "schema_version": 1,
        "operation_id": operation_id or new_operation_id(),
        "type": op_type,
        "device": device.public_id,
        "event": device.event_edition.code,
        "store": "",
        "sequence": 0,
        "prev": ZERO_HASH,
        "occurred_at": occurred,
        "device_time": occurred,
        "server_offset": 0,
        "state": state,
        "package": {
            "id": package.public_id,
            "version": package.package_version,
            "delta_version": delta.delta_version if delta is not None else None,
            "band": band,
            "data_cutoff_at": cutoff,
            "critical_delta_cutoff_at": int(delta.critical_delta_cutoff_at.timestamp())
            if delta is not None
            else cutoff,
        },
        "grant": grant.public_id if grant is not None else None,
        "gate": package.scope.gate.code,
        "zone": zone_code
        if zone_code is not None
        else (grant.device_session.zone.code if grant is not None else None),
        "credential": {"jti": credential.jti, "kid": credential.signing_key_id}
        if credential is not None
        else None,
        "local": local
        or {
            "code": "VALID",
            "result": "ALLOWED",
            "reason": "",
            "blockers": [],
            "advisories": [],
            "latency_ms": 12,
        },
        "decision": decision,
        "decision_reason": decision_reason,
        "override": override,
    }


def issue_grant(synthetic: SyntheticDevice, *, checkpoint):
    from apps.entry.services.offline_grants import issue_operator_grant

    body, nonce, signature = synthetic.signed("grant", {})
    grant, compact = issue_operator_grant(
        checkpoint=checkpoint, nonce=nonce, signature=signature, body=body
    )
    return grant, compact


def sync(synthetic: SyntheticDevice, store: SyntheticStore, envelopes: list, *, now=None):
    """One signed upload; returns the service's `SyncResult`."""
    from apps.entry.services.offline_sync import synchronize

    payload = {"store": store.store_id, "operations": envelopes}
    body, nonce, signature = synthetic.signed("sync", payload)
    import json as _json

    return synchronize(
        device=type(synthetic.device)
        .objects.select_related("event_edition")
        .get(pk=synthetic.device.pk),
        payload=_json.loads(body),
        nonce=nonce,
        signature=signature,
        body=body,
        now=now,
    )


def self_test(synthetic: SyntheticDevice, *, admin, package, checks=None):
    from apps.entry.services.offline_devices import record_self_test

    payload = {"package_id": package.public_id, "checks": checks or all_checks_passed()}
    body, nonce, signature = synthetic.signed("self-test", payload)
    result = record_self_test(
        device=synthetic.device,
        actor=admin,
        package_public_id=payload["package_id"],
        checks=payload["checks"],
        nonce=nonce,
        signature=signature,
        body=body,
    )
    synthetic.device.refresh_from_db()
    return result
