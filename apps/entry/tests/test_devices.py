"""Device enrollment, credential handling, lifecycle, expiry, and scope versions."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.entry.models import (
    DeviceScope,
    EntryDevice,
    EntryDeviceSession,
    EntryDeviceStatus,
    EntryOperatorSession,
)
from apps.entry.services import EntryConcurrencyError, EntryPermissionError, EntryStateError
from apps.entry.services.devices import (
    DeviceConfigurationError,
    DeviceEnrollmentError,
    authenticate_device,
    change_device_scope,
    enroll_device_with_code,
    expire_lapsed_devices,
    issue_activation_code,
    register_device,
    resume_device,
    revoke_device,
    suspend_device,
)
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db


def _register(event, layout, admin, **overrides):
    params = {
        "event_edition": event,
        "public_name": "Tablet",
        "expires_at": timezone.now() + timedelta(days=3),
        "venue": layout.venue,
        "gate": layout.gate_a,
        "zones": [layout.main],
        "verification_methods": ["QR"],
        "actor": admin,
    }
    params.update(overrides)
    return register_device(**params)


def _audit_blob() -> str:
    return json.dumps(
        list(AuditEvent.objects.values("action_code", "after_summary", "before_summary")),
        default=str,
    )


class TestRegistration:
    def test_registration_creates_pending_device_with_scope_v1(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        device = registration.device
        assert device.status == EntryDeviceStatus.PENDING_ENROLLMENT
        assert device.credential_hash == ""
        scope = DeviceScope.objects.get(device=device, is_current=True)
        assert scope.scope_version == 1
        assert scope.gate == layout.gate_a
        assert list(scope.permitted_zones.all()) == [layout.main]
        assert scope.offline_capable is False

    def test_activation_code_is_stored_only_as_a_digest(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        device = EntryDevice.objects.get(pk=registration.device.pk)
        assert registration.activation_code not in device.activation_code_hash
        assert len(device.activation_code_hash) == 64
        assert registration.activation_code not in _audit_blob()

    def test_registration_requires_manage_permission(self, event, layout, operator):
        with pytest.raises(EntryPermissionError):
            _register(event, layout, operator)

    def test_registration_scoped_to_another_event_is_denied(self, event, other_event, layout):
        other_admin = factories.make_user(
            "other.admin@example.test", group_name="Entry Device Administrators", event=other_event
        )
        with pytest.raises(EntryPermissionError):
            _register(event, layout, other_admin)

    def test_gate_from_another_venue_is_rejected(self, event, layout, device_admin):
        other = factories.VenueLayout(event, code="V2")
        with pytest.raises(DeviceConfigurationError):
            _register(event, layout, device_admin, gate=other.gate_a)

    def test_zone_from_another_venue_is_rejected(self, event, layout, device_admin):
        other = factories.VenueLayout(event, code="V2")
        with pytest.raises(DeviceConfigurationError):
            _register(event, layout, device_admin, zones=[other.main])

    def test_unknown_verification_method_is_rejected(self, event, layout, device_admin):
        with pytest.raises(DeviceConfigurationError):
            _register(event, layout, device_admin, verification_methods=["QR", "BADGE"])

    def test_expiry_must_be_future_and_bounded(self, event, layout, device_admin):
        with pytest.raises(DeviceConfigurationError):
            _register(event, layout, device_admin, expires_at=timezone.now() - timedelta(minutes=1))
        with pytest.raises(DeviceConfigurationError):
            _register(event, layout, device_admin, expires_at=timezone.now() + timedelta(days=365))

    def test_offline_capable_scope_requires_coherent_offline_entry_evidence(
        self, event, layout, device_admin
    ):
        # Scope capability is necessary, never sufficient (ADR-0023 section 2).
        # Prompt 3 replaces the former online-only event restriction with
        # database constraints binding offline events and overrides to sync evidence.
        from django.db import connection

        registration = _register(event, layout, device_admin)
        DeviceScope.objects.filter(device=registration.device).update(offline_capable=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('entry_scope_online_only', 'entry_event_online_only', "
                "'entry_event_offline_coherent', 'entry_override_offline_coherent')"
            )
            names = {row[0] for row in cursor.fetchall()}
        assert names == {"entry_event_offline_coherent", "entry_override_offline_coherent"}
        assert DeviceScope.objects.get(device=registration.device).offline_capable


class TestEnrollment:
    def test_enrollment_installs_a_digest_and_rotates_key(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        enrollment = enroll_device_with_code(
            raw_code=registration.activation_code, actor=device_admin
        )
        device = EntryDevice.objects.get(pk=registration.device.pk)
        assert device.status == EntryDeviceStatus.ENROLLED
        assert device.enrolled_at is not None
        assert device.device_key_id
        assert enrollment.device_secret not in device.credential_hash
        assert device.activation_code_hash == ""
        assert enrollment.device_secret not in _audit_blob()
        assert AuditEvent.objects.filter(action_code="ENT_DEVICE_ENROLLED").count() == 1

    def test_code_is_single_use(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        enroll_device_with_code(raw_code=registration.activation_code, actor=device_admin)
        with pytest.raises(DeviceEnrollmentError):
            enroll_device_with_code(raw_code=registration.activation_code, actor=device_admin)

    def test_code_accepts_display_formatting(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        code = registration.activation_code
        formatted = f" {code[:4]}-{code[4:8]} {code[8:].lower()} "
        enroll_device_with_code(raw_code=formatted, actor=device_admin)

    def test_expired_code_is_rejected(self, event, layout, device_admin):
        registration = _register(event, layout, device_admin)
        EntryDevice.objects.filter(pk=registration.device.pk).update(
            activation_code_expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(DeviceEnrollmentError):
            enroll_device_with_code(raw_code=registration.activation_code, actor=device_admin)

    def test_code_alone_never_enrolls_without_a_device_administrator(
        self, event, layout, device_admin, operator
    ):
        registration = _register(event, layout, device_admin)
        with pytest.raises(DeviceEnrollmentError):
            enroll_device_with_code(raw_code=registration.activation_code, actor=operator)
        assert (
            EntryDevice.objects.get(pk=registration.device.pk).status
            == EntryDeviceStatus.PENDING_ENROLLMENT
        )
        assert AuditEvent.objects.filter(
            action_code="ENT_DEVICE_ENROLLMENT_REJECTED", result="DENIED"
        ).exists()

    def test_unknown_code_is_rejected_with_the_same_error(self, device_admin):
        with pytest.raises(DeviceEnrollmentError):
            enroll_device_with_code(raw_code="ABCDEFGHJKMN", actor=device_admin)
        with pytest.raises(DeviceEnrollmentError):
            enroll_device_with_code(raw_code="short", actor=device_admin)

    def test_re_enrollment_invalidates_the_previous_secret(
        self, event, layout, device_admin, device_and_secret
    ):
        device, old_secret = device_and_secret
        old_key_id = device.device_key_id
        code = issue_activation_code(
            device=device, actor=device_admin, expected_version=device.version
        )
        enrollment = enroll_device_with_code(raw_code=code, actor=device_admin)
        assert authenticate_device(old_secret) is None
        assert authenticate_device(enrollment.device_secret).pk == device.pk
        assert enrollment.device.device_key_id != old_key_id


class TestAuthentication:
    def test_enrolled_device_authenticates(self, device_and_secret):
        device, secret = device_and_secret
        assert authenticate_device(secret).pk == device.pk

    @pytest.mark.parametrize("value", [None, "", "x" * 10, "y" * 500, 12345])
    def test_malformed_credentials_fail_closed(self, device, value):
        assert authenticate_device(value) is None

    def test_wrong_secret_fails(self, device):
        assert authenticate_device("A" * 43) is None

    def test_last_seen_is_refreshed(self, device_and_secret):
        device, secret = device_and_secret
        EntryDevice.objects.filter(pk=device.pk).update(
            last_seen_at=timezone.now() - timedelta(hours=1)
        )
        authenticate_device(secret)
        assert EntryDevice.objects.get(pk=device.pk).last_seen_at > timezone.now() - timedelta(
            minutes=1
        )

    def test_expired_device_is_refused_and_expiry_is_persisted(self, device_and_secret, checkpoint):
        device, secret = device_and_secret
        EntryDevice.objects.filter(pk=device.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert authenticate_device(secret) is None
        device.refresh_from_db()
        assert device.status == EntryDeviceStatus.EXPIRED
        assert not EntryDeviceSession.objects.filter(device=device, ended_at__isnull=True).exists()
        assert not EntryOperatorSession.objects.filter(ended_at__isnull=True).exists()
        assert AuditEvent.objects.filter(action_code="ENT_DEVICE_EXPIRED").count() == 1


class TestLifecycle:
    def test_suspension_blocks_authentication_and_ends_sessions(
        self, device_and_secret, device_admin, checkpoint
    ):
        device, secret = device_and_secret
        suspend_device(
            device=device,
            actor=device_admin,
            expected_version=device.version,
            reason_code="MAINTENANCE",
        )
        assert authenticate_device(secret) is None
        assert not EntryOperatorSession.objects.filter(ended_at__isnull=True).exists()
        device.refresh_from_db()
        resume_device(
            device=device,
            actor=device_admin,
            expected_version=device.version,
            reason_code="MAINTENANCE",
        )
        assert authenticate_device(secret).pk == device.pk

    def test_revocation_is_permanent_and_clears_the_credential(
        self, device_and_secret, device_admin, checkpoint
    ):
        device, secret = device_and_secret
        revoke_device(
            device=device,
            actor=device_admin,
            expected_version=device.version,
            reason_code="LOST_OR_STOLEN",
            reason_text="Synthetic loss report",
        )
        device.refresh_from_db()
        assert device.status == EntryDeviceStatus.REVOKED
        assert device.credential_hash == ""
        assert authenticate_device(secret) is None
        assert not EntryDeviceSession.objects.filter(device=device, ended_at__isnull=True).exists()
        with pytest.raises(EntryStateError):
            resume_device(
                device=device,
                actor=device_admin,
                expected_version=device.version,
                reason_code="MAINTENANCE",
            )
        with pytest.raises(EntryStateError):
            issue_activation_code(
                device=device, actor=device_admin, expected_version=device.version
            )
        assert AuditEvent.objects.filter(
            action_code="ENT_DEVICE_REVOKED", reason_code="LOST_OR_STOLEN"
        ).exists()

    def test_stale_version_is_rejected(self, device, device_admin):
        with pytest.raises(EntryConcurrencyError):
            suspend_device(
                device=device,
                actor=device_admin,
                expected_version=device.version - 1,
                reason_code="MAINTENANCE",
            )

    def test_lifecycle_requires_a_reason_and_permission(self, device, device_admin, operator):
        with pytest.raises(DeviceConfigurationError):
            suspend_device(
                device=device, actor=device_admin, expected_version=device.version, reason_code=""
            )
        with pytest.raises(EntryPermissionError):
            revoke_device(
                device=device,
                actor=operator,
                expected_version=device.version,
                reason_code="LOST_OR_STOLEN",
            )


class TestScopeVersions:
    def test_scope_change_writes_next_version_and_ends_sessions(
        self, device, device_admin, layout, checkpoint
    ):
        scope = change_device_scope(
            device=device,
            venue=layout.venue,
            gate=layout.gate_b,
            zones=[layout.vip],
            verification_methods=["QR", "REFERENCE"],
            actor=device_admin,
            expected_version=device.version,
            reason="Moved to gate B",
        )
        assert scope.scope_version == 2
        assert DeviceScope.objects.filter(device=device).count() == 2
        assert DeviceScope.objects.filter(device=device, is_current=True).get() == scope
        assert not EntryDeviceSession.objects.filter(device=device, ended_at__isnull=True).exists()


class TestExpirySweep:
    def test_sweep_expires_lapsed_devices_idempotently(self, event, layout, device_admin, device):
        pending = _register(event, layout, device_admin).device
        EntryDevice.objects.filter(pk__in=[device.pk, pending.pk]).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        assert expire_lapsed_devices() == 2
        assert expire_lapsed_devices() == 0
        assert set(EntryDevice.objects.values_list("status", flat=True)) == {
            EntryDeviceStatus.EXPIRED
        }
