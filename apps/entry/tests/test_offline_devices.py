"""Offline preparation: provisioning, proofs, self-test, readiness, blocking,
lifecycle withdrawal, emergency wipe and heartbeat directives
(Phase 4 Prompt 2, binding decisions P2-D/P2-E/P2-F). PostgreSQL.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import transaction
from django.db.utils import DatabaseError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.tests.mfa_double import ACCEPTED_TEST_RESPONSE
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.models import (
    DeviceEvidenceReport,
    DeviceKeyStatus,
    DeviceWipeOrder,
    EntryDeviceKey,
    EntryDeviceStatus,
    OfflinePackageStatus,
)
from apps.entry.offline_contract import TYP_WIPE_ORDER
from apps.entry.services import EntryPermissionError
from apps.entry.services.devices import (
    authenticate_device,
    change_device_scope,
    resume_device,
    revoke_device,
    suspend_device,
)
from apps.entry.services.offline_crypto import jws_verify
from apps.entry.services.offline_devices import (
    MfaStepUpRequired,
    OfflineRequestRejected,
    OfflineUnavailable,
    authenticate_device_any_status,
    block_offline_use,
    heartbeat,
    order_emergency_wipe,
    provision_device,
    record_evidence_report,
    set_event_offline_enabled,
)
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db

ACCEPTING = "apps.accounts.tests.mfa_double.AcceptingStepUpBackend"
REJECTING = "apps.accounts.tests.mfa_double.RejectingStepUpBackend"
FAILING = "apps.accounts.tests.mfa_double.FailingStepUpBackend"


def _ready(offline_device, device_admin):
    package, _raw = offline_factories.download(offline_device)
    assert offline_factories.self_test(offline_device, admin=device_admin, package=package) is True
    return package


def _beat(synthetic, report=None, signed=False):
    if signed:
        body, nonce, signature = synthetic.signed("heartbeat", {"report": report})
    else:
        body, nonce, signature = b"{}", None, None
    return heartbeat(
        device=synthetic.device, report=report, nonce=nonce, signature=signature, body=body
    )


def _directive_types(data):
    return {d["type"]: d for d in data["directives"]}


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def test_provisioning_registers_public_keys_and_pins_package_keys_only(offline_device):
    keys = EntryDeviceKey.objects.filter(
        device=offline_device.device, status=DeviceKeyStatus.ACTIVE
    )
    assert {k.purpose for k in keys} == {"SIGN", "UNWRAP"}
    assert {a["kid"] for a in offline_device.trust_anchors} == {"p1", "p2"}
    assert offline_device.device.offline_prepared_at is not None
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED  # not ready before self-test
    audit = AuditEvent.objects.get(action_code=action_codes.DEVICE_OFFLINE_PROVISIONED)
    assert "spki" not in str(audit.after_summary)


def test_provisioning_requires_a_device_administrator(
    offline_on, package_signer, event, layout, device, device_admin, operator
):
    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    with pytest.raises(EntryPermissionError):
        offline_factories.provision(synthetic, admin=operator)


def test_provisioning_requires_proof_of_possession_of_the_new_key(
    offline_on, package_signer, event, layout, device, device_admin
):
    offline_factories.enable_event(event, device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    imposter = offline_factories.SyntheticDevice(device=device)
    payload = {"signing_key": synthetic.sign_spki, "unwrap_key": synthetic.unwrap_spki}
    body, nonce, signature = synthetic.signed("provision", payload, key=imposter.sign_key)
    with pytest.raises(OfflineRequestRejected) as exc:
        provision_device(
            device=device,
            actor=device_admin,
            signing_key_spki=synthetic.sign_spki,
            unwrap_key_spki=synthetic.unwrap_spki,
            nonce=nonce,
            signature=signature,
            body=body,
        )
    assert exc.value.code == "PROOF_INVALID"
    assert not EntryDeviceKey.objects.filter(device=device).exists()


def test_provisioning_refuses_a_non_p256_or_reused_key(offline_device, device_admin):
    from cryptography.hazmat.primitives.asymmetric import ec

    weak = offline_factories.SyntheticDevice(
        device=offline_device.device, sign_key=ec.generate_private_key(ec.SECP384R1())
    )
    with pytest.raises(OfflineRequestRejected) as exc:
        offline_factories.provision(weak, admin=device_admin)
    assert exc.value.code == "KEY_INVALID"
    # The same public key cannot be registered twice (fingerprint uniqueness).
    reuse = offline_factories.SyntheticDevice(
        device=offline_device.device,
        sign_key=offline_device.sign_key,
        unwrap_key=offline_device.unwrap_key,
    )
    with pytest.raises(OfflineRequestRejected) as exc:
        offline_factories.provision(reuse, admin=device_admin)
    assert exc.value.code == "KEY_REUSED"


def test_reprovisioning_retires_previous_keys_and_revokes_packages(
    offline_device, device_admin, active_pass
):
    package = _ready(offline_device, device_admin)
    second = offline_factories.SyntheticDevice(device=offline_device.device)
    offline_factories.provision(second, admin=device_admin)
    package.refresh_from_db()
    assert package.status == OfflinePackageStatus.REVOKED
    retired = EntryDeviceKey.objects.filter(
        device=offline_device.device, status=DeviceKeyStatus.RETIRED
    )
    assert retired.count() == 2
    offline_device.device.refresh_from_db()
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED


def test_device_key_material_is_immutable_in_the_database(offline_device):
    key = EntryDeviceKey.objects.filter(device=offline_device.device).first()
    with pytest.raises(DatabaseError), transaction.atomic():
        EntryDeviceKey.objects.filter(pk=key.pk).update(public_key_spki="x")
    with pytest.raises(DatabaseError), transaction.atomic():
        EntryDeviceKey.objects.filter(pk=key.pk).delete()


def test_restricted_zone_requires_the_sensitive_values(
    offline_on, event, layout, device, device_admin
):
    from apps.entry.services.devices import DeviceConfigurationError

    layout.hall.sensitivity = "RESTRICTED"
    layout.hall.save()
    with pytest.raises(DeviceConfigurationError) as exc:
        offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    assert exc.value.code == "SENSITIVITY_REQUIRED"
    offline_factories.make_offline_capable(
        device, admin=device_admin, layout=layout, sensitivity="SENSITIVE"
    )


# ---------------------------------------------------------------------------
# Self-test and readiness
# ---------------------------------------------------------------------------


def test_self_test_success_is_the_only_way_to_offline_ready(
    offline_device, device_admin, active_pass
):
    _ready(offline_device, device_admin)
    assert offline_device.device.status == EntryDeviceStatus.OFFLINE_READY
    assert offline_device.device.offline_self_test_at is not None
    # OFFLINE_READY still operates online exactly like ENROLLED.
    assert authenticate_device(None) is None
    assert AuditEvent.objects.filter(action_code=action_codes.DEVICE_OFFLINE_READY).count() == 1


def test_a_failed_check_leaves_the_device_not_ready(offline_device, device_admin, active_pass):
    package, _ = offline_factories.download(offline_device)
    checks = offline_factories.all_checks_passed() | {"storage_quota": False}
    assert (
        offline_factories.self_test(
            offline_device, admin=device_admin, package=package, checks=checks
        )
        is False
    )
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED
    audit = AuditEvent.objects.get(action_code=action_codes.DEVICE_OFFLINE_SELF_TEST_FAILED)
    assert audit.after_summary["failed"] == ["storage_quota"]


def test_self_test_must_name_the_current_package(offline_device, device_admin, active_pass):
    package, _ = offline_factories.download(offline_device)
    package.public_id = "not-the-current-one"
    assert offline_factories.self_test(offline_device, admin=device_admin, package=package) is False


def test_offline_ready_is_database_guarded(offline_device):
    from apps.entry.models import EntryDevice

    with pytest.raises(DatabaseError), transaction.atomic():
        EntryDevice.objects.filter(pk=offline_device.device.pk).update(
            status="OFFLINE_READY", offline_self_test_at=None
        )


def test_offline_ready_device_can_open_a_checkpoint_online(
    offline_device, device_admin, active_pass, operator, layout
):
    _ready(offline_device, device_admin)
    checkpoint = factories.open_checkpoint(
        device=offline_device.device, user=operator, zone=layout.main
    )
    assert checkpoint.device.pk == offline_device.device.pk


# ---------------------------------------------------------------------------
# Blocking and lifecycle withdrawal (never deleting device evidence)
# ---------------------------------------------------------------------------


def test_block_offline_withdraws_readiness_and_keeps_online_operation(
    offline_device, device_admin, active_pass
):
    package = _ready(offline_device, device_admin)
    block_offline_use(
        device=offline_device.device,
        actor=device_admin,
        expected_version=offline_device.device.version,
        reason_code="SECURITY_CONCERN",
    )
    offline_device.device.refresh_from_db()
    package.refresh_from_db()
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED
    assert offline_device.device.offline_blocked_at is not None
    assert package.status == OfflinePackageStatus.REVOKED
    directives = _directive_types(_beat(offline_device))
    assert directives["PURGE_PACKAGE"]["reason"] == "DEVICE_OFFLINE_BLOCKED"
    assert directives["LOCK_OPERATIONS"]["reason"] == "DEVICE_OFFLINE_BLOCKED"
    assert "BLOCK" not in directives  # online operation continues
    assert "EMERGENCY_WIPE" not in directives  # ordinary blocking never wipes
    with pytest.raises(OfflineUnavailable):
        offline_factories.download(offline_device)


def test_block_offline_requires_device_administration(offline_device, operator):
    with pytest.raises(EntryPermissionError):
        block_offline_use(
            device=offline_device.device,
            actor=operator,
            expected_version=offline_device.device.version,
            reason_code="SECURITY_CONCERN",
        )


def test_suspension_blocks_and_locks_without_wiping(offline_device, device_admin, active_pass):
    package = _ready(offline_device, device_admin)
    suspend_device(
        device=offline_device.device,
        actor=device_admin,
        expected_version=offline_device.device.version,
        reason_code="SECURITY_CONCERN",
    )
    offline_device.device.refresh_from_db()
    package.refresh_from_db()
    assert package.status == OfflinePackageStatus.REVOKED
    directives = _directive_types(_beat(offline_device))
    assert directives["BLOCK"]["reason"] == "DEVICE_SUSPENDED"
    assert {"PURGE_PACKAGE", "LOCK_OPERATIONS"} <= set(directives)
    assert "EMERGENCY_WIPE" not in directives
    resume_device(
        device=offline_device.device,
        actor=device_admin,
        expected_version=offline_device.device.version,
        reason_code="ADMINISTRATIVE_ERROR",
    )
    offline_device.device.refresh_from_db()
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED  # must re-test


def test_revocation_retires_keys_and_the_credential_resolves_to_nothing(
    offline_device, device_admin, device_and_secret, active_pass
):
    _ready(offline_device, device_admin)
    revoke_device(
        device=offline_device.device,
        actor=device_admin,
        expected_version=offline_device.device.version,
        reason_code="LOST_OR_STOLEN",
    )
    assert authenticate_device_any_status(device_and_secret[1]) is None
    assert not EntryDeviceKey.objects.filter(
        device=offline_device.device, status=DeviceKeyStatus.ACTIVE
    ).exists()
    # The public keys are kept (retired), never deleted: the future
    # quarantine boundary can still verify locked operations.
    assert EntryDeviceKey.objects.filter(device=offline_device.device).count() == 2


def test_rescope_withdraws_readiness(offline_device, device_admin, layout, active_pass):
    package = _ready(offline_device, device_admin)
    change_device_scope(
        device=offline_device.device,
        venue=layout.venue,
        gate=layout.gate_a,
        zones=[layout.main],
        verification_methods=factories.ALL_METHODS,
        actor=device_admin,
        expected_version=offline_device.device.version,
        offline_capable=True,
    )
    offline_device.device.refresh_from_db()
    package.refresh_from_db()
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED
    assert package.status == OfflinePackageStatus.REVOKED


def test_event_disable_and_closure_withdraw_every_device(
    offline_device, device_admin, active_pass, event
):
    package = _ready(offline_device, device_admin)
    set_event_offline_enabled(
        event_edition=event, enabled=False, actor=device_admin, reason_code="INCIDENT"
    )
    package.refresh_from_db()
    offline_device.device.refresh_from_db()
    assert package.status == OfflinePackageStatus.REVOKED
    assert offline_device.device.status == EntryDeviceStatus.ENROLLED
    assert _directive_types(_beat(offline_device))["PURGE_PACKAGE"]["reason"] == "OFFLINE_DISABLED"
    type(event).objects.filter(pk=event.pk).update(status="COMPLETED")
    offline_device.device.refresh_from_db()
    directives = _directive_types(_beat(offline_device))
    assert directives["BLOCK"]["reason"] == "EVENT_CLOSED"


def test_event_enablement_requires_its_own_permission(offline_device, operator, event):
    with pytest.raises(EntryPermissionError):
        set_event_offline_enabled(
            event_edition=event, enabled=True, actor=operator, reason_code="REHEARSAL"
        )


# ---------------------------------------------------------------------------
# Heartbeat reports
# ---------------------------------------------------------------------------


def test_signed_report_is_stored_and_unsigned_report_ignored(offline_device):
    report = {
        "state": "ONLINE",
        "pending": 3,
        "locked": 1,
        "sequence_low": 1,
        "sequence_high": 4,
        "chain_head": "a" * 64,
        "package_version": None,
        "delta_version": None,
    }
    data = _beat(offline_device, report=report)  # unsigned: ignored
    assert data["report_accepted"] is False
    data = _beat(offline_device, report=report, signed=True)
    assert data["report_accepted"] is True
    offline_device.device.refresh_from_db()
    assert offline_device.device.reported_pending_operations == 3
    assert offline_device.device.reported_locked_operations == 1
    assert offline_device.device.reported_chain_head == "a" * 64


def test_heartbeat_returns_no_package_content(offline_device, device_admin, active_pass):
    _ready(offline_device, device_admin)
    data = _beat(offline_device)
    text = str(data)
    assert active_pass.jti not in text and "Synthetic Participant" not in text
    assert data["offline"]["ready"] is True


# ---------------------------------------------------------------------------
# Destructive emergency wipe (distinct authority, MFA, confirmation, evidence)
# ---------------------------------------------------------------------------


@pytest.fixture
def security_manager(event):
    return factories.make_user(
        "security.manager@example.test", group_name="Security Restriction Managers", event=event
    )


def _order(device, actor, **overrides):
    arguments = {
        "device": device,
        "actor": actor,
        "reason_code": "DEVICE_STOLEN",
        "note": "Synthetic note",
        "confirmed": True,
        "mfa_response": ACCEPTED_TEST_RESPONSE,
    } | overrides
    return order_emergency_wipe(**arguments)


def test_device_administrators_cannot_order_a_wipe(offline_device, device_admin):
    with override_settings(MFA_BACKEND=ACCEPTING), pytest.raises(EntryPermissionError):
        _order(offline_device.device, device_admin)


@pytest.mark.parametrize(
    ("backend", "code"),
    [(None, "MFA_UNAVAILABLE"), (REJECTING, "MFA_STEP_UP_FAILED"), (FAILING, "MFA_STEP_UP_FAILED")],
)
def test_wipe_fails_closed_without_a_valid_mfa_step_up(
    offline_device, security_manager, backend, code
):
    with override_settings(MFA_BACKEND=backend), pytest.raises(MfaStepUpRequired) as exc:
        _order(offline_device.device, security_manager)
    assert exc.value.code == code
    assert not DeviceWipeOrder.objects.exists()
    assert AuditEvent.objects.filter(
        action_code=action_codes.DEVICE_EMERGENCY_WIPE_REFUSED, reason_code=code
    ).exists()


def test_wipe_requires_confirmation_and_reason(offline_device, security_manager):
    from apps.entry.services import EntryStateError

    with override_settings(MFA_BACKEND=ACCEPTING):
        with pytest.raises(EntryStateError) as exc:
            _order(offline_device.device, security_manager, confirmed=False)
        assert exc.value.code == "CONFIRMATION_REQUIRED"
        with pytest.raises(EntryStateError) as exc:
            _order(offline_device.device, security_manager, reason_code="")
        assert exc.value.code == "REASON_REQUIRED"


def test_wipe_order_records_the_expected_evidence_loss(
    offline_device, security_manager, device_and_secret
):
    report = {
        "state": "ONLINE",
        "pending": 5,
        "locked": 2,
        "sequence_high": 7,
        "chain_head": "b" * 64,
    }
    _beat(offline_device, report=report, signed=True)
    offline_device.device.refresh_from_db()
    with override_settings(MFA_BACKEND=ACCEPTING):
        order = _order(offline_device.device, security_manager)
    assert order.expected_evidence_loss == "OPERATIONS_AT_RISK"
    assert (order.known_pending_operations, order.known_locked_operations) == (5, 2)
    assert order.known_sequence_high == 7 and order.known_chain_head == "b" * 64
    assert order.note_encrypted == "Synthetic note"
    # The device stops operating online at once, but still receives the order.
    assert authenticate_device(device_and_secret[1]) is None
    device = authenticate_device_any_status(device_and_secret[1])
    directives = _directive_types(_beat(offline_factories.SyntheticDevice(device=device)))
    compact = directives["EMERGENCY_WIPE"]["order"]
    payload = jws_verify(
        compact, trusted_keys_der=offline_device.trusted_keys_der(), typ=TYP_WIPE_ORDER
    )
    assert payload["order_id"] == order.public_id
    assert directives["BLOCK"]["reason"] == "DEVICE_WIPE_ORDERED"
    audit = AuditEvent.objects.get(action_code=action_codes.DEVICE_EMERGENCY_WIPE_ORDERED)
    assert "Synthetic note" not in str(audit.after_summary)


def test_wipe_orders_and_evidence_reports_are_append_only(offline_device, security_manager):
    with override_settings(MFA_BACKEND=ACCEPTING):
        order = _order(offline_device.device, security_manager)
    payload = {
        "order_id": order.public_id,
        "pending": 1,
        "locked": 0,
        "sequence_low": 1,
        "sequence_high": 1,
        "chain_head": "c" * 64,
    }
    body, nonce, signature = offline_device.signed("wipe-report", payload)
    record_evidence_report(
        device=offline_device.device, report=payload, nonce=nonce, signature=signature, body=body
    )
    evidence = DeviceEvidenceReport.objects.get()
    assert evidence.pending_operations == 1
    with pytest.raises(DatabaseError), transaction.atomic():
        DeviceWipeOrder.objects.filter(pk=order.pk).update(reason_code="DEVICE_LOST")
    with pytest.raises(DatabaseError), transaction.atomic():
        DeviceEvidenceReport.objects.filter(pk=evidence.pk).delete()


def test_ordinary_paths_never_emit_a_wipe(offline_device, device_admin, active_pass):
    _ready(offline_device, device_admin)
    for _ in range(2):
        assert "EMERGENCY_WIPE" not in _directive_types(_beat(offline_device))


def test_nonce_expiry_is_enforced(offline_device, settings):
    from apps.entry.services.offline_devices import issue_nonce, verify_device_proof

    nonce = issue_nonce(offline_device.device)
    body, _n, signature = offline_device.signed("package", {}, nonce=nonce)
    later = timezone.now() + timedelta(seconds=settings.ENTRY_OFFLINE_NONCE_SECONDS + 5)
    from unittest import mock

    with mock.patch("django.core.signing.time.time", return_value=later.timestamp()):
        with pytest.raises(OfflineRequestRejected) as exc:
            verify_device_proof(
                offline_device.device,
                purpose="package",
                nonce=nonce,
                signature=signature,
                body=body,
            )
    assert exc.value.code == "NONCE_INVALID"


def test_a_nonce_is_bound_to_its_device(offline_device, event, layout, device_admin):
    from apps.entry.services.offline_devices import issue_nonce, verify_device_proof

    other_device, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    nonce = issue_nonce(other_device)
    body, _n, signature = offline_device.signed("package", {}, nonce=nonce)
    with pytest.raises(OfflineRequestRejected) as exc:
        verify_device_proof(
            offline_device.device, purpose="package", nonce=nonce, signature=signature, body=body
        )
    assert exc.value.code == "NONCE_INVALID"
