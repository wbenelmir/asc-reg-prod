"""Generic, purpose-agnostic outbound-message queuing (Phase 2 Prompt 5 §4.2/§4.3).

`queue_communication` is the single shared entry point every Prompt 5
communication purpose (information request, response confirmation,
decision/status, account access) funnels through: it resolves a template
version, renders it inside a bounded context, creates the
`CommunicationMessage` row, enqueues the matching `OutboxEvent` in the same
transaction, and schedules delivery through the Celery outbox consumer
(`apps.communications.tasks.deliver_message_task`) via `transaction.on_commit`
-- never before the domain transaction actually commits.

Idempotent by construction: `idempotency_key` carries a DB-level unique
constraint (`CommunicationMessage.idempotency_key`), so a retried call
(e.g. a duplicate outbox replay) returns the existing message rather than
creating a second logical delivery, mirroring
`apps.registrations.confirmation.send_registration_confirmation` and
`apps.invitations.services`'s delegation-claim delivery.
"""

from __future__ import annotations

import hashlib
import logging
from functools import partial

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    CommunicationMessageStatus,
    SuppressionEntry,
)
from apps.core.crypto import compute_blind_index, get_key_provider
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher

from .rendering import render_message, resolve_template_version

logger = logging.getLogger("asc2026.communications")

_TERMINAL_STATUSES = frozenset(
    {
        CommunicationMessageStatus.SENT,
        CommunicationMessageStatus.DELIVERED,
        CommunicationMessageStatus.FAILED,
        CommunicationMessageStatus.BOUNCED,
        CommunicationMessageStatus.UNDELIVERABLE,
        CommunicationMessageStatus.SUPPRESSED,
        CommunicationMessageStatus.CANCELLED,
    }
)

# Every status a message may legally move TO from its current status. A
# terminal status (see above) has no entry -- and therefore no allowed
# outgoing transition at all.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CommunicationMessageStatus.QUEUED: {
        CommunicationMessageStatus.SENDING,
        CommunicationMessageStatus.CANCELLED,
    },
    CommunicationMessageStatus.SENDING: {
        CommunicationMessageStatus.SENT,
        CommunicationMessageStatus.DEFERRED,
        CommunicationMessageStatus.FAILED,
        CommunicationMessageStatus.UNDELIVERABLE,
        CommunicationMessageStatus.BOUNCED,
    },
    CommunicationMessageStatus.DEFERRED: {
        CommunicationMessageStatus.SENDING,
        CommunicationMessageStatus.FAILED,
        CommunicationMessageStatus.CANCELLED,
    },
    CommunicationMessageStatus.SENT: {
        CommunicationMessageStatus.DELIVERED,
        CommunicationMessageStatus.BOUNCED,
    },
}


class InvalidMessageStateTransitionError(Exception):
    """Raised when a `CommunicationMessage` status transition is not allowed."""


class CommunicationIdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused for different message content."""


def _assert_same_logical_message(
    message: CommunicationMessage,
    *,
    version,
    channel: str,
    event_edition,
    registration,
    person,
    destination_hash: str,
    content_hash: str,
) -> None:
    if (
        message.template_version_id != version.pk
        or message.channel != channel
        or message.event_edition_id != getattr(event_edition, "pk", None)
        or message.registration_id != getattr(registration, "pk", None)
        or message.person_id != getattr(person, "pk", None)
        or message.destination_hash != destination_hash
        or message.content_hash != content_hash
    ):
        raise CommunicationIdempotencyConflictError(
            "The idempotency key is already bound to a different communication."
        )


def is_terminal_status(status: str) -> bool:
    return status in _TERMINAL_STATUSES


def transition_message_status(
    message: CommunicationMessage, new_status: str, **extra_fields: object
) -> CommunicationMessage:
    """Move `message` from its CURRENT persisted status to `new_status` with a
    single conditional `UPDATE ... WHERE status = <old>` (never a blind
    `.update()`), so two concurrent delivery attempts for the same message
    can never both "win" and silently clobber each other's evidence."""
    allowed = _ALLOWED_TRANSITIONS.get(message.status, set())
    if new_status not in allowed:
        raise InvalidMessageStateTransitionError(
            f"CommunicationMessage cannot move from {message.status} to {new_status}."
        )
    updated = CommunicationMessage.objects.filter(pk=message.pk, status=message.status).update(
        status=new_status, **extra_fields
    )
    if updated == 0:
        raise InvalidMessageStateTransitionError(
            "CommunicationMessage status changed concurrently; refusing a stale transition."
        )
    message.status = new_status
    for field_name, value in extra_fields.items():
        setattr(message, field_name, value)
    return message


def _is_suppressed(destination_hash: str, *, channel: str) -> bool:
    now = timezone.now()
    return (
        SuppressionEntry.objects.filter(channel=channel, destination_hash=destination_hash)
        .filter(active_from__lte=now)
        .filter(Q(active_until__isnull=True) | Q(active_until__gte=now))
        .exists()
    )


def queue_communication(
    *,
    purpose_code: str,
    event_edition,
    person,
    language: str,
    destination: str,
    context: dict[str, object],
    idempotency_key: str,
    registration=None,
    channel: str = CommunicationChannel.EMAIL,
) -> CommunicationMessage | None:
    """Queue one outbound message for `purpose_code`, or return the existing
    one for an already-used `idempotency_key`. Returns `None` (never raises)
    when no PUBLISHED template version can be resolved -- a missing template
    is a configuration gap, not a caller error, and must never turn an
    otherwise-successful domain action into a failed one.
    """
    version = resolve_template_version(
        purpose_code=purpose_code, language=language, channel=channel
    )
    if version is None:
        return None

    subject, body = render_message(version, context)
    provider = get_key_provider()
    write_version = provider.current_blind_index_key_version()
    destination_hash = compute_blind_index(destination, version=write_version, provider=provider)
    content_hash = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()
    suppressed = _is_suppressed(destination_hash, channel=channel)
    existing = CommunicationMessage.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        _assert_same_logical_message(
            existing,
            version=version,
            channel=channel,
            event_edition=event_edition,
            registration=registration,
            person=person,
            destination_hash=destination_hash,
            content_hash=content_hash,
        )
        return existing

    with transaction.atomic():
        try:
            with transaction.atomic():
                message = CommunicationMessage.objects.create(
                    event_edition=event_edition,
                    registration=registration,
                    person=person,
                    template_version=version,
                    channel=channel,
                    language=version.language,
                    destination_encrypted=destination,
                    destination_hash=destination_hash,
                    destination_hash_key_version=write_version,
                    content_hash=content_hash,
                    idempotency_key=idempotency_key,
                    rendered_subject=subject[:300],
                    rendered_body_encrypted=body,
                    status=(
                        CommunicationMessageStatus.SUPPRESSED
                        if suppressed
                        else CommunicationMessageStatus.QUEUED
                    ),
                    suppression_reason="destination_suppressed" if suppressed else "",
                    queued_at=timezone.now(),
                )
        except IntegrityError:
            winner = CommunicationMessage.objects.get(idempotency_key=idempotency_key)
            _assert_same_logical_message(
                winner,
                version=version,
                channel=channel,
                event_edition=event_edition,
                registration=registration,
                person=person,
                destination_hash=destination_hash,
                content_hash=content_hash,
            )
            return winner

        outbox_message = OutboxMessage(
            event_type="communications.message_queued",
            aggregate_type="CommunicationMessage",
            aggregate_id=str(message.pk),
            payload={"channel": channel, "purpose_code": purpose_code},
        )
        PersistentOutboxPublisher().enqueue(outbox_message)
        # IDV-2 (ASYNC-01): robust, so a broker outage after commit never turns
        # an already-saved action into an error; the PENDING outbox event is
        # re-sent by `dispatch_pending_communication_events` (beat).
        transaction.on_commit(
            partial(_dispatch_after_commit, str(outbox_message.event_uuid)), robust=True
        )
    return message


def _dispatch_after_commit(event_id: str) -> None:
    """Enqueue one communication outbox event; never raises. Logs the error
    class only, never the destination or the message."""
    from apps.communications.tasks import dispatch_communication_outbox_event_task

    try:
        dispatch_communication_outbox_event_task.delay(event_id)
    except Exception as exc:  # noqa: BLE001 - the committed action must not fail here
        logger.warning(
            "communication dispatch deferred to the outbox sweeper",
            extra={"error_class": type(exc).__name__},
        )
