"""Template context processors."""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104


def _epoch_ms(value) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def participant_session(request: HttpRequest) -> dict:
    """Participant AND operational session-warning context (Prompt 5
    correction pass §1/§4).

    `participant_authenticated` reflects a VALID (non-expired, well-formed)
    session, via the one authoritative boundary
    (`is_participant_session_valid`) -- never merely "a session key is
    present" (Prompt 5 correction pass §1: "Template context must not
    advertise an expired participant as authenticated").

    The `*_inactivity_deadline_epoch_ms` / `*_absolute_deadline_epoch_ms`
    values let a small client-side JavaScript timer reveal an accurate
    warning WHILE the page remains open, without a further server request
    (Prompt 5 correction pass §4) -- `participant_required`/
    `OperationalSessionExpiryMiddleware` both touch the inactivity
    timestamp before the view renders, so a per-request-computed "warn"
    flag alone could never reflect true idle time; a real countdown needs
    the actual deadlines, not just "seconds remaining right now".
    """
    from apps.accounts import session_expiry
    from apps.accounts.participant_auth import is_participant_session_valid

    participant_authenticated = is_participant_session_valid(request)
    context: dict = {
        "participant_authenticated": participant_authenticated,
        "participant_session_warning_seconds": 0,
        "participant_session_inactivity_deadline_epoch_ms": None,
        "participant_session_absolute_deadline_epoch_ms": None,
    }
    if participant_authenticated:
        status = session_expiry.participant_session_status(request)
        context["participant_session_warning_seconds"] = status.warning_seconds
        context["participant_session_inactivity_deadline_epoch_ms"] = _epoch_ms(
            status.inactivity_deadline
        )
        context["participant_session_absolute_deadline_epoch_ms"] = _epoch_ms(
            status.absolute_deadline
        )

    operational_authenticated = bool(getattr(request.user, "is_authenticated", False))
    context["operational_authenticated"] = operational_authenticated
    context["operational_session_warning_seconds"] = 0
    context["operational_session_inactivity_deadline_epoch_ms"] = None
    context["operational_session_absolute_deadline_epoch_ms"] = None
    if operational_authenticated:
        status = session_expiry.operational_session_status(request)
        context["operational_session_warning_seconds"] = status.warning_seconds
        context["operational_session_inactivity_deadline_epoch_ms"] = _epoch_ms(
            status.inactivity_deadline
        )
        context["operational_session_absolute_deadline_epoch_ms"] = _epoch_ms(
            status.absolute_deadline
        )
    return context


def configured_public_base_url() -> str:
    """The absolute public origin, or "" when none is genuinely configured.

    Canonical and social-preview URLs are emitted only from a configured
    HTTPS origin (UX-1, decision S-05): the local default
    (`http://localhost:8000`) and any non-HTTPS or local host are treated as
    "not configured", so no address is ever invented or leaked into a page.
    """
    base = getattr(settings, "PUBLIC_BASE_URL", "") or ""
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return ""
    if host.endswith(".invalid") or host.endswith(".test") or host.endswith(".example"):
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def site_metadata(request: HttpRequest) -> dict:
    """Public-page metadata context: the configured public origin, if any."""
    return {"configured_public_base_url": configured_public_base_url()}
