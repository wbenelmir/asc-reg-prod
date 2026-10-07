"""Ministry NIN service diagnostics page (`/ops/integrations/ministry-nin/`).

GET shows the configuration readiness and the recent checks and never sends
anything. Each network check is its own POST (CSRF-protected): "Test
authentication" or "Test lookup". The result is rendered in the POST response
itself -- never put in the session, a message, a URL or a log -- and every
response is `no-store`. The test number is never echoed back into the form.
Authorization is `apps.people.nin_diagnostics.can_run`, checked here and again
by every service call.
"""

from __future__ import annotations

from django.shortcuts import render
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.policies import deny_operational_access
from apps.core.middleware.correlation import get_correlation_id
from apps.people import nin_diagnostics as diagnostics

_LOGIN = "accounts:operational-sign-in"

#: (title, meaning, next action, tone) per outcome code.
_OUTCOMES = {
    diagnostics.NOT_OFFICIAL: (
        gettext_lazy("The official Ministry service is not configured here"),
        gettext_lazy("This environment does not use the official adapter. Nothing was sent."),
        gettext_lazy(
            "The deployment team sets NIN_PROVIDER_BACKEND to the official adapter and "
            "configures its settings."
        ),
        "neutral",
    ),
    diagnostics.NOT_CONFIGURED: (
        gettext_lazy("Configuration incomplete"),
        gettext_lazy("Required settings are missing or invalid. Nothing was sent."),
        gettext_lazy("The deployment team completes the settings listed under Configuration."),
        "warning",
    ),
    diagnostics.AUTH_OK: (
        gettext_lazy("Authentication succeeded"),
        gettext_lazy(
            "A new authentication request was accepted just now (not a cached token). "
            "This does not show that the lookup endpoint works: test a lookup for that."
        ),
        "",
        "success",
    ),
    diagnostics.AUTH_FAILED: (
        gettext_lazy("Authentication failed"),
        gettext_lazy("The authentication endpoint did not accept the request."),
        gettext_lazy(
            "The deployment team checks the configured credentials and the authentication "
            "request and response contract with the provider. Do not retry repeatedly."
        ),
        "danger",
    ),
    diagnostics.IDENTITY_FOUND: (
        gettext_lazy("Lookup answered: identity found"),
        gettext_lazy(
            "The lookup endpoint answered with an identity for this number. Nothing was "
            "recorded, and this check verifies no participant."
        ),
        "",
        "success",
    ),
    diagnostics.IDENTITY_NOT_FOUND: (
        gettext_lazy("Lookup answered: no identity found"),
        gettext_lazy(
            "The lookup endpoint answered successfully but holds no identity for this "
            "number. This is not an authentication failure and not a decision about anyone."
        ),
        "",
        "success",
    ),
    diagnostics.UNAVAILABLE: (
        gettext_lazy("Service unavailable or too slow"),
        gettext_lazy("No usable answer arrived in time, or the connection failed."),
        gettext_lazy(
            "Try again later. If it persists, the deployment team checks network routing, "
            "TLS and the provider's status."
        ),
        "warning",
    ),
    diagnostics.REDIRECT_REFUSED: (
        gettext_lazy("Redirect refused"),
        gettext_lazy(
            "The service answered with a redirect. Redirects are never followed, so "
            "credentials and identity numbers never go to another address."
        ),
        gettext_lazy(
            "The deployment team confirms the canonical address with the provider and "
            "corrects MINISTRY_NIN_API_BASE_URL and the configured paths (including any "
            "trailing slash), or the proxy routing."
        ),
        "danger",
    ),
    diagnostics.MALFORMED: (
        gettext_lazy("Unreadable or unexpected answer"),
        gettext_lazy("The answer could not be read safely and was discarded."),
        gettext_lazy("The deployment team checks the response contract with the provider."),
        "danger",
    ),
    diagnostics.PROVIDER_RATE_LIMITED: (
        gettext_lazy("The provider asked to slow down"),
        gettext_lazy("The service answered that too many requests were made (HTTP 429)."),
        gettext_lazy("Wait several minutes before another check."),
        "warning",
    ),
    diagnostics.INVALID_INPUT: (
        gettext_lazy("Check the test number"),
        gettext_lazy("An identity number has exactly 18 digits. Nothing was sent."),
        "",
        "warning",
    ),
    diagnostics.THROTTLED: (
        gettext_lazy("Too many checks"),
        gettext_lazy(
            "Checks are limited per operator and for the whole environment. Nothing was sent."
        ),
        gettext_lazy("Wait a few minutes before the next check."),
        "warning",
    ),
    diagnostics.BUSY: (
        gettext_lazy("Another check is running"),
        gettext_lazy("Only one check runs at a time. Nothing was sent."),
        gettext_lazy("Try again in a moment."),
        "neutral",
    ),
}

_ACTIONS = {
    diagnostics.ACTION_AUTHENTICATION: gettext_lazy("Authentication"),
    diagnostics.ACTION_LOOKUP: gettext_lazy("Lookup"),
}


def _outcome_label(code: str) -> str:
    entry = _OUTCOMES.get(code)
    return str(entry[0]) if entry else code


def _page(request, *, result=None, status=200):
    readiness = diagnostics.readiness(request.user)
    recent = diagnostics.recent_checks(request.user)
    for row in recent:
        row["action_label"] = str(_ACTIONS.get(row["action"], row["action"]))
        row["outcome_label"] = _outcome_label(row["outcome"])
    presented = None
    if result is not None:
        title, meaning, action, tone = _OUTCOMES[result.outcome]
        presented = {
            "result": result,
            "action_label": str(_ACTIONS[result.action]),
            "title": str(title),
            "meaning": str(meaning),
            "next_action": str(action),
            "tone": tone,
        }
    response = render(
        request,
        "people/ministry_nin_diagnostics.html",
        {"readiness": readiness, "recent": recent, "presented": presented},
        status=status,
    )
    # Never cached. The site's `same-origin` referrer policy stays: the page's
    # URL never holds a number, and `no-referrer` would make browsers send
    # `Origin: null` with its forms, which CSRF origin checking refuses.
    response["Cache-Control"] = "no-store, private"
    return response


@never_cache
@require_GET
def ministry_nin_diagnostics(request):
    if not diagnostics.can_run(request.user):
        return deny_operational_access(request, login_url=_LOGIN)
    return _page(request)


@never_cache
@require_POST
def ministry_nin_authentication_check(request):
    if not diagnostics.can_run(request.user):
        return deny_operational_access(request, login_url=_LOGIN)
    result = diagnostics.run_authentication_check(
        request.user, correlation_id=get_correlation_id() or ""
    )
    return _page(request, result=result)


@sensitive_post_parameters("nin")
@never_cache
@require_POST
def ministry_nin_lookup_check(request):
    if not diagnostics.can_run(request.user):
        return deny_operational_access(request, login_url=_LOGIN)
    result = diagnostics.run_lookup_check(
        request.user, request.POST.get("nin", ""), correlation_id=get_correlation_id() or ""
    )
    status = 400 if result.outcome == diagnostics.INVALID_INPUT else 200
    return _page(request, result=result, status=status)


def outcome_choices() -> dict[str, str]:
    """Outcome code -> localized title (documentation and tests)."""
    return {code: str(entry[0]) for code, entry in _OUTCOMES.items()}
