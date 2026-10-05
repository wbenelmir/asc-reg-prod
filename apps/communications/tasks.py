"""Idempotent, retry-safe Celery tasks for apps.communications (Phase 2
Prompt 5 §4.3).

`deliver_message` performs exactly ONE delivery attempt for one
`CommunicationMessage` and is safe to call directly in tests, from a
management command, or from the Celery task below. It is idempotent:
calling it again for a message already in a terminal status
(`is_terminal_status`) is a no-op that returns the message unchanged, so a
redelivered/duplicated Celery task execution can never create a second
logical delivery.

`deliver_message_task` is the Celery entry point `queue_communication`
schedules via `transaction.on_commit`. Retry is bounded
(`MAX_DELIVERY_ATTEMPTS`) and handled explicitly in `deliver_message`
itself (by attempt-number comparison), not via Celery's own
`self.retry(...)` machinery -- this keeps retry behavior identical and
directly testable whether Celery is running eagerly (local/test, no
broker, `ADR-0009`) or, in a real deployment, against a real broker,
without depending on Celery's countdown/backoff timing in either case.

Only a failure the provider confirmed is retried. When the adapter
reports that the provider may have accepted the message
(`DeliveryOutcome.acceptance_unknown`), the attempt is recorded as
unconfirmed (attempt status SENDING, response code `UNCONFIRMED:<error>`)
and the message stays in SENDING. Nothing sends a SENDING message again:
not this function, not a duplicate task and not a sweep (open decision
COMM-01). A message left in SENDING with no attempt is the other case of
the same state: a worker stopped during the provider call.

The local environment does not prove a real Redis/Celery broker
integration (`CELERY_TASK_ALWAYS_EAGER=True` locally and in tests, no
worker process) -- this task is exercised only in eager, synchronous,
in-process mode.
"""

from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.communications.adapters import get_email_adapter
from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
)
from apps.core.models import OutboxEvent, OutboxEventStatus

from .services.messaging import is_terminal_status, transition_message_status

MAX_DELIVERY_ATTEMPTS = 3
COMMUNICATION_OUTBOX_EVENT_TYPE = "communications.message_queued"
#: Response-code prefix of an attempt whose outcome the provider never confirmed.
UNCONFIRMED_RESPONSE_PREFIX = "UNCONFIRMED:"


def deliver_message(message_id: str) -> CommunicationMessage:
    """Perform one delivery attempt for `message_id`. Returns the
    (possibly unchanged) `CommunicationMessage`. Never raises for an
    ordinary provider failure -- only for a genuinely missing message id,
    which indicates a programming error, not an operational condition.
    """
    with transaction.atomic():
        message = CommunicationMessage.objects.select_for_update().get(pk=message_id)
        if is_terminal_status(message.status):
            return message
        if message.status == CommunicationMessageStatus.SENDING:
            # Another worker already owns the in-flight provider call. A
            # duplicate task must not contact the provider a second time.
            return message
        attempt_number = message.attempts.count() + 1
        if DeliveryAttempt.objects.filter(message=message, attempt_number=attempt_number).exists():
            # A duplicate task execution raced this one and already recorded
            # this attempt -- never double-record (Phase 2 Prompt 5 §4.2/§4.3
            # "duplicate task execution does not generate duplicate logical
            # delivery").
            return CommunicationMessage.objects.get(pk=message.pk)
        if message.status in (
            CommunicationMessageStatus.QUEUED,
            CommunicationMessageStatus.DEFERRED,
        ):
            transition_message_status(message, CommunicationMessageStatus.SENDING)

    destination = message.destination_encrypted  # decrypted plaintext at the Python attribute level
    started_at = timezone.now()

    if message.channel == CommunicationChannel.EMAIL:
        provider_code = "LOCAL_EMAIL_BACKEND"
        outcome = get_email_adapter().send(
            destination=destination,
            subject=message.rendered_subject,
            body=message.rendered_body_encrypted,
        )
    else:  # pragma: no cover - SMS disabled by default (§4.4); never reached locally.
        provider_code = "SMS_DISABLED"
        from apps.communications.adapters import SmsDisabledError, send_sms

        try:
            outcome = send_sms(destination=destination, body=message.rendered_body_encrypted)
        except SmsDisabledError as exc:
            from apps.communications.adapters import DeliveryOutcome

            outcome = DeliveryOutcome(ok=False, permanent=True, response_code=type(exc).__name__)

    completed_at = timezone.now()
    with transaction.atomic():
        message = CommunicationMessage.objects.select_for_update().get(pk=message.pk)
        if is_terminal_status(message.status):
            return message
        if DeliveryAttempt.objects.filter(message=message, attempt_number=attempt_number).exists():
            return message

        if outcome.ok:
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=attempt_number,
                provider_code=provider_code,
                provider_reference=outcome.provider_reference[:200],
                status=CommunicationMessageStatus.SENT,
                started_at=started_at,
                completed_at=completed_at,
            )
            transition_message_status(
                message, CommunicationMessageStatus.SENT, sent_at=completed_at
            )
        elif outcome.acceptance_unknown:
            # The provider may have accepted the message. Record the attempt
            # and keep the message in SENDING, which nothing re-sends; even on
            # the last allowed attempt it is not marked FAILED, because "not
            # delivered" is not known (COMM-01).
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=attempt_number,
                provider_code=provider_code,
                status=CommunicationMessageStatus.SENDING,
                response_code=f"{UNCONFIRMED_RESPONSE_PREFIX}{outcome.response_code}"[:100],
                started_at=started_at,
                completed_at=completed_at,
            )
        elif outcome.permanent:
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=attempt_number,
                provider_code=provider_code,
                status=CommunicationMessageStatus.UNDELIVERABLE,
                response_code=outcome.response_code[:100],
                started_at=started_at,
                completed_at=completed_at,
            )
            transition_message_status(message, CommunicationMessageStatus.UNDELIVERABLE)
        elif attempt_number >= MAX_DELIVERY_ATTEMPTS:
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=attempt_number,
                provider_code=provider_code,
                status=CommunicationMessageStatus.FAILED,
                response_code=outcome.response_code[:100],
                started_at=started_at,
                completed_at=completed_at,
            )
            transition_message_status(message, CommunicationMessageStatus.FAILED)
        else:
            next_retry_at = timezone.now() + timedelta(seconds=30)
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=attempt_number,
                provider_code=provider_code,
                status=CommunicationMessageStatus.DEFERRED,
                response_code=outcome.response_code[:100],
                started_at=started_at,
                completed_at=completed_at,
                next_retry_at=next_retry_at,
            )
            transition_message_status(message, CommunicationMessageStatus.DEFERRED)
    return message


@shared_task(bind=True, max_retries=MAX_DELIVERY_ATTEMPTS)
def deliver_message_task(self, message_id: str) -> None:
    """Celery entry point: one attempt, then self-reschedule while the
    message remains `DEFERRED` (transient failure) up to
    `MAX_DELIVERY_ATTEMPTS` (`deliver_message` itself enforces the bound,
    so this never loops beyond it even under Celery eager-mode retries).
    """
    message = deliver_message(str(message_id))
    if message.status == CommunicationMessageStatus.DEFERRED:
        deliver_message_task.apply_async(args=[str(message_id)], countdown=30)


def dispatch_communication_outbox_event(event_id: str) -> OutboxEvent:
    """Idempotently dispatch one durable communication outbox event."""
    with transaction.atomic():
        event = OutboxEvent.objects.select_for_update().get(pk=event_id)
        if event.status == OutboxEventStatus.PUBLISHED:
            return event
        if (
            event.event_type != COMMUNICATION_OUTBOX_EVENT_TYPE
            or event.aggregate_type != "CommunicationMessage"
        ):
            event.status = OutboxEventStatus.FAILED
            event.attempts += 1
            event.last_error = "UnsupportedCommunicationOutboxEvent"
            event.save(update_fields=["status", "attempts", "last_error"])
            return event

        try:
            deliver_message_task.delay(event.aggregate_id)
        except Exception as exc:  # noqa: BLE001 - retain durable recovery evidence.
            event.attempts += 1
            event.last_error = type(exc).__name__
            event.save(update_fields=["attempts", "last_error"])
            return event

        event.status = OutboxEventStatus.PUBLISHED
        event.attempts += 1
        event.last_error = ""
        event.published_at = timezone.now()
        event.save(update_fields=["status", "attempts", "last_error", "published_at"])
        return event


#: P4-4: a DEFERRED message whose retry is this long overdue has lost its
#: scheduled task (a broker restart, an expired visibility timeout). The
#: normal path is still the countdown in `deliver_message_task`.
OVERDUE_RETRY_GRACE_SECONDS = 120


@shared_task(name="communications.redeliver_overdue_deferred_messages")
def redeliver_overdue_deferred_messages(limit: int = 100) -> int:
    """Attempt again a bounded batch of DEFERRED messages whose retry never ran.

    Idempotent and retry-safe: `deliver_message` locks the row, records at
    most one attempt per attempt number and keeps the `MAX_DELIVERY_ATTEMPTS`
    bound, so a sweep racing the countdown task cannot deliver twice. Only
    DEFERRED messages, whose last attempt the provider confirmed as not
    accepted, are selected. A message in SENDING is never touched here: its
    provider call was interrupted or ended without confirmation and may have
    succeeded, and re-sending it automatically is an owner decision (COMM-01).
    """
    from django.db.models import OuterRef, Subquery

    bounded_limit = max(1, min(limit, 1000))
    cutoff = timezone.now() - timedelta(seconds=OVERDUE_RETRY_GRACE_SECONDS)
    latest_retry = (
        DeliveryAttempt.objects.filter(message=OuterRef("pk"))
        .order_by("-attempt_number")
        .values("next_retry_at")[:1]
    )
    message_ids = list(
        CommunicationMessage.objects.filter(status=CommunicationMessageStatus.DEFERRED)
        .annotate(latest_retry_at=Subquery(latest_retry))
        .filter(latest_retry_at__lte=cutoff)
        .order_by("latest_retry_at")
        .values_list("pk", flat=True)[:bounded_limit]
    )
    for message_id in message_ids:
        deliver_message(str(message_id))
    return len(message_ids)


@shared_task
def dispatch_communication_outbox_event_task(event_id: str) -> None:
    dispatch_communication_outbox_event(event_id)


@shared_task
def dispatch_pending_communication_events(limit: int = 100) -> int:
    """Recover a bounded batch left pending by a crash or broker outage."""
    bounded_limit = max(1, min(limit, 1000))
    event_ids = list(
        OutboxEvent.objects.filter(
            status=OutboxEventStatus.PENDING,
            event_type=COMMUNICATION_OUTBOX_EVENT_TYPE,
            aggregate_type="CommunicationMessage",
        )
        .order_by("occurred_at")
        .values_list("pk", flat=True)[:bounded_limit]
    )
    for event_id in event_ids:
        dispatch_communication_outbox_event(str(event_id))
    return len(event_ids)
