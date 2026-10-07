"""Participant OTP access views (AF-AUTH-01) and operational sign-in (AF-AUTH-03, partial)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.core import human_check
from apps.core.concurrency import resolve_request_client_network_identity
from apps.core.middleware.correlation import get_correlation_id
from apps.people.services import resolve_or_create_participant_for_email

from . import captcha_guard, participant_auth, session_expiry
from . import operational_sign_in as sign_in_attempts
from .forms import OperationalSignInForm, OtpRequestForm, OtpVerifyForm
from .models import AuthenticationChallengeChannel
from .otp import ConsumeOutcome, consume_challenge, issue_challenge

_PENDING_EMAIL_SESSION_KEY = "otp_pending_email"


@require_http_methods(["GET", "POST"])
def otp_request(request):
    if participant_auth.is_participant_authenticated(request):
        return redirect("registrations:workspace")

    form = OtpRequestForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        # UX-4 (M01, S-16): the proof-of-work check runs before any OTP is
        # issued and before any throttle counter moves. One generic message
        # whatever the failure; it does not depend on the email address, so
        # it reveals nothing about accounts.
        try:
            human_check.verify_and_consume(
                form.cleaned_data["human_check"],
                action=human_check.ACTION_OTP_REQUEST,
                session=request.session,
            )
        except human_check.HumanCheckFailed:
            form.add_error(
                None,
                _(
                    "The automatic security check did not complete or has expired. Your "
                    "email address is kept: wait for the check to finish and send the form "
                    "again. It needs JavaScript."
                ),
            )
            return _render_otp_request(request, form, status=400)
        email = form.cleaned_data["email"]
        network_address = resolve_request_client_network_identity(request)
        issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value=email,
            client_network_address=network_address,
            correlation_id=get_correlation_id() or "",
        )
        # AF-AUTH-01: the response is always the same, regardless of outcome
        # (ISSUED/THROTTLED/LOCKED) or whether this email belongs to an
        # existing participant -- never disclose which.
        request.session[_PENDING_EMAIL_SESSION_KEY] = email.strip().lower()
        messages.info(
            request, _("If this email address is valid, a verification code has been sent.")
        )
        return redirect("accounts:otp-verify")

    if request.method == "POST":
        status = 400
    return _render_otp_request(request, form, status=status)


def _render_otp_request(request, form, *, status: int):
    """Render the start page. The ALTCHA widget fetches its session-bound
    challenge from `human_check_challenge`, and fetches a new one when it
    expires, so the typed email is never lost (UX-C2, UX-D01 option M). A
    re-rendered form after a refused check keeps the posted email too."""
    return render(
        request,
        "accounts/otp_request.html",
        {"form": form, "human_check_enabled": human_check.is_enabled()},
        status=status,
    )


@require_http_methods(["GET"])
def human_check_challenge(request):
    """A fresh signed ALTCHA challenge for the OTP request form, bound to the
    OTP-request action and to this browser session (never to an email). It is
    private, never cached and carries no personal data."""
    from django.conf import settings
    from django.http import Http404, JsonResponse

    if not human_check.is_enabled():
        raise Http404
    # P4-4: the per-network limit runs before the session is read or written,
    # so a refused burst creates no session row.
    if not human_check.challenge_issuance_allowed(resolve_request_client_network_identity(request)):
        refused = JsonResponse({"error": "try_again_later"}, status=429)
        refused["Retry-After"] = str(settings.HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS)
        refused["Cache-Control"] = "private, no-store"
        return refused
    new_session = request.session.is_empty()
    challenge = human_check.issue_challenge(human_check.ACTION_OTP_REQUEST, session=request.session)
    if new_session:
        human_check.mark_anonymous_session_short_lived(request.session)
    response = JsonResponse(challenge)
    response["Cache-Control"] = "private, no-store"
    return response


@require_http_methods(["GET", "POST"])
def otp_verify(request):
    if participant_auth.is_participant_authenticated(request):
        return redirect("registrations:workspace")

    pending_email = request.session.get(_PENDING_EMAIL_SESSION_KEY)
    if not pending_email:
        return redirect("accounts:otp-request")

    form = OtpVerifyForm(request.POST or None)
    error_message = None
    if request.method == "POST" and form.is_valid():
        from .otp import latest_pending_challenge_id

        challenge_id = latest_pending_challenge_id(
            channel=AuthenticationChallengeChannel.EMAIL, recipient_value=pending_email
        )
        if challenge_id is None:
            error_message = _("This code has expired. Request a new one.")
        else:
            result = consume_challenge(
                challenge_id=challenge_id,
                otp_value=form.cleaned_data["code"],
                correlation_id=get_correlation_id() or "",
            )
            if result.outcome == ConsumeOutcome.CONSUMED:
                person = resolve_or_create_participant_for_email(
                    pending_email, default_language=request.LANGUAGE_CODE
                )
                del request.session[_PENDING_EMAIL_SESSION_KEY]
                participant_auth.login_participant(request, person.id)
                # Resume a claim stashed by `invitations.views.on_behalf_claim`
                # for a visitor who was not yet authenticated (Phase 2
                # Prompt 2 V2 correction pass "make the participant claim
                # journey survive OTP authentication") -- checked, never
                # popped, here: `on_behalf_claim_resume` itself pops it, so
                # the reference survives this redirect.
                from apps.invitations.views import PENDING_CLAIM_SESSION_KEY

                if request.session.get(PENDING_CLAIM_SESSION_KEY):
                    return redirect("invitations:on-behalf-claim-resume")
                return redirect("registrations:workspace")
            error_message = {
                ConsumeOutcome.INVALID: _("That code is not correct. Please try again."),
                ConsumeOutcome.EXPIRED: _("This code has expired. Request a new one."),
                ConsumeOutcome.LOCKED: _(
                    "Too many incorrect attempts. Request a new code after a short wait."
                ),
                ConsumeOutcome.ALREADY_USED: _("This code has already been used."),
                ConsumeOutcome.NOT_FOUND: _("This code has expired. Request a new one."),
            }.get(result.outcome, _("This code could not be verified. Request a new one."))

    return render(
        request,
        "accounts/otp_verify.html",
        {"form": form, "email": pending_email, "error_message": error_message},
    )


@participant_auth.participant_required
@require_http_methods(["POST"])
def participant_session_extend(request):
    """Extend the participant session's inactivity clock only (Prompt 5
    correction pass §4). POST-only, CSRF-protected (Django's default
    middleware), and bounded by the absolute lifetime: `participant_required`
    itself already rejects an absolute-expired session before this view body
    ever runs, so extension can never push a session past its absolute
    deadline -- it only ever resets the inactivity deadline from "now"."""
    session_expiry.touch_participant_activity(request)
    messages.info(request, _("Your session has been extended."))
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    from django.utils.http import url_has_allowed_host_and_scheme

    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = "/"
    return redirect(next_url)


@require_http_methods(["POST"])
def participant_logout(request):
    """POST-only, CSRF-protected (Prompt 4 final closure pass §10) -- a GET-reachable
    logout link/URL is a session-riding CSRF vector: an attacker-controlled page could
    otherwise force a sign-out merely by getting the participant's browser to load it."""
    participant_auth.logout_participant(request)
    return redirect("accounts:otp-request")


#: The only post-sign-in destinations a `next` parameter may select: the
#: checkpoint (Phase 3 Prompt 4) and the back-office areas (UI/UX Completion
#: Gate F3). Never the sign-in/out routes or any participant or public page.
#: The destination still enforces its own permission: a signed-in user who
#: may not open it gets the 403 page there, never a redirect back here.
_OPERATIONAL_NEXT_PREFIXES = ("/entry/", "/ops/", "/organizations/")


def _operational_destination(request):
    from django.utils.http import url_has_allowed_host_and_scheme

    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if (
        candidate
        and candidate.startswith(_OPERATIONAL_NEXT_PREFIXES)
        and "\\" not in candidate
        and ".." not in candidate
        and candidate.isprintable()
        and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return candidate
    return None


def _operational_default_destination(user) -> str:
    """The first operations area this user may open (the same rule as the
    shell's brand link), so signing in never lands on a page that refuses."""
    from apps.core.navigation import operations_home_url

    return operations_home_url(user)


@sensitive_post_parameters("password", "captcha_answer")
@require_http_methods(["GET", "POST"])
def operational_sign_in(request):
    if request.user.is_authenticated:
        return redirect(
            _operational_destination(request) or _operational_default_destination(request.user)
        )

    form = OperationalSignInForm(request.POST or None)
    error_message = None
    status = 200
    captcha_error = None
    if request.method == "POST":
        # Staff sign-in image CAPTCHA (apps.accounts.captcha_guard): checked
        # FIRST and consumed whatever the outcome (one attempt per image). A
        # refusal never reaches the password check, so it neither signs in nor
        # counts towards the per-email or per-network sign-in limits; the page
        # then shows a new image and keeps the typed email.
        captcha_error = captcha_guard.verify(
            request.session, request.POST.get("captcha_key"), request.POST.get("captcha_answer")
        )
        if captcha_error:
            form.is_valid()
            form.add_error("captcha_answer", captcha_guard.MESSAGES[captcha_error])
    if request.method == "POST" and not captcha_error and form.is_valid():
        # P4-4 (P44-F03): attempt limits per typed email and per network, and
        # an audit event for every outcome (AF-AUTH-03).
        result = sign_in_attempts.attempt_sign_in(
            request,
            email=form.cleaned_data["email"],
            password=form.cleaned_data["password"],
            network_identity=resolve_request_client_network_identity(request),
            correlation_id=get_correlation_id() or "",
        )
        user = result.user
        if user is not None:
            login(request, user)
            # P4-4: the ordinary session lifetime, never a short-lived
            # pre-sign-in challenge session's.
            request.session.set_expiry(None)
            # AF-AUTH-04 / UI/UX §6.5: independently configured operational
            # inactivity/absolute session clocks (Prompt 5 correction
            # pass §4) -- separate from the participant session's own.
            session_expiry.establish_operational_session(request)
            return redirect(
                _operational_destination(request) or _operational_default_destination(user)
            )
        if result.throttled:
            # The same message for every refused attempt: it does not tell
            # whether the email belongs to an account.
            error_message = _("Too many sign-in attempts. Wait a few minutes and try again.")
            status = 429
        else:
            error_message = _("Incorrect email or password.")

    # Every rendered page (first visit or any refusal) gets a fresh
    # single-use challenge bound to this session -- unless this client network
    # requested too many images (shared limiter; no row, no session then).
    captcha = None
    if captcha_guard.issuance_allowed(resolve_request_client_network_identity(request)):
        new_session = request.session.is_empty()
        captcha = captcha_guard.issue(request.session)
        if new_session:
            human_check.mark_anonymous_session_short_lived(request.session)
    else:
        status = 429
    response = render(
        request,
        "accounts/operational_sign_in.html",
        {
            "form": form,
            "error_message": error_message,
            "captcha": captcha,
            "captcha_error": captcha_guard.MESSAGES.get(captcha_error) if captcha_error else "",
        },
        status=status,
    )
    response["Cache-Control"] = "no-store, private"
    return response


@require_http_methods(["GET"])
def staff_captcha_image(request, key):
    """The image of one challenge bound to THIS session (only the drawn
    characters; the answer never leaves the server). Never cached; an unknown,
    consumed, expired or foreign key answers 410 like the package."""
    from captcha import views as captcha_views
    from django.http import HttpResponse
    from django.utils.cache import add_never_cache_headers

    bound = captcha_guard._bound(request.session)
    if key not in bound.values():
        response = HttpResponse(status=410)
    else:
        response = captcha_views.captcha_image(request, key, scale=1)
    add_never_cache_headers(response)
    return response


@require_http_methods(["POST"])
def staff_captcha_refresh(request):
    """Replace this session's sign-in challenge (CSRF-protected POST; the
    sign-in page is anonymous). Answers with the new key and image URL only,
    never the answer; refused like the page when the network asked for too
    many images."""
    from django.http import JsonResponse
    from django.utils.cache import add_never_cache_headers

    def answer(data, status=200):
        response = JsonResponse(data, status=status)
        add_never_cache_headers(response)
        return response

    if request.POST.get("form") != captcha_guard.STAFF_SIGN_IN:
        return answer({"error": "INVALID_REQUEST"}, 400)
    if not captcha_guard.issuance_allowed(resolve_request_client_network_identity(request)):
        return answer({"error": "TRY_AGAIN_LATER"}, 429)
    challenge = captcha_guard.issue(request.session)
    return answer({"key": challenge.key, "image_url": challenge.image_url})


@login_required(login_url="accounts:operational-sign-in")
@require_http_methods(["POST"])
def operational_sign_out(request):
    """POST-only, CSRF-protected -- see `participant_logout` for the rationale.

    Uses `session_expiry.safe_operational_logout`, never
    `django.contrib.auth.logout()` directly: the latter flushes the WHOLE
    session, which would destroy a valid participant session sharing the
    same browser session (Prompt 5 correction pass §2).
    """
    session_expiry.safe_operational_logout(request)
    return redirect("accounts:operational-sign-in")


@login_required(login_url="accounts:operational-sign-in")
@require_http_methods(["POST"])
def operational_session_extend(request):
    """Extend the operational session's inactivity clock only -- mirrors
    `participant_session_extend` (Prompt 5 correction pass §4). Bounded by
    the absolute lifetime: `OperationalSessionExpiryMiddleware` itself
    already rejects an absolute-expired session before this view body ever
    runs."""
    session_expiry.touch_operational_activity(request)
    messages.info(request, _("Your session has been extended."))
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    from django.utils.http import url_has_allowed_host_and_scheme

    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = "/"
    return redirect(next_url)
