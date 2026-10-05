"""Localized registration confirmation delivery (AF-REG-06, Schema §13.2).

Follows the same eager, transaction-scoped delivery pattern as
`apps.accounts.otp.deliver_otp`: the `CommunicationMessage` row and its
`OutboxEvent` commit together with the domain transaction; actual delivery
happens only after commit, through the local/test email backend -- never a
real provider in Phase 1 (ADR-0009).
"""

from __future__ import annotations

import hashlib

from django.db import transaction
from django.utils import timezone

from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
    MessageTemplate,
    MessageTemplateVersion,
    MessageTemplateVersionStatus,
)
from apps.core.crypto import compute_blind_index, get_key_provider
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher
from apps.people.models import ContactPointStatus, ContactPointType

CONFIRMATION_TEMPLATE_CODE = "REGISTRATION_CONFIRMATION"


def _render(version: MessageTemplateVersion, context: dict[str, str]) -> tuple[str, str]:
    subject, body = version.subject, version.body
    for key, value in context.items():
        placeholder = "{{" + key + "}}"
        subject = subject.replace(placeholder, str(value))
        body = body.replace(placeholder, str(value))
    return subject, body


@transaction.atomic
def send_registration_confirmation(registration) -> CommunicationMessage | None:
    """Queue and (after commit) deliver the localized submission confirmation.

    Idempotent per registration: a retried call (e.g. a double-click on
    Submit that reaches this function twice) returns the existing message
    instead of raising on the unique idempotency key.
    """
    idempotency_key = f"registration-confirmation:{registration.pk}"
    existing = CommunicationMessage.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    person = registration.person
    contact = person.contact_points.filter(
        type=ContactPointType.EMAIL,
        login_enabled=True,
        is_verified=True,
        status=ContactPointStatus.ACTIVE,
    ).first()
    template = MessageTemplate.objects.filter(code=CONFIRMATION_TEMPLATE_CODE).first()
    if contact is None or template is None:
        return None

    preferred_language = registration.preferred_language or "en"
    version = (
        template.versions.filter(
            language=preferred_language, status=MessageTemplateVersionStatus.PUBLISHED
        ).first()
        or template.versions.filter(
            language="en", status=MessageTemplateVersionStatus.PUBLISHED
        ).first()
    )
    if version is None:
        return None
    # The resolved language is the version actually rendered, not merely
    # the participant's preference -- they can silently differ on fallback.
    language = version.language

    context = {
        "public_reference": registration.public_reference,
        # UX-2: the event name in the language of the version actually rendered.
        "event_name": registration.event_edition.display_name(language),
    }
    subject, body = _render(version, context)
    destination = contact.value_encrypted  # decrypted plaintext at the Python attribute level
    provider = get_key_provider()
    write_version = provider.current_blind_index_key_version()
    content_hash = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()

    message = CommunicationMessage.objects.create(
        event_edition=registration.event_edition,
        registration=registration,
        person=person,
        template_version=version,
        channel=CommunicationChannel.EMAIL,
        language=language,
        destination_encrypted=destination,
        destination_hash=compute_blind_index(destination, version=write_version, provider=provider),
        destination_hash_key_version=write_version,
        content_hash=content_hash,
        idempotency_key=idempotency_key,
        status=CommunicationMessageStatus.QUEUED,
        queued_at=timezone.now(),
    )
    PersistentOutboxPublisher().enqueue(
        OutboxMessage(
            event_type="communications.message_queued",
            aggregate_type="CommunicationMessage",
            aggregate_id=str(message.pk),
            payload={"channel": CommunicationChannel.EMAIL},
        )
    )

    def _deliver() -> None:
        """Runs after commit -- a provider failure here MUST NOT surface as an
        unhandled exception (Prompt 4 final closure pass §8): the registration
        itself already committed successfully, so a delivery failure records an
        append-only `DeliveryAttempt` and marks the message FAILED, never a 500
        response for an otherwise-successful submission."""
        from django.core.mail import send_mail

        started_at = timezone.now()
        try:
            send_mail(subject=subject, message=body, from_email=None, recipient_list=[destination])
        except Exception as exc:  # noqa: BLE001 - deliberately broad: no provider exception may propagate.
            DeliveryAttempt.objects.create(
                message=message,
                attempt_number=1,
                provider_code="LOCAL_EMAIL_BACKEND",
                status=CommunicationMessageStatus.FAILED,
                response_code=type(exc).__name__,
                started_at=started_at,
                completed_at=timezone.now(),
            )
            CommunicationMessage.objects.filter(pk=message.pk).update(
                status=CommunicationMessageStatus.FAILED
            )
            return
        DeliveryAttempt.objects.create(
            message=message,
            attempt_number=1,
            provider_code="LOCAL_EMAIL_BACKEND",
            status=CommunicationMessageStatus.SENT,
            started_at=started_at,
            completed_at=timezone.now(),
        )
        CommunicationMessage.objects.filter(pk=message.pk).update(
            status=CommunicationMessageStatus.SENT, sent_at=timezone.now()
        )

    transaction.on_commit(_deliver)
    return message
