"""IDV-C1, R-IDV-03: the total deadline bounds every blocking step of one HTTPS request.

The real `HttpsTransport` talks to a SYNTHETIC local TLS peer
(`apps/people/tests/tls_peer.py`) that is slow on purpose: at the TLS
handshake, while sending the headers, while sending the body one byte at a
time, or before closing a body without a length. Each case would take several
times the configured total deadline; the transport must give up at the
deadline (`TransportError("timeout")`), never return success after it, and
release its connection. The size, TLS-verification and no-redirect controls
are unchanged. A final test runs the official adapter inside the worker
against a slow peer and checks that the provider slot is released.

These are real sockets on 127.0.0.1 with real time: margins are generous
(the bound asserted is the deadline plus 1.5 s, against peers that would take
6 s or more). The transport-only cases use per-operation timeouts longer than
the total on purpose: the total must hold even then (deployed settings also
forbid them). No ministry service is contacted; nothing here is evidence about
it.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from apps.people.nin_provider import HttpsTransport, MinistryApiConfig, TransportError
from apps.people.tests.tls_peer import TlsPeer, elapsed, response_steps, trickle

TOTAL = 2
MARGIN = 1.5
JSON_HEAD = b"HTTP/1.1 200 SYNTHETIC\r\nContent-Type: application/json\r\n"


def _config(peer: TlsPeer, **overrides) -> MinistryApiConfig:
    base = MinistryApiConfig(
        base_url=peer.base_url,
        auth_path="/api/auth/",
        lookup_path_template="/api/get/{nin}",
        username="__synthetic_user__",
        password="__synthetic_password__",  # noqa: S106 - placeholder
        token_path="data.access",  # noqa: S106 - a JSON key path
        expiry_path="",
        expiry_format="",
        token_cache_seconds=300,
        connect_timeout_seconds=5,
        read_timeout_seconds=5,
        total_timeout_seconds=TOTAL,
        max_response_bytes=65536,
        ca_bundle=str(peer.ca_path),
    )
    return replace(base, **overrides)


def _request(peer: TlsPeer, **overrides):
    return HttpsTransport(_config(peer, **overrides)).request(
        "GET", "/api/get/1", headers={"Accept": "application/json"}, body=None
    )


def _times_out(peer: TlsPeer, **overrides) -> float:
    started = time.monotonic()
    with pytest.raises(TransportError) as error:
        _request(peer, **overrides)
    assert error.value.code == "timeout"
    return elapsed(started)


def test_a_prompt_peer_still_succeeds(tmp_path) -> None:
    body = json.dumps({"identite": None}).encode()
    with TlsPeer(tmp_path, lambda _m, _p: response_steps(body=body)) as peer:
        response = _request(peer)
    assert (response.status, response.body) == (200, body)


def test_slow_headers_are_cut_at_the_total_deadline(tmp_path) -> None:
    # One header byte every 0.2 s: about 8 s for the whole head.
    header = b"X-Synthetic-Padding: " + b"a" * 20 + b"\r\nContent-Length: 2\r\n\r\n{}"

    def script(_method, _path):
        return trickle(b"HTTP/1.1 200 SYNTHETIC\r\n", header, interval=0.2)

    with TlsPeer(tmp_path, script) as peer:
        assert _times_out(peer) < TOTAL + MARGIN


def test_a_trickling_body_is_cut_at_the_total_deadline(tmp_path) -> None:
    body = b'{"identite": null, "pad": "' + b"b" * 20 + b'"}'

    def script(_method, _path):
        head = JSON_HEAD + f"Content-Length: {len(body)}\r\n\r\n".encode()
        return trickle(head, body, interval=0.2)  # about 10 s for the body

    with TlsPeer(tmp_path, script) as peer:
        assert _times_out(peer) < TOTAL + MARGIN


def test_an_end_of_body_after_the_deadline_is_not_a_success(tmp_path) -> None:
    # No Content-Length: the body ends only when the peer closes, 4 s later.
    def script(_method, _path):
        return response_steps(body=b'{"identite": null}', content_length=False)

    with TlsPeer(tmp_path, script, hold_after_seconds=4) as peer:
        assert _times_out(peer) < TOTAL + MARGIN


def test_a_slow_tls_handshake_is_cut_at_the_total_deadline(tmp_path) -> None:
    with TlsPeer(tmp_path, lambda _m, _p: response_steps(), handshake_delay=6) as peer:
        assert _times_out(peer) < TOTAL + MARGIN


def test_the_size_tls_and_redirect_controls_still_hold(tmp_path) -> None:
    def script(_method, path):
        if path.endswith("/1"):
            return response_steps(body=b"x" * 2048, content_length=False)
        if path.endswith("/2"):
            return response_steps(
                body=b"{}", headers={"Content-Length": "999999"}, content_length=False
            )
        return response_steps(302, b"", headers={"Location": "https://elsewhere.example.test/"})

    with TlsPeer(tmp_path, script) as peer:
        transport = HttpsTransport(_config(peer, max_response_bytes=1024))
        for path in ("/api/get/1", "/api/get/2"):
            with pytest.raises(TransportError) as error:
                transport.request("GET", path, headers={}, body=None)
            assert error.value.code == "too_large"
        response = transport.request("GET", "/api/get/3", headers={}, body=None)
        assert response.status == 302  # returned, never followed
        assert peer.connections == 3  # nothing went to the redirect target
        untrusted = HttpsTransport(_config(peer, ca_bundle=""))  # the system trust store
        with pytest.raises(TransportError) as error:
            untrusted.request("GET", "/api/get/3", headers={}, body=None)
        assert error.value.code == "tls"


@pytest.mark.django_db
def test_the_worker_releases_its_provider_slot_after_a_timeout(
    tmp_path, idv_event, legal_versions
) -> None:
    from django.db import connection

    from apps.people.models import IdentityStatus, IdentityVerificationAttempt
    from apps.people.nin_provider import MinistryNinProvider, _TokenCache
    from apps.people.services import identity_verification as idv
    from apps.people.tests.conftest import case_for, submit_case

    registration = submit_case(idv_event, legal_versions)
    token_body = json.dumps({"data": {"access": "__synthetic_token__"}}).encode()

    def script(method, _path):
        if method == "POST":
            return response_steps(body=token_body)
        body = b'{"identite": null}'
        return trickle(
            JSON_HEAD + f"Content-Length: {len(body)}\r\n\r\n".encode(), body, interval=0.3
        )

    with TlsPeer(tmp_path, script) as peer:
        # A configuration the adapter accepts: no timeout longer than the total.
        config = _config(peer, connect_timeout_seconds=1, read_timeout_seconds=1)
        assert MinistryNinProvider(config=config).configuration_problems() == []
        provider = MinistryNinProvider(config=config, token_cache=_TokenCache())
        job = case_for(registration).jobs.get()
        started = time.monotonic()
        assert idv.process_identity_job(job.pk, provider=provider) == "retry_scheduled"
        took = elapsed(started)

    # Authentication and the lookup are one bounded request each.
    assert took < 2 * TOTAL + MARGIN
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.is_official_provider is True
    assert attempt.outcome == "UNAVAILABLE"
    assert case_for(registration).status == IdentityStatus.PENDING  # a retry is due later
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = %s",
            [idv.ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT],
        )
        assert cursor.fetchone()[0] == 0  # the slot was released
