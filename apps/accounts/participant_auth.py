"""Participant session authentication (AF-AUTH-01/§3.5).

Deliberately separate from `django.contrib.auth`'s session machinery, which
this project reserves for `OperationalUser` (password + MFA). Participants
authenticate passwordlessly via OTP (`apps.accounts.otp`); a successful
`consume_challenge()` call is the only way `login_participant()` is meant
to be invoked.
"""

from __future__ import annotations

from functools import wraps
from uuid import UUID

from django.contrib import messages
from django.http import HttpRequest
from django.shortcuts import redirect
from django.utils.translation import gettext as _

from apps.accounts import session_expiry

PARTICIPANT_SESSION_KEY = "participant_person_id"
ACTIVE_REGISTRATION_SESSION_KEY = "active_registration_id"


def login_participant(request: HttpRequest, person_id: UUID) -> None:
    request.session.cycle_key()  # session-fixation defence on privilege change
    # P4-4: a pre-sign-in challenge session was short-lived; a signed-in
    # session gets the ordinary lifetime (SESSION_COOKIE_AGE) again.
    request.session.set_expiry(None)
    request.session[PARTICIPANT_SESSION_KEY] = str(person_id)
    # AF-AUTH-04 / UI/UX §6.5: a successful OTP authentication establishes
    # BOTH the inactivity and absolute session clocks (Prompt 5 correction
    # pass §4).
    session_expiry.establish_participant_session(request)


def logout_participant(request: HttpRequest) -> None:
    request.session.pop(PARTICIPANT_SESSION_KEY, None)
    request.session.pop(ACTIVE_REGISTRATION_SESSION_KEY, None)
    session_expiry.clear_participant_session_timestamps(request)
    request.session.cycle_key()


def get_participant_person_id(request: HttpRequest) -> str | None:
    return request.session.get(PARTICIPANT_SESSION_KEY)


def is_participant_authenticated(request: HttpRequest) -> bool:
    """True if a participant session KEY is present -- does NOT check
    expiry. Kept for call sites that only need "is someone signed in as a
    participant at all" (e.g. `otp_request`/`otp_verify` redirecting an
    already-authenticated visitor away from the OTP forms). Anything that
    would grant access, render "authenticated" state, or read/write
    participant-scoped data MUST use `is_participant_session_valid()` or
    `get_valid_participant_person_id()` instead (Prompt 5 correction pass
    §1) -- this predicate alone does not prove the session has not expired.
    """
    return get_participant_person_id(request) is not None


def is_participant_session_valid(request: HttpRequest) -> bool:
    """The ONE authoritative participant-session validity boundary
    (Prompt 5 correction pass §1).

    True only when a participant session key is present AND the session's
    inactivity/absolute clocks have not expired AND its timestamp metadata
    is well-formed -- missing, malformed, or expired timestamps all fail
    closed to `False`, never `True`. Every consumer that authorizes access,
    renders "authenticated" state, or reads/writes participant-scoped
    preferences on the strength of the participant session MUST go through
    this function (or `get_valid_participant_person_id`), not
    `is_participant_authenticated`/`get_participant_person_id` directly.
    """
    if not is_participant_authenticated(request):
        return False
    return not session_expiry.participant_session_status(request).expired


def get_valid_participant_person_id(request: HttpRequest) -> str | None:
    """The participant's person id, but ONLY when the session is valid
    (Prompt 5 correction pass §1) -- `None` for an expired, malformed, or
    absent session, so a caller can use this directly as a single
    authorization/attribution check without separately re-deriving
    validity."""
    if not is_participant_session_valid(request):
        return None
    return get_participant_person_id(request)


def set_active_registration(request: HttpRequest, registration_id: UUID) -> None:
    request.session[ACTIVE_REGISTRATION_SESSION_KEY] = str(registration_id)


def get_active_registration_id(request: HttpRequest) -> str | None:
    return request.session.get(ACTIVE_REGISTRATION_SESSION_KEY)


def participant_required(view_func):
    """Redirect to the OTP start screen when no participant session exists,
    or when the participant session has expired (inactivity or absolute
    lifetime -- Prompt 5 correction pass §4).

    An expired session clears participant authentication and active-
    registration state but leaves the Draft `Registration` row itself (and
    its `current_step`) untouched in the database -- a fresh OTP
    authentication resumes exactly where the participant left off (AF-AUTH-04).
    """

    @wraps(view_func)
    def _wrapped(request: HttpRequest, *args, **kwargs):
        if not is_participant_authenticated(request):
            return redirect("accounts:otp-request")
        if not is_participant_session_valid(request):
            logout_participant(request)
            messages.info(
                request,
                _("Your session has ended. Please verify your email again to continue."),
            )
            return redirect("accounts:otp-request")
        session_expiry.touch_participant_activity(request)
        return view_func(request, *args, **kwargs)

    return _wrapped
