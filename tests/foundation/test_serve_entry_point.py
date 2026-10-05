"""The production HTTP entry point (`config.serve`, waitress).

Covers the option contract, the refusal to start without an explicit settings
module, and a real waitress server on a loopback port: it serves the WSGI
application, sends no `Server` header, and passes `X-Forwarded-Proto` through
to the application (the application's own trusted-proxy guard decides about
it). No external service is contacted.
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from config import serve

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_bind_loopback_and_keep_forwarded_headers_for_the_application() -> None:
    options = serve.server_options([], environ={})
    assert options["listen"] == "127.0.0.1:8000"
    assert options["threads"] == 8
    assert options["connection_limit"] == 200
    assert options["channel_timeout"] == 120
    assert options["max_request_body_size"] == 16 * 1024 * 1024
    assert options["clear_untrusted_proxy_headers"] is False
    assert "trusted_proxy" not in options
    assert options["ident"] == ""
    assert options["expose_tracebacks"] is False


def test_environment_and_arguments_override_the_defaults() -> None:
    environ = {"ASC_HTTP_LISTEN": "10.0.0.5:8001 10.0.0.5:8002", "ASC_HTTP_THREADS": "4"}
    options = serve.server_options([], environ=environ)
    assert options["listen"] == "10.0.0.5:8001 10.0.0.5:8002"
    assert options["threads"] == 4
    options = serve.server_options(["--threads", "12"], environ=environ)
    assert options["threads"] == 12


@pytest.mark.parametrize(
    "environ",
    [
        {"ASC_HTTP_THREADS": "0"},
        {"ASC_HTTP_THREADS": "many"},
        {"ASC_HTTP_CONNECTION_LIMIT": "-1"},
        {"ASC_HTTP_MAX_REQUEST_BODY_BYTES": "1.5"},
        {"ASC_HTTP_LISTEN": "   "},
    ],
)
def test_invalid_values_are_refused(environ) -> None:
    with pytest.raises(serve.ServeConfigurationError):
        serve.server_options([], environ=environ)


def test_the_server_never_falls_back_to_local_settings() -> None:
    with pytest.raises(serve.ServeConfigurationError):
        serve.require_explicit_settings({})
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    run = subprocess.run(
        [sys.executable, "-m", "config.serve", "--listen", "127.0.0.1:0"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 2
    assert "DJANGO_SETTINGS_MODULE" in run.stderr


def test_a_real_waitress_server_serves_the_application_and_passes_forwarded_headers() -> None:
    import waitress

    from config.wsgi import application

    seen: dict[str, str | None] = {}

    def probe(environ, start_response):
        seen["forwarded_proto"] = environ.get("HTTP_X_FORWARDED_PROTO")
        seen["remote_addr"] = environ.get("REMOTE_ADDR")
        return application(environ, start_response)

    options = serve.server_options(["--listen", "127.0.0.1:0"], environ={})
    server = waitress.create_server(probe, **options)
    port = server.effective_port
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        connection.request("GET", "/healthz", headers={"X-Forwarded-Proto": "https"})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, body[:200]
        assert response.getheader("Server") is None
        assert seen["forwarded_proto"] == "https"
        assert seen["remote_addr"] == "127.0.0.1"
    finally:
        server.close()
        thread.join(timeout=10)
