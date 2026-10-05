"""Production HTTP entry point: the project's WSGI application served by waitress.

    DJANGO_SETTINGS_MODULE=config.settings.staging python -m config.serve

Waitress is a pure-Python, multi-threaded WSGI server, so this entry point
behaves the same on every operating system. A reverse proxy that terminates
TLS sits in front of it; the operators choose the proxy and the service
manager (see `docs/deployment/README.md`).

Fixed choices, not options:

* `DJANGO_SETTINGS_MODULE` must be set explicitly. The server never falls back
  to `config.settings.local` the way `manage.py` and `config/wsgi.py` do for
  developer convenience.
* Waitress does not interpret or strip `X-Forwarded-*` headers
  (`clear_untrusted_proxy_headers=False`, no `trusted_proxy`). The application
  is the single place that decides whether to trust them
  (`TRUSTED_PROXY_FORWARDING_ENABLED` and `TRUSTED_PROXY_CIDRS`,
  `apps.core.middleware.security_headers.ForwardedProtoGuardMiddleware`).
  Letting waitress strip them as well would make every request behind a TLS
  proxy look like plain HTTP and loop on the HTTPS redirect.
* No `Server` header and no tracebacks in responses.

Tunable through the environment (or the matching command-line option):
`ASC_HTTP_LISTEN` (default `127.0.0.1:8000`, one or more space-separated
`host:port` values), `ASC_HTTP_THREADS` (default 8),
`ASC_HTTP_CONNECTION_LIMIT` (default 200), `ASC_HTTP_CHANNEL_TIMEOUT_SECONDS`
(default 120) and `ASC_HTTP_MAX_REQUEST_BODY_BYTES` (default 16 MiB, above the
largest accepted upload).
"""

from __future__ import annotations

import argparse
import os
import select
import sys

DEFAULT_LISTEN = "127.0.0.1:8000"
DEFAULT_THREADS = 8
DEFAULT_CONNECTION_LIMIT = 200
DEFAULT_CHANNEL_TIMEOUT_SECONDS = 120
DEFAULT_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


class ServeConfigurationError(Exception):
    """The server cannot start with the given configuration. Never holds a secret."""


def _positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ServeConfigurationError(f"{name} must be a whole number.") from None
    if value < 1:
        raise ServeConfigurationError(f"{name} must be at least 1.")
    return value


def server_options(argv: list[str] | None = None, environ=None) -> dict:
    """The waitress keyword arguments for this deployment (pure; no server started)."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m config.serve", description=__doc__)
    parser.add_argument("--listen", default=environ.get("ASC_HTTP_LISTEN", DEFAULT_LISTEN))
    parser.add_argument("--threads", default=environ.get("ASC_HTTP_THREADS", str(DEFAULT_THREADS)))
    parser.add_argument(
        "--connection-limit",
        default=environ.get("ASC_HTTP_CONNECTION_LIMIT", str(DEFAULT_CONNECTION_LIMIT)),
    )
    parser.add_argument(
        "--channel-timeout",
        default=environ.get(
            "ASC_HTTP_CHANNEL_TIMEOUT_SECONDS", str(DEFAULT_CHANNEL_TIMEOUT_SECONDS)
        ),
    )
    parser.add_argument(
        "--max-request-body-bytes",
        default=environ.get("ASC_HTTP_MAX_REQUEST_BODY_BYTES", str(DEFAULT_MAX_REQUEST_BODY_BYTES)),
    )
    args = parser.parse_args(argv)
    listen = " ".join(str(args.listen).split())
    if not listen:
        raise ServeConfigurationError("ASC_HTTP_LISTEN must name at least one host:port.")
    options = {
        "listen": listen,
        "threads": _positive_int("ASC_HTTP_THREADS", str(args.threads)),
        "connection_limit": _positive_int("ASC_HTTP_CONNECTION_LIMIT", str(args.connection_limit)),
        "channel_timeout": _positive_int(
            "ASC_HTTP_CHANNEL_TIMEOUT_SECONDS", str(args.channel_timeout)
        ),
        "max_request_body_size": _positive_int(
            "ASC_HTTP_MAX_REQUEST_BODY_BYTES", str(args.max_request_body_bytes)
        ),
        # The application, not the server, decides about forwarded headers.
        "clear_untrusted_proxy_headers": False,
        "ident": "",
        "expose_tracebacks": False,
    }
    if hasattr(select, "poll"):
        # poll() has no 1024-descriptor ceiling (Linux and other Unix systems).
        options["asyncore_use_poll"] = True
    return options


def require_explicit_settings(environ=None) -> str:
    environ = os.environ if environ is None else environ
    module = (environ.get("DJANGO_SETTINGS_MODULE") or "").strip()
    if not module:
        raise ServeConfigurationError(
            "Set DJANGO_SETTINGS_MODULE explicitly (for example config.settings.staging); "
            "the server never falls back to the local development settings."
        )
    return module


def create_server(argv: list[str] | None = None, environ=None):
    """Load the WSGI application and build (not run) the waitress server."""
    require_explicit_settings(environ)
    options = server_options(argv, environ)

    import waitress

    from config.wsgi import application

    return waitress.create_server(application, **options)


def main(argv: list[str] | None = None) -> int:
    try:
        server = create_server(argv)
    except ServeConfigurationError as exc:
        print(f"config.serve: {exc}", file=sys.stderr)
        return 2
    server.print_listen("Serving on http://{}:{}")
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
