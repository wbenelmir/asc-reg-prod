"""Synchronization of signed offline operations (Phase 4 Prompt 3, ADR-0024):
intake, idempotency, ordering, acknowledgements, rejection classes, quarantine
and the immutability of the evidence. PostgreSQL; all data is synthetic.

A `SyntheticStore` plays the browser's local operation store: canonical JSON
operations signed with the device's in-memory key, sequenced and hash-chained
exactly as `static/js/entry-offline.js` records them.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.models import (
    EntryEvent,
    ReconciliationCase,
    SyncOperation,
    SyncOperationStatus,
    VerificationSample,
)
from apps.entry.offline_contract import TYP_ACKNOWLEDGEMENT
from apps.entry.services import offline_sync
from apps.entry.services.offline_crypto import jws_verify
from apps.entry.services.offline_devices import OfflineRequestRejected, OfflineUnavailable
from apps.entry.tests import offline_factories

pytestmark = pytest.mark.django_db


def _admit(world, **overrides):
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=world.grant,
        credential=world.credential,
        occurred_at=overrides.pop("occurred_at", timezone.now()),
        **overrides,
    )
    return world.store.next(op)


def _upload(world, *ops, now=None, notes=None, **envelope_kwargs):
    envelopes = [
        world.store.envelope(op, note=(notes or {}).get(op["operation_id"]), **envelope_kwargs)
        for op in ops
    ]
    return offline_factories.sync(world.synthetic, world.store, envelopes, now=now)


def _ack(result, index=0):
    return result.acknowledgements[index]


def _verified_ack(world, ack):
    return jws_verify(
        ack.ack, trusted_keys_der=world.synthetic.trusted_keys_der(), typ=TYP_ACKNOWLEDGEMENT
    )


# ---------------------------------------------------------------------------
# Applied admission, acknowledgement, idempotency
# ---------------------------------------------------------------------------


def test_an_admission_is_applied_once_with_a_signed_durable_ack(offline_world):
    op = _admit(offline_world)
    result = _upload(offline_world, op)
    ack = _ack(result)
    assert ack.status == SyncOperationStatus.APPLIED and ack.durable and not ack.replayed
    payload = _verified_ack(offline_world, ack)
    row = SyncOperation.objects.get(operation_id=op["operation_id"])
    assert payload == {
        "typ": "ASC-OACK",
        "schema_version": 1,
        "device": offline_world.device.public_id,
        "store": offline_world.store.store_id,
        "operation_id": op["operation_id"],
        "sequence": 1,
        "payload_hash": row.payload_hash,
        "status": "APPLIED",
        "outcome": "ADMISSION_APPLIED",
        "conflict_type": "",
        "processed_at": int(row.processed_at.timestamp()),
    }
    event = EntryEvent.objects.get()
    assert event.offline and event.sync_operation_id == row.pk
    assert event.package_version == offline_world.package.package_version
    assert event.operation_id == op["operation_id"] and not event.offline_conflict
    assert event.operator_user == offline_world.operator
    assert event.device_session == offline_world.grant.device_session
    assert row.result_reference == event.pk and row.signing_key is not None
    assert row.chain_hash == offline_world.store.head
    sample = VerificationSample.objects.get()
    assert sample.mode == "OFFLINE" and sample.latency_ms == 12
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_EVENT_APPLIED)
    assert audit.after_summary["gate_id"] == str(offline_world.package.scope.gate_id)
    assert "display_name" not in json.dumps(audit.after_summary)
    offline_world.device.refresh_from_db()
    assert offline_world.device.last_sync_at is not None
    assert not ReconciliationCase.objects.exists()


def test_a_duplicate_upload_returns_the_same_outcome_and_no_second_event(offline_world):
    op = _admit(offline_world)
    first = _ack(_upload(offline_world, op))
    again = _ack(_upload(offline_world, op))
    assert again.replayed and again.durable
    assert (again.status, again.outcome, again.conflict_type) == (
        first.status,
        first.outcome,
        first.conflict_type,
    )
    # Server committed, acknowledgement lost: the retry returns the SAME
    # durable outcome (the signed payloads agree field by field).
    assert _verified_ack(offline_world, again) == _verified_ack(offline_world, first)
    assert EntryEvent.objects.count() == 1
    assert SyncOperation.objects.get().duplicate_submissions == 1


def test_an_operation_id_reused_with_other_content_is_rejected_and_kept(offline_world):
    op = _admit(offline_world)
    _upload(offline_world, op)
    forged = {**op, "decision": "DO_NOT_ADMIT", "decision_reason": "FOLLOWS_RESULT"}
    forged["sequence"] = 2
    ack = _ack(_upload(offline_world, forged))
    assert ack.status == "REJECTED" and ack.outcome == "OPERATION_ID_REUSED" and ack.durable
    original = SyncOperation.objects.get()
    assert original.status == "APPLIED" and original.payload_json["decision"] == "ADMIT"
    case = ReconciliationCase.objects.get(case_type="OPERATION_ID_REUSED")
    assert case.sync_operation == original
    assert EntryEvent.objects.count() == 1
    # Resubmitting the reused id again is idempotent too (one case).
    _upload(offline_world, forged)
    assert ReconciliationCase.objects.filter(case_type="OPERATION_ID_REUSED").count() == 1


def test_a_batch_stops_at_the_first_operation_without_a_durable_outcome(offline_world, monkeypatch):
    first, second, third = (
        _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT")
        for _ in range(3)
    )
    original = offline_sync._classify

    def failing(row, registration, *, now):
        if row.device_sequence == 2:
            raise RuntimeError("synthetic processing failure")
        return original(row, registration, now=now)

    monkeypatch.setattr(offline_sync, "_classify", failing)
    result = _upload(offline_world, first, second, third)
    statuses = [(ack.status, ack.durable) for ack in result.acknowledgements]
    assert statuses == [("APPLIED", True), ("PENDING", False), ("NOT_PROCESSED", False)]
    # The durable PENDING row keeps the evidence; the third was never stored,
    # so it can never be applied before its predecessor.
    assert SyncOperation.objects.get(device_sequence=2).status == "PENDING"
    assert not SyncOperation.objects.filter(device_sequence=3).exists()
    monkeypatch.setattr(offline_sync, "_classify", original)
    retry = _upload(offline_world, second, third)
    assert [ack.status for ack in retry.acknowledgements] == ["APPLIED", "APPLIED"]
    events = list(EntryEvent.objects.order_by("recorded_at").values_list("operation_id", flat=True))
    assert events == [first["operation_id"], second["operation_id"], third["operation_id"]]


def test_a_non_admission_is_applied_with_an_entry_event(offline_world):
    op = _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="IDENTITY_MISMATCH")
    assert _ack(_upload(offline_world, op)).status == "APPLIED"
    event = EntryEvent.objects.get()
    assert event.decision == "DO_NOT_ADMIT" and event.decision_reason_code == "IDENTITY_MISMATCH"
    assert event.offline and not event.offline_conflict


def test_a_verification_attempt_is_applied_without_an_entry_event(offline_world):
    op = offline_world.store.next(
        offline_factories.base_operation(
            device=offline_world.device,
            package=offline_world.package,
            grant=offline_world.grant,
            op_type="VERIFICATION_ATTEMPT",
            credential=None,
            decision=None,
            local={
                "code": "INVALID_SIGNATURE",
                "result": "DENIED",
                "reason": "INVALID_CREDENTIAL",
                "blockers": [],
                "advisories": [],
                "latency_ms": 4,
            },
        )
    )
    ack = _ack(_upload(offline_world, op))
    assert ack.status == "APPLIED" and ack.outcome == "ATTEMPT_RECORDED"
    assert not EntryEvent.objects.exists()
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_VERIFICATION_ATTEMPT)
    assert audit.after_summary["result"] == "DENIED"
    assert audit.after_summary["code"] == "INVALID_SIGNATURE"
    assert VerificationSample.objects.get().credential_code == "INVALID_SIGNATURE"


# ---------------------------------------------------------------------------
# Rejections: never applied, always kept with a case
# ---------------------------------------------------------------------------


def _rejected(world, op, **kwargs):
    ack = _ack(_upload(world, op, **kwargs))
    assert ack.status == "REJECTED" and ack.durable
    assert not EntryEvent.objects.exists()
    return ack


def test_an_invalid_operation_signature_is_rejected(offline_world):
    from cryptography.hazmat.primitives.asymmetric import ec

    other = ec.generate_private_key(ec.SECP256R1())
    ack = _rejected(offline_world, _admit(offline_world), key=other)
    assert ack.outcome == "INVALID_SIGNATURE"
    row = SyncOperation.objects.get()
    assert row.signing_key is None and row.conflict_type == "SECURITY_REJECTION"
    assert ReconciliationCase.objects.get().case_type == "SECURITY_REJECTION"
    assert row.payload_json == {}


@pytest.mark.parametrize("field", ["email", "nin", "raw_qr", "free_text"])
def test_rejected_unknown_fields_leave_only_a_digest(offline_world, field):
    op = _admit(offline_world)
    marker = "synthetic-prohibited-field-value"
    op[field] = marker
    assert _rejected(offline_world, op).outcome == "MALFORMED_OPERATION"
    row = SyncOperation.objects.get()
    assert row.payload_json == {} and len(row.payload_hash) == 64
    case = ReconciliationCase.objects.get()
    assert marker not in json.dumps(case.device_known_state)
    assert marker not in json.dumps(case.server_known_state)


@pytest.mark.parametrize(
    ("field", "value"),
    [("type", []), ("state", {}), ("schema_version", True), ("occurred_at", 2**40)],
)
def test_invalid_types_and_unrepresentable_times_are_durable_rejections(
    offline_world, field, value
):
    op = _admit(offline_world)
    op[field] = value
    assert _rejected(offline_world, op).durable
    assert SyncOperation.objects.get().payload_json == {}


def test_known_pending_predecessor_prevents_later_application(offline_world, monkeypatch):
    original = offline_sync.process_operation

    def interrupted(*args, **kwargs):
        raise RuntimeError("Synthetic interruption")

    first, second = _admit(offline_world), _admit(offline_world)
    monkeypatch.setattr(offline_sync, "process_operation", interrupted)
    assert _ack(_upload(offline_world, first)).status == "PENDING"
    monkeypatch.setattr(offline_sync, "process_operation", original)
    assert _ack(_upload(offline_world, second)).status == "PENDING"
    assert not EntryEvent.objects.exists()
    assert _ack(_upload(offline_world, first)).durable
    assert _ack(_upload(offline_world, second)).durable
    assert EntryEvent.objects.count() == 2


def test_device_suspended_after_intake_is_quarantined(offline_world, device_admin):
    from apps.entry.services.devices import suspend_device

    op = _admit(offline_world)
    intake = offline_sync._intake(
        offline_world.device,
        offline_world.store.store_id,
        offline_world.store.envelope(op),
        quarantined=False,
        now=timezone.now(),
    )
    device = offline_world.device
    device.refresh_from_db()
    suspend_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    ack = offline_sync.process_operation(intake.row_id)
    assert ack.status == "QUARANTINED" and ack.durable
    assert not EntryEvent.objects.exists()
    assert ReconciliationCase.objects.get().case_type == "QUARANTINED_DEVICE"


def test_a_reversed_batch_is_refused_before_any_intake(offline_world):
    first, second = _admit(offline_world), _admit(offline_world)
    with pytest.raises(OfflineRequestRejected) as info:
        _upload(offline_world, second, first)
    assert info.value.code == "UNORDERED_BATCH"
    assert not SyncOperation.objects.exists()


def test_cross_event_id_reuse_never_links_foreign_evidence(offline_world, other_event):
    from apps.entry.tests import factories

    op = _admit(offline_world)
    _upload(offline_world, op)
    layout = factories.VenueLayout(other_event, code="OTHER")
    admin = factories.make_user(
        "other.device.admin@example.test",
        group_name="Entry Device Administrators",
        event=other_event,
    )
    device, _secret = factories.enroll_device(event=other_event, layout=layout, admin=admin)
    synthetic = offline_factories.SyntheticDevice(device=device)
    store = offline_factories.SyntheticStore(synthetic)
    result = offline_sync._intake(
        device,
        store.store_id,
        store.envelope(op),
        quarantined=False,
        now=timezone.now(),
    )
    assert result.ack.durable and result.ack.outcome == "OPERATION_ID_REUSED"
    case = ReconciliationCase.objects.get(event_edition=other_event)
    assert case.sync_operation_id is None and case.registration_id is None
    assert not case.links.exists()
    assert case.server_known_state == {"outcome": "OPERATION_ID_REUSED"}
    assert EntryEvent.objects.count() == 1


def test_arbitrary_signature_text_is_not_retained(offline_world):
    envelope = offline_world.store.envelope(_admit(offline_world))
    envelope["signature"] = "synthetic-prohibited-value"
    result = offline_factories.sync(offline_world.synthetic, offline_world.store, [envelope])
    assert _ack(result).durable and _ack(result).outcome == "MALFORMED_ENVELOPE"
    assert not SyncOperation.objects.exists()
    assert "synthetic-prohibited-value" not in json.dumps(
        ReconciliationCase.objects.get().device_known_state
    )


def test_non_canonical_bytes_are_rejected(offline_world):
    op = _admit(offline_world)
    raw = json.dumps(op, indent=1).encode("utf-8")
    assert _rejected(offline_world, op, raw=raw).outcome == "NON_CANONICAL"


def test_an_unsupported_operation_type_is_rejected(offline_world):
    op = _admit(offline_world)
    op["type"] = "EXIT_DECISION"
    ack = _rejected(offline_world, op)
    assert ack.outcome == "UNSUPPORTED_OPERATION"
    assert SyncOperation.objects.get().operation_type == "UNSUPPORTED"
    assert ReconciliationCase.objects.get().case_type == "UNSUPPORTED_OPERATION"


def test_an_unsupported_schema_version_is_rejected(offline_world):
    op = _admit(offline_world)
    op["schema_version"] = 2
    assert _rejected(offline_world, op).outcome == "UNSUPPORTED_OPERATION"


def test_a_malformed_envelope_is_rejected_without_a_row_but_with_a_case(offline_world):
    op = _admit(offline_world)
    envelope = offline_world.store.envelope(op)
    envelope["operation_id"] = "not-a-client-id"
    result = offline_factories.sync(offline_world.synthetic, offline_world.store, [envelope])
    ack = _ack(result)
    assert ack.status == "REJECTED" and ack.outcome == "MALFORMED_ENVELOPE" and ack.durable
    assert not SyncOperation.objects.exists()
    case = ReconciliationCase.objects.get()
    assert case.case_type == "UNSUPPORTED_OPERATION" and case.sync_operation is None


@pytest.mark.parametrize(
    ("mutate", "outcome"),
    [
        (lambda op: op.update(device="A" * 22), "WRONG_DEVICE"),
        (lambda op: op.update(event="OTHEREVENT"), "WRONG_EVENT_BINDING"),
        (lambda op: op["package"].update(id="B" * 22), "UNKNOWN_PACKAGE"),
        (
            lambda op: op["package"].update(data_cutoff_at=op["package"]["data_cutoff_at"] - 1),
            "PACKAGE_MISMATCH",
        ),
        (lambda op: op["package"].update(delta_version=9), "UNKNOWN_DELTA"),
        (lambda op: op.update(gate="GB"), "SCOPE_MISMATCH"),
        (lambda op: op.update(zone="HALL"), "SCOPE_MISMATCH"),
        (lambda op: op.update(grant="C" * 22), "UNKNOWN_GRANT"),
        (lambda op: op["credential"].update(jti="D" * 22), "UNKNOWN_CREDENTIAL"),
        (lambda op: op["credential"].update(kid="v9"), "CREDENTIAL_MISMATCH"),
    ],
)
def test_binding_and_reference_failures_are_rejected(offline_world, mutate, outcome):
    op = offline_factories.base_operation(
        device=offline_world.device,
        package=offline_world.package,
        grant=offline_world.grant,
        credential=offline_world.credential,
    )
    mutate(op)
    op = offline_world.store.next(op)
    ack = _rejected(offline_world, op)
    assert ack.outcome == outcome
    assert ReconciliationCase.objects.get().case_type == "SECURITY_REJECTION"


def test_a_decision_without_a_grant_is_malformed(offline_world):
    op = _admit(offline_world)
    op["grant"] = None
    assert _rejected(offline_world, op).outcome == "MALFORMED_OPERATION"


def test_a_reused_sequence_is_rejected_and_kept_in_a_case(offline_world):
    first = _admit(offline_world)
    _upload(offline_world, first)
    other = {**_admit(offline_world), "sequence": 1}
    ack = _ack(_upload(offline_world, other))
    assert ack.status == "REJECTED" and ack.outcome == "SEQUENCE_REUSED"
    assert SyncOperation.objects.count() == 1
    assert ReconciliationCase.objects.get().case_type == "CHAIN_BREAK"


# ---------------------------------------------------------------------------
# Batch-level refusal, proof, budget
# ---------------------------------------------------------------------------


def test_a_batch_without_a_valid_proof_stores_nothing(offline_world):
    op = _admit(offline_world)
    payload = {
        "store": offline_world.store.store_id,
        "operations": [offline_world.store.envelope(op)],
    }
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(OfflineRequestRejected):
        offline_sync.synchronize(
            device=offline_world.device, payload=payload, nonce=None, signature=None, body=body
        )
    assert not SyncOperation.objects.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"store": "short", "operations": []},
        {"store": "A" * 22, "operations": []},
        {"store": "A" * 22, "operations": [{}] * 21},
        {"store": "A" * 22, "operations": "x"},
    ],
)
def test_a_malformed_batch_is_refused(offline_world, payload):
    body, nonce, signature = offline_world.synthetic.signed("sync", payload)
    with pytest.raises(OfflineRequestRejected) as info:
        offline_sync.synchronize(
            device=offline_world.device,
            payload=json.loads(body),
            nonce=nonce,
            signature=signature,
            body=body,
        )
    assert info.value.code == "MALFORMED_BATCH"
    assert not SyncOperation.objects.exists()


def test_the_upload_budget_is_enforced(offline_world, settings):
    settings.ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW = 1
    _upload(offline_world, _admit(offline_world))
    with pytest.raises(OfflineUnavailable) as info:
        _upload(offline_world, _admit(offline_world))
    assert info.value.code == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# Sequence, chain and clock anomalies: recorded, never corrected
# ---------------------------------------------------------------------------


def test_a_sequence_gap_is_flagged_and_the_operation_still_processed(offline_world):
    _admit(offline_world)  # sequence 1 never uploaded (lost on the device)
    second = _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT")
    assert _ack(_upload(offline_world, second)).status == "APPLIED"
    assert SyncOperation.objects.get().flags == ["SEQUENCE_GAP"]
    assert ReconciliationCase.objects.get().case_type == "SEQUENCE_GAP"


def test_a_broken_chain_is_flagged(offline_world):
    first = _admit(offline_world)
    _upload(offline_world, first)
    offline_world.store.head = "f" * 64  # the device's chain no longer matches
    second = _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT")
    _upload(offline_world, second)
    assert SyncOperation.objects.get(device_sequence=2).flags == ["CHAIN_BREAK"]
    assert ReconciliationCase.objects.filter(case_type="CHAIN_BREAK").exists()


def test_a_future_dated_operation_is_a_clock_anomaly(offline_world):
    op = _admit(
        offline_world,
        decision="DO_NOT_ADMIT",
        decision_reason="FOLLOWS_RESULT",
        occurred_at=timezone.now() + timedelta(hours=1),
    )
    _upload(offline_world, op)
    row = SyncOperation.objects.get()
    assert row.flags == ["CLOCK_ANOMALY"] and row.occurred_at > timezone.now()  # never re-dated


# ---------------------------------------------------------------------------
# Quarantine (binding decision P2-F)
# ---------------------------------------------------------------------------


def test_uploads_from_a_suspended_device_are_quarantined_never_applied(offline_world, device_admin):
    from apps.entry.services.devices import suspend_device

    first = _admit(offline_world)
    second = _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT")
    device = offline_world.device
    device.refresh_from_db()
    suspend_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    result = _upload(offline_world, first, second)
    assert [ack.status for ack in result.acknowledgements] == ["QUARANTINED", "QUARANTINED"]
    assert all(ack.durable for ack in result.acknowledgements)
    assert not EntryEvent.objects.exists()
    case = ReconciliationCase.objects.get(case_type="QUARANTINED_DEVICE")
    assert case.links.count() == 2  # one open case per device, extended per upload


def test_a_revoked_device_uploads_through_the_quarantine_boundary(offline_world, device_admin):
    from apps.entry.services.devices import revoke_device

    op = _admit(offline_world)
    device = offline_world.device
    device.refresh_from_db()
    revoke_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    payload = {
        "device": device.public_id,
        "store": offline_world.store.store_id,
        "operations": [offline_world.store.envelope(op)],
    }
    result = offline_sync.synchronize_quarantine(payload=payload)
    assert _ack(result).status == "QUARANTINED" and _ack(result).durable
    assert not EntryEvent.objects.exists()
    # Unsigned (or foreign-signed) operations are unattributable: refused.
    from cryptography.hazmat.primitives.asymmetric import ec

    forged = offline_world.store.envelope(
        _admit(offline_world), key=ec.generate_private_key(ec.SECP256R1())
    )
    with pytest.raises(OfflineRequestRejected):
        offline_sync.synchronize_quarantine(payload={**payload, "operations": [forged]})
    assert SyncOperation.objects.count() == 1


def test_the_quarantine_boundary_refuses_a_device_that_is_not_revoked(offline_world):
    payload = {
        "device": offline_world.device.public_id,
        "store": offline_world.store.store_id,
        "operations": [offline_world.store.envelope(_admit(offline_world))],
    }
    with pytest.raises(OfflineRequestRejected):
        offline_sync.synchronize_quarantine(payload=payload)


# ---------------------------------------------------------------------------
# Unauthenticated quarantine traffic is bounded (Prompt 3 correction 4, R-02)
# ---------------------------------------------------------------------------


def _revoke(world, device_admin):
    from apps.entry.services.devices import revoke_device

    device = world.device
    device.refresh_from_db()
    revoke_device(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    device.refresh_from_db()
    return device


def _quarantine_payload(world, envelopes):
    return {
        "device": world.device.public_id,
        "store": world.store.store_id,
        "operations": envelopes,
    }


def _forged(world, count=1):
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())  # never registered to the device
    return [world.store.envelope(_admit(world), key=key) for _ in range(count)]


def _refusals(device):
    return list(
        AuditEvent.objects.filter(
            target_type="EntryDevice",
            target_uuid=device.pk,
            action_code=action_codes.OFFLINE_SYNC_REFUSED,
        ).order_by("reason_code")
    )


def _batches_received(device):
    return AuditEvent.objects.filter(
        target_type="EntryDevice",
        target_uuid=device.pk,
        action_code=action_codes.OFFLINE_SYNC_BATCH_RECEIVED,
    ).count()


def test_unauthenticated_quarantine_bursts_write_no_per_request_audit_or_budget(
    offline_world, device_admin, settings
):
    settings.ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW = 2
    device = _revoke(offline_world, device_admin)
    first, second = _admit(offline_world), _admit(offline_world)
    usable = [offline_world.store.envelope(op) for op in (first, second)]
    bursts = {
        "MALFORMED_BATCH": {"device": device.public_id, "store": "short", "operations": []},
        "UNORDERED_BATCH": _quarantine_payload(offline_world, list(reversed(usable))),
        "INVALID_SIGNATURE": _quarantine_payload(offline_world, _forged(offline_world, 3)),
    }
    for _ in range(25):  # far more requests than the whole upload budget
        for payload in bursts.values():
            with pytest.raises(OfflineRequestRejected):
                offline_sync.synchronize_quarantine(payload=payload)
    # One bounded record per reason and window, instead of 75 rows.
    refusals = _refusals(device)
    assert [row.reason_code for row in refusals] == sorted(bursts)
    assert all(row.after_summary["authenticated"] is False for row in refusals)
    assert not SyncOperation.objects.exists() and not ReconciliationCase.objects.exists()
    # Unauthenticated traffic never spends the device's budget: its signed
    # evidence is still accepted -- and quarantined, never applied.
    assert _batches_received(device) == 0
    result = offline_sync.synchronize_quarantine(payload=_quarantine_payload(offline_world, usable))
    assert [ack.status for ack in result.acknowledgements] == ["QUARANTINED", "QUARANTINED"]
    assert all(ack.durable for ack in result.acknowledgements)
    assert _batches_received(device) == 1 and not EntryEvent.objects.exists()
    assert len(_refusals(device)) == len(bursts)


def test_a_forged_quarantine_batch_costs_at_most_one_pass_over_the_device_keys(
    offline_world, device_admin, monkeypatch
):
    from apps.entry.models import DeviceKeyPurpose, EntryDeviceKey

    device = _revoke(offline_world, device_admin)
    keys = EntryDeviceKey.objects.filter(
        device=device, purpose=DeviceKeyPurpose.OPERATION_SIGNING
    ).count()
    real = offline_sync.verify_device_signature
    calls = []

    def counting(public_key, message, signature):
        calls.append(1)
        return real(public_key, message, signature)

    monkeypatch.setattr(offline_sync, "verify_device_signature", counting)
    with pytest.raises(OfflineRequestRejected) as refused:
        offline_sync.synchronize_quarantine(
            payload=_quarantine_payload(offline_world, _forged(offline_world, 20))
        )
    assert refused.value.code == "QUARANTINE_REFUSED"
    assert 1 <= len(calls) <= keys  # the first forged operation stops the batch
    # A genuine prefix is quarantined; nothing after the first forged operation
    # is processed (no operation ever runs ahead of one without an outcome).
    calls.clear()
    genuine = offline_world.store.envelope(_admit(offline_world))
    tail = _forged(offline_world, 19)
    result = offline_sync.synchronize_quarantine(
        payload=_quarantine_payload(offline_world, [genuine, *tail])
    )
    assert [ack.operation_id for ack in result.acknowledgements] == [genuine["operation_id"]]
    assert result.acknowledgements[0].status == "QUARANTINED"
    assert SyncOperation.objects.count() == 1
    assert len(calls) <= 2 * keys + keys  # the prefix (filter and intake), then one miss


def test_quarantine_budget_exhaustion_is_audited_once_per_window_and_per_device(
    offline_world, device_admin, settings, event, layout
):
    from apps.entry.tests import factories

    settings.ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW = 1
    device = _revoke(offline_world, device_admin)
    genuine = offline_world.store.envelope(_admit(offline_world))
    payload = _quarantine_payload(offline_world, [genuine])
    assert offline_sync.synchronize_quarantine(payload=payload).acknowledgements[0].durable
    for _ in range(10):
        with pytest.raises(OfflineUnavailable) as limited:
            offline_sync.synchronize_quarantine(payload=payload)
        assert limited.value.code == "RATE_LIMITED"
    assert [row.reason_code for row in _refusals(device)] == ["RATE_LIMITED"]
    # The budget is the device's own: another device keeps its budget.
    other, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    now = timezone.now()
    assert offline_sync._sync_budget_exhausted(device, now=now)
    assert not offline_sync._sync_budget_exhausted(other, now=now)


def test_authenticated_batch_refusals_keep_their_per_request_audit(offline_world):
    first, second = _admit(offline_world), _admit(offline_world)
    for _ in range(2):
        with pytest.raises(OfflineRequestRejected) as info:
            _upload(offline_world, second, first)
        assert info.value.code == "UNORDERED_BATCH"
    # A signed, nonce-bound request stays individually attributable.
    assert [row.reason_code for row in _refusals(offline_world.device)] == [
        "UNORDERED_BATCH",
        "UNORDERED_BATCH",
    ]


# ---------------------------------------------------------------------------
# Database-enforced immutability
# ---------------------------------------------------------------------------


def test_sync_evidence_is_immutable_in_the_database(offline_world):
    _upload(offline_world, _admit(offline_world))
    row = SyncOperation.objects.get()
    for change in (
        {"payload_json": {}},
        {"status": "REJECTED"},
        {"payload_hash": "0" * 64},
        {"duplicate_submissions": 0, "occurred_at": timezone.now()},
    ):
        with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
            SyncOperation.objects.filter(pk=row.pk).update(**change)
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        SyncOperation.objects.filter(pk=row.pk).delete()
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        EntryEvent.objects.filter(sync_operation=row).update(offline_conflict=True)


def test_an_offline_entry_event_must_name_its_sync_operation(offline_world):
    _upload(offline_world, _admit(offline_world))
    event = EntryEvent.objects.get()
    clone = {
        field.attname: getattr(event, field.attname)
        for field in EntryEvent._meta.concrete_fields
        if field.attname not in ("id", "sync_operation_id", "operation_id")
    }
    with pytest.raises(IntegrityError) as rejected, transaction.atomic():
        EntryEvent.objects.create(**clone, operation_id="x" * 32)
    assert rejected.value.__cause__.diag.constraint_name == "entry_event_offline_coherent"
    # An ONLINE event can never carry offline evidence (here: the conflict flag).
    online = {**clone, "offline": False, "package_version": None, "offline_conflict": True}
    with pytest.raises(IntegrityError) as rejected, transaction.atomic():
        EntryEvent.objects.create(**online, operation_id="y" * 32)
    assert rejected.value.__cause__.diag.constraint_name == "entry_event_offline_coherent"
    # Even without the conflict flag, an online row cannot claim a package.
    online_package = {**clone, "offline": False, "offline_conflict": False}
    with pytest.raises(IntegrityError) as rejected, transaction.atomic():
        EntryEvent.objects.create(**online_package, operation_id="z" * 32)
    assert rejected.value.__cause__.diag.constraint_name == "entry_event_offline_coherent"


# ---------------------------------------------------------------------------
# NFR-SYNC-001 and OFF-005
# ---------------------------------------------------------------------------


def test_every_accepted_operation_ends_applied_duplicate_or_in_an_explicit_case(
    offline_world, staff
):
    from apps.badges.models import PassReasonCode
    from apps.badges.services import new_operation_id, revoke_pass

    ops = [
        _admit(offline_world, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT"),
        _admit(offline_world, band="STALE", state="STALE"),
    ]
    credential = offline_world.credential
    revoke_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    ops.append(_admit(offline_world, occurred_at=timezone.now() + timedelta(seconds=5)))
    result = _upload(offline_world, *ops)
    _upload(offline_world, ops[0])  # a duplicate
    for ack in result.acknowledgements:
        assert ack.durable
        row = SyncOperation.objects.get(operation_id=ack.operation_id)
        if row.status != "APPLIED":
            assert ReconciliationCase.objects.filter(links__sync_operation=row).exists()
    from apps.entry.services.reconciliation import recovery_counts

    counts = recovery_counts(SyncOperation.objects.all())
    assert counts == {
        "uploaded": 3,
        "accepted": 1,
        "duplicate": 1,
        "rejected": 0,
        "conflict": 2,
        "quarantined": 0,
        "pending": 0,
    }
