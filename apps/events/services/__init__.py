"""Venue-structure configuration commands (Schema §5.2, ADR-0021).

Only the cross-row invariants a CHECK constraint cannot express live here:
a Zone's parent and a Gate's default Zone must belong to the same Venue.
Configuration changes are audited; there is deliberately no delete path --
codes "remain stable after operational use" (Schema §5.2), so a retired
structure is deactivated, never removed.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.events.models import EventEdition, Gate, Venue, Zone, ZoneSensitivity


class VenueConfigurationError(Exception):
    """Raised for an invalid venue, zone, or gate configuration."""


def _audit(*, action_code: str, actor, target, event_edition_id, summary: dict) -> None:
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=type(target).__name__,
            target_uuid=target.pk,
            event_edition_id=event_edition_id,
            result="SUCCESS",
            after_summary=summary,
        )
    )


@transaction.atomic
def create_venue(*, event_edition: EventEdition, code: str, name: str, actor, **extra) -> Venue:
    venue = Venue.objects.create(event_edition=event_edition, code=code, name=name, **extra)
    _audit(
        action_code=action_codes.VENUE_CREATED,
        actor=actor,
        target=venue,
        event_edition_id=event_edition.pk,
        summary={"code": code},
    )
    return venue


@transaction.atomic
def create_zone(
    *,
    venue: Venue,
    code: str,
    name: str,
    actor,
    parent: Zone | None = None,
    sensitivity: str = ZoneSensitivity.STANDARD,
    **extra,
) -> Zone:
    if parent is not None and parent.venue_id != venue.pk:
        raise VenueConfigurationError("A parent zone must belong to the same venue.")
    zone = Zone.objects.create(
        venue=venue, code=code, name=name, parent=parent, sensitivity=sensitivity, **extra
    )
    _audit(
        action_code=action_codes.ZONE_CREATED,
        actor=actor,
        target=zone,
        event_edition_id=venue.event_edition_id,
        summary={"code": code, "venue": venue.code},
    )
    return zone


@transaction.atomic
def create_gate(*, venue: Venue, code: str, name: str, default_zone: Zone, actor, **extra) -> Gate:
    if default_zone.venue_id != venue.pk:
        raise VenueConfigurationError("A gate's default zone must belong to the same venue.")
    gate = Gate.objects.create(
        venue=venue, code=code, name=name, default_zone=default_zone, **extra
    )
    _audit(
        action_code=action_codes.GATE_CREATED,
        actor=actor,
        target=gate,
        event_edition_id=venue.event_edition_id,
        summary={"code": code, "venue": venue.code, "default_zone": default_zone.code},
    )
    return gate


def zone_and_ancestor_ids(zone: Zone) -> list:
    """The zone's own id followed by every ancestor id, nearest first.

    Bounded by a visited set so a misconfigured cycle can never loop.
    """
    ids: list = []
    seen: set = set()
    current: Zone | None = zone
    while current is not None and current.pk not in seen:
        ids.append(current.pk)
        seen.add(current.pk)
        current = current.parent
    return ids


# ---------------------------------------------------------------------------
# Registration channel control (UX-4, M24, decision D-12)
# ---------------------------------------------------------------------------

#: Bounds of the operator's reason. It is stored in the audit summary, so it
#: must stay short; the form tells operators not to include personal data.
CHANNEL_REASON_MIN_LENGTH = 5
CHANNEL_REASON_MAX_LENGTH = 300


class RegistrationChannelChangeError(Exception):
    """An invalid channel change request (bad mode, window or reason)."""


class RegistrationChannelChangeConflict(Exception):
    """The event's configuration changed since the operator loaded the page."""


class RegistrationChannelChangeDenied(Exception):
    """The actor lacks `events.manage_registration_channels` for this event."""


@dataclass(frozen=True)
class RegistrationChannelChange:
    event_edition: EventEdition
    changed: bool


def _channel_summary(event: EventEdition) -> dict:
    return {
        "mode": event.public_registration_mode,
        "opens_at": event.registration_opens_at.isoformat()
        if event.registration_opens_at
        else None,
        "closes_at": (
            event.registration_closes_at.isoformat() if event.registration_closes_at else None
        ),
    }


def change_registration_channel(
    *,
    event_edition_id,
    actor,
    mode: str,
    opens_at,
    closes_at,
    reason: str,
    expected_settings_version: int,
    correlation_id: str = "",
) -> RegistrationChannelChange:
    """Open, restrict or close an event's registration channels.

    Authorization is decided here as well as in the view: the actor needs
    `events.manage_registration_channels` through a membership scoped to this
    event (or to every event), never one narrowed to an organization. The event
    row is locked with `FOR NO KEY UPDATE`, which waits for in-flight
    submissions holding `FOR SHARE` and makes every later submission see the
    new state. `expected_settings_version` rejects a change made from a
    stale page. Re-sending the current state is a no-op (idempotent) and is
    not audited again. Closure never deletes or alters a registration or a
    draft.
    """
    from apps.events.models import PublicRegistrationMode
    from apps.events.policies.registration_channels import can_manage_registration_channels

    if not can_manage_registration_channels(actor, event_edition_id):
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REGISTRATION_CHANNEL_CHANGE_DENIED,
                target_type="EventEdition",
                target_uuid=event_edition_id,
                event_edition_id=event_edition_id,
                result="DENIED",
                reason_code="events.manage_registration_channels",
                correlation_id=correlation_id,
            )
        )
        raise RegistrationChannelChangeDenied("Not permitted for this event.")
    if mode not in PublicRegistrationMode.values:
        raise RegistrationChannelChangeError("Unknown registration mode.")
    reason = " ".join((reason or "").split())
    if not CHANNEL_REASON_MIN_LENGTH <= len(reason) <= CHANNEL_REASON_MAX_LENGTH:
        raise RegistrationChannelChangeError("A reason is required.")
    if opens_at is not None and closes_at is not None and closes_at <= opens_at:
        raise RegistrationChannelChangeError("The window must close after it opens.")

    with transaction.atomic():
        event = (
            EventEdition.objects.select_for_update(no_key=True).filter(pk=event_edition_id).first()
        )
        if event is None:
            raise RegistrationChannelChangeError("Unknown event.")
        if event.settings_version != expected_settings_version:
            raise RegistrationChannelChangeConflict("The configuration changed meanwhile.")
        before = _channel_summary(event)
        event.public_registration_mode = mode
        event.registration_opens_at = opens_at
        event.registration_closes_at = closes_at
        after = _channel_summary(event)
        if after == before:
            return RegistrationChannelChange(event_edition=event, changed=False)
        event.settings_version += 1
        event.save(
            update_fields=[
                "public_registration_mode",
                "registration_opens_at",
                "registration_closes_at",
                "settings_version",
                "updated_at",
            ]
        )
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.REGISTRATION_CHANNEL_CHANGED,
                target_type="EventEdition",
                target_uuid=event.pk,
                event_edition_id=event.pk,
                result="SUCCESS",
                before_summary=before,
                after_summary={**after, "reason": reason},
                correlation_id=correlation_id,
            )
        )
    return RegistrationChannelChange(event_edition=event, changed=True)
