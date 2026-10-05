"""P4-4 finding P44-F05: a DEFERRED message whose scheduled retry was lost is
attempted again by a bounded, idempotent sweep; SENDING is never re-sent.

Before P4-4 nothing recovered such a message: it stayed DEFERRED forever.
Synthetic data only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.communications.adapters.email import TRANSIENT_FAILURE_MARKER
from apps.communications.models import (
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
)
from apps.communications.tasks import (
    MAX_DELIVERY_ATTEMPTS,
    OVERDUE_RETRY_GRACE_SECONDS,
    redeliver_overdue_deferred_messages,
)
from apps.communications.tests.test_messaging_pipeline import _queue, _template  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

DEFERRED = CommunicationMessageStatus.DEFERRED


def _as_deferred(key: str, *, retry_in_seconds: int, status=DEFERRED, attempts: int = 1):
    """A message whose last attempt was deferred, as if its countdown was lost."""
    message = _queue(idempotency_key=key)
    DeliveryAttempt.objects.filter(message=message).delete()
    CommunicationMessage.objects.filter(pk=message.pk).update(status=status)
    now = timezone.now()
    for number in range(1, attempts + 1):
        DeliveryAttempt.objects.create(
            message=message,
            attempt_number=number,
            provider_code="LOCAL_EMAIL_BACKEND",
            status=DEFERRED,
            response_code="TRANSIENT",
            started_at=now - timedelta(minutes=10),
            completed_at=now - timedelta(minutes=10),
            next_retry_at=now + timedelta(seconds=retry_in_seconds),
        )
    return message


def test_an_overdue_deferred_message_is_attempted_again_once():
    message = _as_deferred("p44-overdue", retry_in_seconds=-(OVERDUE_RETRY_GRACE_SECONDS + 60))
    mail.outbox.clear()

    assert redeliver_overdue_deferred_messages() == 1
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENT
    assert list(
        DeliveryAttempt.objects.filter(message=message)
        .order_by("attempt_number")
        .values_list("attempt_number", flat=True)
    ) == [1, 2]
    assert len(mail.outbox) == 1
    # Idempotent: a second sweep finds nothing and sends nothing.
    assert redeliver_overdue_deferred_messages() == 0
    assert len(mail.outbox) == 1


def test_a_retry_that_is_due_but_within_the_grace_is_left_to_its_countdown():
    message = _as_deferred("p44-due", retry_in_seconds=-(OVERDUE_RETRY_GRACE_SECONDS // 2))
    mail.outbox.clear()
    assert redeliver_overdue_deferred_messages() == 0
    message.refresh_from_db()
    assert message.status == DEFERRED
    assert len(mail.outbox) == 0


def test_a_message_in_sending_is_never_re_sent():
    message = _as_deferred(
        "p44-sending",
        retry_in_seconds=-3600,
        status=CommunicationMessageStatus.SENDING,
    )
    mail.outbox.clear()
    assert redeliver_overdue_deferred_messages() == 0
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENDING
    assert len(mail.outbox) == 0


def test_the_attempt_bound_still_holds():
    message = _as_deferred("p44-last", retry_in_seconds=-3600, attempts=MAX_DELIVERY_ATTEMPTS - 1)
    message.destination_encrypted = f"someone{TRANSIENT_FAILURE_MARKER}@example.com"
    message.save(update_fields=["destination_encrypted"])
    assert redeliver_overdue_deferred_messages() == 1
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.FAILED
    assert DeliveryAttempt.objects.filter(message=message).count() == MAX_DELIVERY_ATTEMPTS
    assert redeliver_overdue_deferred_messages() == 0


def test_the_sweep_is_bounded():
    for index in range(3):
        _as_deferred(f"p44-batch-{index}", retry_in_seconds=-3600)
    assert redeliver_overdue_deferred_messages(limit=2) == 2
    assert redeliver_overdue_deferred_messages(limit=2) == 1
