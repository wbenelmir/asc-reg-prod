"""Participant and operational session expiry (Prompt 5 correction pass §4,
AF-AUTH-04, UI/UX §6.5).

Two independently configured lifetimes per audience:
  - inactivity (idle) lifetime -- reset by ordinary authenticated activity;
  - absolute lifetime -- fixed at the moment the session was established,
    NEVER extended by activity.

Every timestamp is stored as a timezone-aware ISO 8601 string in the
session dict. A missing, non-string, or naive (no UTC offset) value is
treated as expired -- this module fails closed on malformed session
metadata rather than guessing or silently trusting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

PARTICIPANT_ESTABLISHED_AT_KEY = "participant_session_established_at"
PARTICIPANT_LAST_ACTIVITY_AT_KEY = "participant_session_last_activity_at"

OPERATIONAL_ESTABLISHED_AT_KEY = "operational_session_established_at"
OPERATIONAL_LAST_ACTIVITY_AT_KEY = "operational_session_last_activity_at"


def _parse_aware_timestamp(raw: object) -> datetime | None:
    """Return a timezone-aware `datetime`, or `None` for anything malformed.

    Fails closed: not a string, unparsable, or naive (no offset) all return
    `None` -- the caller then treats the session as expired rather than
    trusting an ambiguous or tampered value.
    """
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value


@dataclass(frozen=True)
class SessionExpiryStatus:
    expired: bool
    reason: str | None  # "inactivity" | "absolute" | "missing" | None
    seconds_remaining: int | None  # None when expired or timestamps are missing
    warn: bool
    # Real deadlines, exposed so a page can run its OWN client-side timer
    # instead of relying on a fresh server round-trip to notice it is close
    # to expiring (Prompt 5 correction pass §4: "the current participant
    # decorator touches activity before rendering, so an inactivity warning
    # cannot appear on page load" -- a per-request-computed `warn` flag can
    # never reveal a warning while the participant sits idle on one page
    # with no further requests). `None` whenever timestamps are missing or
    # malformed. Never sensitive: two timestamps, nothing else.
    inactivity_deadline: datetime | None = None
    absolute_deadline: datetime | None = None
    warning_seconds: int = 0


def _evaluate(
    *,
    established_at_raw: object,
    last_activity_at_raw: object,
    inactivity_seconds: int,
    absolute_seconds: int,
    warning_seconds: int,
    now: datetime | None = None,
) -> SessionExpiryStatus:
    now = now or timezone.now()
    established_at = _parse_aware_timestamp(established_at_raw)
    last_activity_at = _parse_aware_timestamp(last_activity_at_raw)
    if established_at is None or last_activity_at is None:
        return SessionExpiryStatus(
            expired=True, reason="missing", seconds_remaining=None, warn=False
        )

    absolute_deadline = established_at + timezone.timedelta(seconds=absolute_seconds)
    inactivity_deadline = last_activity_at + timezone.timedelta(seconds=inactivity_seconds)

    if now >= absolute_deadline:
        return SessionExpiryStatus(
            expired=True,
            reason="absolute",
            seconds_remaining=None,
            warn=False,
            inactivity_deadline=inactivity_deadline,
            absolute_deadline=absolute_deadline,
            warning_seconds=warning_seconds,
        )
    if now >= inactivity_deadline:
        return SessionExpiryStatus(
            expired=True,
            reason="inactivity",
            seconds_remaining=None,
            warn=False,
            inactivity_deadline=inactivity_deadline,
            absolute_deadline=absolute_deadline,
            warning_seconds=warning_seconds,
        )

    next_deadline = min(absolute_deadline, inactivity_deadline)
    seconds_remaining = int((next_deadline - now).total_seconds())
    return SessionExpiryStatus(
        expired=False,
        reason=None,
        seconds_remaining=seconds_remaining,
        warn=seconds_remaining <= warning_seconds,
        inactivity_deadline=inactivity_deadline,
        absolute_deadline=absolute_deadline,
        warning_seconds=warning_seconds,
    )


def establish_participant_session(request: HttpRequest) -> None:
    """Set BOTH timestamps -- called only from a successful OTP authentication."""
    now = timezone.now().isoformat()
    request.session[PARTICIPANT_ESTABLISHED_AT_KEY] = now
    request.session[PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now


def touch_participant_activity(request: HttpRequest) -> None:
    """Reset the inactivity clock only -- NEVER touches the absolute deadline."""
    request.session[PARTICIPANT_LAST_ACTIVITY_AT_KEY] = timezone.now().isoformat()


def clear_participant_session_timestamps(request: HttpRequest) -> None:
    request.session.pop(PARTICIPANT_ESTABLISHED_AT_KEY, None)
    request.session.pop(PARTICIPANT_LAST_ACTIVITY_AT_KEY, None)


def participant_session_status(request: HttpRequest) -> SessionExpiryStatus:
    return _evaluate(
        established_at_raw=request.session.get(PARTICIPANT_ESTABLISHED_AT_KEY),
        last_activity_at_raw=request.session.get(PARTICIPANT_LAST_ACTIVITY_AT_KEY),
        inactivity_seconds=settings.PARTICIPANT_SESSION_INACTIVITY_SECONDS,
        absolute_seconds=settings.PARTICIPANT_SESSION_ABSOLUTE_SECONDS,
        warning_seconds=settings.PARTICIPANT_SESSION_WARNING_SECONDS,
    )


def establish_operational_session(request: HttpRequest) -> None:
    now = timezone.now().isoformat()
    request.session[OPERATIONAL_ESTABLISHED_AT_KEY] = now
    request.session[OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now


def touch_operational_activity(request: HttpRequest) -> None:
    request.session[OPERATIONAL_LAST_ACTIVITY_AT_KEY] = timezone.now().isoformat()


def clear_operational_session_timestamps(request: HttpRequest) -> None:
    request.session.pop(OPERATIONAL_ESTABLISHED_AT_KEY, None)
    request.session.pop(OPERATIONAL_LAST_ACTIVITY_AT_KEY, None)


def operational_session_status(request: HttpRequest) -> SessionExpiryStatus:
    return _evaluate(
        established_at_raw=request.session.get(OPERATIONAL_ESTABLISHED_AT_KEY),
        last_activity_at_raw=request.session.get(OPERATIONAL_LAST_ACTIVITY_AT_KEY),
        inactivity_seconds=settings.OPERATIONAL_SESSION_INACTIVITY_SECONDS,
        absolute_seconds=settings.OPERATIONAL_SESSION_ABSOLUTE_SECONDS,
        warning_seconds=settings.OPERATIONAL_SESSION_WARNING_SECONDS,
    )


def safe_operational_logout(request: HttpRequest) -> None:
    """Remove ONLY operational authentication and operational expiry
    metadata, preserving a valid participant session, active-registration
    state, and participant expiry clocks in the SAME browser session
    (Prompt 5 correction pass §2).

    Deliberately never calls `django.contrib.auth.logout()`: that helper
    calls `request.session.flush()`, which deletes the ENTIRE session
    (every key, participant or operational) and creates a brand new empty
    one -- exactly the cross-audience destruction this function exists to
    avoid. `cycle_key()` is used instead: it rotates the session's storage
    key (the same session-fixation defence `flush()` provides) while
    preserving every key/value already in the session dict.
    """
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.auth.signals import user_logged_out

    user = getattr(request, "user", None)
    request.session.pop(SESSION_KEY, None)
    request.session.pop(BACKEND_SESSION_KEY, None)
    request.session.pop(HASH_SESSION_KEY, None)
    clear_operational_session_timestamps(request)
    # Phase 3 Prompt 4: an operational sign-out (or expiry) also ends this
    # browser's entry operator session and drops every pending verification,
    # candidate list, and photo grant, so no participant detail outlives the
    # operator who looked it up. The device credential cookie is untouched:
    # it identifies the DEVICE, not the operator.
    from apps.entry.session_state import clear_entry_session_state

    clear_entry_session_state(request, reason="SIGNED_OUT")
    request.session.cycle_key()
    request.user = AnonymousUser()
    if user is not None and getattr(user, "is_authenticated", False):
        user_logged_out.send(sender=user.__class__, request=request, user=user)
