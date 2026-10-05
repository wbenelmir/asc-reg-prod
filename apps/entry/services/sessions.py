"""Checkpoint (device) sessions and operator sessions (Flow §10.3, ADR-0021 §3).

"Before operation, the user selects or confirms Event Edition, Venue, Gate,
Zone and shift. The device must be enrolled, the session valid and the
user's scope active."

`resolve_checkpoint` is called on EVERY checkpoint request. It re-derives
the whole authorization chain from the database -- device status and
expiry, the device session, the device scope version it was opened under,
the operator session (absolute and inactivity lifetimes), and the user's
checkpoint-scoped permission -- and raises `CheckpointUnavailable` the
moment any link fails. Nothing is trusted from a previous request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    OPERATIONAL_DEVICE_STATUSES,
    DeviceScope,
    EntryDevice,
    EntryDeviceSession,
    EntryOperatorSession,
)
from apps.entry.policies import has_checkpoint_permission
from apps.entry.services import EntryPermissionError, EntryServiceError, audit


class CheckpointUnavailable(EntryServiceError):
    """The device, checkpoint session, or operator session is not usable.

    `reason` is a stable, operator-safe code the view maps to a localized
    message; it never reveals another checkpoint's configuration.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CheckpointSetupError(EntryServiceError):
    """Raised when a checkpoint set-up request is outside the device scope."""


@dataclass(frozen=True)
class Checkpoint:
    """The fully validated context of one checkpoint request."""

    device: EntryDevice
    device_session: EntryDeviceSession
    operator_session: EntryOperatorSession
    scope: DeviceScope
    user: object
    event_edition: object
    gate: object
    zone: object
    verification_methods: tuple[str, ...]


# ---------------------------------------------------------------------------
# Starting sessions
# ---------------------------------------------------------------------------


def _require_verify_permission(user, *, event_edition_id, venue_id, gate_id) -> None:
    if not has_checkpoint_permission(
        user,
        "verify_entry",
        event_edition_id=event_edition_id,
        venue_id=venue_id,
        gate_id=gate_id,
    ):
        raise EntryPermissionError("You are not authorized to operate this checkpoint.")


def start_checkpoint_session(*, device: EntryDevice, user, gate_id, zone_id, now=None):
    """Open a device session at (gate, zone) plus the user's operator session.

    The gate must be EXACTLY the device scope's gate and the zone one of
    its permitted zones (cross-checkpoint denial). Any open device session
    on this device is ended first: one checkpoint per device at a time.
    """
    from apps.entry.services.devices import current_scope

    now = now or timezone.now()
    if device.status not in OPERATIONAL_DEVICE_STATUSES or device.expires_at <= now:
        raise CheckpointUnavailable("DEVICE_NOT_ENROLLED")
    scope = current_scope(device)
    if scope is None:
        raise CheckpointUnavailable("DEVICE_NOT_SCOPED")
    zone_ids = {z.pk for z in scope.permitted_zones.all()}
    if str(gate_id) != str(scope.gate_id) or not any(str(zone_id) == str(z) for z in zone_ids):
        _reject_session(device=device, user=user, reason="OUTSIDE_DEVICE_SCOPE")
        raise CheckpointSetupError(
            "This checkpoint is outside this device's scope.", code="OUTSIDE_DEVICE_SCOPE"
        )
    zone = next(z for z in scope.permitted_zones.all() if str(z.pk) == str(zone_id))
    if not scope.gate.is_active or not zone.is_active:
        _reject_session(device=device, user=user, reason="CHECKPOINT_INACTIVE")
        raise CheckpointSetupError("This checkpoint is not active.", code="CHECKPOINT_INACTIVE")
    try:
        _require_verify_permission(
            user,
            event_edition_id=scope.event_edition_id,
            venue_id=scope.venue_id,
            gate_id=scope.gate_id,
        )
    except EntryPermissionError:
        _reject_session(device=device, user=user, reason="USER_NOT_IN_SCOPE")
        raise

    with transaction.atomic():
        locked = EntryDevice.objects.select_for_update().get(pk=device.pk)
        if locked.status not in OPERATIONAL_DEVICE_STATUSES:
            raise CheckpointUnavailable("DEVICE_NOT_ENROLLED")
        end_device_sessions(device=locked, reason="REPLACED", now=now)
        device_session = EntryDeviceSession.objects.create(
            device=locked,
            scope=scope,
            event_edition_id=scope.event_edition_id,
            gate=scope.gate,
            zone=zone,
            started_by=user,
            started_at=now,
            expires_at=min(
                now + timedelta(seconds=settings.ENTRY_DEVICE_SESSION_SECONDS), locked.expires_at
            ),
        )
        audit(
            action_code=action_codes.DEVICE_SESSION_STARTED,
            actor=user,
            target_type="EntryDeviceSession",
            target_uuid=device_session.pk,
            event_edition_id=scope.event_edition_id,
            after_summary={
                "device": locked.public_id,
                "gate": scope.gate.code,
                "zone": zone.code,
                "scope_version": scope.scope_version,
            },
        )
        operator_session = _open_operator_session(device_session=device_session, user=user, now=now)
    return device_session, operator_session


def join_checkpoint_session(*, device_session: EntryDeviceSession, user, now=None):
    """Open another named operator's session on an existing checkpoint."""
    now = now or timezone.now()
    if device_session.ended_at is not None or device_session.expires_at <= now:
        raise CheckpointUnavailable("DEVICE_SESSION_ENDED")
    _require_verify_permission(
        user,
        event_edition_id=device_session.event_edition_id,
        venue_id=device_session.gate.venue_id,
        gate_id=device_session.gate_id,
    )
    with transaction.atomic():
        return _open_operator_session(device_session=device_session, user=user, now=now)


def _open_operator_session(*, device_session, user, now) -> EntryOperatorSession:
    EntryOperatorSession.objects.filter(
        device_session=device_session, user=user, ended_at__isnull=True
    ).update(ended_at=now, end_reason="REPLACED")
    operator_session = EntryOperatorSession.objects.create(
        device_session=device_session,
        user=user,
        started_at=now,
        last_activity_at=now,
        expires_at=min(
            now + timedelta(seconds=settings.ENTRY_OPERATOR_SESSION_SECONDS),
            device_session.expires_at,
        ),
    )
    audit(
        action_code=action_codes.OPERATOR_SESSION_STARTED,
        actor=user,
        target_type="EntryOperatorSession",
        target_uuid=operator_session.pk,
        event_edition_id=device_session.event_edition_id,
        after_summary={"device_session": str(device_session.pk)},
    )
    return operator_session


def _reject_session(*, device, user, reason: str) -> None:
    with transaction.atomic():
        audit(
            action_code=action_codes.DEVICE_SESSION_REJECTED,
            actor=user,
            target_type="EntryDevice",
            target_uuid=device.pk,
            event_edition_id=device.event_edition_id,
            result="DENIED",
            reason_code=reason,
        )


# ---------------------------------------------------------------------------
# Resolving (every request)
# ---------------------------------------------------------------------------


def resolve_checkpoint(
    *,
    device: EntryDevice | None,
    device_session_id,
    operator_session_id,
    user,
    now=None,
    touch: bool = True,
) -> Checkpoint:
    """Re-validate the whole checkpoint chain; raise `CheckpointUnavailable`.

    `touch=False` is the PASSIVE mode used by the connection status check
    (Phase 3 Prompt 5): every validation and every expiry still applies,
    but the operator session's inactivity clock is not refreshed, so a
    background status request can never keep an idle session alive.
    """
    now = now or timezone.now()
    if device is None:
        raise CheckpointUnavailable("DEVICE_NOT_ENROLLED")
    if not device_session_id or not operator_session_id:
        raise CheckpointUnavailable("NO_CHECKPOINT_SESSION")
    operator_session = (
        EntryOperatorSession.objects.select_related(
            "device_session",
            "device_session__scope",
            "device_session__gate",
            "device_session__gate__venue",
            "device_session__zone",
            "device_session__event_edition",
        )
        .filter(pk=operator_session_id, device_session_id=device_session_id)
        .first()
    )
    if operator_session is None:
        raise CheckpointUnavailable("NO_CHECKPOINT_SESSION")
    device_session = operator_session.device_session
    if device_session.device_id != device.pk or operator_session.user_id != getattr(
        user, "pk", None
    ):
        raise CheckpointUnavailable("NO_CHECKPOINT_SESSION")
    if device_session.ended_at is not None or device_session.expires_at <= now:
        raise CheckpointUnavailable("DEVICE_SESSION_ENDED")
    scope = device_session.scope
    if not scope.is_current:
        raise CheckpointUnavailable("DEVICE_SCOPE_CHANGED")
    if operator_session.ended_at is not None or operator_session.expires_at <= now:
        raise CheckpointUnavailable("OPERATOR_SESSION_ENDED")
    idle = (now - operator_session.last_activity_at).total_seconds()
    if idle >= settings.ENTRY_OPERATOR_INACTIVITY_SECONDS:
        end_operator_session(operator_session=operator_session, reason="INACTIVITY", now=now)
        raise CheckpointUnavailable("OPERATOR_SESSION_ENDED")
    if not has_checkpoint_permission(
        user,
        "verify_entry",
        event_edition_id=device_session.event_edition_id,
        venue_id=device_session.gate.venue_id,
        gate_id=device_session.gate_id,
    ):
        end_operator_session(operator_session=operator_session, reason="SCOPE_LOST", now=now)
        raise CheckpointUnavailable("OPERATOR_NOT_AUTHORIZED")
    if touch:
        EntryOperatorSession.objects.filter(pk=operator_session.pk).update(last_activity_at=now)
    return Checkpoint(
        device=device,
        device_session=device_session,
        operator_session=operator_session,
        scope=scope,
        user=user,
        event_edition=device_session.event_edition,
        gate=device_session.gate,
        zone=device_session.zone,
        verification_methods=tuple(scope.verification_methods),
    )


# ---------------------------------------------------------------------------
# Ending sessions
# ---------------------------------------------------------------------------


def end_operator_session(*, operator_session: EntryOperatorSession, reason: str, now=None) -> None:
    now = now or timezone.now()
    with transaction.atomic():
        updated = EntryOperatorSession.objects.filter(
            pk=operator_session.pk, ended_at__isnull=True
        ).update(ended_at=now, end_reason=reason[:32])
        if updated:
            audit(
                action_code=action_codes.OPERATOR_SESSION_ENDED,
                actor=operator_session.user if reason == "SIGNED_OUT" else None,
                target_type="EntryOperatorSession",
                target_uuid=operator_session.pk,
                reason_code=reason,
            )
            # An operator offline grant never outlives its online session
            # (Phase 4 Prompt 2, binding decision P2-B).
            from apps.entry.services.offline_grants import revoke_operator_session_grants

            revoke_operator_session_grants(operator_session, reason=reason, now=now)


def end_operator_sessions_for_user(*, user, reason: str, now=None) -> int:
    """End every open operator session of `user` (account disabled/expired)."""
    now = now or timezone.now()
    ended = 0
    for session in EntryOperatorSession.objects.filter(user=user, ended_at__isnull=True):
        end_operator_session(operator_session=session, reason=reason, now=now)
        ended += 1
    return ended


def end_device_sessions(*, device: EntryDevice, reason: str, now=None) -> int:
    """End every open device session (and its operator sessions) of `device`."""
    now = now or timezone.now()
    ended = 0
    for device_session in EntryDeviceSession.objects.filter(device=device, ended_at__isnull=True):
        for operator_session in EntryOperatorSession.objects.filter(
            device_session=device_session, ended_at__isnull=True
        ):
            end_operator_session(operator_session=operator_session, reason=reason, now=now)
        EntryDeviceSession.objects.filter(pk=device_session.pk).update(
            ended_at=now, end_reason=reason[:32]
        )
        audit(
            action_code=action_codes.DEVICE_SESSION_ENDED,
            target_type="EntryDeviceSession",
            target_uuid=device_session.pk,
            event_edition_id=device_session.event_edition_id,
            reason_code=reason,
        )
        ended += 1
    return ended
