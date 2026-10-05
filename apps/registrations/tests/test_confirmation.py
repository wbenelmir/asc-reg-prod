"""Localized confirmation delivery tests (AF-REG-06, Schema §13.2)."""

from __future__ import annotations

import pytest
from django.core import mail
from django.utils import timezone

from apps.communications.models import (
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
    MessageTemplate,
    MessageTemplateVersion,
)
from apps.core.models import Country
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.people.services import create_contact_point
from apps.registrations.confirmation import (
    CONFIRMATION_TEMPLATE_CODE,
    send_registration_confirmation,
)
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _confirmation_template():
    """Guarantee the confirmation template exists independent of migration-seed state.

    `django_db(transaction=True)` truncates and re-flushes tables between
    tests (TransactionTestCase semantics) WITHOUT re-running one-off
    RunPython data migrations such as `communications.0002` -- only the
    first test in a module still sees that seed data. Tests must not
    depend on migration-seeded reference data surviving a flush.
    """
    template, _ = MessageTemplate.objects.get_or_create(
        code=CONFIRMATION_TEMPLATE_CODE,
        defaults={"channel": "EMAIL", "purpose_code": "REGISTRATION_CONFIRMATION"},
    )
    for language in ("en", "fr"):
        MessageTemplateVersion.objects.get_or_create(
            template=template,
            language=language,
            version_label="test",
            defaults={
                "subject": "Confirmed",
                "body": "Reference {{public_reference}} for {{event_name}}",
                "status": "PUBLISHED",
                "effective_from": timezone.now(),
                "content_hash": "c" * 64,
            },
        )


@pytest.fixture
def registration() -> Registration:
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    event = EventEdition.objects.create(
        code="CONFTEST",
        name="Confirmation Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
    )
    person = Person.objects.create(display_name="Confirm Me", preferred_language="fr")
    create_contact_point(
        person=person,
        contact_type="EMAIL",
        raw_value="confirm-me@example.com",
        is_verified=True,
        login_enabled=True,
        is_primary=True,
    )
    return Registration.objects.create(
        public_reference="CONFTEST-R-000001",
        event_edition=event,
        person=person,
        source_kind="OPEN",
        source_context_key="open",
        preferred_language="fr",
    )


def test_confirmation_uses_the_participants_preferred_language(registration: Registration) -> None:
    message = send_registration_confirmation(registration)
    assert message is not None
    assert message.language == "fr"
    assert message.status in (CommunicationMessageStatus.QUEUED, CommunicationMessageStatus.SENT)


def test_confirmation_is_delivered_through_the_local_email_backend(
    registration: Registration,
) -> None:
    send_registration_confirmation(registration)
    assert len(mail.outbox) == 1
    assert "CONFTEST-R-000001" in mail.outbox[0].body


def test_confirmation_falls_back_to_english_when_language_has_no_published_version() -> None:
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    event = EventEdition.objects.create(
        code="FALLBACK",
        name="Fallback Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
    )
    person = Person.objects.create(display_name="No Version", preferred_language="de")
    create_contact_point(
        person=person,
        contact_type="EMAIL",
        raw_value="no-version@example.com",
        is_verified=True,
        login_enabled=True,
        is_primary=True,
    )
    registration = Registration.objects.create(
        public_reference="FALLBACK-R-000001",
        event_edition=event,
        person=person,
        source_kind="OPEN",
        source_context_key="open",
        preferred_language="de",
    )
    message = send_registration_confirmation(registration)
    assert message is not None
    assert message.language == "en"


def test_confirmation_is_idempotent_per_registration(registration: Registration) -> None:
    first = send_registration_confirmation(registration)
    second = send_registration_confirmation(registration)
    assert first.pk == second.pk
    assert CommunicationMessage.objects.filter(registration=registration).count() == 1


def test_confirmation_creates_an_append_only_delivery_attempt_on_success(
    registration: Registration,
) -> None:
    message = send_registration_confirmation(registration)
    attempts = DeliveryAttempt.objects.filter(message=message)
    assert attempts.count() == 1
    assert attempts.get().status == CommunicationMessageStatus.SENT
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENT
    assert message.sent_at is not None


def test_confirmation_provider_failure_never_raises_and_marks_the_message_failed(
    registration: Registration, monkeypatch
) -> None:
    """A provider exception during delivery (raised AFTER the registration itself
    already committed) must never surface as an unhandled exception -- it would
    otherwise turn an already-successful registration response into an HTTP 500
    (Prompt 4 final closure pass §8)."""

    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic provider outage")

    monkeypatch.setattr("django.core.mail.send_mail", _raise)

    message = send_registration_confirmation(registration)  # must not raise
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.FAILED
    attempts = DeliveryAttempt.objects.filter(message=message)
    assert attempts.count() == 1
    attempt = attempts.get()
    assert attempt.status == CommunicationMessageStatus.FAILED
    assert attempt.response_code  # a bounded diagnostic code, never the raw exception text


def test_confirmation_retry_after_success_does_not_duplicate_the_delivery_attempt(
    registration: Registration,
) -> None:
    first = send_registration_confirmation(registration)
    second = send_registration_confirmation(registration)
    assert first.pk == second.pk
    assert DeliveryAttempt.objects.filter(message=first).count() == 1


def test_confirmation_never_stores_plaintext_email_outside_the_encrypted_field(
    registration: Registration,
) -> None:
    from django.db import connection

    send_registration_confirmation(registration)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT destination_encrypted FROM communications_message WHERE registration_id = %s",
            [str(registration.pk)],
        )
        (raw_value,) = cursor.fetchone()
    assert "confirm-me@example.com" not in raw_value
    assert raw_value.startswith("v")


@pytest.mark.parametrize("newer_decision_message", [False, True])
def test_confirmation_page_does_not_claim_failed_email_was_sent(
    client, registration: Registration, newer_decision_message: bool
) -> None:
    from django.urls import reverse

    from apps.accounts.participant_auth import PARTICIPANT_SESSION_KEY
    from apps.accounts.session_expiry import (
        PARTICIPANT_ESTABLISHED_AT_KEY,
        PARTICIPANT_LAST_ACTIVITY_AT_KEY,
    )

    registration.public_status = "SUBMITTED"
    registration.save(update_fields=["public_status"])
    message = send_registration_confirmation(registration)
    CommunicationMessage.objects.filter(pk=message.pk).update(
        status=CommunicationMessageStatus.FAILED
    )
    if newer_decision_message:
        template, _ = MessageTemplate.objects.get_or_create(
            code="DECISION_STATUS", defaults={"channel": "EMAIL", "purpose_code": "DECISION_STATUS"}
        )
        version = MessageTemplateVersion.objects.create(
            template=template,
            language="en",
            version_label="receipt-isolation-test",
            subject="Decision update",
            body="A synthetic decision update.",
            status="PUBLISHED",
            effective_from=timezone.now(),
            content_hash="d" * 64,
        )
        CommunicationMessage.objects.create(
            registration=registration,
            event_edition=registration.event_edition,
            person=registration.person,
            template_version=version,
            channel=message.channel,
            language="en",
            destination_encrypted=message.destination_encrypted,
            destination_hash=message.destination_hash,
            destination_hash_key_version=message.destination_hash_key_version,
            content_hash="d" * 64,
            idempotency_key=f"decision-receipt-isolation:{registration.pk}",
            status=CommunicationMessageStatus.SENT,
        )
    now = timezone.now().isoformat()
    session = client.session
    session[PARTICIPANT_SESSION_KEY] = str(registration.person_id)
    session[PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()

    response = client.get(
        reverse("registrations:confirmation", args=[registration.public_reference])
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert response.context["confirmation_message"].pk == message.pk
    assert "receipt email could not be sent" in content
    assert "acknowledging receipt of your request has been sent" not in content
