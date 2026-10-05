"""Registration channel policy (UX-4, M24, decision D-12).

Pure decisions, no writes. The effective public state combines the event's
manual `public_registration_mode` with its registration window; the most
restrictive of the two wins:

* `PUBLIC_OPEN`: the mode is OPEN and the time is inside the window
  (`registration_opens_at <= now < registration_closes_at`, each bound
  applying only when it is set). Every channel is accepted.
* `INVITATION_ONLY`: the mode is INVITATION_ONLY, or the mode is OPEN and the
  time is outside the window. Public sign-up is refused; invitation,
  delegation and on-behalf registrations continue under their own validity,
  capacity and expiry rules.
* `CLOSED`: the mode is CLOSED. No participant-initiated draft creation,
  claim or submission on any channel, and no staff-created draft.

Timestamps are aware and compared in UTC. The event timezone matters only for
how an operator enters and reads the window (`apps.events.forms`).

Whether an action may run is decided per registration source: a public
(`OPEN`-source) draft needs `PUBLIC_OPEN`; an invitation, delegation or
on-behalf draft needs anything but `CLOSED`. The source is server-held on the
Registration, so no field, query parameter or session value can move a public
draft onto the invitation rules.

Callers that write (draft creation, claim, final submission) re-evaluate
this policy inside their transaction while holding a lock on the event row,
so a closure that commits first always wins against a concurrent submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db import connection
from django.utils import timezone

from apps.events.models import EventEdition, PublicRegistrationMode


class ChannelState:
    PUBLIC_OPEN = "PUBLIC_OPEN"
    INVITATION_ONLY = "INVITATION_ONLY"
    CLOSED = "CLOSED"


#: Registration source kinds that follow the invitation rules. Values of
#: `apps.registrations.models.RegistrationSourceKind`, repeated here to keep
#: this policy free of an import cycle.
RESTRICTED_SOURCE_KINDS = frozenset({"INVITATION", "ON_BEHALF", "DELEGATION"})
PUBLIC_SOURCE_KIND = "OPEN"


class RegistrationChannelClosed(Exception):
    """A participant or staff action that the event's channel state refuses.

    Carries only the stable decision; never a participant identifier."""

    def __init__(self, decision: ChannelDecision):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class ChannelDecision:
    allowed: bool
    state: str
    #: A stable, non-sensitive code for audit and tests: "OK",
    #: "PUBLIC_REGISTRATION_CLOSED" or "REGISTRATION_CLOSED".
    reason: str


def _within_window(event: EventEdition, now: datetime) -> bool:
    if event.registration_opens_at is not None and now < event.registration_opens_at:
        return False
    if event.registration_closes_at is not None and now >= event.registration_closes_at:
        return False
    return True


def effective_channel_state(event: EventEdition, *, now: datetime | None = None) -> str:
    """The effective public registration state of `event` at `now`."""
    now = now or timezone.now()
    if event.public_registration_mode == PublicRegistrationMode.CLOSED:
        return ChannelState.CLOSED
    if event.public_registration_mode == PublicRegistrationMode.INVITATION_ONLY:
        return ChannelState.INVITATION_ONLY
    if event.public_registration_mode == PublicRegistrationMode.OPEN and _within_window(event, now):
        return ChannelState.PUBLIC_OPEN
    if event.public_registration_mode == PublicRegistrationMode.OPEN:
        return ChannelState.INVITATION_ONLY
    # An unknown stored value (a forged or future code) fails closed.
    return ChannelState.CLOSED


def decide_for_source(
    event: EventEdition, source_kind: str, *, now: datetime | None = None
) -> ChannelDecision:
    """May a participant create, continue, claim or submit a registration of
    `source_kind` for `event` at `now`?"""
    state = effective_channel_state(event, now=now)
    if state == ChannelState.CLOSED:
        return ChannelDecision(False, state, "REGISTRATION_CLOSED")
    if source_kind in RESTRICTED_SOURCE_KINDS:
        return ChannelDecision(True, state, "OK")
    if source_kind == PUBLIC_SOURCE_KIND and state == ChannelState.PUBLIC_OPEN:
        return ChannelDecision(True, state, "OK")
    # A public draft outside the open window, or an unknown source kind.
    return ChannelDecision(False, state, "PUBLIC_REGISTRATION_CLOSED")


def decide_for_registration(registration, *, now: datetime | None = None) -> ChannelDecision:
    return decide_for_source(registration.event_edition, registration.source_kind, now=now)


def public_registration_is_open(event: EventEdition, *, now: datetime | None = None) -> bool:
    return effective_channel_state(event, now=now) == ChannelState.PUBLIC_OPEN


def staff_draft_creation_allowed(event: EventEdition, *, now: datetime | None = None) -> bool:
    """On-behalf creation and delegation apply by authorized staff: refused
    only while the event is CLOSED (D-12 recommended option)."""
    return effective_channel_state(event, now=now) != ChannelState.CLOSED


def lock_event_for_channel_read(event_id) -> EventEdition:
    """Read the event's channel fields under `FOR SHARE` inside the caller's
    transaction.

    `FOR SHARE` lets concurrent submissions proceed together while blocking a
    channel change (which takes `FOR UPDATE`) until they commit, and makes a
    submission that starts after a committed change see that change. Must be
    called inside `transaction.atomic()`.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM events_event_edition WHERE id = %s FOR SHARE", [event_id])
    return EventEdition.objects.get(pk=event_id)


def can_manage_registration_channels(user, event_edition_id) -> bool:
    """Authorization for `change_registration_channel` (UX-4, S-17).

    Same rule as `apps.events.selectors.events_with_channel_control`: the
    permission must come from a membership that is not narrowed to an
    organization, venue or gate.
    """
    from apps.events.selectors import events_with_channel_control

    return events_with_channel_control(user).filter(pk=event_edition_id).exists()
