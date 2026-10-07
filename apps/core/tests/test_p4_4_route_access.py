"""P4-4: whole-platform route inventory and access guard.

1. Every URL route has an access class. A new route that nobody classified
   fails here, so it is reviewed before it ships.
2. Every operational route is gated by `operational_permission_required` or
   is one of the listed views that apply their own scoped policy in the view.
3. Behaviour, not only declarations: an anonymous visitor and a signed-in
   operational user WITHOUT any scoped membership are refused on every
   operational route, and an anonymous visitor is sent to the OTP start page on
   every participant route. A refusal comes before any object lookup, so
   random identifiers are enough.

The matrix in docs/security/p4_4_threat_permission_data_flow_matrix.md is
derived from the same inventory. Synthetic data only.
"""

from __future__ import annotations

import re
import uuid

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from apps.accounts.models import OperationalUser, OperationalUserStatus

pytestmark = pytest.mark.django_db

PUBLIC = {
    "healthz",
    "readyz",
    "robots.txt",
    "favicon.ico",
    "i18n/setlang/",
    "",
    "accounts/start/",
    "accounts/start/human-check/",
    "accounts/verify/",
    "accounts/sign-out/",
    "accounts/session/extend/",
    "accounts/ops/sign-in/",
    "accounts/ops/sign-out/",
    "accounts/ops/session/extend/",
    # Staff sign-in image: served only to the session it was issued to (410
    # otherwise); the refresh is a CSRF-protected POST returning a new key.
    "accounts/ops/sign-in/security-image/<str:key>/",
    "accounts/ops/sign-in/security-image-refresh/",
    # Staff credential setup: a single-use, expiring link moved into the
    # session, then a password form bound to that session only.
    "accounts/setup/<str:token>/",
    "accounts/setup/",
    "invite/<str:token>/",
    "claim/resume/",
    "claim/<str:token>/",
    "legal/",
    # Static per language; no participant value (ADR-0023).
    "entry/offline/",
    "entry/sw.js",
    "entry/manifest.webmanifest",
}
#: Participant routes: `participant_auth.participant_required`, then an
#: ownership-scoped lookup (`person=`) inside the view.
PARTICIPANT_PREFIXES = ("workspace/", "register/", "my-passes/")
#: A private document: operational scope OR participant ownership, checked in
#: the view, a generic 404 otherwise.
DOCUMENT = {"documents/<uuid:document_id>/"}
#: Operational routes that decide access in the view with a scoped policy
#: instead of the decorator (reviewed in P4-4; each refuses before lookup).
OPS_IN_VIEW_POLICY = {
    "ops/accreditation/registrations/<uuid:pk>/<str:kind>/assign/",
    "ops/accreditation/registrations/<uuid:pk>/<str:kind>/<uuid:assignment_pk>/revoke/",
    "ops/accreditation/registrations/<uuid:pk>/<str:kind>/<uuid:assignment_pk>/change/",
    "ops/exports/<uuid:pk>/download/",
    "ops/entry/events/<uuid:event_pk>/reconciliation/",
    "ops/entry/reconciliation/<str:public_id>/",
    "ops/entry/reconciliation/<str:public_id>/action/",
    "ops/entry/devices/<str:public_id>/emergency-wipe/",
    # Ministry NIN service diagnostics: a GLOBAL membership with
    # `people.run_ministry_nin_diagnostics` (or a superuser), checked in the
    # view and again by every service call (`apps.people.nin_diagnostics`).
    "ops/integrations/ministry-nin/",
    "ops/integrations/ministry-nin/authentication/",
    "ops/integrations/ministry-nin/lookup/",
}
#: Checkpoint (gate) routes: `checkpoint_required` = operational sign-in plus
#: an enrolled device cookie plus an open checkpoint session.
CHECKPOINT_PREFIX = "entry/"
#: Device API: DRF, deny-by-default, operational session plus device proof;
#: the quarantine endpoint is signature-only by design (ADR-0024).
DEVICE_API_PREFIX = "entry/api/v1/"


def _walk(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            yield prefix + str(pattern.pattern), pattern


ROUTES = dict(_walk(get_resolver().url_patterns))


def _wrapper_codes(callback):
    """`co_qualname` of every wrapper: `functools.wraps` rewrites
    `__qualname__`, but never the code object's own name."""
    names, func, seen = [], callback, set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        code = getattr(func, "__code__", None)
        if code is not None:
            names.append(code.co_qualname)
        func = getattr(func, "__wrapped__", None)
    return names


def _permission(callback) -> str | None:
    func, seen = callback, set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        code = getattr(func, "__code__", None)
        for name, cell in zip(
            getattr(code, "co_freevars", ()), getattr(func, "__closure__", None) or (), strict=False
        ):
            if name == "permission_codename":
                return cell.cell_contents
        func = getattr(func, "__wrapped__", None)
    return None


def classify(route: str, callback) -> str:
    if route in PUBLIC:
        return "public"
    if route in DOCUMENT:
        return "document"
    if route.startswith(DEVICE_API_PREFIX):
        return "device-api"
    if route.startswith(("ops/", "organizations/")):
        if _permission(callback):
            return "operational-permission"
        if route in OPS_IN_VIEW_POLICY:
            return "operational-in-view"
        return "UNCLASSIFIED"
    if route.startswith(PARTICIPANT_PREFIXES):
        codes = _wrapper_codes(callback)
        return (
            "participant"
            if any(code.startswith("participant_required") for code in codes)
            else "UNCLASSIFIED"
        )
    if route.startswith(CHECKPOINT_PREFIX):
        return "checkpoint"
    if route == "admin/" or route.startswith("admin/"):
        return "admin-local-only"
    return "UNCLASSIFIED"


def _concrete(route: str) -> str:
    def value(match):
        converter = match.group(1)
        return str(uuid.uuid4()) if converter == "uuid" else "p44-x"

    return "/" + re.sub(r"<(\w+):\w+>", value, route)


def test_every_route_has_an_access_class() -> None:
    unclassified = [
        route
        for route, pattern in ROUTES.items()
        if classify(route, pattern.callback) == "UNCLASSIFIED"
    ]
    assert unclassified == []


def test_the_in_view_policy_list_has_no_stale_entry() -> None:
    assert OPS_IN_VIEW_POLICY <= set(ROUTES)
    for route in OPS_IN_VIEW_POLICY:
        assert _permission(ROUTES[route].callback) is None, route


OPERATIONAL = sorted(
    route
    for route, pattern in ROUTES.items()
    if classify(route, pattern.callback) in {"operational-permission", "operational-in-view"}
)
PARTICIPANT = sorted(
    route for route, pattern in ROUTES.items() if classify(route, pattern.callback) == "participant"
)


@pytest.mark.parametrize("route", OPERATIONAL)
@pytest.mark.parametrize("method", ["get", "post"])
def test_anonymous_visitors_are_refused_on_every_operational_route(route, method) -> None:
    response = getattr(Client(), method)(_concrete(route))
    assert response.status_code in {302, 403, 404, 405}, response.status_code
    if response.status_code == 302:
        assert "/accounts/ops/sign-in/" in response["Location"]


@pytest.fixture
def unscoped_user():
    from django.utils import timezone

    from apps.accounts import session_expiry

    user = OperationalUser.objects.create_user(
        email="p44-unscoped@example.test",
        password=None,  # force_login below; no password is needed
        status=OperationalUserStatus.ACTIVE,
    )
    client = Client()
    client.force_login(user)
    session = client.session
    now = timezone.now().isoformat()
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return client


@pytest.mark.parametrize("route", OPERATIONAL)
@pytest.mark.parametrize("method", ["get", "post"])
def test_signed_in_users_without_scope_are_refused_on_every_operational_route(
    unscoped_user, route, method
) -> None:
    response = getattr(unscoped_user, method)(_concrete(route))
    assert response.status_code in {403, 404, 405}, (route, response.status_code)


@pytest.mark.parametrize("route", PARTICIPANT)
def test_anonymous_visitors_are_sent_to_the_start_page_on_participant_routes(route) -> None:
    response = Client().get(_concrete(route))
    assert response.status_code in {302, 405}
    if response.status_code == 302:
        assert response["Location"].endswith("/accounts/start/")


def test_a_private_document_is_a_generic_404_for_everyone_else() -> None:
    response = Client().get(_concrete("documents/<uuid:document_id>/"))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "route",
    [
        "ops/accreditation/registrations/<uuid:pk>/p44-x/assign/",
        "ops/accreditation/registrations/<uuid:pk>/p44-x/<uuid:assignment_pk>/revoke/",
    ],
)
def test_an_unknown_assignment_kind_is_a_404_before_any_state_change(route) -> None:
    """P4-4 finding P44-F02: these views stored a flash message (the `messages`
    cookie) and redirected before any authorization check."""
    from django.contrib.sessions.models import Session

    before = Session.objects.count()
    client = Client()
    response = client.post(_concrete(route))
    assert response.status_code == 404
    assert Session.objects.count() == before
    assert "sessionid" not in response.cookies
    assert "messages" not in response.cookies
