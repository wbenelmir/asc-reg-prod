"""Delivery through Django's SMTP backend, against a LOCAL fake SMTP server.

The server below is a minimal scripted stand-in on a loopback port: no real
SMTP service is contacted and no real mail is sent. Each connection follows
the next step of its script. It proves that:

* the participant OTP and the communication pipeline use the configured SMTP
  host, port, sender and timeout;
* a confirmed refusal (an SMTP error reply) and a failure before the message
  is sent (no connection, no greeting) are retried under the bounded policy,
  and the retry delivers once;
* an exchange that ends without the server's final answer, after the server
  may already have accepted the message, is recorded as one unconfirmed
  attempt and is never sent again automatically: not by a duplicate task,
  not by the outbox recovery sweep and not by the overdue-retry sweep
  (open decision COMM-01).
"""

from __future__ import annotations

import smtplib
import socket
import socketserver
import threading
import time
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AuthenticationChallengeChannel
from apps.accounts.otp import deliver_otp
from apps.communications.models import (
    CommunicationChannel,
    CommunicationMessage,
    CommunicationMessageStatus,
    DeliveryAttempt,
    MessageTemplate,
    MessageTemplateVersion,
)
from apps.communications.services import queue_communication
from apps.communications.tasks import (
    deliver_message,
    deliver_message_task,
    dispatch_pending_communication_events,
    redeliver_overdue_deferred_messages,
)

pytestmark = pytest.mark.django_db

SENDER = "ASC 2026 <no-reply@example.invalid>"
#: The fake server holds a delayed answer this long; the client timeout is 1 s.
SERVER_DELAY_SECONDS = 3

ACCEPT = "accept"
REFUSE_RECIPIENT = "refuse-recipient"  # 451 to RCPT TO: nothing was accepted
REFUSE_DATA = "refuse-data"  # 451 after the message data: refused, not accepted
SILENT_GREETING = "silent-greeting"  # never sends the 220 greeting
DROP_AFTER_DATA = "drop-after-data"  # accepts the message, closes without answering
DELAY_AFTER_DATA = "delay-after-data"  # accepts the message, answers after the timeout


class _Mailbox:
    """What the fake server accepted, and how many connections it served."""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.envelopes: list[dict] = []
        self.connections = 0
        self._lock = threading.Lock()

    def next_step(self) -> str:
        with self._lock:
            self.connections += 1
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def _handler(mailbox: _Mailbox):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            step = mailbox.next_step()
            if step == SILENT_GREETING:
                time.sleep(SERVER_DELAY_SECONDS)
                return
            self.wfile.write(b"220 fake.smtp.invalid ESMTP\r\n")
            envelope: dict = {"rcpt": []}
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                command = line.strip().upper()
                if command.startswith((b"EHLO", b"HELO")):
                    self.wfile.write(b"250-fake.smtp.invalid\r\n250 8BITMIME\r\n")
                elif command.startswith(b"MAIL FROM:"):
                    envelope["from"] = line.strip()[10:].decode()
                    self.wfile.write(b"250 OK\r\n")
                elif command.startswith(b"RCPT TO:"):
                    if step == REFUSE_RECIPIENT:
                        self.wfile.write(b"451 4.3.0 try again later\r\n")
                        continue
                    envelope["rcpt"].append(line.strip()[8:].decode())
                    self.wfile.write(b"250 OK\r\n")
                elif command == b"DATA":
                    self.wfile.write(b"354 go ahead\r\n")
                    body = b""
                    while not body.endswith(b"\r\n.\r\n"):
                        piece = self.rfile.readline()
                        if not piece:
                            return
                        body += piece
                    if step == REFUSE_DATA:
                        self.wfile.write(b"451 4.3.0 try again later\r\n")
                        continue
                    # Accepted: from here on the message counts as received.
                    envelope["data"] = body.decode("utf-8", "replace")
                    mailbox.envelopes.append(envelope)
                    envelope = {"rcpt": []}
                    if step == DROP_AFTER_DATA:
                        return
                    if step == DELAY_AFTER_DATA:
                        time.sleep(SERVER_DELAY_SECONDS)
                        try:
                            self.wfile.write(b"250 queued\r\n")
                        except OSError:
                            pass
                        return
                    self.wfile.write(b"250 queued\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 OK\r\n")

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _smtp_settings(port: int):
    return override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="127.0.0.1",
        EMAIL_PORT=port,
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
        EMAIL_TIMEOUT=1,
        DEFAULT_FROM_EMAIL=SENDER,
        COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION=False,
    )


@pytest.fixture
def smtp():
    servers = []

    def start(*script: str):
        mailbox = _Mailbox(list(script) or [ACCEPT])
        server = _Server(("127.0.0.1", 0), _handler(mailbox))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return mailbox, _smtp_settings(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def template():
    template = MessageTemplate.objects.create(
        code="SMTP_TEST", channel=CommunicationChannel.EMAIL, purpose_code="SMTP_TEST"
    )
    MessageTemplateVersion.objects.create(
        template=template,
        language="en",
        version_label="v1",
        subject="Synthetic subject {{public_reference}}",
        body="Synthetic body {{public_reference}}",
        allowed_variables=["public_reference"],
        status="PUBLISHED",
        effective_from=timezone.now(),
        content_hash="d" * 64,
    )
    return template


def _queue(idempotency_key: str = "smtp-1") -> CommunicationMessage:
    message = queue_communication(
        purpose_code="SMTP_TEST",
        event_edition=None,
        person=None,
        language="en",
        destination="participant@example.invalid",
        context={"public_reference": "SMTP-REF-1"},
        idempotency_key=idempotency_key,
    )
    assert message is not None
    return message


def _attempts(message) -> list[tuple[str, str]]:
    return list(
        DeliveryAttempt.objects.filter(message=message)
        .order_by("attempt_number")
        .values_list("status", "response_code")
    )


def _every_automatic_path(message, monkeypatch) -> None:
    """Run everything that may deliver a message without a person deciding."""
    deliver_message(str(message.pk))  # a duplicate task execution
    deliver_message_task.delay(str(message.pk))  # the Celery task (eager in tests)
    dispatch_pending_communication_events()  # the outbox recovery sweep
    later = timezone.now() + timedelta(hours=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    redeliver_overdue_deferred_messages()  # the overdue-retry sweep, an hour later
    monkeypatch.undo()


def test_the_participant_code_goes_through_the_configured_smtp_server(smtp) -> None:
    mailbox, settings = smtp()
    with settings:
        deliver_otp(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value="participant@example.invalid",
            otp_value="246810",
        )
    assert len(mailbox.envelopes) == 1
    envelope = mailbox.envelopes[0]
    assert envelope["from"] == "<no-reply@example.invalid>"
    assert envelope["rcpt"] == ["<participant@example.invalid>"]
    assert "From: ASC 2026 <no-reply@example.invalid>" in envelope["data"]
    assert "246810" in envelope["data"]


def test_a_communication_message_is_sent_once_through_smtp(smtp, template) -> None:
    mailbox, settings = smtp()
    message = _queue()
    with settings:
        delivered = deliver_message(str(message.pk))
        again = deliver_message(str(message.pk))
    assert delivered.status == CommunicationMessageStatus.SENT
    assert again.status == CommunicationMessageStatus.SENT
    assert len(mailbox.envelopes) == 1
    assert DeliveryAttempt.objects.filter(message=message).count() == 1
    assert "Synthetic subject SMTP-REF-1" in mailbox.envelopes[0]["data"]


@pytest.mark.parametrize("refusal", [REFUSE_DATA, REFUSE_RECIPIENT])
def test_a_confirmed_refusal_is_retried_and_the_retry_delivers_once(
    smtp, template, refusal
) -> None:
    mailbox, settings = smtp(refusal, ACCEPT)
    message = _queue(f"smtp-{refusal}")
    with settings:
        first = deliver_message(str(message.pk))
        assert first.status == CommunicationMessageStatus.DEFERRED
        assert mailbox.envelopes == []
        second = deliver_message(str(message.pk))
    assert second.status == CommunicationMessageStatus.SENT
    assert len(mailbox.envelopes) == 1
    assert [status for status, _code in _attempts(message)] == [
        CommunicationMessageStatus.DEFERRED,
        CommunicationMessageStatus.SENT,
    ]


def test_a_server_that_never_greets_is_a_safe_retry(smtp, template) -> None:
    mailbox, settings = smtp(SILENT_GREETING, ACCEPT)
    message = _queue("smtp-silent")
    started = time.monotonic()
    with settings:
        first = deliver_message(str(message.pk))
        elapsed = time.monotonic() - started
        second = deliver_message(str(message.pk))
    assert elapsed < SERVER_DELAY_SECONDS - 0.1  # the timeout applies, no hang
    assert first.status == CommunicationMessageStatus.DEFERRED
    assert second.status == CommunicationMessageStatus.SENT
    assert len(mailbox.envelopes) == 1


def test_an_unreachable_server_is_a_safe_retry(template) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    message = _queue("smtp-unreachable")
    with _smtp_settings(closed_port):
        result = deliver_message(str(message.pk))
    assert result.status == CommunicationMessageStatus.DEFERRED
    assert _attempts(message)[0][0] == CommunicationMessageStatus.DEFERRED


@pytest.mark.parametrize("lost_answer", [DROP_AFTER_DATA, DELAY_AFTER_DATA])
def test_a_lost_final_answer_is_not_sent_again_by_a_task_or_the_outbox_sweep(
    smtp, template, lost_answer
) -> None:
    mailbox, settings = smtp(lost_answer, ACCEPT)
    message = _queue(f"smtp-task-{lost_answer}")
    started = time.monotonic()
    with settings:
        result = deliver_message(str(message.pk))
        elapsed = time.monotonic() - started
        deliver_message(str(message.pk))
        deliver_message_task.delay(str(message.pk))
        dispatch_pending_communication_events()
    # The server received the message once and was never contacted again.
    assert len(mailbox.envelopes) == 1
    assert mailbox.connections == 1
    assert elapsed < SERVER_DELAY_SECONDS - 0.1
    assert result.status == CommunicationMessageStatus.SENDING
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENDING
    [(status, response_code)] = _attempts(message)
    assert status == CommunicationMessageStatus.SENDING
    assert response_code.startswith("UNCONFIRMED:")
    assert DeliveryAttempt.objects.get(message=message).completed_at is not None


@pytest.mark.parametrize("lost_answer", [DROP_AFTER_DATA, DELAY_AFTER_DATA])
def test_a_lost_final_answer_is_not_sent_again_by_the_overdue_retry_sweep(
    smtp, template, monkeypatch, lost_answer
) -> None:
    mailbox, settings = smtp(lost_answer, ACCEPT)
    message = _queue(f"smtp-sweep-{lost_answer}")
    with settings:
        deliver_message(str(message.pk))
        later = timezone.now() + timedelta(hours=1)
        monkeypatch.setattr(timezone, "now", lambda: later)
        swept = redeliver_overdue_deferred_messages()
        monkeypatch.undo()
    assert len(mailbox.envelopes) == 1
    assert mailbox.connections == 1
    assert swept == 0
    message.refresh_from_db()
    assert message.status == CommunicationMessageStatus.SENDING


def test_an_unconfirmed_last_attempt_is_kept_unconfirmed_not_failed(
    smtp, template, monkeypatch
) -> None:
    mailbox, settings = smtp(REFUSE_DATA, REFUSE_DATA, DROP_AFTER_DATA, ACCEPT)
    message = _queue("smtp-last")
    with settings:
        for _attempt in range(3):
            deliver_message(str(message.pk))
        _every_automatic_path(message, monkeypatch)
    assert len(mailbox.envelopes) == 1
    assert mailbox.connections == 3
    message.refresh_from_db()
    # FAILED would claim "not delivered", which is not known here.
    assert message.status == CommunicationMessageStatus.SENDING
    assert [status for status, _code in _attempts(message)] == [
        CommunicationMessageStatus.DEFERRED,
        CommunicationMessageStatus.DEFERRED,
        CommunicationMessageStatus.SENDING,
    ]


@pytest.mark.parametrize(
    ("error", "refusal"),
    [
        (smtplib.SMTPRecipientsRefused({"a@example.invalid": (550, b"no")}), True),
        (smtplib.SMTPSenderRefused(451, b"later", "s@example.invalid"), True),
        (smtplib.SMTPDataError(451, b"later"), True),
        (smtplib.SMTPDataError(554, b"rejected"), True),
        (smtplib.SMTPHeloError(501, b"bad hello"), True),
        (smtplib.SMTPNotSupportedError("SMTPUTF8 not supported"), True),
        # A reply that cannot be read as a refusal leaves acceptance open.
        (smtplib.SMTPDataError(-1, b"garbled"), False),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), False),
        (TimeoutError("timed out"), False),
        (ConnectionResetError("reset"), False),
        (smtplib.SMTPException("unknown"), False),
    ],
)
def test_only_an_explicit_smtp_refusal_counts_as_not_accepted(error, refusal) -> None:
    from apps.communications.adapters.email import is_confirmed_refusal

    assert is_confirmed_refusal(error) is refusal
