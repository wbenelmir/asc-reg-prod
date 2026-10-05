"""Health and readiness endpoints (accepted plan Prompt 2 CP 2f).

Liveness answers "is the process running" with no dependency checks at all.
Readiness performs a bare `SELECT 1` against the default database
connection -- it does not and must not require any project table -- a
private-storage readiness check (P4-4) and, since P4-4-C3, the state of the
challenge-issuance counter (CACHE-01).
Neither endpoint requires authentication, and
neither ever includes a credential, connection string, or any other secret
in its response.
"""

from __future__ import annotations

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.templatetags.static import static
from django.views.decorators.http import require_GET
from django.views.i18n import set_language as _django_set_language


def liveness(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def _database_ready() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return True


def _storage_ready() -> bool:
    # P4-4-C1 (R-02): a backend-aware check. A missing local root or an absent
    # bucket is unavailable; a healthy absent object is not. See
    # `check_private_storage_ready` for what is and is not proven.
    from apps.documents.storage import check_private_storage_ready

    check_private_storage_ready()
    return True


def _challenge_counter_state() -> str:
    # P4-4-C3 (CACHE-01), corrected in P4-4-C4 (R-C3-01): "shared" needs a
    # completed counter write (an issuance count or the bounded capability
    # write on the probe key), never a PING; "local" means none is configured
    # (local development and tests), and is never reported as shared.
    from apps.core.issuance_counter import STATE_UNAVAILABLE, get_issuance_limiter

    try:
        return get_issuance_limiter().probe()
    except Exception:
        return STATE_UNAVAILABLE


def readiness(request: HttpRequest) -> JsonResponse:
    """P4-4 (DEP-005, partial) and P4-4-C3: the database, the private storage
    adapter and the challenge-issuance counter.

    * `database` and `storage` are "reachable" or "unreachable"; either one
      unreachable is HTTP 503.
    * `challenge_counter` is "shared" (a shared counter write completed within
      the retry interval; P4-4-C4), "local" (no shared counter by
      configuration; local development only),
      "degraded" (the shared counter fails and the stricter per-process
      fallback limits issuance) or "unavailable" (no counter can count, so
      issuance is refused). Only "unavailable" is HTTP 503: a degraded node
      still serves bounded traffic, and failing every node at once on a shared
      Redis outage would take the whole service down. "degraded" never hides
      another failure: the status is "unavailable" with 503 whenever any
      required dependency fails.

    No answer ever includes an exception text, a host, a bucket or a key. The
    broker, the mail provider and the signing-key alignment are not probed
    here (see docs/security/p4_4_hardening_notes.md)."""
    probes = {"database": _database_ready, "storage": _storage_ready}
    body: dict[str, str] = {}
    for name, probe in probes.items():
        try:
            ready = probe()
        except Exception:
            # Intentionally generic: never include the driver's exception text,
            # which could contain the connection string or host details.
            ready = False
        body[name] = "reachable" if ready else "unreachable"
    body["challenge_counter"] = _challenge_counter_state()
    dependencies_ok = all(body[name] == "reachable" for name in probes)
    if not dependencies_ok or body["challenge_counter"] == "unavailable":
        status, http_status = "unavailable", 503
    elif body["challenge_counter"] == "degraded":
        status, http_status = "degraded", 200
    else:
        status, http_status = "ok", 200
    response = JsonResponse({"status": status, **body}, status=http_status)
    response["Cache-Control"] = "no-store"
    return response


# Only the public participant start page may be crawled (UX-1, decision S-05).
# `Disallow: /` covers everything else; no sitemap is published, so no private
# URL (invitation, claim, pass, export, entry) is ever listed.
_ROBOTS_TXT = "User-agent: *\nAllow: /accounts/start/\nDisallow: /\n"


@require_GET
def robots_txt(request: HttpRequest) -> HttpResponse:
    return HttpResponse(_ROBOTS_TXT, content_type="text/plain; charset=utf-8")


@require_GET
def favicon_ico(request: HttpRequest) -> HttpResponse:
    """Browsers ask for `/favicon.ico` by convention even when the page links
    another icon; answer with the compact mark PNG instead of a 404."""
    return HttpResponsePermanentRedirect(static("img/brand/asc-mark-32.png"))


_VALID_LANGUAGE_CODES = frozenset(code for code, _name in settings.LANGUAGES)


def set_language(request: HttpRequest) -> HttpResponse:
    """Wraps Django's own `set_language` view to ALSO keep the active
    Registration Context's (and the Person's) recorded language preference
    in sync (Prompt 5 correction pass §5): "the active language must update
    the participant/registration preference safely so legal notices and
    confirmation delivery use the intended language."

    Never creates, duplicates, or resubmits anything -- this only updates an
    existing `preferred_language` column on the current Draft (if any) and
    the current Person, exactly like any other project selector/service
    read/update, using the SAME requested language Django's own view just
    validated and applied to the session/cookie.
    """
    response = _django_set_language(request)
    if request.method != "POST":
        return response
    requested_language = request.POST.get("language")
    if requested_language not in _VALID_LANGUAGE_CODES:
        return response

    from apps.accounts.participant_auth import (
        get_active_registration_id,
        get_valid_participant_person_id,
    )

    # `get_valid_participant_person_id`, never the raw, expiry-blind
    # accessor (Prompt 5 correction pass §1): an expired or malformed
    # participant session must never have its language preference synced,
    # exactly as if no participant were signed in at all.
    person_id = get_valid_participant_person_id(request)
    if not person_id:
        return response

    from apps.people.models import Person

    Person.objects.filter(pk=person_id).update(preferred_language=requested_language)

    registration_id = get_active_registration_id(request)
    if registration_id:
        from apps.registrations.models import Registration, RegistrationPublicStatus

        Registration.objects.filter(
            pk=registration_id, public_status=RegistrationPublicStatus.DRAFT
        ).update(preferred_language=requested_language)

    return response


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Localized CSRF failure page (settings.CSRF_FAILURE_VIEW; UI/UX
    Completion Gate F4).

    Django's CSRF middleware still decides every rejection; this view only
    renders the response. `reason` is deliberately not shown or echoed:
    Django's own `django.security.csrf` logger already records it, and it can
    describe the request's cookies and referer to whoever triggered it.
    """
    from django.shortcuts import render

    return render(request, "csrf_failure.html", status=403)
