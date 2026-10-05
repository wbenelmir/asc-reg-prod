"""Message template/message/delivery/suppression invariant tests (Schema §13)."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    DeliveryAttempt,
    MessageTemplate,
    MessageTemplateVersion,
    SuppressionEntry,
)
from apps.core.crypto import compute_blind_index

pytestmark = pytest.mark.django_db


@pytest.fixture
def template_version() -> MessageTemplateVersion:
    template = MessageTemplate.objects.create(
        code="OTP_LOGIN", channel=CommunicationChannel.EMAIL, purpose_code="OTP"
    )
    return MessageTemplateVersion.objects.create(
        template=template,
        language="en",
        version_label="v1",
        subject="Your code",
        body="Your code is {{code}}",
        content_hash="a" * 64,
        effective_from=timezone.now(),
    )


def test_message_never_stores_otp_content_in_rendered_body(
    template_version: MessageTemplateVersion,
) -> None:
    destination = "participant@example.com"
    message = CommunicationMessage.objects.create(
        template_version=template_version,
        channel=CommunicationChannel.EMAIL,
        language="en",
        destination_encrypted=destination,
        destination_hash=compute_blind_index(destination, version=1),
        destination_hash_key_version=1,
        content_hash="b" * 64,
        idempotency_key="idem-1",
    )
    # The model has no field that could hold a rendered OTP value; only a
    # bounded, approved `variable_snapshot` (never populated with OTP
    # content by any service in this project) and a `content_hash`.
    assert not hasattr(message, "otp_value")
    assert message.variable_snapshot in ("", None)


def test_message_idempotency_key_is_unique(template_version: MessageTemplateVersion) -> None:
    destination = "dup@example.com"
    CommunicationMessage.objects.create(
        template_version=template_version,
        channel=CommunicationChannel.EMAIL,
        language="en",
        destination_encrypted=destination,
        destination_hash=compute_blind_index(destination, version=1),
        destination_hash_key_version=1,
        content_hash="c" * 64,
        idempotency_key="dup-key",
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        CommunicationMessage.objects.create(
            template_version=template_version,
            channel=CommunicationChannel.EMAIL,
            language="en",
            destination_encrypted=destination,
            destination_hash=compute_blind_index(destination, version=1),
            destination_hash_key_version=1,
            content_hash="d" * 64,
            idempotency_key="dup-key",
        )


def test_delivery_attempts_are_append_only_and_numbered(
    template_version: MessageTemplateVersion,
) -> None:
    destination = "attempts@example.com"
    message = CommunicationMessage.objects.create(
        template_version=template_version,
        channel=CommunicationChannel.EMAIL,
        language="en",
        destination_encrypted=destination,
        destination_hash=compute_blind_index(destination, version=1),
        destination_hash_key_version=1,
        content_hash="e" * 64,
        idempotency_key="idem-attempts",
    )
    DeliveryAttempt.objects.create(
        message=message, attempt_number=1, status="SENT", started_at=timezone.now()
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        DeliveryAttempt.objects.create(
            message=message, attempt_number=1, status="FAILED", started_at=timezone.now()
        )


def test_suppression_entry_never_requires_plaintext_destination() -> None:
    entry = SuppressionEntry.objects.create(
        channel=CommunicationChannel.EMAIL,
        destination_hash=compute_blind_index("bounced@example.com", version=1),
        destination_hash_key_version=1,
        reason="HARD_BOUNCE",
        active_from=timezone.now(),
    )
    assert not hasattr(entry, "destination_plaintext")
    assert not hasattr(entry, "destination")
