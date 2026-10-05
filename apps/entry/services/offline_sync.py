"""Ordered, idempotent synchronization of signed offline operations
(Phase 4 Prompt 3, ADR-0024, `docs/security/offline_sync_contract.md`).

One upload is a bounded batch of operations from ONE local store, in the
device's sequence order, authenticated twice: the batch by the device
cookie plus a proof over a single-use nonce (purpose `sync`), and every
operation by its OWN signature, made with a signing key registered to the
device when it was recorded. Each operation then goes through two
transactions:

1. **Intake.** Under a per-device and a per-operation advisory lock, the
   exact signed bytes are parsed, checked for canonical form, validated
   against the contract, verified against the device's keys and resolved
   against their package, grant, gate, zone and pass. The immutable
   `SyncOperation` evidence row is written FIRST -- PENDING, or directly
   REJECTED (with a reconciliation case) or QUARANTINED (device not
   operational). A replay of the same operation id and bytes never writes a
   second row: it only counts the duplicate and returns the original,
   durable outcome.
2. **Processing.** Under the same locks plus the Registration row lock (the
   serialization point of every online decision too), the PENDING row is
   evaluated against authoritative state AS OF its occurrence time
   (`apps.entry.services.offline_evaluation`), classified, and its outcome
   written in the same transaction: the offline Entry Event (and override),
   the telemetry sample, the audit record, any reconciliation case, and the
   final status. A failure leaves the durable PENDING row, and the device
   simply uploads again.

Only then is a signed acknowledgement returned. The batch stops at the
first operation without a durable outcome, so an operation is never
processed before its predecessor. Correctness never rests on a
process-local lock: the unique `operation_id`, the unique (device, store,
sequence), the unique `EntryEvent.operation_id` and one-to-one
`EntryEvent.sync_operation`, and the transactional locks above protect it.

A conflicting admission is NEVER silently turned into an admission, nor
silently discarded: its Entry Event records the physical fact
(`offline_conflict`), its case records what the device knew and what the
server knew, and a supervisor must close the case. The one exception is an
override whose catalogue reason can no longer be linked: the schema allows
it no Entry Event, so the immutable operation itself keeps the fact -- and
every later admission decision still counts it as a prior admission
(`apps.entry.selectors.admissions`, correction 5, decision D3 option S1).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    ADMITTABLE_RESULTS,
    NEVER_OVERRIDEABLE_REASON_CODES,
    OPERATIONAL_DEVICE_STATUSES,
    DeviceKeyPurpose,
    EntryDecision,
    EntryDevice,
    EntryDeviceKey,
    EntryDeviceStatus,
    EntryEvent,
    EntryEventType,
    EntryOverride,
    EntryOverrideReason,
    EntryReasonCode,
    OfflineCriticalDelta,
    OfflineOperatorGrant,
    OfflinePackage,
    OfflinePackageStatus,
    ReconciliationCase,
    ReconciliationCaseType,
    SyncOperation,
    SyncOperationStatus,
    SyncOperationType,
    VerificationMethod,
    VerificationMode,
    VerificationSample,
)
from apps.entry.offline_contract import (
    ACK_SCHEMA_VERSION,
    MAX_OPERATION_BYTES,
    OPERATION_TYPE_ATTEMPT,
    OPERATION_TYPE_DECISION,
    OPERATION_TYPES,
    TYP_ACKNOWLEDGEMENT,
    OfflineContractError,
    band_at,
    is_client_operation_id,
    validate_operation,
)
from apps.entry.selectors.admissions import unlinked_admissions_for
from apps.entry.services import audit, new_public_id
from apps.entry.services.offline_crypto import (
    OfflineCryptoError,
    b64url_decode,
    canonical_json,
    jws_sign,
    load_device_public_key,
    sha256_hex,
    verify_device_signature,
)
from apps.entry.services.offline_devices import (
    OfflineRequestRejected,
    OfflineUnavailable,
    record_request_rejection,
    verify_device_proof,
)
from apps.entry.services.offline_evaluation import (
    RESTRICTION_REASONS,
    ServerEvaluation,
    at_or_before,
    evaluate_at,
    status_changed_at,
)
from apps.entry.services.reconciliation import link_operation, open_case, open_quarantine_case

logger = logging.getLogger(__name__)

ZERO_HASH = "0" * 64
#: Advisory-lock namespaces (distinct from every other classid in use).
_LOCK_CLASS_DEVICE = 0x53594E43  # "SYNC"
_LOCK_CLASS_OPERATION = 0x534F5052  # "SOPR"
#: Only ever TRIED, never waited for (correction 5, R-02): serializes the
#: existence check and insert of an unauthenticated refusal audit per device
#: and refusal reason.
_LOCK_CLASS_UNAUTHENTICATED_REFUSAL = 0x53515246  # "SQRF"
_ENVELOPE = frozenset({"operation_id", "sequence", "op", "signature"})
#: An override note travels BESIDE the signed operation, which carries only
#: its salted digest, so the note never enters `payload_json` in clear.
_ENVELOPE_WITH_NOTE = _ENVELOPE | {"note"}
_NOTE_MAX_LENGTH = 500
_BATCH = frozenset({"store", "operations"})
_B64U = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class SyncBatchRefused(OfflineRequestRejected):
    """The whole batch is refused (shape); nothing in it was stored."""


def _epoch(value):
    return int(value.timestamp()) if value is not None else None


def _from_epoch(value):
    return datetime.fromtimestamp(value, tz=UTC)


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Acknowledgement:
    """What the device receives for one submitted operation.

    `durable` acknowledgements carry a compact JWS (`typ` ASC-OACK) signed
    with the package-signing family: the device verifies it against its
    PINNED keys and marks its local record acknowledged only then. A
    non-durable one (PENDING, NOT_PROCESSED) keeps the record pending."""

    operation_id: str
    sequence: int | None
    status: str
    outcome: str = ""
    conflict_type: str = ""
    durable: bool = False
    replayed: bool = False
    ack: str = ""

    def as_json(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "status": self.status,
            "outcome": self.outcome,
            "conflict_type": self.conflict_type,
            "durable": self.durable,
            "replayed": self.replayed,
            "ack": self.ack,
        }


def _sign_ack(
    *, device, store, operation_id, sequence, payload_hash, status, outcome, conflict, processed
):
    from apps.core.crypto.package_signing import get_package_signing_key_provider

    compact, _kid = jws_sign(
        {
            "typ": TYP_ACKNOWLEDGEMENT,
            "schema_version": ACK_SCHEMA_VERSION,
            "device": device.public_id,
            "store": store,
            "operation_id": operation_id,
            "sequence": sequence,
            "payload_hash": payload_hash,
            "status": status,
            "outcome": outcome,
            "conflict_type": conflict,
            "processed_at": _epoch(processed),
        },
        typ=TYP_ACKNOWLEDGEMENT,
        provider=get_package_signing_key_provider(),
    )
    return compact


def acknowledgement_for(row: SyncOperation, *, replayed: bool = False) -> Acknowledgement:
    """The acknowledgement of a stored row: durable (signed) once final."""
    if row.status == SyncOperationStatus.PENDING:
        return Acknowledgement(
            operation_id=row.operation_id,
            sequence=row.device_sequence,
            status=SyncOperationStatus.PENDING,
            replayed=replayed,
        )
    return Acknowledgement(
        operation_id=row.operation_id,
        sequence=row.device_sequence,
        status=row.status,
        outcome=row.outcome_code,
        conflict_type=row.conflict_type,
        durable=True,
        replayed=replayed,
        ack=_sign_ack(
            device=row.device,
            store=row.store_id,
            operation_id=row.operation_id,
            sequence=row.device_sequence,
            payload_hash=row.payload_hash,
            status=row.status,
            outcome=row.outcome_code,
            conflict=row.conflict_type,
            processed=row.processed_at,
        ),
    )


def _rowless_rejection(*, device, store, envelope, payload_hash, outcome, case) -> Acknowledgement:
    """A durable REJECTED acknowledgement for evidence recorded only in a
    reconciliation case (no own row: an unusable envelope, a reused id or a
    reused sequence). Deterministic: the case opening time is the outcome
    time, so a retry returns the same outcome."""
    operation_id = str(envelope.get("operation_id") or "")[:36]
    sequence = envelope.get("sequence")
    sequence = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None
    return Acknowledgement(
        operation_id=operation_id,
        sequence=sequence,
        status=SyncOperationStatus.REJECTED,
        outcome=outcome,
        conflict_type=case.case_type,
        durable=True,
        ack=_sign_ack(
            device=device,
            store=store,
            operation_id=operation_id,
            sequence=sequence,
            payload_hash=payload_hash,
            status=SyncOperationStatus.REJECTED,
            outcome=outcome,
            conflict=case.case_type,
            processed=case.opened_at,
        ),
    )


def _not_processed(envelope) -> Acknowledgement:
    sequence = envelope.get("sequence") if isinstance(envelope, dict) else None
    return Acknowledgement(
        operation_id=str((envelope or {}).get("operation_id") or "")[:36]
        if isinstance(envelope, dict)
        else "",
        sequence=sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else None,
        status="NOT_PROCESSED",
    )


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def _lock(classid: int, value: str) -> None:
    from apps.core.concurrency import acquire_advisory_locks, lock_key

    digest = hashlib.sha256(value.encode("utf-8")).digest()
    acquire_advisory_locks(connection, [lock_key(classid, digest)])


def _lock_device_and_operation(device_id, operation_id: str) -> None:
    """Always in this order (device, then operation), in every transaction."""
    _lock(_LOCK_CLASS_DEVICE, str(device_id))
    _lock(_LOCK_CLASS_OPERATION, operation_id or "-")


def _refusal_lock_key(device, code: str) -> tuple[int, int]:
    """The (classid, objid) of one device's unauthenticated refusal reason."""
    from apps.core.concurrency import lock_key

    digest = hashlib.sha256(f"{device.pk}:{code}".encode()).digest()
    return lock_key(_LOCK_CLASS_UNAUTHENTICATED_REFUSAL, digest)


def _try_transaction_lock(key: tuple[int, int]) -> bool:
    """`pg_try_advisory_xact_lock`: never waits. Released at the end of the
    caller's transaction; a single key, so no ordering applies (ADR-0007)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", list(key))
        return bool(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Batch entry points
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    acknowledgements: list[Acknowledgement] = field(default_factory=list)
    server_time: int = 0

    def as_json(self) -> dict:
        return {
            "server_time": self.server_time,
            "acknowledgements": [ack.as_json() for ack in self.acknowledgements],
        }


def device_is_quarantined(device: EntryDevice, *, now) -> bool:
    """Uploads from a device that is not operational are never applied."""
    return (
        device.status not in OPERATIONAL_DEVICE_STATUSES
        or device.expires_at <= now
        or device.data_wipe_requested_at is not None
    )


def _latest_sign_key(device):
    return (
        EntryDeviceKey.objects.filter(device=device, purpose=DeviceKeyPurpose.OPERATION_SIGNING)
        .order_by("-key_version")
        .first()
    )


def _sync_window_start(now):
    return now - timedelta(seconds=settings.ENTRY_OFFLINE_SYNC_WINDOW_SECONDS)


def _sync_budget_exhausted(device, *, now) -> bool:
    """The per-device upload budget counts batches that ran -- attributable
    to the device's keys -- never refusals, so nobody can spend it with
    unauthenticated traffic."""
    from apps.audit.models import AuditEvent

    used = AuditEvent.objects.filter(
        target_type="EntryDevice",
        target_uuid=device.pk,
        action_code=action_codes.OFFLINE_SYNC_BATCH_RECEIVED,
        occurred_at__gte=_sync_window_start(now),
    ).count()
    return used >= settings.ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW


def _enforce_sync_budget(device, *, now) -> None:
    if _sync_budget_exhausted(device, now=now):
        _refuse(device, "RATE_LIMITED")
        raise OfflineUnavailable("Too many uploads.", code="RATE_LIMITED")


def _refuse(device, code: str) -> None:
    with transaction.atomic():
        audit(
            action_code=action_codes.OFFLINE_SYNC_REFUSED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            result="DENIED",
            reason_code=code,
            actor_type="SYSTEM",
        )


def _refuse_unauthenticated(device, code: str, *, now) -> None:
    """A refusal on the quarantine boundary BEFORE any operation signature
    verified (R-02): whoever knows a revoked device's public id could send
    it. At most one audit event (and one WARNING line) per device, reason
    and budget window records it -- as the entry anomaly signals do
    (`apps.entry.services.limits`) -- instead of one row per request.

    The existence check and the insert run in ONE transaction under a
    transaction-scoped advisory lock on (device, reason) that is only ever
    TRIED (correction 5, R-02): concurrent refusals cannot all observe "no
    row yet", and a flood never queues on the lock. A request that finds it
    held skips this optional audit -- the holder is recording that same
    refusal -- and is refused exactly as before. Refusals that are not
    recorded write nothing and log nothing."""
    from apps.audit.models import AuditEvent

    with transaction.atomic():
        if not _try_transaction_lock(_refusal_lock_key(device, code)):
            return
        recorded = AuditEvent.objects.filter(
            target_type="EntryDevice",
            target_uuid=device.pk,
            action_code=action_codes.OFFLINE_SYNC_REFUSED,
            reason_code=code,
            occurred_at__gte=_sync_window_start(now),
        ).exists()
        if recorded:
            return
        audit(
            action_code=action_codes.OFFLINE_SYNC_REFUSED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            result="DENIED",
            reason_code=code,
            actor_type="SYSTEM",
            after_summary={
                "channel": "QUARANTINE",
                "authenticated": False,
                "window_seconds": settings.ENTRY_OFFLINE_SYNC_WINDOW_SECONDS,
            },
        )
    logger.warning(
        "Unauthenticated quarantine upload refused.",
        extra={"device": device.public_id, "reason": code},
    )


def _parse_batch(payload) -> tuple[str, list]:
    """The batch shape. Raises `SyncBatchRefused`; the caller records it."""
    if not isinstance(payload, dict) or set(payload) - (_BATCH | {"device"}):
        raise SyncBatchRefused("Malformed batch.", code="MALFORMED_BATCH")
    store = payload.get("store")
    operations = payload.get("operations")
    if (
        not isinstance(store, str)
        or len(store) != 22
        or any(c not in _B64U for c in store)
        or not isinstance(operations, list)
        or not 1 <= len(operations) <= settings.ENTRY_OFFLINE_SYNC_BATCH_SIZE
    ):
        raise SyncBatchRefused("Malformed batch.", code="MALFORMED_BATCH")
    # A device may retry a prefix, but may not reverse or duplicate the
    # declared order inside a batch. Malformed envelopes retain their
    # individual rejection path without influencing the sequence check.
    sequences = [env["sequence"] for env in operations if _envelope_usable(env)]
    if any(
        current <= previous for previous, current in zip(sequences, sequences[1:], strict=False)
    ):
        raise SyncBatchRefused("Batch order is invalid.", code="UNORDERED_BATCH")
    return store, operations


def synchronize(*, device, payload, nonce, signature, body: bytes, now=None) -> SyncResult:
    """Receive and process one signed batch from an authenticated device."""
    now = now or timezone.now()
    quarantined = device_is_quarantined(device, now=now)
    # A device that is no longer operational may have lost its ACTIVE key
    # (expiry retires keys): its batch proof is checked against its most
    # recent signing key, and everything it uploads is quarantined.
    public_key = None
    if quarantined:
        latest = _latest_sign_key(device)
        if latest is not None:
            public_key = load_device_public_key(latest.public_key_spki)
    try:
        verify_device_proof(
            device,
            purpose="sync",
            nonce=nonce,
            signature=signature,
            body=body,
            public_key=public_key,
            now=now,
        )
    except OfflineRequestRejected as exc:
        record_request_rejection(device, purpose="sync", code=exc.code)
        raise
    _enforce_sync_budget(device, now=now)
    try:
        store, envelopes = _parse_batch(payload)
    except SyncBatchRefused as exc:
        _refuse(device, exc.code)
        raise
    return _run_batch(device, store, envelopes, quarantined=quarantined, now=now, channel="SYNC")


def synchronize_quarantine(*, payload, now=None) -> SyncResult:
    """Quarantine ingestion for a REVOKED device (P2-F): no device cookie
    exists any more, so every operation must carry a valid signature by a
    key registered to the named device. Nothing unattributable is stored,
    and nothing uploaded here is ever applied.

    Until one operation signature verifies, the request is unauthenticated
    (R-02): anyone who knows the device's public id can send it. Such a
    request stores nothing, never spends the device's upload budget, is
    audited at most once per device, reason and window -- also under
    concurrency, and without ever waiting (`_refuse_unauthenticated`) --
    and costs at most one pass over the device's signing keys: verification
    stops at the first operation no registered key verifies, so -- as
    everywhere in synchronization -- no operation is processed after one
    without a durable outcome."""
    now = now or timezone.now()
    device_id = payload.get("device") if isinstance(payload, dict) else None
    device = (
        EntryDevice.objects.select_related("event_edition").filter(public_id=device_id).first()
        if isinstance(device_id, str) and len(device_id) == 22
        else None
    )
    if device is None or device.status != EntryDeviceStatus.REVOKED:
        raise OfflineRequestRejected("Quarantine upload refused.", code="QUARANTINE_REFUSED")
    if _sync_budget_exhausted(device, now=now):
        _refuse_unauthenticated(device, "RATE_LIMITED", now=now)
        raise OfflineUnavailable("Too many uploads.", code="RATE_LIMITED")
    try:
        store, envelopes = _parse_batch(payload)
    except SyncBatchRefused as exc:
        _refuse_unauthenticated(device, exc.code, now=now)
        raise
    keys = _device_sign_keys(device)
    verified = []
    for envelope in envelopes:
        if _envelope_signature_key(envelope, keys) is None:
            break
        verified.append(envelope)
    if not verified:
        _refuse_unauthenticated(device, "INVALID_SIGNATURE", now=now)
        raise OfflineRequestRejected("Quarantine upload refused.", code="QUARANTINE_REFUSED")
    return _run_batch(device, store, verified, quarantined=True, now=now, channel="QUARANTINE")


def _run_batch(device, store, envelopes, *, quarantined, now, channel) -> SyncResult:
    result = SyncResult(server_time=int(now.timestamp()))
    stopped = False
    for envelope in envelopes:
        if stopped:
            result.acknowledgements.append(_not_processed(envelope))
            continue
        ack = _handle(device, store, envelope, quarantined=quarantined, now=now)
        result.acknowledgements.append(ack)
        if not ack.durable:
            stopped = True  # never process an operation before its predecessor
    durable = [ack for ack in result.acknowledgements if ack.durable]
    if durable:
        EntryDevice.objects.filter(pk=device.pk).update(last_sync_at=now)
    counts: dict[str, int] = {}
    for ack in result.acknowledgements:
        counts[ack.status] = counts.get(ack.status, 0) + 1
    with transaction.atomic():
        audit(
            action_code=action_codes.OFFLINE_SYNC_BATCH_RECEIVED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            actor_type="SYSTEM",
            after_summary={
                "device": device.public_id,
                "channel": channel,
                "store": store,
                "operations": len(envelopes),
                "durable": len(durable),
                "replayed": sum(1 for ack in result.acknowledgements if ack.replayed),
                "statuses": counts,
            },
        )
    return result


def _handle(device, store, envelope, *, quarantined, now) -> Acknowledgement:
    try:
        intake = _intake(device, store, envelope, quarantined=quarantined, now=now)
    except Exception as exc:  # noqa: BLE001 - the device retries
        logger.error("Offline operation intake failed (%s).", type(exc).__name__)
        pending = _not_processed(envelope)
        return Acknowledgement(
            operation_id=pending.operation_id,
            sequence=pending.sequence,
            status=SyncOperationStatus.PENDING,
        )
    if intake.ack is not None:
        return intake.ack
    try:
        return process_operation(intake.row_id, now=now)
    except Exception as exc:  # noqa: BLE001 - durable evidence is retried later
        logger.error("Offline operation processing failed (%s).", type(exc).__name__)
        row = SyncOperation.objects.select_related("device").get(pk=intake.row_id)
        return acknowledgement_for(row)


# ---------------------------------------------------------------------------
# Phase 1: intake (validate, verify, resolve, store the evidence row)
# ---------------------------------------------------------------------------


@dataclass
class _Intake:
    row_id: object = None
    ack: Acknowledgement | None = None


@dataclass
class _Resolved:
    outcome: str = ""  # a REJECTED outcome code, or "" when accepted
    parsed: dict | None = None
    operation_type: str = SyncOperationType.UNSUPPORTED
    signature_key: EntryDeviceKey | None = None
    package: OfflinePackage | None = None
    grant: OfflineOperatorGrant | None = None
    gate: object = None
    zone: object = None
    credential: object = None
    occurred_at: datetime | None = None
    flags: list = field(default_factory=list)


def _device_sign_keys(device) -> list[EntryDeviceKey]:
    return list(
        EntryDeviceKey.objects.filter(
            device=device, purpose=DeviceKeyPurpose.OPERATION_SIGNING
        ).order_by("-key_version")
    )


def _signature_key(raw: bytes, signature_text, keys):
    try:
        signature = b64url_decode(signature_text)
    except OfflineCryptoError:
        return None
    if len(signature) != 64:
        return None
    for key in keys:
        try:
            public = load_device_public_key(key.public_key_spki)
        except OfflineCryptoError:
            continue
        if verify_device_signature(public, raw, signature):
            return key
    return None


def _envelope_signature_key(envelope, keys):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("op"), str):
        return None
    raw = envelope["op"].encode("utf-8")
    if len(raw) > MAX_OPERATION_BYTES:
        return None
    return _signature_key(raw, envelope.get("signature"), keys)


def _envelope_usable(envelope) -> bool:
    if not isinstance(envelope, dict):
        return False
    try:
        if len(b64url_decode(envelope.get("signature"))) != 64:
            return False
        if isinstance(envelope.get("op"), str):
            envelope["op"].encode("utf-8")
    except OfflineCryptoError, UnicodeError:
        return False
    return (
        isinstance(envelope, dict)
        and set(envelope) in (_ENVELOPE, _ENVELOPE_WITH_NOTE)
        and (
            "note" not in envelope
            or (isinstance(envelope["note"], str) and len(envelope["note"]) <= _NOTE_MAX_LENGTH)
        )
        and is_client_operation_id(envelope["operation_id"])
        and isinstance(envelope["sequence"], int)
        and not isinstance(envelope["sequence"], bool)
        and 1 <= envelope["sequence"] <= 2**53 - 1
        and isinstance(envelope["op"], str)
        and isinstance(envelope["signature"], str)
        and len(envelope["signature"]) <= 128
    )


def _schema_version(payload: dict):
    value = payload.get("schema_version")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**15:
        return value
    return None


def _intake(device, store, envelope, *, quarantined, now) -> _Intake:
    if not _envelope_usable(envelope):
        envelope = envelope if isinstance(envelope, dict) else {}
        return _Intake(ack=_reject_without_row(device, store, envelope, "MALFORMED_ENVELOPE", now))
    operation_id = envelope["operation_id"]
    raw = envelope["op"].encode("utf-8")
    payload_hash = sha256_hex(raw)
    with transaction.atomic():
        _lock_device_and_operation(device.pk, operation_id)
        device = (
            EntryDevice.objects.select_for_update(of=("self",))
            .select_related("event_edition")
            .get(pk=device.pk)
        )
        quarantined = quarantined or device_is_quarantined(device, now=timezone.now())
        existing = (
            SyncOperation.objects.select_for_update()
            .select_related("device")
            .filter(operation_id=operation_id)
            .first()
        )
        if existing is not None:
            if existing.device_id == device.pk and existing.payload_hash == payload_hash:
                SyncOperation.objects.filter(pk=existing.pk).update(
                    duplicate_submissions=F("duplicate_submissions") + 1, last_duplicate_at=now
                )
                if existing.status == SyncOperationStatus.PENDING:
                    return _Intake(row_id=existing.pk)
                return _Intake(ack=acknowledgement_for(existing, replayed=True))
            return _Intake(
                ack=_reject_reuse(device, store, envelope, existing, payload_hash, now=now)
            )
        resolved = _resolve(device, store, envelope, raw, now=now)
        status = SyncOperationStatus.PENDING
        outcome = conflict = ""
        if resolved.outcome:
            status, outcome = SyncOperationStatus.REJECTED, resolved.outcome
            conflict = _rejection_case_type(outcome)
            if outcome == "WRONG_EVENT":
                status = SyncOperationStatus.SECURITY_CONFLICT
        elif quarantined:
            status, outcome = SyncOperationStatus.QUARANTINED, "DEVICE_NOT_OPERATIONAL"
            conflict = ReconciliationCaseType.QUARANTINED_DEVICE
        payload = dict(resolved.parsed) if isinstance(resolved.parsed, dict) else {}
        # The note is kept (encrypted) only for an override that names it.
        note = ""
        if isinstance(payload.get("override"), dict) and isinstance(envelope.get("note"), str):
            note = envelope["note"].strip()[:_NOTE_MAX_LENGTH]
        package_claim = payload.get("package") if isinstance(payload.get("package"), dict) else {}
        try:
            with transaction.atomic():
                row = SyncOperation.objects.create(
                    public_id=new_public_id(),
                    operation_id=operation_id,
                    device=device,
                    event_edition_id=device.event_edition_id,
                    store_id=store,
                    device_sequence=envelope["sequence"],
                    operation_type=resolved.operation_type,
                    schema_version=_schema_version(payload),
                    payload_json=payload,
                    payload_hash=payload_hash,
                    signature=envelope["signature"],
                    signing_key=resolved.signature_key,
                    prev_hash=str(payload.get("prev") or "")[:64],
                    chain_hash=sha256_hex(str(payload.get("prev") or "").encode("utf-8") + raw)
                    if resolved.signature_key is not None
                    else "",
                    note_encrypted=note,
                    occurred_at=resolved.occurred_at,
                    received_at=now,
                    package=resolved.package,
                    package_version=getattr(resolved.package, "package_version", None),
                    delta_version=package_claim.get("delta_version")
                    if resolved.package is not None
                    else None,
                    grant=resolved.grant,
                    operator_user_id=getattr(resolved.grant, "user_id", None),
                    gate=resolved.gate,
                    zone=resolved.zone,
                    digital_entry_pass=resolved.credential,
                    registration_id=getattr(resolved.credential, "registration_id", None),
                    status=status,
                    outcome_code=outcome,
                    conflict_type=conflict,
                    flags=resolved.flags,
                    processed_at=None if status == SyncOperationStatus.PENDING else now,
                )
        except IntegrityError:
            # Another operation already holds this (device, store, sequence).
            return _Intake(
                ack=_reject_without_row(
                    device, store, envelope, "SEQUENCE_REUSED", now, payload_hash=payload_hash
                )
            )
        if status in (SyncOperationStatus.REJECTED, SyncOperationStatus.SECURITY_CONFLICT):
            _record_rejection(row, now=now)
            return _Intake(ack=acknowledgement_for(row))
        if status == SyncOperationStatus.QUARANTINED:
            _record_quarantine(row, now=now)
            return _Intake(ack=acknowledgement_for(row))
    return _Intake(row_id=row.pk)


def _resolve(device, store, envelope, raw: bytes, *, now) -> _Resolved:
    """Validate the signed bytes and resolve every reference, in a fixed
    order. The first failure is the REJECTED outcome code."""
    resolved = _Resolved()
    if len(raw) > MAX_OPERATION_BYTES:
        resolved.outcome = "OPERATION_TOO_LARGE"
        return resolved
    try:
        parsed = json.loads(raw)
    except ValueError:
        resolved.outcome = "MALFORMED_OPERATION"
        return resolved
    if not isinstance(parsed, dict):
        resolved.outcome = "MALFORMED_OPERATION"
        return resolved
    if isinstance(parsed.get("type"), str) and parsed["type"] in OPERATION_TYPES:
        resolved.operation_type = parsed["type"]
    try:
        canonical = canonical_json(parsed)
    except (OfflineCryptoError, TypeError, ValueError):  # fmt: skip
        canonical = b""
    if canonical != raw:
        resolved.outcome = "NON_CANONICAL"
        return resolved
    resolved.signature_key = _signature_key(raw, envelope["signature"], _device_sign_keys(device))
    if resolved.signature_key is None:
        resolved.outcome = "INVALID_SIGNATURE"
        return resolved
    try:
        validate_operation(parsed)
    except (OfflineContractError, TypeError, ValueError):  # fmt: skip
        schema = parsed.get("schema_version")
        kind = parsed.get("type")
        unsupported = (
            type(schema) is not int
            or schema != 1
            or not isinstance(kind, str)
            or kind not in OPERATION_TYPES
        )
        resolved.outcome = "UNSUPPORTED_OPERATION" if unsupported else "MALFORMED_OPERATION"
        return resolved
    # Only the validated, minimal contract may enter JSONB or a supervisor
    # case. Rejected arbitrary input retains its digest, never its fields.
    resolved.parsed = parsed
    if (
        parsed["operation_id"] != envelope["operation_id"]
        or parsed["sequence"] != envelope["sequence"]
        or parsed["store"] != store
    ):
        resolved.outcome = "ENVELOPE_MISMATCH"
        return resolved
    if parsed["device"] != device.public_id:
        resolved.outcome = "WRONG_DEVICE"
        return resolved
    if parsed["event"] != device.event_edition.code:
        resolved.outcome = "WRONG_EVENT_BINDING"
        return resolved
    resolved.occurred_at = _from_epoch(parsed["occurred_at"])
    package = (
        OfflinePackage.objects.select_related("scope", "scope__gate", "scope__gate__venue")
        .filter(
            device=device,
            public_id=parsed["package"]["id"],
            package_version=parsed["package"]["version"],
        )
        .first()
    )
    if package is None:
        resolved.outcome = "UNKNOWN_PACKAGE"
        return resolved
    resolved.package = package
    resolved.gate = package.scope.gate
    if parsed["package"]["data_cutoff_at"] != _epoch(package.data_cutoff_at):
        resolved.outcome = "PACKAGE_MISMATCH"
        return resolved
    delta_version = parsed["package"]["delta_version"]
    critical = parsed["package"]["critical_delta_cutoff_at"]
    if delta_version is None:
        if critical != _epoch(package.data_cutoff_at):
            resolved.outcome = "PACKAGE_MISMATCH"
            return resolved
    else:
        delta = OfflineCriticalDelta.objects.filter(
            package=package, delta_version=delta_version
        ).first()
        if delta is None or _epoch(delta.critical_delta_cutoff_at) != critical:
            resolved.outcome = "UNKNOWN_DELTA"
            return resolved
    if parsed["gate"] != package.scope.gate.code:
        resolved.outcome = "SCOPE_MISMATCH"
        return resolved
    if parsed["grant"] is not None:
        grant = (
            OfflineOperatorGrant.objects.select_related(
                "device_session", "device_session__zone", "operator_session", "user"
            )
            .filter(device=device, public_id=parsed["grant"])
            .first()
        )
        if grant is None:
            resolved.outcome = "UNKNOWN_GRANT"
            return resolved
        session = grant.device_session
        if session.scope_id != package.scope_id or parsed["zone"] != session.zone.code:
            resolved.outcome = "SCOPE_MISMATCH"
            return resolved
        resolved.grant = grant
        resolved.zone = session.zone
    elif parsed["zone"] is not None:
        zone = package.scope.permitted_zones.filter(code=parsed["zone"]).first()
        if zone is None:
            resolved.outcome = "SCOPE_MISMATCH"
            return resolved
        resolved.zone = zone
    if parsed["credential"] is not None:
        from apps.badges.models import DigitalEntryPass

        credential = (
            DigitalEntryPass.objects.select_related("registration", "event_edition")
            .filter(jti=parsed["credential"]["jti"])
            .first()
        )
        matches = (
            credential is not None and credential.signing_key_id == (parsed["credential"]["kid"])
        )
        if credential is not None and credential.event_edition_id != device.event_edition_id:
            # Retain the submitted opaque claim, never a foreign registration,
            # person or pass reference in this event's reconciliation queue.
            resolved.outcome = "WRONG_EVENT"
            return resolved
        if parsed["type"] == OPERATION_TYPE_DECISION:
            if credential is None:
                resolved.outcome = "UNKNOWN_CREDENTIAL"
                return resolved
            if not matches:
                resolved.outcome = "CREDENTIAL_MISMATCH"
                return resolved
        resolved.credential = credential if matches else None
    resolved.flags = _flags(device, store, parsed, package, resolved.occurred_at, now=now)
    return resolved


def _flags(device, store, parsed, package, occurred_at, *, now) -> list[str]:
    """Sequence, chain and clock anomalies (recorded, never corrected)."""
    tolerance = timedelta(seconds=settings.ENTRY_OFFLINE_CLOCK_TOLERANCE_SECONDS)
    flags = []
    sequence = parsed["sequence"]
    previous = SyncOperation.objects.filter(
        device=device, store_id=store, device_sequence=sequence - 1
    ).first()
    if previous is not None:
        if previous.chain_hash and previous.chain_hash != parsed["prev"]:
            flags.append(ReconciliationCaseType.CHAIN_BREAK)
        if previous.occurred_at is not None and occurred_at < previous.occurred_at - tolerance:
            flags.append(ReconciliationCaseType.CLOCK_ANOMALY)
    elif sequence == 1:
        if parsed["prev"] != ZERO_HASH:
            flags.append(ReconciliationCaseType.CHAIN_BREAK)
    else:
        flags.append(ReconciliationCaseType.SEQUENCE_GAP)
    if (
        occurred_at > now + tolerance or occurred_at < package.issued_at - tolerance
    ) and ReconciliationCaseType.CLOCK_ANOMALY not in flags:
        flags.append(ReconciliationCaseType.CLOCK_ANOMALY)
    return [str(flag) for flag in flags]


def _gate_for(device):
    from apps.entry.services.devices import current_scope

    scope = current_scope(device)
    if scope is not None:
        return scope.gate
    latest = device.scopes.order_by("-scope_version").select_related("gate").first()
    return latest.gate if latest is not None else None


def _reject_without_row(device, store, envelope, outcome, now, *, payload_hash=None):
    """Evidence that cannot have its own row still leaves a case (deduplicated
    by content digest) and a durable REJECTED acknowledgement."""
    op_text = envelope.get("op") if isinstance(envelope, dict) else None
    # Malformed Unicode still has stable digest evidence, never stored text.
    raw = op_text.encode("utf-8", errors="surrogatepass") if isinstance(op_text, str) else b""
    payload_hash = payload_hash or sha256_hex(raw)
    case_type = (
        ReconciliationCaseType.CHAIN_BREAK
        if outcome == "SEQUENCE_REUSED"
        else ReconciliationCaseType.UNSUPPORTED_OPERATION
    )
    with transaction.atomic():
        _lock(_LOCK_CLASS_DEVICE, str(device.pk))
        case = ReconciliationCase.objects.filter(
            device=device,
            case_type=case_type,
            sync_operation__isnull=True,
            device_known_state__payload_hash=payload_hash,
        ).first()
        if case is None:
            gate = _gate_for(device)
            if gate is None:
                raise OfflineRequestRejected("No checkpoint scope.", code="MALFORMED_BATCH")
            sequence = envelope.get("sequence") if isinstance(envelope, dict) else None
            case = open_case(
                case_type=case_type,
                device=device,
                gate=gate,
                device_known_state={
                    "payload_hash": payload_hash,
                    "size": len(raw),
                    "store": store,
                    "sequence": sequence
                    if isinstance(sequence, int) and not isinstance(sequence, bool)
                    else None,
                },
                server_known_state={"outcome": outcome},
                operational_fact={"note": "EVIDENCE_WITHOUT_ROW"},
                now=now,
            )
            audit(
                action_code=action_codes.OFFLINE_OPERATION_REJECTED,
                target_type="EntryDevice",
                target_uuid=device.pk,
                event_edition_id=device.event_edition_id,
                result="DENIED",
                reason_code=outcome,
                actor_type="SYSTEM",
                after_summary={"device": device.public_id, "case": case.public_id},
            )
    return _rowless_rejection(
        device=device,
        store=store,
        envelope=envelope,
        payload_hash=payload_hash,
        outcome=outcome,
        case=case,
    )


def _reject_reuse(device, store, envelope, existing, payload_hash, *, now):
    """The id is already bound to OTHER content (or another device): the
    original row is untouched; the conflicting submission is kept in a case."""
    gate = _gate_for(device)
    same_scope = existing.device_id == device.pk and existing.gate_id == getattr(gate, "pk", None)
    linked = existing if same_scope else None
    case = ReconciliationCase.objects.filter(
        device=device,
        sync_operation=linked,
        case_type=ReconciliationCaseType.OPERATION_ID_REUSED,
        device_known_state__payload_hash=payload_hash,
        device_known_state__operation_id=envelope["operation_id"],
    ).first()
    if case is None:
        case = open_case(
            case_type=ReconciliationCaseType.OPERATION_ID_REUSED,
            device=device,
            gate=gate,
            sync_operation=linked,
            device_known_state={
                "operation_id": envelope["operation_id"],
                "payload_hash": payload_hash,
                "sequence": envelope["sequence"],
                "store": store,
            },
            server_known_state={
                "original_payload_hash": existing.payload_hash,
                "original_device": existing.device.public_id,
                "original_status": existing.status,
            }
            if same_scope
            else {"outcome": "OPERATION_ID_REUSED"},
            operational_fact={"note": "OPERATION_ID_REUSED"},
            now=now,
        )
        audit(
            action_code=action_codes.OFFLINE_OPERATION_REJECTED,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            result="DENIED",
            reason_code="OPERATION_ID_REUSED",
            actor_type="SYSTEM",
            after_summary={"device": device.public_id, "case": case.public_id},
        )
    return _rowless_rejection(
        device=device,
        store=store,
        envelope=envelope,
        payload_hash=payload_hash,
        outcome="OPERATION_ID_REUSED",
        case=case,
    )


def _device_state(row: SyncOperation) -> dict:
    payload = row.payload_json or {}
    return {
        "local": payload.get("local"),
        "state": payload.get("state"),
        "band": (payload.get("package") or {}).get("band"),
        "package_version": row.package_version,
        "delta_version": row.delta_version,
        "decision": payload.get("decision"),
        "decision_reason": payload.get("decision_reason"),
        "override": (payload.get("override") or {}).get("code")
        if isinstance(payload.get("override"), dict)
        else None,
        "sequence": row.device_sequence,
        "operation": row.public_id,
    }


def _fact(row: SyncOperation) -> dict:
    payload = row.payload_json or {}
    return {
        "decision": payload.get("decision"),
        "occurred_at": _epoch(row.occurred_at),
        "received_at": _epoch(row.received_at),
        "type": row.operation_type,
    }


def _rejection_case_type(outcome: str) -> str:
    """Unsupported or malformed content is its own class; every other
    rejection is a security rejection (signature, binding, reference)."""
    if outcome == "WRONG_EVENT":
        return ReconciliationCaseType.WRONG_EVENT
    if outcome in ("UNSUPPORTED_OPERATION", "MALFORMED_OPERATION", "OPERATION_TOO_LARGE"):
        return ReconciliationCaseType.UNSUPPORTED_OPERATION
    return ReconciliationCaseType.SECURITY_REJECTION


def _record_rejection(row: SyncOperation, *, now) -> None:
    """The case and audit of a REJECTED row (its `conflict_type` was set at
    insert: the row is final and immutable from then on)."""
    gate = row.gate or _gate_for(row.device)
    open_case(
        case_type=row.conflict_type,
        device=row.device,
        gate=gate,
        sync_operation=row,
        device_known_state=_device_state(row),
        server_known_state={"outcome": row.outcome_code},
        operational_fact=_fact(row),
        now=now,
    )
    audit(
        action_code=action_codes.OFFLINE_OPERATION_REJECTED,
        target_type="SyncOperation",
        target_uuid=row.pk,
        event_edition_id=row.event_edition_id,
        result="DENIED",
        reason_code=row.outcome_code,
        actor_type="SYSTEM",
        after_summary={
            "device": row.device.public_id,
            "operation": row.public_id,
            "sequence": row.device_sequence,
            "gate_id": str(gate.pk) if gate is not None else "",
        },
    )


def _record_quarantine(row: SyncOperation, *, now) -> None:
    gate = row.gate or _gate_for(row.device)
    case = open_quarantine_case(device=row.device, gate=gate, sync_operation=row, now=now)
    audit(
        action_code=action_codes.OFFLINE_OPERATION_QUARANTINED,
        target_type="SyncOperation",
        target_uuid=row.pk,
        event_edition_id=row.event_edition_id,
        result="DENIED",
        reason_code="DEVICE_NOT_OPERATIONAL",
        actor_type="SYSTEM",
        after_summary={
            "device": row.device.public_id,
            "device_status": row.device.status,
            "operation": row.public_id,
            "sequence": row.device_sequence,
            "case": case.public_id,
        },
    )


# ---------------------------------------------------------------------------
# Phase 2: processing (evaluate, classify, apply -- one transaction)
# ---------------------------------------------------------------------------


@dataclass
class _Outcome:
    status: str
    outcome: str
    conflict_type: str = ""
    create_event: bool = False
    create_override: bool = False
    case_type: str = ""
    advisory_case: str = ""
    related_event: EntryEvent | None = None
    #: Other admissions of the context kept only in their SyncOperation
    #: (correction 5, D3 S1): linked to the case, never as `related_event`.
    related_operations: list = field(default_factory=list)
    server: dict = field(default_factory=dict)
    evaluation: ServerEvaluation | None = None
    override_reason: EntryOverrideReason | None = None


def process_operation(row_id, *, now=None) -> Acknowledgement:
    """Process one PENDING row to its final outcome. Idempotent: a row that
    is already final returns its original acknowledgement."""
    now = now or timezone.now()
    head = SyncOperation.objects.filter(pk=row_id).values("device_id", "operation_id").first()
    with transaction.atomic():
        _lock_device_and_operation(head["device_id"], head["operation_id"])
        device = EntryDevice.objects.select_for_update().get(pk=head["device_id"])
        row = (
            SyncOperation.objects.select_for_update(of=("self",))
            .select_related(
                "device",
                "device__event_edition",
                "package",
                "grant",
                "grant__device_session",
                "grant__operator_session",
                "grant__user",
                "gate",
                "zone",
                "digital_entry_pass",
            )
            .get(pk=row_id)
        )
        if row.status != SyncOperationStatus.PENDING:
            return acknowledgement_for(row, replayed=True)
        row.device = device
        if device_is_quarantined(device, now=max(now, timezone.now())):
            row.status = SyncOperationStatus.QUARANTINED
            row.outcome_code = "DEVICE_NOT_OPERATIONAL"
            row.conflict_type = ReconciliationCaseType.QUARANTINED_DEVICE
            row.processed_at = now
            row.save(update_fields=["status", "outcome_code", "conflict_type", "processed_at"])
            _record_quarantine(row, now=now)
        elif SyncOperation.objects.filter(
            device_id=row.device_id,
            store_id=row.store_id,
            device_sequence__lt=row.device_sequence,
            status=SyncOperationStatus.PENDING,
        ).exists():
            return acknowledgement_for(row)
        elif row.operation_type == OPERATION_TYPE_ATTEMPT:
            outcome = _Outcome(status=SyncOperationStatus.APPLIED, outcome="ATTEMPT_RECORDED")
            _finalize(row, outcome, registration=None, now=now)
        else:
            from apps.registrations.models import Registration

            registration = (
                Registration.objects.select_for_update(of=("self",))
                .select_related("event_edition", "person")
                .get(pk=row.registration_id)
            )
            outcome = _classify(row, registration, now=now)
            _finalize(row, outcome, registration=registration, now=now)
    row.refresh_from_db()
    return acknowledgement_for(row)


def _knowledge_time(row: SyncOperation):
    critical = (row.payload_json.get("package") or {}).get("critical_delta_cutoff_at")
    base = row.package.data_cutoff_at
    if isinstance(critical, int):
        return max(base, _from_epoch(critical))
    return base


def _security(conflict: str, outcome: str, server: dict) -> _Outcome:
    return _Outcome(
        status=SyncOperationStatus.SECURITY_CONFLICT,
        outcome=outcome,
        conflict_type=conflict,
        case_type=conflict,
        server=server,
    )


def _conflict(conflict: str, outcome: str, server: dict, *, status=None) -> _Outcome:
    return _Outcome(
        status=status or SyncOperationStatus.CONFLICT,
        outcome=outcome,
        conflict_type=conflict,
        create_event=True,
        case_type=conflict,
        server=server,
    )


def _note_digest(operation_id: str, note: str) -> str:
    return hashlib.sha256(f"{operation_id}\n{note}".encode()).hexdigest()


#: Journal conditions under which the uncovered changes can no longer be
#: attributed row by row (`journal_changes` stops early, or cannot name the
#: row), so no catalogue entry can be shown unchanged since the snapshot.
_JOURNAL_UNATTRIBUTABLE = frozenset({"SNAPSHOT_MISSING", "BULK_CHANGE", "UNTRACKED_CHANGE"})

#: What the server can establish about the named reason's entry in the
#: device's package (recorded as `override_catalogue_evidence`).
#: Exact -- the entry is known:
CATALOGUE_UNCHANGED = "UNCHANGED"  # the current row is the package's entry
CATALOGUE_ABSENT = "ABSENT"  # the code was provably not in the package
#: Only state known, membership uncertain (correction 5, D2 option A2):
CATALOGUE_INSERTED_ONLY = "INSERTED_ONLY"
#: Unknown -- nothing about the entry can be shown:
CATALOGUE_CHANGED = "CHANGED"  # updated, renamed, moved or deleted
CATALOGUE_UNATTRIBUTABLE = "UNATTRIBUTABLE"  # retention elapsed, or no attribution


@dataclass(frozen=True)
class _PackageReason:
    row: EntryOverrideReason | None
    evidence: str

    @property
    def only_state_known(self) -> bool:
        """The override can be checked against every state the package could
        have carried for its code."""
        return self.evidence in (CATALOGUE_UNCHANGED, CATALOGUE_ABSENT, CATALOGUE_INSERTED_ONLY)

    @property
    def membership_known(self) -> bool:
        return self.evidence in (CATALOGUE_UNCHANGED, CATALOGUE_ABSENT)


def _package_override_reason(row: SyncOperation, code: str, *, now) -> _PackageReason:
    """The event's override reason `code`, and what the server can establish
    about its entry in the device's package (R-01; correction 5, D2 A2).

    A package carries its event's ACTIVE reasons as its projection query read
    them. That query runs at READ COMMITTED after the watermark snapshot, so
    it reads a state AT LEAST AS NEW as the snapshot: a reason committed in
    between is in the package while its INSERT is still uncovered (probe P8).
    Reason codes are also mutable. The trigger-written journal records, per
    changed row, only the operation kinds -- never previous values.

    * No uncovered change to the named row: the current row is exactly what
      the device was given (UNCHANGED).
    * The named row's uncovered history is INSERT-only, and no uncovered
      UPDATE or DELETE touched ANY reason of the event: its current values
      are the only ones it ever had, and no other row can have carried this
      code in the package. It may or may not be in the package, but an
      override violating its only state is provable (INSERTED_ONLY).
    * No row has the code now, and every uncovered change is such an INSERT:
      a row that carried the code would have needed an uncovered rename,
      move or deletion, so the code was never in the package (ABSENT).
    * Anything else is unknown: an uncovered UPDATE or DELETE (CHANGED), or a
      journal that cannot attribute its changes -- elapsed retention, a
      missing snapshot, a bulk or untracked change (UNATTRIBUTABLE)."""
    from apps.entry.services.offline_journal import journal_changes

    reason = EntryOverrideReason.objects.filter(
        event_edition_id=row.event_edition_id, code=code
    ).first()
    if now - row.package.data_cutoff_at >= timedelta(
        seconds=settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS
    ):
        return _PackageReason(reason, CATALOGUE_UNATTRIBUTABLE)
    changes = journal_changes(row.package)
    operations = changes.override_reason_operations
    if changes.rebuild_reasons & _JOURNAL_UNATTRIBUTABLE or "" in operations:
        return _PackageReason(reason, CATALOGUE_UNATTRIBUTABLE)
    only_insertions = all(kinds == {"I"} for kinds in operations.values())
    if reason is None:
        return _PackageReason(None, CATALOGUE_ABSENT if only_insertions else CATALOGUE_CHANGED)
    if str(reason.pk) not in operations:
        return _PackageReason(reason, CATALOGUE_UNCHANGED)
    return _PackageReason(reason, CATALOGUE_INSERTED_ONLY if only_insertions else CATALOGUE_CHANGED)


def _override_conforms(reason, blockers: set, note: str) -> bool:
    """The override is one this catalogue entry permits."""
    return (
        reason is not None
        and reason.is_active
        and blockers <= set(reason.overridable_reason_codes or [])
        and not (reason.requires_note and not note)
    )


def _historical_access_uncertain(row, registration, evaluation, *, now) -> bool:
    """Never interpret a later relaxation as proof of an earlier admission.

    The journal records changed references, not previous rule values. Once
    relevant mutable facts changed, or the retention window elapsed, a
    supervisor must review an otherwise admittable operation.
    """
    from apps.entry.services.offline_journal import journal_changes

    if now - row.package.data_cutoff_at >= timedelta(
        seconds=settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS
    ):
        return True
    changes = journal_changes(row.package)
    return bool(
        changes.rebuild_reasons
        or str(registration.pk) in changes.assignment_registration_ids
        or str(registration.pk) in changes.restriction_registration_ids
        or str(registration.person_id) in changes.restriction_person_ids
        or str(getattr(evaluation.access_profile, "pk", "")) in changes.profile_ids
        or (
            str(row.digital_entry_pass_id) in changes.pass_ids
            and row.digital_entry_pass.suspended_at is not None
            and row.digital_entry_pass.resumed_at is not None
        )
    )


def _classify(row: SyncOperation, registration, *, now) -> _Outcome:
    """Classify one ENTRY_DECISION against authoritative state at its
    occurrence (Flow §11.8, approved plan §6 mapping table). An override is
    checked against the override catalogue of the device's OWN package
    (`_package_override_reason`), never merely against the current one."""
    op = row.payload_json
    local = op["local"]
    decision = op["decision"]
    override = op["override"]
    admission = decision == EntryDecision.ADMIT
    credential = row.digital_entry_pass
    package = row.package
    grant = row.grant
    at = row.occurred_at
    tolerance = timedelta(seconds=settings.ENTRY_OFFLINE_CLOCK_TOLERANCE_SECONDS)
    knowledge = _knowledge_time(row)
    band = band_at(
        at, aging_at=package.aging_at, stale_at=package.stale_at, expires_at=package.expires_at
    )
    server = {
        "knowledge_at": _epoch(knowledge),
        "occurred_at": _epoch(at),
        "band_at_occurrence": band,
        "package_status": package.status,
        "package_status_changed_at": _epoch(package.status_changed_at),
        "pass_status_now": credential.status,
        "grant_revoked_at": _epoch(grant.revoked_at),
        "grant_expires_at": _epoch(grant.expires_at),
    }
    if (
        credential.event_edition_id != row.event_edition_id
        or registration.event_edition_id != row.event_edition_id
    ):
        return _security(ReconciliationCaseType.WRONG_EVENT, "WRONG_EVENT", server)
    evaluation = evaluate_at(
        registration=registration,
        credential=credential,
        event_edition=row.event_edition,
        gate=row.gate,
        zone=row.zone,
        at=at,
        exclude_operation=row.pk,
    )
    server.update(evaluation.as_json())
    status_at = evaluation.pass_status_at
    server["pass_status_changed_at"] = _epoch(status_changed_at(credential, status_at))
    # Expired data permits only an unresolved referral, never a resolved
    # ENTRY_DECISION, including a denial. Every resolved decision needs a
    # valid grant at its occurrence.
    if band == "EXPIRED":
        return _security(
            ReconciliationCaseType.PACKAGE_EXPIRED,
            "ADMITTED_ON_EXPIRED_DATA" if admission else "DECISION_ON_EXPIRED_DATA",
            server,
        )
    if at >= grant.expires_at or at < grant.issued_at - tolerance:
        return _security(ReconciliationCaseType.POLICY_VIOLATION, "GRANT_NOT_VALID", server)
    if not admission and op["state"] not in ("OFFLINE_ACTIVE", "STALE"):
        return _security(ReconciliationCaseType.POLICY_VIOLATION, "STATE_FORBIDS_DECISION", server)
    if not admission:
        outcome = _Outcome(
            status=SyncOperationStatus.APPLIED,
            outcome="NON_ADMISSION_RECORDED",
            create_event=True,
            server=server,
            evaluation=evaluation,
        )
        return outcome

    # --- Policy: what the device must never have done (no Entry Event) ---
    if band == "STALE":
        return _security(ReconciliationCaseType.PACKAGE_STALE, "ADMITTED_ON_STALE_DATA", server)
    if op["state"] != "OFFLINE_ACTIVE" or op["package"]["band"] not in ("FRESH", "AGING"):
        return _security(ReconciliationCaseType.POLICY_VIOLATION, "STATE_FORBIDS_ADMISSION", server)
    if (
        row.delta_version is not None
        and OfflineCriticalDelta.objects.filter(
            package=package, delta_version=row.delta_version, rebuild_required=True
        ).exists()
    ):
        return _security(ReconciliationCaseType.POLICY_VIOLATION, "DELTA_REQUIRES_REBUILD", server)
    local_admittable = local["result"] in ADMITTABLE_RESULTS
    local_blockers = set(local["blockers"])
    reason_row = None
    catalogue_unknown = False
    if override is None:
        if not local_admittable:
            return _security(
                ReconciliationCaseType.POLICY_VIOLATION, "LOCAL_RESULT_NOT_ADMITTABLE", server
            )
    else:
        note = row.note_encrypted or ""
        # What no package could ever permit, whatever its override catalogue.
        if (
            local_admittable
            or not grant.may_override
            or not local_blockers
            or local_blockers & {str(c) for c in NEVER_OVERRIDEABLE_REASON_CODES}
            or (note and _note_digest(row.operation_id, note) != override["note_digest"])
        ):
            return _security(
                ReconciliationCaseType.POLICY_VIOLATION, "OVERRIDE_NOT_PERMITTED", server
            )
        # What the device's OWN package permitted (R-01): the reason must have
        # been in it -- active -- with these codes and this note rule. An
        # override that violates every state the package could have carried
        # is the device's policy violation (correction 5, D2 A2: that includes
        # the only state of a reason inserted after the snapshot). When the
        # server cannot know whether the package carried the entry, a
        # conforming override is reconciled as a changed rule below: never
        # applied as a clean admission, never a false accusation.
        packaged = _package_override_reason(row, override["code"], now=now)
        reason_row = packaged.row
        server["override_catalogue_evidence"] = packaged.evidence
        if packaged.only_state_known and not _override_conforms(reason_row, local_blockers, note):
            return _security(
                ReconciliationCaseType.POLICY_VIOLATION, "OVERRIDE_NOT_PERMITTED", server
            )
        catalogue_unknown = not packaged.membership_known
        if evaluation.restriction_blocks_override:
            return _security(
                ReconciliationCaseType.NON_OVERRIDEABLE_RESTRICTION,
                "NON_OVERRIDEABLE_RESTRICTION",
                server,
            )
        if catalogue_unknown:
            server["override_catalogue_uncertain"] = True

    # --- Authority withdrawn while the device was disconnected ---
    outcome = None
    from apps.badges.models import VerificationKey, VerificationKeyStatus

    key = VerificationKey.objects.filter(key_id=credential.signing_key_id).first()
    key_unusable = (
        key is None
        or key.status == VerificationKeyStatus.PENDING
        or (
            key.status == VerificationKeyStatus.REVOKED
            and (key.revoked_at is None or at_or_before(key.revoked_at, at))
        )
        or (key.not_before is not None and at < key.not_before)
        or (key.not_after is not None and at >= key.not_after)
    )
    if key_unusable:
        outcome = _conflict(
            ReconciliationCaseType.PACKAGE_UNUSABLE, "QR_SIGNING_KEY_UNUSABLE", server
        )
    elif package.status == OfflinePackageStatus.REVOKED and (
        at_or_before(package.status_changed_at, at)
    ):
        conflict = (
            ReconciliationCaseType.WRONG_SCOPE
            if package.status_reason_code == "SCOPE_CHANGED"
            else ReconciliationCaseType.PACKAGE_UNUSABLE
        )
        outcome = _conflict(conflict, "PACKAGE_WITHDRAWN_BEFORE_ADMISSION", server)
    elif at_or_before(grant.revoked_at, at):
        outcome = _conflict(
            ReconciliationCaseType.AUTHORIZATION_WITHDRAWN, "GRANT_REVOKED_BEFORE_ADMISSION", server
        )
    # --- The pass itself, as it stood at the admission ---
    elif status_at == "REPLACED":
        outcome = _conflict(
            ReconciliationCaseType.PASS_REPLACED,
            "PASS_REPLACED_BEFORE_ADMISSION",
            server,
            status=SyncOperationStatus.RECONCILIATION_REQUIRED,
        )
    elif status_at == "REVOKED":
        outcome = _conflict(
            ReconciliationCaseType.PASS_REVOKED, "PASS_REVOKED_BEFORE_ADMISSION", server
        )
    elif status_at == "SUSPENDED":
        outcome = _conflict(
            ReconciliationCaseType.PASS_SUSPENDED, "PASS_SUSPENDED_BEFORE_ADMISSION", server
        )
    if outcome is None:
        codes = set(evaluation.blocker_codes) - {EntryReasonCode.ALREADY_ADMITTED}
        if override is not None:
            # What the override could cover: its reason's codes -- or, when
            # that catalogue entry is unknown, no more than the device overrode.
            codes -= (
                local_blockers
                if catalogue_unknown
                else set(reason_row.overridable_reason_codes or [])
            )
        if codes & RESTRICTION_REASONS:
            outcome = _conflict(
                ReconciliationCaseType.RESTRICTION_ADDED, "RESTRICTION_AT_ADMISSION", server
            )
        elif codes:
            outcome = _conflict(
                ReconciliationCaseType.ACCESS_CHANGED, "ACCESS_CHANGED_AT_ADMISSION", server
            )
    # Every other admission of this context: its Entry Events, and the
    # admissions kept only in their synchronized operation (correction 5, D3
    # S1). The latter are linked to the case, never shown as an Entry Event.
    others = list(
        EntryEvent.objects.filter(
            registration=registration,
            event_type=EntryEventType.ENTRY,
            decision=EntryDecision.ADMIT,
        ).order_by("occurred_at")
    )
    unlinked = list(unlinked_admissions_for(registration, exclude=row.pk).order_by("occurred_at"))
    nearest = min(others, key=lambda e: abs((e.occurred_at - at).total_seconds()), default=None)
    if outcome is None and (others or unlinked) and evaluation.reentry_policy == "SINGLE_ENTRY":
        outcome = _conflict(ReconciliationCaseType.DUPLICATE_ENTRY, "SINGLE_ENTRY_EXCEEDED", server)
        outcome.related_event = nearest
        outcome.related_operations = unlinked
    if outcome is None and (
        catalogue_unknown or _historical_access_uncertain(row, registration, evaluation, now=now)
    ):
        server["historical_access_uncertain"] = True
        outcome = _conflict(
            ReconciliationCaseType.ACCESS_CHANGED, "HISTORICAL_ACCESS_UNCERTAIN", server
        )
    if outcome is None:
        outcome = _Outcome(
            status=SyncOperationStatus.APPLIED, outcome="ADMISSION_APPLIED", create_event=True
        )
        window = settings.ENTRY_RECENT_REENTRY_SECONDS

        def _close(admissions):
            return [
                other
                for other in admissions
                if other.device_id != row.device_id
                and abs((other.occurred_at - at).total_seconds()) <= window
            ]

        close, close_unlinked = _close(others), _close(unlinked)
        if close or close_unlinked:
            outcome.advisory_case = ReconciliationCaseType.DUPLICATE_ADMISSION_ADVISORY
            outcome.conflict_type = ReconciliationCaseType.DUPLICATE_ADMISSION_ADVISORY
            outcome.related_event = min(
                close, key=lambda e: abs((e.occurred_at - at).total_seconds()), default=None
            )
            outcome.related_operations = close_unlinked
    outcome.server = server
    outcome.evaluation = evaluation
    outcome.create_override = override is not None
    outcome.override_reason = reason_row
    if override is not None and reason_row is None:
        # Reachable only with an unknown catalogue entry (a reason deleted,
        # renamed or moved after the snapshot), so `outcome` is a conflict.
        # An Entry Override must name its catalogue reason, and an admission
        # on a non-admittable result must name its override (Schema §11.5,
        # constraint `entry_event_admit_requires_allowed_or_override`), so no
        # faithful Entry Event can be written without a schema change or an
        # invented reason. This is NOT what the approved mapping (plan §6)
        # prescribes for a changed-rule conflict -- it keeps an Entry Event;
        # it keeps the fact in SyncOperation only for SECURITY_CONFLICT rows.
        # The physical fact stays in the immutable operation and its case,
        # and every later admission decision still counts it as a prior
        # admission (`apps.entry.selectors.admissions`, correction 5, D3 S1).
        # No Entry Event, attendance or report entry is created for it.
        server["override_reason_unavailable"] = True
        outcome.create_event = False
        outcome.create_override = False
    return outcome


def _finalize(row: SyncOperation, outcome: _Outcome, *, registration, now) -> None:
    """Write every consequence of the outcome, then the final status. The
    caller holds the transaction and the locks."""
    op = row.payload_json
    local = op["local"]
    event = None
    override_row = None
    if outcome.create_event:
        grant = row.grant
        evaluation = outcome.evaluation
        if outcome.create_override:
            override_row = EntryOverride.objects.create(
                operation_id=row.operation_id,
                event_edition_id=row.event_edition_id,
                registration=registration,
                digital_entry_pass=row.digital_entry_pass,
                original_result=local["result"],
                original_reason_code=local["reason"],
                reason=outcome.override_reason,
                reason_code=outcome.override_reason.code,
                note_encrypted=row.note_encrypted or "",
                user=grant.user,
                gate=row.gate,
                zone=row.zone,
                device=row.device,
                occurred_at=row.occurred_at,
                offline=True,
                sync_operation=row,
            )
        event = EntryEvent.objects.create(
            operation_id=row.operation_id,
            command_fingerprint=row.payload_hash,
            event_edition_id=row.event_edition_id,
            registration=registration,
            digital_entry_pass=row.digital_entry_pass,
            badge_assignment=getattr(evaluation, "badge_assignment", None),
            gate=row.gate,
            zone=row.zone,
            device=row.device,
            device_session=grant.device_session,
            operator_session=grant.operator_session,
            operator_user=grant.user,
            event_type=EntryEventType.ENTRY,
            verification_method=VerificationMethod.QR,
            result=local["result"],
            reason_code=local["reason"] if local["blockers"] else "",
            advisory_codes=list(local["advisories"]),
            decision=op["decision"],
            decision_reason_code=op["decision_reason"],
            prior_entry_event=getattr(evaluation, "prior_admission", None),
            override=override_row,
            occurred_at=row.occurred_at,
            recorded_at=now,
            offline=True,
            package_version=row.package_version,
            sync_operation=row,
            offline_conflict=outcome.status != SyncOperationStatus.APPLIED,
        )
    row.status = outcome.status
    row.outcome_code = outcome.outcome
    row.conflict_type = outcome.conflict_type
    row.server_evaluation = outcome.server
    row.result_reference = event.pk if event is not None else None
    row.processed_at = now
    row.save(
        update_fields=[
            "status",
            "outcome_code",
            "conflict_type",
            "server_evaluation",
            "result_reference",
            "processed_at",
        ]
    )
    _sample(row)
    case_type = outcome.case_type or outcome.advisory_case
    if case_type:
        case = open_case(
            case_type=case_type,
            device=row.device,
            gate=row.gate,
            sync_operation=row,
            entry_event=event,
            related_entry_event=outcome.related_event,
            registration=registration,
            device_known_state=_device_state(row),
            server_known_state=outcome.server,
            operational_fact=_fact(row),
            now=now,
        )
        # The other admissions kept only in their operation (append-only links).
        for other in outcome.related_operations:
            link_operation(case, other, now=now)
    for flag in row.flags or []:
        open_case(
            case_type=flag,
            device=row.device,
            gate=row.gate,
            sync_operation=row,
            entry_event=event,
            registration=registration,
            device_known_state=_device_state(row),
            server_known_state={"flag": flag, "occurred_at": _epoch(row.occurred_at)},
            operational_fact=_fact(row),
            now=now,
        )
    _audit_outcome(row, event, override_row)


def _sample(row: SyncOperation) -> None:
    """One identifier-free OFFLINE telemetry sample (codes and latency)."""
    if row.zone is None or row.gate is None:
        return
    local = row.payload_json["local"]
    VerificationSample.objects.create(
        occurred_at=row.occurred_at,
        event_edition_id=row.event_edition_id,
        gate=row.gate,
        zone=row.zone,
        device=row.device,
        method=VerificationMethod.QR,
        result=local["result"],
        reason_code=local["reason"][:32],
        credential_code=local["code"][:32],
        latency_ms=int(local["latency_ms"]),
        mode=VerificationMode.OFFLINE,
    )


def _audit_outcome(row: SyncOperation, event, override_row) -> None:
    local = row.payload_json["local"]
    summary = {
        "device": row.device.public_id,
        "operation": row.public_id,
        "sequence": row.device_sequence,
        "method": VerificationMethod.QR,
        "result": local["result"],
        "reason": local["reason"],
        "code": local["code"],
        "decision": row.payload_json.get("decision") or "",
        "status": row.status,
        "outcome": row.outcome_code,
        "conflict_type": row.conflict_type,
        "gate": row.gate.code if row.gate is not None else "",
        "gate_id": str(row.gate_id) if row.gate_id else "",
        "zone": row.zone.code if row.zone is not None else "",
        "offline": True,
        "entry_event": event is not None,
        "override": override_row is not None,
        "package_version": row.package_version,
    }
    if row.operation_type == OPERATION_TYPE_ATTEMPT:
        code = action_codes.OFFLINE_VERIFICATION_ATTEMPT
    elif row.status == SyncOperationStatus.APPLIED:
        code = action_codes.OFFLINE_EVENT_APPLIED
    elif row.status == SyncOperationStatus.SECURITY_CONFLICT:
        code = action_codes.OFFLINE_SECURITY_CONFLICT
    else:
        code = action_codes.OFFLINE_CONFLICT
    audit(
        action_code=code,
        actor=row.operator_user,
        target_type="EntryEvent" if event is not None else "SyncOperation",
        target_uuid=event.pk if event is not None else row.pk,
        event_edition_id=row.event_edition_id,
        result="SUCCESS" if row.status == SyncOperationStatus.APPLIED else "DENIED",
        reason_code=row.outcome_code,
        after_summary=summary,
    )
    if override_row is not None:
        audit(
            action_code=action_codes.ENTRY_OVERRIDE_RECORDED,
            actor=row.operator_user,
            target_type="EntryOverride",
            target_uuid=override_row.pk,
            event_edition_id=row.event_edition_id,
            reason_code=override_row.reason_code,
            after_summary={
                "original_result": override_row.original_result,
                "reason": override_row.reason_code,
                "gate": row.gate.code,
                "zone": row.zone.code,
                "device": row.device.public_id,
                "note_recorded": bool(override_row.note_encrypted),
                "offline": True,
                "operation": row.public_id,
            },
        )
