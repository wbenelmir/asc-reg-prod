"""Generic communications pipeline tests (Phase 2 Prompt 5 §4.1-§4.4, §7).

`transaction=True`: `queue_communication` schedules delivery via
`transaction.on_commit`, which only fires on a REAL commit -- exactly like
`apps.registrations.tests.test_confirmation`. The autouse fixture creates
its own template fresh in every test rather than depending on migration
seed data surviving a truncate-and-reflush between `transaction=True` tests
(the same documented caveat that file's own fixture explains).
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.communications.adapters.email import PERMANENT_FAILURE_MARKER, TRANSIENT_FAILURE_MARKER
from apps.communications.adapters.sms import SmsDisabledError, send_sms
from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
    MessageTemplate,
    MessageTemplateVersion,
    SuppressionEntry,
)
from apps.communications.services import (
    CommunicationIdempotencyConflictError,
    InvalidMessageStateTransitionError,
    is_terminal_status,
    queue_communication,
    render_message,
    resolve_template_version,
    transition_message_status,
)
from apps.communications.tasks import MAX_DELIVERY_ATTEMPTS, deliver_message
from apps.core.crypto import compute_blind_index
from apps.core.models import OutboxEvent, OutboxEventStatus

pytestmark = pytest.mark.django_db(transaction=True)

PURPOSE_CODE = "TEST_PURPOSE"


@pytest.fixture(autouse=True)
def _template():
    template = MessageTemplate.objects.create(
        code=PURPOSE_CODE, channel=CommunicationChannel.EMAIL, purpose_code=PURPOSE_CODE
    )
    for language in ("en", "fr"):
        MessageTemplateVersion.objects.create(
            template=template,
            language=language,
            version_label="v1",
            subject=f"Subject [{language}] {{{{public_reference}}}}",
            body=f"Body [{language}] {{{{public_reference}}}} {{{{unapproved_variable}}}}",
            allowed_variables=["public_reference"],
            status="PUBLISHED",
            effective_from=timezone.now(),
            content_hash="c" * 64,
        )
    return template


def _queue(*, destination="participant@example.com", idempotency_key="idem-1", language="en"):
    return queue_communication(
        purpose_code=PURPOSE_CODE,
        event_edition=None,
        person=None,
        language=language,
        destination=destination,
        context={"public_reference": "REF-001", "unapproved_variable": "SHOULD_NOT_LEAK"},
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Template resolution / locale fallback / bounded rendering
# ---------------------------------------------------------------------------


def test_resolve_template_version_falls_back_to_english(_template) -> None:
    version = resolve_template_version(purpose_code=PURPOSE_CODE, language="ar")
    assert version is not None
    assert version.language == "en"


def test_resolve_template_version_returns_none_without_any_match() -> None:
    assert resolve_template_version(purpose_code="NO_SUCH_PURPOSE", language="en") is None


def test_future_or_expired_template_version_is_not_selected(_template) -> None:
    from datetime import timedelta

    version = resolve_template_version(purpose_code=PURPOSE_CODE, language="en")
    MessageTemplateVersion.objects.filter(pk=version.pk).update(
        effective_until=timezone.now() - timedelta(seconds=1)
    )
    assert resolve_template_version(purpose_code=PURPOSE_CODE, language="en") is None


def test_published_template_version_cannot_be_edited(_template) -> None:
    version = resolve_template_version(purpose_code=PURPOSE_CODE, language="en")
    version.body = "Changed content"
    with pytest.raises(ValidationError):
        version.save()


def test_render_message_only_substitutes_allowed_variables(_template) -> None:
    version = resolve_template_version(purpose_code=PURPOSE_CODE, language="en")
    subject, body = render_message(
        version, {"public_reference": "REF-001", "unapproved_variable": "SHOULD_NOT_LEAK"}
    )
    assert "REF-001" in subject
    assert "REF-001" in body
    # "secret" is not in `allowed_variables` -- its context value must never
    # be substituted into the rendered message, even though it was present
    # in the context dict (Phase 2 Prompt 5 §4.1 "prevent accidental secret
    # exposure in rendered messages").
    assert "SHOULD_NOT_LEAK" not in body
    assert "{{unapproved_variable}}" in body


# ---------------------------------------------------------------------------
# Queuing: idempotency, suppression, immutable historical rendering
# ---------------------------------------------------------------------------


def test_queue_communication_is_idempotent(_template) -> None:
    first = _queue()
    second = _queue()
    assert first.pk == second.pk
    assert CommunicationMessage.objects.filter(idempotency_key="idem-1").count() == 1
    event = OutboxEvent.objects.get(aggregate_id=str(first.pk))
    assert event.status == OutboxEventStatus.PUBLISHED
    assert event.attempts == 1


def test_idempotency_key_reuse_for_different_destination_is_rejected(_template) -> None:
    _queue(destination="first@example.com", idempotency_key="idem-collision")
    with pytest.raises(CommunicationIdempotencyConflictError):
        _queue(destination="second@example.com", idempotency_key="idem-collision")


def test_published_outbox_event_replay_is_a_no_op(_template) -> None:
    from apps.communications.tasks import dispatch_communication_outbox_event

    message = _queue(idempotency_key="idem-outbox-replay")
    event = OutboxEvent.objects.get(aggregate_id=str(message.pk))
    dispatch_communication_outbox_event(str(event.pk))
    event.refresh_from_db()
    assert event.status == OutboxEventStatus.PUBLISHED
    assert event.attempts == 1
    assert DeliveryAttempt.objects.filter(message=message).count() == 1


def test_dispatch_pending_communication_events_recovers_a_crashed_dispatch(_template) -> None:
    """Phase 2 Prompt 6 §5.I "recovery of pending events": a durable outbox
    row left `PENDING` by a crash/broker outage (its Celery dispatch never
    ran, or ran but the process died before marking it `PUBLISHED`) must
    still be deliverable by the recovery sweep, not stuck forever."""
    from apps.communications.tasks import dispatch_pending_communication_events

    message = _queue(idempotency_key="idem-recovery")
    event = OutboxEvent.objects.get(aggregate_id=str(message.pk))
    # `_queue` dispatches eagerly in tests. Rewind both durable records to
    # the state left when a process commits the message/outbox rows but dies
    # before the dispatch callback reaches the provider. This makes the
    # recovery assertion prove a real delivery, not merely a terminal-message
    # no-op followed by marking the event PUBLISHED.
    CommunicationMessage.objects.filter(pk=message.pk).update(
        status=CommunicationMessageStatus.QUEUED,
        sent_at=None,
        delivered_at=None,
    )
    DeliveryAttempt.objects.filter(message=message).delete()
    mail.outbox.clear()
    OutboxEvent.objects.filter(pk=event.pk).update(status=OutboxEventStatus.PENDING, attempts=0)

    recovered_count = dispatch_pending_communication_events()

    assert recovered_count == 1
    event.refresh_from_db()
    assert event.status == OutboxEventStatus.PUBLISHED
    assert event.attempts == 1
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENT
    assert DeliveryAttempt.objects.filter(message=message).count() == 1
    assert len(mail.outbox) == 1


def test_dispatch_pending_communication_events_is_bounded_by_limit(_template) -> None:
    from apps.communications.tasks import dispatch_pending_communication_events

    for index in range(3):
        message = _queue(idempotency_key=f"idem-recovery-bound-{index}")
        OutboxEvent.objects.filter(aggregate_id=str(message.pk)).update(
            status=OutboxEventStatus.PENDING, attempts=0
        )

    recovered_count = dispatch_pending_communication_events(limit=2)

    assert recovered_count == 2
    assert (
        OutboxEvent.objects.filter(
            event_type="communications.message_queued", status=OutboxEventStatus.PENDING
        ).count()
        == 1
    )


def test_dispatch_pending_communication_events_ignores_unrelated_outbox_events(_template) -> None:
    from apps.communications.tasks import dispatch_pending_communication_events

    unrelated = OutboxEvent.objects.create(
        event_type="reviews.information_request_sent",
        aggregate_type="InformationRequest",
        aggregate_id="00000000-0000-0000-0000-000000000000",
        payload={},
        occurred_at=timezone.now(),
        status=OutboxEventStatus.PENDING,
    )

    recovered_count = dispatch_pending_communication_events()

    assert recovered_count == 0
    unrelated.refresh_from_db()
    assert unrelated.status == OutboxEventStatus.PENDING


def test_queue_communication_returns_none_without_a_published_template() -> None:
    result = queue_communication(
        purpose_code="NO_SUCH_PURPOSE",
        event_edition=None,
        person=None,
        language="en",
        destination="nobody@example.com",
        context={},
        idempotency_key="idem-missing-template",
    )
    assert result is None


def test_a_later_template_edit_never_changes_an_already_queued_message(_template) -> None:
    message = _queue()
    original_body = message.rendered_body_encrypted
    MessageTemplateVersion.objects.filter(pk=message.template_version_id).update(
        body="MUTATED {{public_reference}}"
    )
    message.refresh_from_db()
    assert message.rendered_body_encrypted == original_body
    assert "MUTATED" not in message.rendered_body_encrypted
    delivered = deliver_message(str(message.pk))
    assert "MUTATED" not in mail.outbox[-1].body
    assert delivered.status == CommunicationMessageStatus.SENT


def test_suppressed_destination_is_queued_but_never_delivered(_template) -> None:
    destination = "suppressed@example.com"
    write_version = 1
    SuppressionEntry.objects.create(
        channel=CommunicationChannel.EMAIL,
        destination_hash=compute_blind_index(destination, version=write_version),
        destination_hash_key_version=write_version,
        reason="hard_bounce",
        active_from=timezone.now(),
    )
    message = _queue(destination=destination, idempotency_key="idem-suppressed")
    assert message.status == CommunicationMessageStatus.SUPPRESSED
    assert message.suppression_reason == "destination_suppressed"
    assert DeliveryAttempt.objects.filter(message=message).count() == 0
    assert len(mail.outbox) == 0
    assert (
        OutboxEvent.objects.get(aggregate_id=str(message.pk)).status == OutboxEventStatus.PUBLISHED
    )


# ---------------------------------------------------------------------------
# Delivery: success, retryable failure, permanent failure, retry exhaustion,
# idempotent duplicate dispatch
# ---------------------------------------------------------------------------


def test_successful_delivery_transitions_to_sent_with_one_attempt(_template) -> None:
    message = _queue(idempotency_key="idem-success")
    delivered = deliver_message(str(message.pk))
    assert delivered.status == CommunicationMessageStatus.SENT
    assert delivered.sent_at is not None
    attempts = DeliveryAttempt.objects.filter(message=message)
    assert attempts.count() == 1
    assert attempts.get().attempt_number == 1
    assert attempts.get().provider_code == "LOCAL_EMAIL_BACKEND"
    assert len(mail.outbox) == 1


def test_duplicate_dispatch_of_an_already_sent_message_is_a_no_op(_template) -> None:
    message = _queue(idempotency_key="idem-duplicate")
    deliver_message(str(message.pk))
    deliver_message(str(message.pk))
    assert DeliveryAttempt.objects.filter(message=message).count() == 1
    assert len(mail.outbox) == 1


def test_duplicate_dispatch_while_sending_never_contacts_provider(_template) -> None:
    message = _queue(idempotency_key="idem-in-flight")
    CommunicationMessage.objects.filter(pk=message.pk).update(
        status=CommunicationMessageStatus.SENDING
    )
    DeliveryAttempt.objects.filter(message=message).delete()
    mail.outbox.clear()
    delivered = deliver_message(str(message.pk))
    assert delivered.status == CommunicationMessageStatus.SENDING
    assert DeliveryAttempt.objects.filter(message=message).count() == 0
    assert len(mail.outbox) == 0


def test_permanent_failure_marks_undeliverable_and_never_retries(_template) -> None:
    destination = f"someone{PERMANENT_FAILURE_MARKER}@example.com"
    message = _queue(destination=destination, idempotency_key="idem-permanent")
    delivered = deliver_message(str(message.pk))
    assert delivered.status == CommunicationMessageStatus.UNDELIVERABLE
    attempts = DeliveryAttempt.objects.filter(message=message)
    assert attempts.count() == 1
    assert attempts.get().status == CommunicationMessageStatus.UNDELIVERABLE
    # A second call must not create a second attempt -- UNDELIVERABLE is terminal.
    deliver_message(str(message.pk))
    assert DeliveryAttempt.objects.filter(message=message).count() == 1


def test_transient_failure_retries_up_to_the_bound_then_fails(_template) -> None:
    """`queue_communication` schedules `deliver_message_task` via
    `transaction.on_commit`, which (Celery eager mode, no broker/worker --
    ADR-0009) runs synchronously in-process; the task self-reschedules
    while DEFERRED, so by the time `_queue()` returns, every bounded retry
    has already happened. This asserts the resulting evidence trail rather
    than manually driving each attempt."""
    destination = f"someone{TRANSIENT_FAILURE_MARKER}@example.com"
    message = _queue(destination=destination, idempotency_key="idem-transient")
    message.refresh_from_db()
    attempts = list(DeliveryAttempt.objects.filter(message=message).order_by("attempt_number"))
    assert len(attempts) == MAX_DELIVERY_ATTEMPTS
    assert [a.attempt_number for a in attempts] == list(range(1, MAX_DELIVERY_ATTEMPTS + 1))
    assert [a.status for a in attempts[:-1]] == [CommunicationMessageStatus.DEFERRED] * (
        MAX_DELIVERY_ATTEMPTS - 1
    )
    assert attempts[-1].status == CommunicationMessageStatus.FAILED
    assert message.status == CommunicationMessageStatus.FAILED
    # Retries are BOUNDED: an explicit extra call after exhaustion is a no-op.
    deliver_message(str(message.pk))
    assert DeliveryAttempt.objects.filter(message=message).count() == MAX_DELIVERY_ATTEMPTS


# ---------------------------------------------------------------------------
# State-machine safety
# ---------------------------------------------------------------------------


def test_invalid_state_transition_is_rejected(_template) -> None:
    message = _queue(idempotency_key="idem-invalid-transition")
    with pytest.raises(InvalidMessageStateTransitionError):
        transition_message_status(message, CommunicationMessageStatus.DELIVERED)


def test_terminal_statuses_have_no_outgoing_transition() -> None:
    assert is_terminal_status(CommunicationMessageStatus.SENT)
    assert is_terminal_status(CommunicationMessageStatus.UNDELIVERABLE)
    assert not is_terminal_status(CommunicationMessageStatus.QUEUED)
    assert not is_terminal_status(CommunicationMessageStatus.DEFERRED)


# ---------------------------------------------------------------------------
# SMS: disabled by default
# ---------------------------------------------------------------------------


def test_sms_is_disabled_by_default() -> None:
    with pytest.raises(SmsDisabledError):
        send_sms(destination="+15551234567", body="test")
