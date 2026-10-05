"""SMS adapter boundary -- disabled by default (Phase 2 Prompt 5 §4.4).

No real SMS provider is selected or integrated. `settings.SMS_ENABLED`
defaults to `False` everywhere (`config/settings/base.py`); `send_sms`
fails closed with `SmsDisabledError` whenever it is not explicitly turned on
by an approved provider decision, which has not been made. `NullSmsAdapter`
exists only so the `SmsAdapter` protocol has a concrete, safe reference
implementation to test against -- it never sends anything, in any environment.
"""

from __future__ import annotations

from django.conf import settings

from .contracts import DeliveryOutcome


class SmsDisabledError(Exception):
    """Raised whenever SMS delivery is attempted while the feature is disabled."""


class NullSmsAdapter:
    """Reference `SmsAdapter` implementation. Always fails closed -- no
    provider is configured or approved, in any environment."""

    def send(self, *, destination: str, body: str) -> DeliveryOutcome:
        raise SmsDisabledError("No SMS provider is configured or approved.")


def send_sms(*, destination: str, body: str) -> DeliveryOutcome:
    """Fail closed unless `settings.SMS_ENABLED` is explicitly `True` --
    which it never is without an approved provider decision (out of Prompt
    5 scope). Even if enabled, no real provider is wired: this always
    raises, by design, until a future prompt makes that decision.
    """
    if not getattr(settings, "SMS_ENABLED", False):
        raise SmsDisabledError("SMS delivery is disabled (settings.SMS_ENABLED is False).")
    return NullSmsAdapter().send(destination=destination, body=body)
