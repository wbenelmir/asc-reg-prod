"""Provider-neutral delivery adapter contracts (Phase 2 Prompt 5 §4.4).

Every adapter returns a `DeliveryOutcome` -- never raises for an ordinary
provider-side failure (a raised exception is reserved for a genuine
programming error, e.g. a malformed call). This lets
`apps.communications.tasks.deliver_message` treat every adapter the same
way regardless of channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeliveryOutcome:
    """Adapter-neutral delivery result.

    `permanent` distinguishes a failure the retry policy must never retry
    (e.g. a rejected/invalid destination) from a transient one that is
    still worth retrying up to the bounded attempt limit. A failure with
    neither flag is confirmed: the provider did not accept the message.

    `acceptance_unknown` marks a failed call after which the provider may
    nevertheless have accepted the message (for example the connection was
    lost after the message data was sent). Such a message must never be
    sent again automatically (open decision COMM-01).

    `response_code` is a short, bounded diagnostic string -- never the
    provider's raw exception text, a stack trace, or any personal data.
    """

    ok: bool
    permanent: bool = False
    acceptance_unknown: bool = False
    response_code: str = ""
    provider_reference: str = ""


class EmailAdapter(Protocol):
    def send(self, *, destination: str, subject: str, body: str) -> DeliveryOutcome: ...


class SmsAdapter(Protocol):
    def send(self, *, destination: str, body: str) -> DeliveryOutcome: ...
