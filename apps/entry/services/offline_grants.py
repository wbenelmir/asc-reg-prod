"""Operator offline grants -- the preparation boundary only (Phase 4 Prompt 2).

A grant is issued ONLINE, to a named operator who holds a live checkpoint
session on a prepared device. There is no local sign-in, and nothing here
lets a device create or extend a grant (binding decision P2-B).

Expiry = the EARLIEST of the operator session absolute expiry and the
approved maximum after this authenticated online contact (2 h Standard,
1 h Sensitive). Offline use of a grant -- its inactivity lock, operator
switching, and the actions it permits -- is enforced on the device, and
every synchronized operation is checked against the grant row again
(Phase 4 Prompt 3, `apps.entry.services.offline_sync`).

Grant schema version 2 (Phase 4 Prompt 3) binds the CHECKPOINT: the event,
the gate and the zone of the operator's checkpoint session, and the device
scope version it was opened under. The offline verifier evaluates access at
exactly that zone, and refuses a grant whose checkpoint does not match its
active package.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import EntryDeviceStatus, OfflineOperatorGrant
from apps.entry.offline_contract import GRANT_SCHEMA_VERSION, TYP_OPERATOR_GRANT, validity_for
from apps.entry.services import audit, new_public_id

#: How far back revocations are replayed to a device in its heartbeat.
REVOCATION_REPLAY_SECONDS = 24 * 60 * 60


def grant_expiry(*, operator_session, sensitivity: str, now):
    limit = now + timedelta(seconds=validity_for(sensitivity)["grant_after_contact_seconds"])
    return min(operator_session.expires_at, limit)


def issue_operator_grant(*, checkpoint, nonce, signature, body: bytes):
    """Issue a signed grant for the checkpoint's operator on its device.

    Returns `(grant_row, compact_jws)`. Requires the device to be
    OFFLINE_READY and offline to be available; any previous open grant for
    the same operator session is revoked first.
    """
    from apps.core.crypto.package_signing import get_package_signing_key_provider
    from apps.entry.policies import checkpoint_permission
    from apps.entry.services.offline_crypto import jws_sign
    from apps.entry.services.offline_devices import (
        OfflineUnavailable,
        offline_unavailability,
        verify_device_proof,
    )

    now = timezone.now()
    device = checkpoint.device
    reason = offline_unavailability(device, scope=checkpoint.scope, now=now)
    if not reason and device.status != EntryDeviceStatus.OFFLINE_READY:
        reason = "DEVICE_NOT_READY"
    if reason:
        raise OfflineUnavailable("Offline grants are not available.", code=reason)
    verify_device_proof(
        device, purpose="grant", nonce=nonce, signature=signature, body=body, now=now
    )
    sensitivity = checkpoint.scope.offline_sensitivity
    may_override = checkpoint_permission(checkpoint.user, "override_entry", checkpoint)
    expires_at = grant_expiry(
        operator_session=checkpoint.operator_session, sensitivity=sensitivity, now=now
    )
    provider = get_package_signing_key_provider()
    with transaction.atomic():
        for previous in OfflineOperatorGrant.objects.select_for_update().filter(
            operator_session=checkpoint.operator_session, revoked_at__isnull=True
        ):
            _revoke(previous, reason="REISSUED", now=now)
        grant = OfflineOperatorGrant.objects.create(
            public_id=new_public_id(),
            device=device,
            device_session=checkpoint.device_session,
            operator_session=checkpoint.operator_session,
            user=checkpoint.user,
            sensitivity=sensitivity,
            may_override=may_override,
            signing_key_id=provider.current_signing_key_id(),
            issued_at=now,
            expires_at=expires_at,
        )
        permissions = ["VERIFY_OFFLINE_QR"] + (["OVERRIDE_OFFLINE"] if may_override else [])
        compact, _kid = jws_sign(
            {
                "typ": TYP_OPERATOR_GRANT,
                "schema_version": GRANT_SCHEMA_VERSION,
                "grant_id": grant.public_id,
                "device": device.public_id,
                "event": checkpoint.event_edition.code,
                "gate": checkpoint.gate.code,
                "zone": checkpoint.zone.code,
                "scope_version": checkpoint.scope.scope_version,
                "display_name": checkpoint.user.display_name,
                "permissions": permissions,
                "sensitivity": sensitivity,
                "issued_at": int(now.timestamp()),
                "expires_at": int(expires_at.timestamp()),
            },
            typ=TYP_OPERATOR_GRANT,
            provider=provider,
        )
        audit(
            action_code=action_codes.OFFLINE_GRANT_ISSUED,
            actor=checkpoint.user,
            target_type="OfflineOperatorGrant",
            target_uuid=grant.pk,
            event_edition_id=device.event_edition_id,
            after_summary={
                "device": device.public_id,
                "grant": grant.public_id,
                "expires_at": expires_at.isoformat(),
                "may_override": may_override,
            },
        )
    return grant, compact


def _revoke(grant: OfflineOperatorGrant, *, reason: str, now) -> None:
    grant.revoked_at = now
    grant.revoke_reason = reason[:32]
    grant.save(update_fields=["revoked_at", "revoke_reason"])
    audit(
        action_code=action_codes.OFFLINE_GRANT_REVOKED,
        target_type="OfflineOperatorGrant",
        target_uuid=grant.pk,
        event_edition_id=grant.device.event_edition_id,
        reason_code=reason,
        after_summary={"grant": grant.public_id},
    )


def revoke_operator_session_grants(operator_session, *, reason: str, now=None) -> int:
    now = now or timezone.now()
    revoked = 0
    with transaction.atomic():
        for grant in OfflineOperatorGrant.objects.select_for_update().filter(
            operator_session=operator_session, revoked_at__isnull=True
        ):
            _revoke(grant, reason=reason, now=now)
            revoked += 1
    return revoked


def revoke_device_grants(device, *, reason: str, now=None) -> int:
    now = now or timezone.now()
    revoked = 0
    for grant in OfflineOperatorGrant.objects.select_for_update().filter(
        device=device, revoked_at__isnull=True, expires_at__gt=now
    ):
        _revoke(grant, reason=reason, now=now)
        revoked += 1
    return revoked


def recently_revoked_grant_ids(device, *, now) -> list[str]:
    """Grant ids revoked recently (still within their own validity), so a
    device that stored them drops them at its next contact."""
    since = now - timedelta(seconds=REVOCATION_REPLAY_SECONDS)
    return sorted(
        OfflineOperatorGrant.objects.filter(
            device=device, revoked_at__gte=since, expires_at__gt=now
        ).values_list("public_id", flat=True)
    )
