"""Checkpoint (device) sessions, operator sessions, and checkpoint scope."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import ScopedGroupMembership
from apps.accounts.policies import has_scoped_permission
from apps.audit.models import AuditEvent
from apps.entry.models import EntryDeviceSession, EntryOperatorSession
from apps.entry.services import EntryPermissionError
from apps.entry.services.devices import change_device_scope
from apps.entry.services.sessions import (
    CheckpointSetupError,
    CheckpointUnavailable,
    join_checkpoint_session,
    resolve_checkpoint,
    start_checkpoint_session,
)
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db


def _resolve(checkpoint, *, user=None, device=None, now=None):
    return resolve_checkpoint(
        device=device or checkpoint.device,
        device_session_id=checkpoint.device_session.pk,
        operator_session_id=checkpoint.operator_session.pk,
        user=user or checkpoint.user,
        now=now,
    )


class TestStartingASession:
    def test_session_inside_scope(self, checkpoint, layout):
        assert checkpoint.gate == layout.gate_a
        assert checkpoint.zone == layout.main
        assert checkpoint.device_session.expires_at <= checkpoint.device.expires_at
        assert checkpoint.operator_session.expires_at <= checkpoint.device_session.expires_at
        assert AuditEvent.objects.filter(action_code="ENT_DEVICE_SESSION_STARTED").exists()
        assert AuditEvent.objects.filter(action_code="ENT_OPERATOR_SESSION_STARTED").exists()

    def test_cross_checkpoint_gate_is_denied(self, device, operator, layout):
        with pytest.raises(CheckpointSetupError):
            start_checkpoint_session(
                device=device, user=operator, gate_id=layout.gate_b.pk, zone_id=layout.main.pk
            )
        assert AuditEvent.objects.filter(
            action_code="ENT_DEVICE_SESSION_REJECTED", reason_code="OUTSIDE_DEVICE_SCOPE"
        ).exists()
        assert not EntryDeviceSession.objects.exists()

    def test_zone_outside_device_scope_is_denied(self, device, operator, layout):
        with pytest.raises(CheckpointSetupError):
            start_checkpoint_session(
                device=device, user=operator, gate_id=layout.gate_a.pk, zone_id=layout.vip.pk
            )

    def test_operator_scoped_to_another_gate_is_denied(self, device, event, layout):
        gate_b_operator = factories.make_user(
            "gateb@example.test", group_name="Entry Operators", event=event, gate=layout.gate_b
        )
        with pytest.raises(EntryPermissionError):
            start_checkpoint_session(
                device=device,
                user=gate_b_operator,
                gate_id=layout.gate_a.pk,
                zone_id=layout.main.pk,
            )

    def test_operator_of_another_event_is_denied(self, device, other_event, layout):
        other = factories.make_user(
            "otherevent@example.test", group_name="Entry Operators", event=other_event
        )
        with pytest.raises(EntryPermissionError):
            start_checkpoint_session(
                device=device, user=other, gate_id=layout.gate_a.pk, zone_id=layout.main.pk
            )

    def test_device_administrator_cannot_operate_a_checkpoint(self, device, device_admin, layout):
        with pytest.raises(EntryPermissionError):
            start_checkpoint_session(
                device=device, user=device_admin, gate_id=layout.gate_a.pk, zone_id=layout.main.pk
            )

    def test_new_session_replaces_the_open_one(self, device, operator, layout, checkpoint):
        factories.open_checkpoint(device=device, user=operator, zone=layout.hall)
        assert EntryDeviceSession.objects.filter(device=device, ended_at__isnull=True).count() == 1
        with pytest.raises(CheckpointUnavailable):
            _resolve(checkpoint)

    def test_second_operator_joins_with_their_own_session(self, checkpoint, supervisor):
        operator_session = join_checkpoint_session(
            device_session=checkpoint.device_session, user=supervisor
        )
        assert operator_session.user == supervisor
        resolved = resolve_checkpoint(
            device=checkpoint.device,
            device_session_id=checkpoint.device_session.pk,
            operator_session_id=operator_session.pk,
            user=supervisor,
        )
        assert resolved.user == supervisor
        # The first operator's session is untouched.
        assert _resolve(checkpoint).operator_session == checkpoint.operator_session


class TestResolvingEveryRequest:
    def test_another_users_operator_session_is_rejected(self, checkpoint, supervisor):
        with pytest.raises(CheckpointUnavailable):
            _resolve(checkpoint, user=supervisor)

    def test_another_device_cannot_reuse_the_session(self, checkpoint, event, layout, device_admin):
        other_device, _ = factories.enroll_device(event=event, layout=layout, admin=device_admin)
        with pytest.raises(CheckpointUnavailable):
            _resolve(checkpoint, device=other_device)

    def test_scope_change_invalidates_the_session(self, checkpoint, device_admin, layout):
        device = checkpoint.device
        device.refresh_from_db()
        change_device_scope(
            device=device,
            venue=layout.venue,
            gate=layout.gate_a,
            zones=[layout.main],
            verification_methods=["QR"],
            actor=device_admin,
            expected_version=device.version,
        )
        with pytest.raises(CheckpointUnavailable):
            _resolve(checkpoint)

    @override_settings(ENTRY_OPERATOR_INACTIVITY_SECONDS=60)
    def test_operator_inactivity_ends_the_session(self, checkpoint):
        later = timezone.now() + timedelta(seconds=61)
        with pytest.raises(CheckpointUnavailable) as exc:
            _resolve(checkpoint, now=later)
        assert exc.value.reason == "OPERATOR_SESSION_ENDED"
        assert EntryOperatorSession.objects.get(pk=checkpoint.operator_session.pk).end_reason == (
            "INACTIVITY"
        )

    def test_operator_session_absolute_expiry(self, checkpoint):
        later = checkpoint.operator_session.expires_at + timedelta(seconds=1)
        with pytest.raises(CheckpointUnavailable) as exc:
            _resolve(checkpoint, now=later)
        assert exc.value.reason == "OPERATOR_SESSION_ENDED"

    def test_device_session_expiry(self, checkpoint):
        later = checkpoint.device_session.expires_at + timedelta(seconds=1)
        with pytest.raises(CheckpointUnavailable) as exc:
            _resolve(checkpoint, now=later)
        assert exc.value.reason == "DEVICE_SESSION_ENDED"

    def test_permission_removed_mid_session_is_enforced(self, checkpoint, operator):
        ScopedGroupMembership.objects.filter(user=operator).update(
            active_until=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(CheckpointUnavailable) as exc:
            _resolve(checkpoint)
        assert exc.value.reason == "OPERATOR_NOT_AUTHORIZED"

    def test_missing_device_fails_closed(self, checkpoint):
        with pytest.raises(CheckpointUnavailable):
            resolve_checkpoint(
                device=None,
                device_session_id=checkpoint.device_session.pk,
                operator_session_id=checkpoint.operator_session.pk,
                user=checkpoint.user,
            )


class TestGateScopedMembershipsNeverLeak:
    def test_gate_scoped_grant_does_not_authorize_broad_checks(self, operator, event, layout):
        # A gate-narrowed membership authorizes that gate only...
        assert has_scoped_permission(
            operator,
            "entry.verify_entry",
            event_edition_id=event.pk,
            venue_id=layout.venue.pk,
            gate_id=layout.gate_a.pk,
        )
        # ...never a different gate...
        assert not has_scoped_permission(
            operator,
            "entry.verify_entry",
            event_edition_id=event.pk,
            venue_id=layout.venue.pk,
            gate_id=layout.gate_b.pk,
        )
        # ...and never a check that names no checkpoint at all.
        assert not has_scoped_permission(operator, "entry.verify_entry")
        assert not has_scoped_permission(operator, "entry.verify_entry", event_edition_id=event.pk)

    def test_event_wide_grant_covers_every_gate(self, event, layout):
        wide = factories.make_user("wide@example.test", group_name="Entry Operators", event=event)
        for gate in (layout.gate_a, layout.gate_b):
            assert has_scoped_permission(
                wide,
                "entry.verify_entry",
                event_edition_id=event.pk,
                venue_id=layout.venue.pk,
                gate_id=gate.pk,
            )
