"""Structured (JSON) log formatting.

Applied after the correlation-ID and redaction filters have already run
against the record (see `config/settings/base.py` LOGGING) -- but the
message is the ONLY thing those filters can redact at that point, since
`exc_info`/`stack_info` text does not exist as a string yet (it is
rendered lazily, here, by `formatException`/`formatStack`). This formatter
therefore applies `apps.core.redaction.redact()` itself to the exception
and stack text before it is ever written out, and to every project-
attached structured field (Prompt 2 correction §3) -- so a secret that
happens to surface only inside an exception message or traceback is masked
exactly as reliably as one in an ordinary log message, and is never
reintroduced through an unredacted `repr()` of the exception.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from apps.core.redaction import extra_fields, redact


class StructuredFormatter(logging.Formatter):
    """Renders one JSON object per log line, with no unredacted secret path."""

    def format(self, record: logging.LogRecord) -> str:
        # `redact()` is applied directly here, independently of whatever
        # filter chain the record passed through -- `RedactingLogFilter`
        # already redacts `record.msg`/`record.args` on the intended
        # path, but a record reaching this formatter by any other route
        # (a differently configured logger, a directly constructed
        # `LogRecord` in a test, a future handler wiring change) must
        # still never emit an unredacted secret (Prompt 2 correction §5).
        # Redacting already-redacted text is a harmless no-op.
        message = redact(record.getMessage())
        correlation_id = redact(str(getattr(record, "correlation_id", "-")))
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "correlation_id": correlation_id,
        }
        payload.update(extra_fields(record))
        if record.exc_info:
            # redact() is applied to the fully rendered exception text
            # (including the exception's own str()/repr() on its last
            # line) -- never replaced with an unredacted repr of the
            # exception object itself.
            payload["exc_info"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = redact(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, default=str)
