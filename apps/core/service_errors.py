"""Presentation-layer resolution of domain service failures (Phase 3 Prompt 8, P8-01).

Domain services raise exceptions whose message is developer-facing English:
it may name internal concepts (a signing provider, a lock version, a
canonical DER encoding) and it is never translated. Rendering `str(exc)` to
a user therefore showed English on French and Arabic pages and leaked
implementation wording.

A view instead asks this module for the user-facing text. Each app keeps a
small, explicit table mapping a stable `(exception class, code)` pair --
`code` being the exception's machine-readable refinement, "" when none --
to a lazily translated message. Resolution walks the exception's class
hierarchy, most specific first, trying the exact code before the class's
catch-all entry, and ends at one generic localized fallback. The exception
message itself is never consulted, so no internal wording can ever reach a
page, whatever a future raise site says.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

#: The one message shown for a failure no table knows about.
GENERIC_SERVICE_ERROR = _(
    "The request could not be completed. Reload the page and try again. "
    "If the problem continues, contact the operations desk."
)


def resolve_service_error_message(exc: BaseException, table: dict) -> str:
    """The localized, safe message for `exc`, never its own text.

    `table` maps `(ExceptionClass, code)` to a message, where `code` is a
    string or `None` for the class-wide entry.
    """
    code = getattr(exc, "code", "") or ""
    for cls in type(exc).__mro__:
        if code and (cls, code) in table:
            return str(table[(cls, code)])
        if (cls, None) in table:
            return str(table[(cls, None)])
    return str(GENERIC_SERVICE_ERROR)
