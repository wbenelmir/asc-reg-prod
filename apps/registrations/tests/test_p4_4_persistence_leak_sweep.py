"""P4-4: a whole-journey sweep for sensitive values in persisted side channels.

One synthetic participant goes from the OTP request through every wizard step,
an accommodation request with a note, submission with every consent, and a
withdrawal of the accommodation consent. Then every audit row, every outbox
payload, every captured log record and every stored communication is searched
for the distinctive values that must never appear there: the NIN, the mobile
number, the accommodation note, the biography, the objectives and the OTP.
The email address may appear only where a message is addressed to it. Encrypted
columns are checked separately by their own tests. Synthetic data only.
"""

from __future__ import annotations

import datetime
import json
import logging

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.audit.models import AuditEvent
from apps.core.models import Country, OutboxEvent, Sector
from apps.core.testing import otp_request_data
from apps.events.models import EventEdition, EventEditionStatus
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.selectors import effective_published_version
from apps.registrations.models import Registration
from apps.registrations.services import (
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_accommodation_request,
    submit_full_registration,
    withdraw_accommodation_consent,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db

EMAIL = "p44-sweep-participant@example.com"
NIN = "109987654321012345"
MOBILE = "0661987321"
NOTE = "P44 sweep note: seat near exit door seven."
FORBIDDEN = {
    "nin": NIN,
    "mobile": MOBILE[1:],  # national significant number, in any formatting
    "accommodation note": "seat near exit door seven",
    "otp": DeterministicTestOtpGenerator.FIXED_VALUE,
}


@pytest.fixture
def event():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    now = timezone.now()
    return EventEdition.objects.create(
        code="P44SWEEP",
        name="P4-4 sweep",
        timezone="UTC",
        starts_at=now + datetime.timedelta(days=20),
        ends_at=now + datetime.timedelta(days=21),
        status=EventEditionStatus.REGISTRATION_OPEN,
    )


def _journey(event) -> Registration:
    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(reverse("accounts:otp-request"), otp_request_data(EMAIL, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    person = resolve_or_create_participant_for_email(EMAIL)  # the signed-in participant
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft, nin_value=NIN, mobile_number=MOBILE)
    save_accommodation_request(
        registration=draft, answer="YES", categories=["QUIET_SPACE"], note=NOTE
    )
    submit_full_registration(
        registration=draft,
        privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
        terms_version=effective_published_version("TERMS", "en"),
        data_processing_consent_granted=True,
        sensitive_data_consent_granted=True,
        session_reference="p44-sweep",
        idempotency_key=initial_submission_operation_key(draft.pk),
    )
    draft.refresh_from_db()
    withdraw_accommodation_consent(registration=draft, person=person)
    return draft


def _audit_text() -> str:
    rows = AuditEvent.objects.values()
    return json.dumps([{k: str(v) for k, v in row.items()} for row in rows], ensure_ascii=False)


def _outbox_text() -> str:
    return json.dumps(list(OutboxEvent.objects.values_list("payload", flat=True)), default=str)


def _messages_text() -> str:
    from apps.communications.models import CommunicationMessage

    return json.dumps(
        [{k: str(v) for k, v in row.items()} for row in CommunicationMessage.objects.values()],
        ensure_ascii=False,
    )


def test_no_sensitive_value_reaches_audit_outbox_logs_or_messages(
    event, caplog, django_capture_on_commit_callbacks
) -> None:
    caplog.set_level(logging.DEBUG)
    # Commit callbacks run, so outbox dispatch and delivery are swept too.
    with django_capture_on_commit_callbacks(execute=True):
        registration = _journey(event)
    assert registration.public_status == "SUBMITTED"
    assert AuditEvent.objects.exists() and OutboxEvent.objects.exists()

    channels = {
        "audit": _audit_text(),
        "outbox": _outbox_text(),
        "logs": caplog.text,
        "messages": _messages_text(),
    }
    found = [
        f"{label} in {channel}"
        for channel, text in channels.items()
        for label, value in FORBIDDEN.items()
        if value in text
    ]
    assert found == []
    # Sent mail (the OTP message) carries the code by design, nothing else.
    from django.core import mail

    assert mail.outbox, "the OTP message was sent"
    mail_text = " ".join(f"{m.subject} {m.body}" for m in mail.outbox)
    assert [
        label for label, value in FORBIDDEN.items() if label != "otp" and value in mail_text
    ] == []
    # The address is never in audit rows or logs; a message may be addressed to it.
    assert EMAIL not in channels["audit"]
    assert EMAIL not in channels["logs"]
