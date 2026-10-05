"""Provider-neutral delivery adapters (Phase 2 Prompt 5 §4.4)."""

from __future__ import annotations

from .contracts import DeliveryOutcome, EmailAdapter, SmsAdapter
from .email import DjangoEmailAdapter, get_email_adapter
from .sms import NullSmsAdapter, SmsDisabledError, send_sms

__all__ = [
    "DeliveryOutcome",
    "EmailAdapter",
    "DjangoEmailAdapter",
    "get_email_adapter",
    "SmsAdapter",
    "NullSmsAdapter",
    "SmsDisabledError",
    "send_sms",
]
