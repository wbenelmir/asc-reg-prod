"""The official ministry adapter against a SYNTHETIC fake transport (IDV-2).

These tests prove the client's own behaviour -- configurable authentication
extraction, expiry, missing configuration, one re-authentication on a
confirmed rejection, bounded attempts, a single-flight token under concurrency,
strict response handling and TLS settings. They are NOT evidence about the
real ministry service: the authentication response shape used here is an
invented example, because the real one has not been supplied (API-01). No
network access happens. Every NIN, token and name is synthetic.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import replace

import pytest

from apps.people.identity_contract import LookupKind
from apps.people.nin_provider import (
    HttpResponse,
    HttpsTransport,
    MinistryApiConfig,
    MinistryNinProvider,
    TransportError,
    _TokenCache,
)

NIN = "990000000000000010"
#: Approved placeholder shapes (`__...__`), never real-looking credentials.
TOKEN = "__synthetic_token_one__"  # noqa: S105 - placeholder
TOKEN_2 = "__synthetic_token_two__"  # noqa: S105 - placeholder
USERNAME = "__synthetic_user__"
PASSWORD = "__synthetic_password__"  # noqa: S105 - placeholder
FOUND = {
    "identite": {
        "nin": NIN,
        "nom_f": "BENTEST",
        "pren_f": "AMINA",
        "d_nais": "07/03/1990",
        "presume": "False",
    }
}


def _config(**overrides) -> MinistryApiConfig:
    base = MinistryApiConfig(
        base_url="https://identity.example.test",
        auth_path="/api/auth/",
        lookup_path_template="/api/get/{nin}",
        username=USERNAME,
        password=PASSWORD,
        token_path="data.access",  # noqa: S106 - a JSON key path
        expiry_path="",
        expiry_format="",
        token_cache_seconds=300,
        connect_timeout_seconds=5,
        read_timeout_seconds=10,
        total_timeout_seconds=20,
        max_response_bytes=65536,
        ca_bundle="",
    )
    return replace(base, **overrides)


class FakeTransport:
    """Scripted responses per path kind; records every request."""

    def __init__(self, *, auth=None, lookup=None) -> None:
        self.auth = list(auth or [])
        self.lookup = list(lookup or [])
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self._lock = threading.Lock()

    def request(self, method, path, *, headers, body):
        with self._lock:
            self.requests.append((method, path, dict(headers), body))
            queue = self.auth if path.startswith("/api/auth") else self.lookup
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item

    def count(self, prefix: str) -> int:
        return sum(1 for _m, path, _h, _b in self.requests if path.startswith(prefix))


def _json(status: int, document) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(document).encode("utf-8"))


def _auth_ok(token: str = TOKEN, **extra) -> HttpResponse:
    return _json(200, {"data": {"access": token, **extra}})


def _provider(transport, **config_overrides) -> MinistryNinProvider:
    return MinistryNinProvider(
        config=_config(**config_overrides), transport=transport, token_cache=_TokenCache()
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_missing_settings_keep_the_adapter_unavailable_without_a_request() -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(200, FOUND)])
    provider = _provider(transport, token_path="", password="")
    outcome = provider.lookup(NIN)
    assert outcome.kind == LookupKind.NOT_CONFIGURED
    assert transport.requests == []
    problems = provider.configuration_problems()
    assert any("MINISTRY_NIN_API_AUTH_TOKEN_PATH" in problem for problem in problems)
    assert all("__synthetic" not in problem for problem in problems)  # never echoes values


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"base_url": "http://identity.example.test"}, "https://"),
        ({"base_url": "https://user:pw@identity.example.test"}, "credentials"),
        ({"base_url": "https://identity.example.test/api"}, "without a path"),
        ({"base_url": "https://identity.example.test?x=1"}, "query"),
        ({"token_path": "data..access"}, "dotted path"),
        ({"token_path": "a.b.c.d.e.f"}, "dotted path"),
        ({"expiry_path": "data.expires_in"}, "set together"),
        ({"expiry_path": "e", "expiry_format": "minutes"}, "relative_seconds"),
        ({"lookup_path_template": "/api/get/"}, "{nin}"),
        ({"connect_timeout_seconds": 0}, "CONNECT_TIMEOUT"),
        ({"max_response_bytes": 10}, "MAX_RESPONSE_BYTES"),
        ({"token_cache_seconds": 99999}, "TOKEN_CACHE"),
        ({"ca_bundle": "Z:/does/not/exist.pem"}, "CA_BUNDLE"),
    ],
)
def test_malformed_settings_are_reported_by_name(overrides, fragment) -> None:
    problems = _config(**overrides).malformed_problems()
    assert any(fragment in problem for problem in problems), problems


def test_a_complete_configuration_has_no_problem() -> None:
    assert _config().malformed_problems() == []
    assert _config().missing_problems() == []


# ---------------------------------------------------------------------------
# Authentication (API-01: configurable, validated mapping)
# ---------------------------------------------------------------------------


def test_the_token_is_read_from_the_configured_path_and_sent_as_bearer() -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.FOUND
    method, path, headers, body = transport.requests[0]
    assert (method, path) == ("POST", "/api/auth/")
    assert json.loads(body) == {"username": USERNAME, "password": PASSWORD}
    method, path, headers, body = transport.requests[1]
    assert (method, path, body) == ("GET", f"/api/get/{NIN}", None)
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "auth_body",
    [
        {"data": {}},
        {"data": {"access": ""}},
        {"data": {"access": "short"}},
        {"data": {"access": "has spaces in the token value"}},
        {"data": {"access": 12345678901234567890}},
        {"token": TOKEN},  # the token is elsewhere than configured
        [],
    ],
)
def test_an_auth_response_that_does_not_match_the_mapping_is_an_auth_error(auth_body) -> None:
    transport = FakeTransport(auth=[_json(200, auth_body)], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.AUTH_ERROR
    assert outcome.detail == "auth_contract_mismatch"
    assert transport.count("/api/get") == 0


def test_invalid_auth_json_is_an_auth_error() -> None:
    transport = FakeTransport(auth=[HttpResponse(200, b"{not json")], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(NIN)
    assert (outcome.kind, outcome.detail) == (LookupKind.AUTH_ERROR, "auth_response_invalid")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_an_auth_error_not_retried_here(status) -> None:
    transport = FakeTransport(auth=[_json(status, {})], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.AUTH_ERROR and not outcome.retryable
    assert transport.count("/api/auth") == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_unavailable_auth_service_is_transient(status) -> None:
    transport = FakeTransport(auth=[_json(status, {})], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.UNAVAILABLE and outcome.retryable


def test_a_configured_relative_expiry_bounds_the_cache() -> None:
    cache = _TokenCache()
    transport = FakeTransport(auth=[_auth_ok(expires_in=3600)], lookup=[_json(200, FOUND)])
    provider = MinistryNinProvider(
        config=_config(
            expiry_path="data.expires_in", expiry_format="relative_seconds", token_cache_seconds=60
        ),
        transport=transport,
        token_cache=cache,
    )
    provider.lookup(NIN)
    remaining = cache.current().expires_at_monotonic - time.monotonic()
    assert 0 < remaining <= 60  # never longer than the local policy


def test_a_short_declared_expiry_is_reduced_by_the_safety_margin() -> None:
    cache = _TokenCache()
    transport = FakeTransport(auth=[_auth_ok(expires_in=90)], lookup=[_json(200, FOUND)])
    MinistryNinProvider(
        config=_config(expiry_path="data.expires_in", expiry_format="relative_seconds"),
        transport=transport,
        token_cache=cache,
    ).lookup(NIN)
    remaining = cache.current().expires_at_monotonic - time.monotonic()
    assert 0 < remaining <= 60


def test_an_epoch_expiry_is_supported() -> None:
    cache = _TokenCache()
    expires_at = int(time.time()) + 600
    transport = FakeTransport(auth=[_auth_ok(exp=expires_at)], lookup=[_json(200, FOUND)])
    MinistryNinProvider(
        config=_config(expiry_path="data.exp", expiry_format="unix_epoch_seconds"),
        transport=transport,
        token_cache=cache,
    ).lookup(NIN)
    assert cache.current() is not None


@pytest.mark.parametrize("expiry", ["3600", None, True, -5, 0])
def test_a_missing_or_invalid_configured_expiry_is_an_auth_error(expiry) -> None:
    extra = {} if expiry is None else {"expires_in": expiry}
    transport = FakeTransport(auth=[_auth_ok(**extra)], lookup=[_json(200, FOUND)])
    outcome = _provider(
        transport, expiry_path="data.expires_in", expiry_format="relative_seconds"
    ).lookup(NIN)
    assert (outcome.kind, outcome.detail) == (LookupKind.AUTH_ERROR, "auth_expiry_mismatch")


def test_a_cached_token_is_reused_without_a_new_authentication() -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(200, FOUND)])
    provider = _provider(transport)
    provider.lookup(NIN)
    provider.lookup(NIN)
    assert transport.count("/api/auth") == 1
    assert transport.count("/api/get") == 2


def test_one_reauthentication_after_a_confirmed_rejection() -> None:
    transport = FakeTransport(
        auth=[_auth_ok(TOKEN), _auth_ok(TOKEN_2)],
        lookup=[_json(401, {}), _json(200, FOUND)],
    )
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.FOUND
    assert transport.count("/api/auth") == 2
    assert transport.requests[-1][2]["Authorization"] == f"Bearer {TOKEN_2}"


def test_a_second_rejection_stops_without_a_loop() -> None:
    transport = FakeTransport(auth=[_auth_ok(TOKEN), _auth_ok(TOKEN_2)], lookup=[_json(401, {})])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.AUTH_ERROR
    assert outcome.detail == "rejected_after_reauthentication"
    assert outcome.retryable
    assert transport.count("/api/auth") == 2
    assert transport.count("/api/get") == 2  # bounded: two lookups, two authentications


def test_concurrent_lookups_share_one_authentication() -> None:
    def slow_auth():
        time.sleep(0.05)
        return _auth_ok()

    transport = FakeTransport(auth=[slow_auth], lookup=[_json(200, FOUND)])
    provider = _provider(transport)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(provider.lookup(NIN))) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and all(r.kind == LookupKind.FOUND for r in results)
    assert transport.count("/api/auth") == 1


def test_a_burst_of_rejections_renews_once() -> None:
    """Only the token that was rejected is invalidated, so concurrent 401s for
    the same token renew it once, not once per request."""
    cache = _TokenCache()
    token, _failure = cache.obtain(lambda: (TOKEN, 300, None))
    cache.invalidate(token.generation)
    renewed, _ = cache.obtain(lambda: (TOKEN_2, 300, None))
    cache.invalidate(token.generation)  # a late 401 for the old one changes nothing
    assert cache.current() is renewed


# ---------------------------------------------------------------------------
# Lookup responses
# ---------------------------------------------------------------------------


def test_explicit_null_with_http_200_is_not_found() -> None:
    transport = FakeTransport(
        auth=[_auth_ok()], lookup=[_json(200, {"identite": None, "result": "synthetic text"})]
    )
    assert _provider(transport).lookup(NIN).kind == LookupKind.NOT_FOUND


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b'{"identite": {"nin": "1",}}', "json_invalid"),  # the trailing-comma artifact
        (b"", "json_invalid"),
        (b"\xff\xfe", "json_invalid"),
        (b'{"identite": null, "identite": {}}', "json_invalid"),  # duplicate keys
        (b'{"identite": NaN}', "json_invalid"),
        (b'{"identite": null} trailing', "json_invalid"),
        (b"[]", "body_not_object"),
        (b"{}", "identite_missing"),
    ],
)
def test_malformed_live_responses_are_invalid_never_repaired(body, detail) -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[HttpResponse(200, body)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.INVALID_RESPONSE
    assert outcome.detail == detail


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, LookupKind.UNAVAILABLE, True),
        (500, LookupKind.UNAVAILABLE, True),
        (502, LookupKind.UNAVAILABLE, True),
        (403, LookupKind.AUTH_ERROR, False),
        (404, LookupKind.INVALID_RESPONSE, False),
        (302, LookupKind.INVALID_RESPONSE, False),
        (204, LookupKind.INVALID_RESPONSE, False),
    ],
)
def test_status_codes_are_classified_without_ever_meaning_not_found(
    status, kind, retryable
) -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(status, {"identite": None})])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == kind
    assert outcome.retryable is retryable
    assert outcome.kind != LookupKind.NOT_FOUND


@pytest.mark.parametrize("code", ["timeout", "connection", "tls"])
def test_transport_failures_are_transient(code) -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[TransportError(code)])
    outcome = _provider(transport).lookup(NIN)
    assert outcome.kind == LookupKind.UNAVAILABLE and outcome.retryable


def test_an_oversized_response_is_invalid() -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[TransportError("too_large")])
    outcome = _provider(transport).lookup(NIN)
    assert (outcome.kind, outcome.detail) == (LookupKind.INVALID_RESPONSE, "response_too_large")


@pytest.mark.parametrize("bad_nin", ["", "12345", "99000000000000001A", "../../admin"])
def test_only_an_18_digit_nin_ever_reaches_a_request_path(bad_nin) -> None:
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(200, FOUND)])
    outcome = _provider(transport).lookup(bad_nin)
    assert outcome.kind == LookupKind.INVALID_RESPONSE
    assert transport.requests == []


def test_a_leading_zero_nin_is_sent_as_a_string() -> None:
    nin = "009900000000000044"
    found = {"identite": {**FOUND["identite"], "nin": nin}}
    transport = FakeTransport(auth=[_auth_ok()], lookup=[_json(200, found)])
    outcome = _provider(transport).lookup(nin)
    assert transport.requests[-1][1] == f"/api/get/{nin}"
    assert outcome.facts.nin == nin


def test_outcome_details_never_carry_provider_text() -> None:
    transport = FakeTransport(
        auth=[_auth_ok()], lookup=[HttpResponse(500, b"Internal error for 990000000000000010")]
    )
    outcome = _provider(transport).lookup(NIN)
    assert NIN not in repr(outcome)


# ---------------------------------------------------------------------------
# HTTPS transport configuration (no network)
# ---------------------------------------------------------------------------


def test_the_tls_context_verifies_certificates_and_host_names() -> None:
    transport = HttpsTransport(_config())
    context = transport._context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_the_transport_targets_only_the_configured_host_with_deadline_bound_sockets() -> None:
    """IDV-C1 (R-IDV-03): the size bound, the refused redirect and the total
    deadline are proven over real TLS against a synthetic local peer in
    `test_idv_c1_transport_deadline.py`; this checks the fixed target and that
    every TLS socket of the transport is deadline-bound."""
    from apps.people.nin_provider import _DeadlineSSLSocket

    transport = HttpsTransport(_config())
    assert (transport._host, transport._port) == ("identity.example.test", 443)
    assert transport._context.sslsocket_class is _DeadlineSSLSocket
    custom = HttpsTransport(_config(base_url="https://identity.example.test:8443"))
    assert (custom._host, custom._port) == ("identity.example.test", 8443)
