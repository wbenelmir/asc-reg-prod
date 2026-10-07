"""Ministry NIN service diagnostics (`apps.people.nin_diagnostics`, page
`/ops/integrations/ministry-nin/`).

The provider's HTTPS transport is replaced by a scripted fake: nothing leaves
the test process. Synthetic NINs, names, credentials and tokens only.
"""

from __future__ import annotations

import json
import logging

import pytest
from django.contrib.auth.models import Group
from django.db import connections
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.core.concurrency import ADVISORY_LOCK_CLASS_NIN_DIAGNOSTICS
from apps.events.models import EventEdition
from apps.people import nin_diagnostics as diagnostics
from apps.people import nin_provider
from apps.people.nin_provider import HttpResponse, TransportError

pytestmark = pytest.mark.django_db

GOOD = "diagnostics-tests-correct-horse-42"
TEST_NIN = "990000000000000077"
ISSUED_BEARER = "synthetic-diagnostic-bearer-ABCDEFGHIJKLMNOP"
RAW_MARKER = "raw-provider-body-marker"
CONFIGURED_B = "synthetic-pass-value"
FOUND_BODY = {
    "identite": {
        "nin": TEST_NIN,
        "nom_f": "DIAGNOSTIQUE",
        "pren_f": "SYNTHETIQUE",
        "d_nais": "01/01/1990",
        "presume": "False",
    },
    "note": RAW_MARKER,
}

OFFICIAL = {
    "NIN_PROVIDER_BACKEND": "apps.people.nin_provider.MinistryNinProvider",
    "MINISTRY_NIN_API_BASE_URL": "https://ministry.example.test",
    "MINISTRY_NIN_API_USERNAME": "synthetic-user",
    "MINISTRY_NIN_API_PASSWORD": CONFIGURED_B,
    "MINISTRY_NIN_API_AUTH_TOKEN_PATH": "token",
    "MINISTRY_NIN_API_AUTH_EXPIRY_PATH": "",
    "MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT": "",
}


def _json(status, document):
    return HttpResponse(status=status, body=json.dumps(document).encode())


AUTH_OK_RESPONSE = _json(200, {"token": ISSUED_BEARER, "note": RAW_MARKER})


class FakeTransport:
    """Records every request; answers from per-method scripts."""

    calls: list = []
    auth: list = []
    lookup: list = []

    def __init__(self, config):
        self.config = config

    def request(self, method, path, *, headers, body):
        FakeTransport.calls.append((method, path))
        script = FakeTransport.auth if method == "POST" else FakeTransport.lookup
        answer = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def fake_transport(monkeypatch, settings):
    for name, value in OFFICIAL.items():
        setattr(settings, name, value)
    settings.NIN_DIAGNOSTICS_MAX_PER_OPERATOR = 50
    settings.NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT = 100
    FakeTransport.calls = []
    FakeTransport.auth = [AUTH_OK_RESPONSE]
    FakeTransport.lookup = [_json(200, {"identite": None})]
    monkeypatch.setattr(nin_provider, "HttpsTransport", FakeTransport)
    nin_provider.clear_token_cache()
    diagnostics.reset_limiters()
    yield FakeTransport
    nin_provider.clear_token_cache()
    diagnostics.reset_limiters()


def _staff(email, group=None, *, event=None, superuser=False):
    user = OperationalUser.objects.create_user(
        email=email, password=GOOD, status=OperationalUserStatus.ACTIVE, is_superuser=superuser
    )
    if group:
        ScopedGroupMembership.objects.create(
            user=user, group=Group.objects.get(name=group), event_edition=event, granted_by=user
        )
    return user


@pytest.fixture
def operator():
    return _staff("tech-operator@example.test", "Integration Diagnostics Operators")


def _client(user) -> Client:
    client = Client()
    assert staff_sign_in(client, user.email_normalized, GOOD).status_code == 302
    return client


URL = reverse("identity:nin-diagnostics")
AUTH_URL = reverse("identity:nin-diagnostics-authentication")
LOOKUP_URL = reverse("identity:nin-diagnostics-lookup")


def _outcome(response) -> str:
    return response.context["presented"]["result"].outcome


def _no_secret_in(text: str) -> None:
    for secret in (ISSUED_BEARER, TEST_NIN, RAW_MARKER, "DIAGNOSTIQUE", "SYNTHETIQUE",
                   "synthetic-pass-value", "synthetic-user", "ministry.example.test"):  # fmt: skip
        assert secret not in text, secret


# ---------------------------------------------------------------------------
# Readiness: no request
# ---------------------------------------------------------------------------


def test_opening_the_page_sends_nothing(operator, fake_transport):
    client = _client(operator)
    # Query parameters never trigger a check (a number is never put in a URL
    # by the page; the language switcher would echo a hand-typed one).
    assert client.get(URL, {"nin": TEST_NIN, "action": "lookup"}).status_code == 200
    response = client.get(URL)
    assert response.status_code == 200
    assert fake_transport.calls == []
    readiness = response.context["readiness"]
    assert readiness.is_official and readiness.ready
    assert readiness.backend_code == "MINISTRY_NIN_API"
    assert response["Cache-Control"].startswith("no-store")
    _no_secret_in(response.content.decode())


def test_missing_settings_are_named_never_shown(operator, settings, fake_transport):
    settings.MINISTRY_NIN_API_PASSWORD = ""
    settings.MINISTRY_NIN_API_BASE_URL = "http://not-https.example.test/path"
    response = _client(operator).get(URL)
    readiness = response.context["readiness"]
    assert readiness.missing_settings == ("MINISTRY_NIN_API_PASSWORD",)
    assert "MINISTRY_NIN_API_BASE_URL" in readiness.invalid_settings
    html = response.content.decode()
    assert "not-https.example.test" not in html
    assert "synthetic-user" not in html
    posted = _client(operator).post(AUTH_URL)
    assert _outcome(posted) == diagnostics.NOT_CONFIGURED
    assert fake_transport.calls == []


def test_a_disabled_backend_sends_nothing(operator, settings, fake_transport):
    settings.NIN_PROVIDER_BACKEND = "apps.people.nin_provider.DisabledNinProvider"
    client = _client(operator)
    assert client.get(URL).context["readiness"].is_official is False
    assert _outcome(client.post(AUTH_URL)) == diagnostics.NOT_OFFICIAL
    assert _outcome(client.post(LOOKUP_URL, {"nin": TEST_NIN})) == diagnostics.NOT_OFFICIAL
    assert fake_transport.calls == []


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_authentication_is_fresh_even_with_a_cached_token(operator, fake_transport):
    client = _client(operator)
    client.post(LOOKUP_URL, {"nin": TEST_NIN})  # fills this process's token cache
    cached = nin_provider._TOKEN_CACHE.current()
    assert cached is not None
    fake_transport.calls.clear()
    response = client.post(AUTH_URL)
    assert _outcome(response) == diagnostics.AUTH_OK
    assert fake_transport.calls == [("POST", "/api/auth/")]  # a new request, not the cache
    assert nin_provider._TOKEN_CACHE.current() is cached  # normal lookups keep their token
    result = response.context["presented"]["result"]
    assert result.token_lifetime_seconds == 300 and result.reached_provider
    _no_secret_in(response.content.decode())


def test_a_declared_token_expiry_is_reported_without_the_token(operator, settings, fake_transport):
    settings.MINISTRY_NIN_API_AUTH_EXPIRY_PATH = "expires_in"
    settings.MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT = "relative_seconds"
    fake_transport.auth = [_json(200, {"token": ISSUED_BEARER, "expires_in": 120})]
    response = _client(operator).post(AUTH_URL)
    assert _outcome(response) == diagnostics.AUTH_OK
    assert response.context["presented"]["result"].token_lifetime_seconds == 90
    _no_secret_in(response.content.decode())


@pytest.mark.parametrize(
    ("answer", "outcome", "detail"),
    [
        (_json(400, {"error": RAW_MARKER}), diagnostics.AUTH_FAILED, "auth_unexpected_status"),
        (_json(401, {"error": RAW_MARKER}), diagnostics.AUTH_FAILED, "credentials_rejected"),
        (_json(403, {"error": RAW_MARKER}), diagnostics.AUTH_FAILED, "credentials_rejected"),
        (
            _json(301, {"location": RAW_MARKER}),
            diagnostics.REDIRECT_REFUSED,
            "auth_unexpected_status",
        ),
        (_json(429, {}), diagnostics.PROVIDER_RATE_LIMITED, "auth_unavailable"),
        (_json(503, {}), diagnostics.UNAVAILABLE, "auth_unavailable"),
        (TransportError("timeout"), diagnostics.UNAVAILABLE, "auth_timeout"),
        (
            HttpResponse(200, b"{not json " + RAW_MARKER.encode()),
            diagnostics.MALFORMED,
            "auth_response_invalid",
        ),
        (_json(200, {"access": ISSUED_BEARER}), diagnostics.AUTH_FAILED, "auth_contract_mismatch"),
    ],
)
def test_authentication_failures_are_named_and_sanitized(
    operator, fake_transport, answer, outcome, detail
):
    fake_transport.auth = [answer]
    response = _client(operator).post(AUTH_URL)
    result = response.context["presented"]["result"]
    assert (result.outcome, result.detail) == (outcome, detail)
    assert response.context["presented"]["next_action"] or outcome == diagnostics.AUTH_OK
    assert nin_provider._TOKEN_CACHE.current() is None
    _no_secret_in(response.content.decode())


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_a_found_identity_is_reported_without_identity_data(operator, fake_transport, caplog):
    fake_transport.lookup = [_json(200, FOUND_BODY)]
    with caplog.at_level(logging.DEBUG):
        response = _client(operator).post(LOOKUP_URL, {"nin": TEST_NIN})
    assert _outcome(response) == diagnostics.IDENTITY_FOUND
    assert ("GET", f"/api/get/{TEST_NIN}") in fake_transport.calls
    html = response.content.decode()
    _no_secret_in(html)
    assert 'value="' + TEST_NIN not in html
    _no_secret_in(caplog.text)


def test_identite_null_is_a_successful_lookup_without_identity(operator, fake_transport):
    response = _client(operator).post(LOOKUP_URL, {"nin": TEST_NIN})
    assert _outcome(response) == diagnostics.IDENTITY_NOT_FOUND
    assert response.context["presented"]["tone"] == "success"
    _no_secret_in(response.content.decode())


@pytest.mark.parametrize("nin", ["", "12345", "99000000000000007A", TEST_NIN + "0", " ' or 1=1"])
def test_an_invalid_number_is_refused_before_any_request(operator, fake_transport, nin):
    response = _client(operator).post(LOOKUP_URL, {"nin": nin})
    assert response.status_code == 400
    assert _outcome(response) == diagnostics.INVALID_INPUT
    assert fake_transport.calls == []


@pytest.mark.parametrize(
    ("answer", "outcome", "detail"),
    [
        (_json(301, {"location": RAW_MARKER}), diagnostics.REDIRECT_REFUSED, "redirect_refused"),
        (_json(401, {}), diagnostics.AUTH_FAILED, "rejected_after_reauthentication"),
        (_json(403, {}), diagnostics.AUTH_FAILED, "forbidden"),
        (_json(429, {}), diagnostics.PROVIDER_RATE_LIMITED, "service_error"),
        (_json(500, {}), diagnostics.UNAVAILABLE, "service_error"),
        (TransportError("timeout"), diagnostics.UNAVAILABLE, "timeout"),
        (TransportError("tls"), diagnostics.UNAVAILABLE, "tls"),
        (
            HttpResponse(200, b"{broken " + RAW_MARKER.encode()),
            diagnostics.MALFORMED,
            "json_invalid",
        ),
        (TransportError("too_large"), diagnostics.MALFORMED, "response_too_large"),
    ],
)
def test_lookup_failures_are_named_and_sanitized(operator, fake_transport, answer, outcome, detail):
    fake_transport.lookup = [answer]
    response = _client(operator).post(LOOKUP_URL, {"nin": TEST_NIN})
    result = response.context["presented"]["result"]
    assert (result.outcome, result.detail) == (outcome, detail)
    _no_secret_in(response.content.decode())


def test_a_lookup_changes_nothing_but_one_sanitized_audit_event(operator, fake_transport):
    from django.apps import apps

    fake_transport.lookup = [_json(200, FOUND_BODY)]
    models = [
        apps.get_model(label)
        for label in (
            "registrations.Registration",
            "people.Person",
            "people.IdentityVerification",
            "reviews.ReviewCase",
            "reviews.RegistrationDecision",
            "badges.BadgeIssuance",
            "communications.CommunicationMessage",
            "accounts.OperationalUser",
        )
    ]
    client = _client(operator)
    before = {model: model.objects.count() for model in models}
    audit_before = AuditEvent.objects.count()
    client.post(LOOKUP_URL, {"nin": TEST_NIN})
    assert {model: model.objects.count() for model in models} == before
    new = list(AuditEvent.objects.order_by("id")[audit_before:])
    diagnostic = [e for e in new if e.action_code == action_codes.IDV_NIN_DIAGNOSTIC_RUN]
    assert len(diagnostic) == 1
    event = diagnostic[0]
    assert event.actor_user_id == operator.pk
    assert event.after_summary["action"] == "LOOKUP"
    assert event.after_summary["outcome"] == diagnostics.IDENTITY_FOUND
    assert event.after_summary["environment"] == "test"
    serialized = json.dumps(
        {"after": event.after_summary, "before": event.before_summary, "reason": event.reason_code}
    )
    _no_secret_in(serialized)
    assert {e.action_code for e in new} <= {
        action_codes.IDV_NIN_DIAGNOSTIC_RUN,
        *{e.action_code for e in new if e.action_code.startswith("ACC_")},
    }


def test_recent_checks_show_history_metadata_only(operator, fake_transport):
    client = _client(operator)
    client.post(AUTH_URL)
    response = client.get(URL)
    (row,) = response.context["recent"]
    assert row["action"] == "AUTHENTICATION" and row["outcome"] == diagnostics.AUTH_OK
    assert "not the current state of the service" in response.content.decode()


# ---------------------------------------------------------------------------
# Who may use it
# ---------------------------------------------------------------------------


def test_only_a_global_diagnostics_operator_or_superuser_gets_in(fake_transport):
    event = EventEdition.objects.create(
        code="DIAG1",
        name="Diag",
        timezone="UTC",
        starts_at="2026-12-01T00:00Z",
        ends_at="2026-12-04T00:00Z",
    )
    denied = [
        _staff("scoped-diag@example.test", "Integration Diagnostics Operators", event=event),
        _staff("event-admin@example.test", "Account Administrators", event=event),
        _staff("manager@example.test", "Accreditation Managers", event=event),
        _staff("reviewer@example.test", "Registration Reviewers", event=event),
        _staff("intake@example.test", "Registration Intake"),
        _staff("no-role@example.test"),
    ]
    for user in denied:
        client = _client(user)
        assert client.get(URL).status_code == 403, user.email_normalized
        assert client.post(AUTH_URL).status_code == 403, user.email_normalized
        assert client.post(LOOKUP_URL, {"nin": TEST_NIN}).status_code == 403
    assert fake_transport.calls == []
    for user in (
        _staff("global-diag@example.test", "Integration Diagnostics Operators"),
        _staff("root@example.test", superuser=True),
    ):
        assert _client(user).get(URL).status_code == 200


def test_anonymous_and_participants_are_sent_to_staff_sign_in(fake_transport):
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import participant_client

    participant = participant_client(resolve_or_create_participant_for_email("p@example.test"))
    for client in (Client(), participant):
        response = client.get(URL)
        assert response.status_code == 302 and "/accounts/ops/sign-in/" in response["Location"]
        assert client.post(LOOKUP_URL, {"nin": TEST_NIN}).status_code == 302
    assert fake_transport.calls == []


def test_checks_need_post_and_a_csrf_token(operator, fake_transport):
    client = _client(operator)
    assert client.get(AUTH_URL).status_code == 405
    assert client.get(LOOKUP_URL, {"nin": TEST_NIN}).status_code == 405
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = client.cookies
    assert strict.post(AUTH_URL).status_code == 403
    assert strict.post(LOOKUP_URL, {"nin": TEST_NIN}).status_code == 403
    assert fake_transport.calls == []


def test_the_role_is_granted_only_platform_wide(operator):
    from apps.accounts import administration as admin

    root = _staff("root-admin@example.test", "Account Administrators")
    target = _staff("candidate@example.test")
    event = EventEdition.objects.create(
        code="DIAG2",
        name="Diag2",
        timezone="UTC",
        starts_at="2026-12-01T00:00Z",
        ends_at="2026-12-04T00:00Z",
    )
    with pytest.raises(admin.AccountAdministrationError) as refused:
        admin.grant_role(
            actor=root,
            target=target,
            role_key="integration-diagnostics",
            event=event,
            reason="Test",
        )
    assert refused.value.code == "INVALID_SCOPE"
    admin.grant_role(
        actor=root,
        target=target,
        role_key="integration-diagnostics",
        all_events=True,
        reason="Test",
    )
    assert diagnostics.can_run(target)


def test_the_navigation_shows_the_page_only_to_permitted_users(operator):
    from apps.core.navigation import operations_navigation

    assert "integrations" in {item.key for item in operations_navigation(operator)}
    other = _staff("plain@example.test", "Accreditation Managers")
    assert "integrations" not in {item.key for item in operations_navigation(other)}


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_checks_are_limited_per_operator_and_per_environment(operator, settings, fake_transport):
    settings.NIN_DIAGNOSTICS_MAX_PER_OPERATOR = 2
    settings.NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT = 3
    diagnostics.reset_limiters()
    client = _client(operator)
    outcomes = [_outcome(client.post(AUTH_URL)) for _ in range(3)]
    assert outcomes == [diagnostics.AUTH_OK, diagnostics.AUTH_OK, diagnostics.THROTTLED]
    assert len(fake_transport.calls) == 2
    second = _client(_staff("tech-two@example.test", "Integration Diagnostics Operators"))
    assert [_outcome(second.post(AUTH_URL)) for _ in range(2)] == [
        diagnostics.AUTH_OK,
        diagnostics.THROTTLED,  # the environment's allowance (3) is used up
    ]
    throttled = AuditEvent.objects.filter(
        action_code=action_codes.IDV_NIN_DIAGNOSTIC_RUN, reason_code=diagnostics.THROTTLED
    )
    assert throttled.count() == 2 and all(e.result == "DENIED" for e in throttled)


@pytest.mark.django_db(transaction=True)
def test_a_second_simultaneous_check_is_refused_as_busy(fake_transport):
    operator = _staff("tech-busy@example.test", "Integration Diagnostics Operators")
    client = _client(operator)
    holder = connections.create_connection("default")
    try:
        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)", [ADVISORY_LOCK_CLASS_NIN_DIAGNOSTICS, 0]
            )
        assert _outcome(client.post(AUTH_URL)) == diagnostics.BUSY
        assert fake_transport.calls == []
    finally:
        holder.close()
    assert _outcome(client.post(AUTH_URL)) == diagnostics.AUTH_OK


@override_settings(LANGUAGE_CODE="fr")
def test_outcomes_are_translated(operator):
    from django.utils import translation

    from apps.people.diagnostics_views import outcome_choices

    for language in ("fr", "ar"):
        with translation.override(language):
            english_free = outcome_choices()
        with translation.override("en"):
            english = outcome_choices()
        untranslated = [code for code in english if english[code] == english_free[code]]
        assert untranslated == [], (language, untranslated)


def test_a_successful_authentication_reports_its_http_status(operator, fake_transport):
    """Review A02-02: the status of the accepted authentication response is
    shown and audited, never the token."""
    audit_before = AuditEvent.objects.count()
    response = _client(operator).post(AUTH_URL)
    result = response.context["presented"]["result"]
    assert (result.outcome, result.http_status) == (diagnostics.AUTH_OK, 200)
    html = response.content.decode()
    assert "HTTP status" in html and '<span dir="ltr">200</span>' in html
    _no_secret_in(html)
    event = AuditEvent.objects.filter(
        action_code=action_codes.IDV_NIN_DIAGNOSTIC_RUN, id__gt=0
    ).order_by("-id")[0]
    assert AuditEvent.objects.count() > audit_before
    assert event.after_summary["http_status"] == 200
    assert event.after_summary["outcome"] == diagnostics.AUTH_OK
    _no_secret_in(json.dumps(event.after_summary))
