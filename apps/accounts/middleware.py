"""Operational session expiry middleware (Prompt 5 correction pass §4,
AF-AUTH-04, UI/UX §6.5).

Django's own `AuthenticationMiddleware` loads `request.user` from the
session on every request but never re-checks `is_active` or re-evaluates
session age after the initial login -- an operational account disabled,
suspended, or expired mid-session would otherwise keep a fully working
session until the browser's cookie itself expires. This middleware closes
that gap: every request from an authenticated operational user re-checks
the account's current status AND the independently configured inactivity/
absolute session lifetimes, logging out and redirecting to sign-in the
moment either fails -- never weakening the production MFA requirement,
which this middleware does not touch at all.
"""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _

from apps.accounts import session_expiry


class OperationalSessionExpiryMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)
        is_authenticated = user is not None and getattr(user, "is_authenticated", False)

        # Django's authentication backend deliberately returns AnonymousUser
        # when a stored user has become inactive.  It does not, however,
        # remove that now-stale operational authentication record from the
        # shared session.  Clear it here while preserving participant state.
        if not is_authenticated and SESSION_KEY in request.session:
            session_expiry.safe_operational_logout(request)
            messages.info(request, _("Your operational session has ended. Please sign in again."))
            return redirect_to_login(
                request.get_full_path(), login_url="accounts:operational-sign-in"
            )

        if is_authenticated:
            if not self._session_is_valid(request, user):
                # `safe_operational_logout`, never `django.contrib.auth.logout()`:
                # the latter flushes the WHOLE session, destroying a valid
                # participant session sharing the same browser session
                # (Prompt 5 correction pass §2).
                session_expiry.safe_operational_logout(request)
                messages.info(
                    request, _("Your operational session has ended. Please sign in again.")
                )
                return redirect_to_login(
                    request.get_full_path(), login_url="accounts:operational-sign-in"
                )
            # A PASSIVE request (Phase 3 Prompt 5: the entry connection
            # status check) is validated exactly like any other, but it is
            # not operator activity, so it never extends the inactivity
            # deadline -- a page left open cannot keep a session alive.
            if request.path not in settings.OPERATIONAL_PASSIVE_PATHS:
                session_expiry.touch_operational_activity(request)
        return self.get_response(request)

    @staticmethod
    def _session_is_valid(request: HttpRequest, user) -> bool:
        # `OperationalUser.is_active` is the authoritative account gate: it
        # covers status as well as `active_from`/`active_until`. Checking the
        # stored status alone would let an already-authenticated temporary
        # account keep its session after its time window elapsed.
        if not user.is_active:
            return False
        status = session_expiry.operational_session_status(request)
        return not status.expired
