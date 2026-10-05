"""Email adapter: delivery through Django's configured `EMAIL_BACKEND`.

`DjangoEmailAdapter` delegates to Django's mail API, so the backend decides
where a message goes: the file sink under `var/mail/` locally, the in-memory
`locmem` outbox in tests, and the SMTP service configured from the environment
in staging and production (`config/settings/_deployment.py`). It is the same
delivery boundary `apps.registrations.confirmation` and the participant OTP
use.

A failure is never an exception the caller must handle. It is one of two
outcomes, decided by the stage at which it happened:

* **Not accepted, safe to retry**: the message could not be prepared, the
  connection could not be opened (connection, greeting, TLS, sign-in), or the
  server answered with an explicit SMTP refusal (`is_confirmed_refusal`).
  `apps.communications.tasks.deliver_message` retries it under its bounded
  policy.
* **Acceptance unknown**: anything else once the message is being sent, such
  as a lost connection or a timeout while waiting for the server's final
  answer. The server may have accepted the message, so it is recorded as one
  unconfirmed attempt and never sent again automatically (open decision
  COMM-01). Django's mail API does not report the SMTP stage of such a
  failure; treating every one of them as possibly accepted is deliberate.

Deterministic failure simulation is gated behind
`settings.COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION`, which is `True`
only in `test.py`/`local.py` and `False` in the deployed settings
(`config/settings/base.py` sets the fail-closed default). A destination
containing one of the two marker substrings below deterministically
produces a transient or permanent outcome WITHOUT ever attempting a real
send -- this is how tests exercise retry/permanent-failure/retry-exhaustion
behavior without depending on monkeypatching every call site.
"""

from __future__ import annotations

import smtplib

from django.conf import settings

from .contracts import DeliveryOutcome

TRANSIENT_FAILURE_MARKER = "+simulate-transient-failure"
PERMANENT_FAILURE_MARKER = "+simulate-permanent-failure"


def is_confirmed_refusal(error: BaseException) -> bool:
    """True when `error` proves the SMTP server did not accept the message.

    These are raised only on an explicit error reply: to the greeting, the
    sender or every recipient (before any message data), or a 4xx/5xx reply
    to the message data. Anything else -- a lost connection, a timeout, a
    reply that cannot be read -- leaves acceptance open.
    """
    if isinstance(
        error,
        smtplib.SMTPRecipientsRefused
        | smtplib.SMTPSenderRefused
        | smtplib.SMTPHeloError
        | smtplib.SMTPNotSupportedError,
    ):
        return True
    if isinstance(error, smtplib.SMTPDataError):
        return 400 <= error.smtp_code < 600
    return False


class DjangoEmailAdapter:
    """Email adapter over Django's configured mail backend."""

    def send(self, *, destination: str, subject: str, body: str) -> DeliveryOutcome:
        if getattr(settings, "COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION", False):
            if TRANSIENT_FAILURE_MARKER in destination:
                return DeliveryOutcome(
                    ok=False, permanent=False, response_code="SIMULATED_TRANSIENT"
                )
            if PERMANENT_FAILURE_MARKER in destination:
                return DeliveryOutcome(
                    ok=False, permanent=True, response_code="SIMULATED_PERMANENT"
                )
        from django.core.mail import EmailMessage, get_connection

        email = EmailMessage(subject=subject, body=body, from_email=None, to=[destination])
        try:
            email.message()  # render and validate locally; nothing is sent yet
            connection = get_connection()
            connection.open()
        except Exception as exc:  # noqa: BLE001 - a provider failure must never propagate.
            return DeliveryOutcome(ok=False, permanent=False, response_code=type(exc).__name__)
        try:
            sent = connection.send_messages([email])
        except Exception as exc:  # noqa: BLE001 - a provider failure must never propagate.
            if is_confirmed_refusal(exc):
                return DeliveryOutcome(ok=False, permanent=False, response_code=type(exc).__name__)
            return DeliveryOutcome(
                ok=False, acceptance_unknown=True, response_code=type(exc).__name__
            )
        finally:
            _close_quietly(connection)
        if not sent:
            return DeliveryOutcome(ok=False, permanent=False, response_code="NOT_SENT")
        return DeliveryOutcome(ok=True)


def _close_quietly(connection) -> None:
    """Close the connection; a failure here never changes the outcome."""
    try:
        connection.close()
    except Exception:  # noqa: BLE001, S110 - the outcome is already known.
        pass


def get_email_adapter() -> DjangoEmailAdapter:
    """Return the email adapter (Django's configured backend decides the transport)."""
    return DjangoEmailAdapter()
