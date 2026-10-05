"""Request correlation ID: middleware and logging filter.

Every request receives (or propagates) a correlation ID for the duration of
that request, made available to structured logging and, from Prompt 3
onward, to `AuditEvent.correlation_id` (Schema §15.1).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

CORRELATION_ID_HEADER = "X-Correlation-ID"
_MAX_LENGTH = 64

_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def get_correlation_id() -> str | None:
    """Return the correlation ID active for the current request, if any."""
    return _correlation_id_var.get()


def _sanitize(value: str) -> str | None:
    """Accept a caller-supplied correlation ID only if it is short and plain.

    A forwarded X-Correlation-ID is untrusted input coming from outside this
    application: it is length-limited and character-restricted before use,
    and it is never treated as an authorization or identity claim.
    """
    value = value.strip()
    if not value or len(value) > _MAX_LENGTH:
        return None
    if not all(char.isalnum() or char in "-_" for char in value):
        return None
    return value


class CorrelationIdMiddleware:
    """Assigns (or propagates) a correlation ID for the duration of one request."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get(CORRELATION_ID_HEADER, "")
        correlation_id = _sanitize(incoming) or uuid.uuid4().hex
        token = _correlation_id_var.set(correlation_id)
        request.correlation_id = correlation_id  # type: ignore[attr-defined]
        try:
            response = self.get_response(request)
        finally:
            _correlation_id_var.reset(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class CorrelationIdLogFilter(logging.Filter):
    """Injects the active correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True
