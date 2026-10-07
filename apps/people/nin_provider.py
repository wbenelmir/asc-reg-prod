"""NIN lookup provider adapters (TRD §15.3, IDV-2, amendment A-13, ADR-0026).

The observed ministry contract is a lookup by NIN: an authentication POST
returning a bearer token, then `GET /api/get/{nin}` returning the official
identity or `identite: null`. Only the NIN is transmitted; names and the birth
date are compared locally (`apps.people.identity_contract`).

Backends:

* `MinistryNinProvider` -- the only official backend. HTTPS with certificate
  and host-name verification, a fixed configured host, no redirects, bounded
  connect/read/total time and response size, at most one re-authentication per
  lookup after a confirmed rejection, and a single-flight token cache per
  process. It reports itself unavailable (`NOT_CONFIGURED`) until every
  required setting is present and valid; the authentication response contract
  is configurable because it has not been supplied (API-01).
* `DisabledNinProvider` -- the default outside local development: every lookup
  is `NOT_CONFIGURED`, so every Algerian case goes to manual review.
* `LocalSimulationNinProvider`, `UnavailableSimulationNinProvider` --
  DEVELOPMENT SIMULATIONS. They are never official verification
  (`IS_OFFICIAL = False`); staging and production refuse them at startup
  (`config/settings/validation.py`, A13-06), and the worker refuses them at
  run time unless `IDENTITY_ALLOW_SIMULATED_PROVIDER` is on (local and test).

No adapter logs a URL, a header, a body or an exception message. A NIN reaches
only the request path of the lookup, which is never logged.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from apps.people.identity_contract import (
    NIN_PATTERN,
    LookupKind,
    OfficialIdentityFacts,
    classify_lookup_body,
)


def generate_opaque_local_reference(prefix: str = "ref") -> str:
    """Return a short, opaque, non-identity-derived correlation reference
    (Prompt 6 P6-H-01 correction).

    Never built from any participant-supplied value (NIN, name, date of
    birth, phone, email, passport).
    """
    return f"{prefix}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class LookupOutcome:
    """The result of one `lookup()` call.

    `detail` is a fixed machine code, never provider text. `retryable` marks a
    transient failure the worker may retry with backoff.
    """

    kind: str
    facts: OfficialIdentityFacts | None = None
    detail: str = ""
    http_status: int | None = None
    retryable: bool = False
    request_reference: str = ""


@dataclass(frozen=True)
class AuthenticationProbe:
    """The result of `MinistryNinProvider.probe_authentication`: `failure` is
    None on success. Never carries the token."""

    failure: LookupOutcome | None
    lifetime_seconds: int | None
    #: The HTTP status of the authentication response (a failure carries its
    #: own in `failure.http_status`).
    http_status: int | None = None


class _StatusRecordingTransport:
    """Wraps a transport to remember the status of the last response (for the
    diagnostics probe); passes everything else through unchanged."""

    def __init__(self, transport) -> None:
        self._transport = transport
        self.last_status: int | None = None

    def request(self, method, path, *, headers, body):
        response = self._transport.request(method, path, headers=headers, body=body)
        self.last_status = response.status
        return response


class NinLookupProvider(Protocol):
    PROVIDER_CODE: str
    IS_OFFICIAL: bool

    def configuration_problems(self) -> list[str]: ...

    def lookup(self, nin: str) -> LookupOutcome: ...


# ---------------------------------------------------------------------------
# Configuration of the official adapter
# ---------------------------------------------------------------------------

#: Observed contract facts (addendum §3). The bearer scheme and the JSON field
#: names of the authentication request are observed; the authentication
#: RESPONSE is not (API-01), so the token path is configuration.
OBSERVED_AUTH_PATH = "/api/auth/"
OBSERVED_LOOKUP_PATH_TEMPLATE = "/api/get/{nin}"
AUTH_SCHEME = "Bearer"

TOKEN_PATH_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,4}")
EXPIRY_FORMATS = ("relative_seconds", "unix_epoch_seconds")
#: RFC 6750 token68 characters; anything else is refused, never sent.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-._~+/]{16,4096}=*")
_PATH_PATTERN = re.compile(r"/[A-Za-z0-9\-._~/]*")

#: Bounds checked both at startup (staging/production) and at run time.
BOUNDS = {
    "MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS": (1, 30),
    "MINISTRY_NIN_API_READ_TIMEOUT_SECONDS": (1, 60),
    "MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS": (2, 120),
    "MINISTRY_NIN_API_MAX_RESPONSE_BYTES": (1024, 1024 * 1024),
    "MINISTRY_NIN_API_TOKEN_CACHE_SECONDS": (30, 3600),
}
#: Seconds subtracted from a provider-declared token lifetime, for clock skew.
TOKEN_EXPIRY_SAFETY_SECONDS = 30


@dataclass(frozen=True)
class MinistryApiConfig:
    base_url: str
    auth_path: str
    lookup_path_template: str
    username: str
    password: str
    token_path: str
    expiry_path: str
    expiry_format: str
    token_cache_seconds: int
    connect_timeout_seconds: int
    read_timeout_seconds: int
    total_timeout_seconds: int
    max_response_bytes: int
    ca_bundle: str

    @classmethod
    def from_settings(cls) -> MinistryApiConfig:
        from django.conf import settings

        return cls(
            base_url=str(getattr(settings, "MINISTRY_NIN_API_BASE_URL", "") or ""),
            auth_path=str(getattr(settings, "MINISTRY_NIN_API_AUTH_PATH", OBSERVED_AUTH_PATH)),
            lookup_path_template=str(
                getattr(
                    settings, "MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE", OBSERVED_LOOKUP_PATH_TEMPLATE
                )
            ),
            username=str(getattr(settings, "MINISTRY_NIN_API_USERNAME", "") or ""),
            password=str(getattr(settings, "MINISTRY_NIN_API_PASSWORD", "") or ""),
            token_path=str(getattr(settings, "MINISTRY_NIN_API_AUTH_TOKEN_PATH", "") or ""),
            expiry_path=str(getattr(settings, "MINISTRY_NIN_API_AUTH_EXPIRY_PATH", "") or ""),
            expiry_format=str(getattr(settings, "MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT", "") or ""),
            token_cache_seconds=int(getattr(settings, "MINISTRY_NIN_API_TOKEN_CACHE_SECONDS", 300)),
            connect_timeout_seconds=int(
                getattr(settings, "MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS", 5)
            ),
            read_timeout_seconds=int(
                getattr(settings, "MINISTRY_NIN_API_READ_TIMEOUT_SECONDS", 10)
            ),
            total_timeout_seconds=int(
                getattr(settings, "MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS", 20)
            ),
            max_response_bytes=int(getattr(settings, "MINISTRY_NIN_API_MAX_RESPONSE_BYTES", 65536)),
            ca_bundle=str(getattr(settings, "MINISTRY_NIN_API_CA_BUNDLE", "") or ""),
        )

    def malformed_problems(self) -> list[str]:
        """Problems with values that are present but malformed.

        Used by the staging/production startup validation: a malformed value is
        a configuration mistake, so startup fails. Never echoes a value.
        """
        problems: list[str] = []
        if self.base_url:
            problems.extend(_base_url_problems(self.base_url))
        if not _PATH_PATTERN.fullmatch(self.auth_path):
            problems.append("MINISTRY_NIN_API_AUTH_PATH must be an absolute path.")
        template = self.lookup_path_template
        if template.count("{nin}") != 1 or not _PATH_PATTERN.fullmatch(
            template.replace("{nin}", "0")
        ):
            problems.append(
                "MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE must be an absolute path holding {nin} once."
            )
        if self.token_path and not TOKEN_PATH_PATTERN.fullmatch(self.token_path):
            problems.append(
                "MINISTRY_NIN_API_AUTH_TOKEN_PATH must be a dotted path of up to five JSON keys."
            )
        if self.expiry_path and not TOKEN_PATH_PATTERN.fullmatch(self.expiry_path):
            problems.append(
                "MINISTRY_NIN_API_AUTH_EXPIRY_PATH must be a dotted path of up to five JSON keys."
            )
        if self.expiry_format and self.expiry_format not in EXPIRY_FORMATS:
            problems.append(
                "MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT must be relative_seconds or "
                "unix_epoch_seconds."
            )
        if bool(self.expiry_path) != bool(self.expiry_format):
            problems.append(
                "MINISTRY_NIN_API_AUTH_EXPIRY_PATH and MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT must be "
                "set together."
            )
        for name, value in (
            ("MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS", self.connect_timeout_seconds),
            ("MINISTRY_NIN_API_READ_TIMEOUT_SECONDS", self.read_timeout_seconds),
            ("MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS", self.total_timeout_seconds),
            ("MINISTRY_NIN_API_MAX_RESPONSE_BYTES", self.max_response_bytes),
            ("MINISTRY_NIN_API_TOKEN_CACHE_SECONDS", self.token_cache_seconds),
        ):
            low, high = BOUNDS[name]
            if not low <= value <= high:
                problems.append(f"{name} must be between {low} and {high}.")
        if self.total_timeout_seconds < max(
            self.connect_timeout_seconds, self.read_timeout_seconds
        ):
            problems.append(
                "MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS must not be shorter than the connect or "
                "read timeout."
            )
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            problems.append("MINISTRY_NIN_API_CA_BUNDLE must name a readable certificate file.")
        return problems

    def missing_problems(self) -> list[str]:
        """Required values that are absent. The adapter stays unavailable (every
        case goes to manual review) until they are supplied; startup does not
        fail, because the authentication contract is still open (API-01)."""
        missing = []
        for name, value in (
            ("MINISTRY_NIN_API_BASE_URL", self.base_url),
            ("MINISTRY_NIN_API_USERNAME", self.username),
            ("MINISTRY_NIN_API_PASSWORD", self.password),
            ("MINISTRY_NIN_API_AUTH_TOKEN_PATH", self.token_path),
        ):
            if not value:
                missing.append(f"{name} is not configured.")
        return missing


def _base_url_problems(base_url: str) -> list[str]:
    try:
        parts = urlsplit(base_url)
        _port = parts.port  # forces validation of a malformed port
    except ValueError:
        return ["MINISTRY_NIN_API_BASE_URL is not a valid URL."]
    problems = []
    if parts.scheme != "https" or not parts.hostname:
        problems.append("MINISTRY_NIN_API_BASE_URL must be an absolute https:// URL.")
    if parts.username is not None or parts.password is not None:
        problems.append("MINISTRY_NIN_API_BASE_URL must not embed credentials.")
    if parts.query or parts.fragment:
        problems.append("MINISTRY_NIN_API_BASE_URL must not carry a query or a fragment.")
    if parts.path not in ("", "/"):
        problems.append("MINISTRY_NIN_API_BASE_URL must be an origin without a path.")
    return problems


# ---------------------------------------------------------------------------
# HTTPS transport (standard library only; no new dependency)
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """A failure before a complete HTTP response was read. `code` is fixed text:
    timeout, connection, tls or too_large. Never carries provider data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(
        self, method: str, path: str, *, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse: ...


class _DeadlineSSLSocket(ssl.SSLSocket):
    """An SSL socket whose every blocking call is bounded by one request's
    ABSOLUTE deadline (IDV-C1, R-IDV-03).

    Before each handshake, send, receive or read call, the socket timeout is
    set to the time left (capped by the per-operation timeout), and a call
    after the deadline fails at once. `http.client` reads headers and bodies
    through a buffered reader that may make many receive calls for one read;
    each of them is bounded by the time left, so together they can never run
    past the deadline. Set `_asc_bounds` before the handshake.
    """

    _asc_bounds = None  # (clock, deadline, per-operation cap in seconds)

    def _asc_bound(self) -> None:
        clock, deadline, cap = self._asc_bounds
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("deadline")
        self.settimeout(min(cap, remaining))

    def do_handshake(self, *args, **kwargs):
        self._asc_bound()
        return super().do_handshake(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._asc_bound()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._asc_bound()
        return super().recv_into(*args, **kwargs)

    def read(self, *args, **kwargs):
        self._asc_bound()
        return super().read(*args, **kwargs)

    def send(self, *args, **kwargs):
        self._asc_bound()
        return super().send(*args, **kwargs)

    def write(self, *args, **kwargs):
        self._asc_bound()
        return super().write(*args, **kwargs)


def _resolve_address(host: str, port: int, timeout: float) -> list:
    """`getaddrinfo` bounded by `timeout`. The resolver itself cannot be
    interrupted, so a slow lookup runs on a daemon thread that is abandoned
    (it ends when the system resolver gives up) while the request fails at
    the deadline. A literal address resolves at once."""
    try:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except OSError:
        pass
    result: dict = {}

    def lookup() -> None:
        try:
            result["addresses"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            result["error"] = error

    thread = threading.Thread(target=lookup, name="asc-identity-dns", daemon=True)
    thread.start()
    thread.join(max(timeout, 0))
    if thread.is_alive():
        raise TimeoutError("dns")
    if "error" in result:
        raise result["error"]
    return result["addresses"]


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """`http.client` over a connection whose resolution, connection, TLS
    handshake and every later socket call are bounded by one deadline."""

    def __init__(self, host, port, *, context, clock, deadline, connect_cap, io_cap) -> None:
        super().__init__(host, port, timeout=connect_cap, context=context)
        self._asc_clock = clock
        self._asc_deadline = deadline
        self._asc_connect_cap = connect_cap
        self._asc_io_cap = io_cap

    def _asc_remaining(self) -> float:
        remaining = self._asc_deadline - self._asc_clock()
        if remaining <= 0:
            raise TimeoutError("deadline")
        return remaining

    def connect(self) -> None:
        last_error: OSError | None = None
        plain = None
        for family, socktype, proto, _name, address in _resolve_address(
            self.host, self.port, self._asc_remaining()
        ):
            candidate = socket.socket(family, socktype, proto)
            try:
                candidate.settimeout(min(self._asc_connect_cap, self._asc_remaining()))
                candidate.connect(address)
            except OSError as error:
                candidate.close()
                last_error = error
                if isinstance(error, TimeoutError):
                    raise
                continue
            plain = candidate
            break
        if plain is None:
            raise last_error or OSError("no address")
        try:
            tls = self._context.wrap_socket(
                plain, server_hostname=self.host, do_handshake_on_connect=False
            )
        except BaseException:
            plain.close()
            raise
        self.sock = tls  # closed by close() whatever happens next
        tls._asc_bounds = (self._asc_clock, self._asc_deadline, self._asc_connect_cap)
        tls.do_handshake()
        tls._asc_bounds = (self._asc_clock, self._asc_deadline, self._asc_io_cap)


class HttpsTransport:
    """One HTTPS request per call, to the configured host only.

    `http.client` never follows redirects, so a credential-bearing request can
    never be replayed to another host. TLS uses the platform trust store (or
    the configured CA bundle), certificate and host-name verification, and at
    least TLS 1.2. The response size is bounded.

    `total_timeout_seconds` is an ABSOLUTE bound on the whole request (IDV-C1,
    R-IDV-03): name resolution, the TCP connection, the TLS handshake, sending
    the request, the status line and headers, and the body. Every blocking
    socket call is given only the time left (and at most the connect or read
    timeout), a call after the deadline fails at once, and a response that
    completes after the deadline is refused. A timeout is `TransportError
    ("timeout")`; the connection is always closed.
    """

    def __init__(self, config: MinistryApiConfig, *, clock=None) -> None:
        parts = urlsplit(config.base_url)
        self._host = parts.hostname or ""
        self._port = parts.port or 443
        self._config = config
        self._clock = clock or time.monotonic
        context = ssl.create_default_context(cafile=config.ca_bundle or None)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.sslsocket_class = _DeadlineSSLSocket
        self._context = context

    def request(
        self, method: str, path: str, *, headers: dict[str, str], body: bytes | None
    ) -> HttpResponse:
        config = self._config
        deadline = self._clock() + config.total_timeout_seconds
        connection = _DeadlineHTTPSConnection(
            self._host,
            self._port,
            context=self._context,
            clock=self._clock,
            deadline=deadline,
            connect_cap=config.connect_timeout_seconds,
            io_cap=config.read_timeout_seconds,
        )
        try:
            try:
                connection.connect()
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
            except TimeoutError:  # fmt: skip
                raise TransportError("timeout") from None
            except ssl.SSLError:
                raise TransportError("tls") from None
            except (OSError, http.client.HTTPException):  # fmt: skip
                raise TransportError("connection") from None
            declared = response.getheader("Content-Length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > config.max_response_bytes
            ):
                raise TransportError("too_large")
            chunks: list[bytes] = []
            received = 0
            while True:
                try:
                    chunk = response.read(8192)
                except TimeoutError:  # fmt: skip
                    raise TransportError("timeout") from None
                except (OSError, http.client.HTTPException):  # fmt: skip
                    raise TransportError("connection") from None
                if not chunk:
                    break
                received += len(chunk)
                if received > config.max_response_bytes:
                    raise TransportError("too_large")
                chunks.append(chunk)
            if self._clock() > deadline:
                raise TransportError("timeout")  # never a success after the deadline
            return HttpResponse(status=response.status, body=b"".join(chunks))
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# Token cache (per process; never persisted, never logged)
# ---------------------------------------------------------------------------


@dataclass
class _CachedToken:
    value: str
    expires_at_monotonic: float
    generation: int


class _TokenCache:
    """Single-flight token cache for one process.

    Only one thread authenticates at a time; the others wait for it and reuse
    its token. A token is invalidated only when the token that was rejected is
    still the cached one, so a burst of rejections triggers one renewal, not
    one per request. Authentication happens inside a provider concurrency slot
    (`apps.people.services.identity_verification`), which bounds the number of
    concurrent authentications across processes as well.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: _CachedToken | None = None
        self._generation = 0

    def current(self) -> _CachedToken | None:
        token = self._token
        if token is not None and time.monotonic() < token.expires_at_monotonic:
            return token
        return None

    def invalidate(self, generation: int) -> None:
        with self._lock:
            if self._token is not None and self._token.generation == generation:
                self._token = None

    def obtain(self, authenticate) -> tuple[_CachedToken | None, LookupOutcome | None]:
        with self._lock:
            token = self.current()
            if token is not None:
                return token, None
            value, lifetime, failure = authenticate()
            if failure is not None:
                return None, failure
            self._generation += 1
            token = _CachedToken(
                value=value,
                expires_at_monotonic=time.monotonic() + max(lifetime, 0),
                generation=self._generation,
            )
            # A zero lifetime is used for this lookup only and not cached.
            self._token = token if lifetime > 0 else None
            return token, None

    def clear(self) -> None:
        with self._lock:
            self._token = None


_TOKEN_CACHE = _TokenCache()


def clear_token_cache() -> None:
    """Forget the cached token (tests, and after a configuration change)."""
    _TOKEN_CACHE.clear()


def _value_at(document: object, dotted_path: str) -> tuple[bool, object]:
    current = document
    for key in dotted_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name):
    raise ValueError("non-finite number")


def decode_json_strictly(body: bytes) -> tuple[bool, Any]:
    """Strict UTF-8 JSON decoding: no duplicate keys, no NaN or Infinity, no
    trailing data. A malformed body is never repaired (A13-10)."""
    try:
        text = body.decode("utf-8")
        return True, json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):  # fmt: skip
        return False, None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class MinistryNinProvider:
    """The official ministry adapter (see the module docstring)."""

    PROVIDER_CODE = "MINISTRY_NIN_API"
    IS_OFFICIAL = True

    def __init__(
        self,
        *,
        config: MinistryApiConfig | None = None,
        transport: Transport | None = None,
        token_cache: _TokenCache | None = None,
    ) -> None:
        self._config = config or MinistryApiConfig.from_settings()
        self._transport = transport
        self._tokens = token_cache or _TOKEN_CACHE

    def configuration_problems(self) -> list[str]:
        return [*self._config.malformed_problems(), *self._config.missing_problems()]

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = HttpsTransport(self._config)
        return self._transport

    # -- authentication ------------------------------------------------------

    def _authenticate(self) -> tuple[str, int, LookupOutcome | None]:
        config = self._config
        body = json.dumps({"username": config.username, "password": config.password}).encode()
        try:
            response = self._get_transport().request(
                "POST",
                config.auth_path,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "ASC2026-Registration/1",
                },
                body=body,
            )
        except TransportError as exc:
            return (
                "",
                0,
                LookupOutcome(LookupKind.UNAVAILABLE, detail=f"auth_{exc.code}", retryable=True),
            )
        status = response.status
        if status in (401, 403):
            return (
                "",
                0,
                LookupOutcome(
                    LookupKind.AUTH_ERROR, detail="credentials_rejected", http_status=status
                ),
            )
        if status == 429 or 500 <= status <= 599:
            return (
                "",
                0,
                LookupOutcome(
                    LookupKind.UNAVAILABLE,
                    detail="auth_unavailable",
                    http_status=status,
                    retryable=True,
                ),
            )
        if status != 200:
            return (
                "",
                0,
                LookupOutcome(
                    LookupKind.AUTH_ERROR, detail="auth_unexpected_status", http_status=status
                ),
            )
        decoded_ok, document = decode_json_strictly(response.body)
        if not decoded_ok:
            return (
                "",
                0,
                LookupOutcome(
                    LookupKind.AUTH_ERROR, detail="auth_response_invalid", http_status=status
                ),
            )
        found, token = _value_at(document, config.token_path)
        if not found or not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            # The configured mapping does not describe this response (API-01).
            return (
                "",
                0,
                LookupOutcome(
                    LookupKind.AUTH_ERROR, detail="auth_contract_mismatch", http_status=status
                ),
            )
        lifetime = config.token_cache_seconds
        if config.expiry_path:
            found, raw_expiry = _value_at(document, config.expiry_path)
            if not found or type(raw_expiry) is not int:
                return (
                    "",
                    0,
                    LookupOutcome(
                        LookupKind.AUTH_ERROR, detail="auth_expiry_mismatch", http_status=status
                    ),
                )
            if config.expiry_format == "relative_seconds":
                declared = raw_expiry
            else:
                declared = raw_expiry - int(time.time())
            if declared <= 0:
                return (
                    "",
                    0,
                    LookupOutcome(
                        LookupKind.AUTH_ERROR, detail="auth_expiry_mismatch", http_status=status
                    ),
                )
            lifetime = min(declared - TOKEN_EXPIRY_SAFETY_SECONDS, config.token_cache_seconds)
        return token, lifetime, None

    def probe_authentication(self) -> AuthenticationProbe:
        """One FRESH authentication request, for the diagnostics page.

        Never answered from the token cache, and the token obtained is
        discarded: it is neither cached nor returned, so normal lookups keep
        their own token. Only the outcome, the HTTP status of the response
        and the usable lifetime (seconds, when the response declares one,
        otherwise the configured cache time) are reported."""
        if self.configuration_problems():
            return AuthenticationProbe(
                LookupOutcome(LookupKind.NOT_CONFIGURED, detail="not_configured"), None
            )
        transport = self._get_transport()
        recorder = _StatusRecordingTransport(transport)
        self._transport = recorder
        try:
            _token, lifetime, failure = self._authenticate()
        finally:
            self._transport = transport
        if failure is not None:
            return AuthenticationProbe(failure, None, failure.http_status)
        return AuthenticationProbe(None, max(int(lifetime), 0), recorder.last_status)

    def cached_token_seconds_left(self) -> int | None:
        """Seconds left of this process's cached token, or None. Metadata only
        (never the token), and true for this process only."""
        token = self._tokens.current()
        if token is None:
            return None
        return max(int(token.expires_at_monotonic - time.monotonic()), 0)

    # -- lookup --------------------------------------------------------------

    def _request_lookup(self, nin: str, bearer: str) -> HttpResponse | LookupOutcome:
        path = self._config.lookup_path_template.replace("{nin}", nin)
        try:
            return self._get_transport().request(
                "GET",
                path,
                headers={
                    "Authorization": f"{AUTH_SCHEME} {bearer}",
                    "Accept": "application/json",
                    "User-Agent": "ASC2026-Registration/1",
                },
                body=None,
            )
        except TransportError as exc:
            if exc.code == "too_large":
                return LookupOutcome(LookupKind.INVALID_RESPONSE, detail="response_too_large")
            return LookupOutcome(LookupKind.UNAVAILABLE, detail=exc.code, retryable=True)

    def lookup(self, nin: str) -> LookupOutcome:
        if NIN_PATTERN.fullmatch(nin or "") is None:
            # Never build a request path from anything but 18 ASCII digits.
            return LookupOutcome(LookupKind.INVALID_RESPONSE, detail="nin_not_requestable")
        if self.configuration_problems():
            return LookupOutcome(LookupKind.NOT_CONFIGURED, detail="not_configured")

        reauthenticated = False
        while True:
            token, failure = self._tokens.obtain(self._authenticate)
            if failure is not None:
                return failure
            response = self._request_lookup(nin, token.value)
            if isinstance(response, LookupOutcome):
                return response
            if response.status == 401 and not reauthenticated:
                # A confirmed rejection: renew once, then retry this lookup once.
                self._tokens.invalidate(token.generation)
                reauthenticated = True
                continue
            return self._interpret_lookup(response, reauthenticated=reauthenticated)

    def _interpret_lookup(self, response: HttpResponse, *, reauthenticated: bool) -> LookupOutcome:
        status = response.status
        if status == 200:
            decoded_ok, document = decode_json_strictly(response.body)
            if not decoded_ok:
                return LookupOutcome(
                    LookupKind.INVALID_RESPONSE, detail="json_invalid", http_status=status
                )
            classification = classify_lookup_body(document)
            return LookupOutcome(
                classification.kind,
                facts=classification.facts,
                detail=classification.detail,
                http_status=status,
            )
        if status == 401:
            return LookupOutcome(
                LookupKind.AUTH_ERROR,
                detail="rejected_after_reauthentication" if reauthenticated else "rejected",
                http_status=status,
                retryable=True,
            )
        if status == 403:
            return LookupOutcome(LookupKind.AUTH_ERROR, detail="forbidden", http_status=status)
        if status == 429 or 500 <= status <= 599:
            return LookupOutcome(
                LookupKind.UNAVAILABLE, detail="service_error", http_status=status, retryable=True
            )
        if 300 <= status <= 399:
            return LookupOutcome(
                LookupKind.INVALID_RESPONSE, detail="redirect_refused", http_status=status
            )
        return LookupOutcome(
            LookupKind.INVALID_RESPONSE, detail="unexpected_status", http_status=status
        )


class DisabledNinProvider:
    """No ministry access: every lookup is NOT_CONFIGURED (manual review)."""

    PROVIDER_CODE = "DISABLED"
    IS_OFFICIAL = False

    def configuration_problems(self) -> list[str]:
        return ["The ministry NIN service is disabled (NIN_PROVIDER_BACKEND)."]

    def lookup(self, nin: str) -> LookupOutcome:
        return LookupOutcome(LookupKind.NOT_CONFIGURED, detail="disabled")


#: Synthetic lookup bodies of the development simulation. Every NIN, name and
#: date below is invented; none belongs to a real person.
SIMULATION_FIXTURE = Path(__file__).with_name("fixtures") / "identity_simulation.json"

#: Synthetic NINs that make the simulation fail in a chosen way.
SIMULATED_UNAVAILABLE_NIN = "990000000000000001"
SIMULATED_INVALID_NIN = "990000000000000002"


class LocalSimulationNinProvider:
    """DEVELOPMENT SIMULATION -- never official verification.

    Answers from synthetic response bodies (`fixtures/identity_simulation.json`)
    through the same strict contract code as the real adapter, so local
    development and UAT exercise the real classification and matching. Any
    other NIN is NOT_FOUND. Refused in staging and production.
    """

    PROVIDER_CODE = "LOCAL_SIMULATION"
    IS_OFFICIAL = False

    def configuration_problems(self) -> list[str]:
        return []

    def lookup(self, nin: str) -> LookupOutcome:
        reference = generate_opaque_local_reference(prefix="sim")
        if nin == SIMULATED_UNAVAILABLE_NIN:
            return LookupOutcome(
                LookupKind.UNAVAILABLE,
                detail="simulated_outage",
                retryable=True,
                request_reference=reference,
            )
        if nin == SIMULATED_INVALID_NIN:
            return LookupOutcome(
                LookupKind.INVALID_RESPONSE,
                detail="json_invalid",
                http_status=200,
                request_reference=reference,
            )
        bodies = json.loads(SIMULATION_FIXTURE.read_text(encoding="utf-8"))["responses"]
        body = bodies.get(nin, {"identite": None, "result": "synthetic: no record"})
        classification = classify_lookup_body(body)
        return LookupOutcome(
            classification.kind,
            facts=classification.facts,
            detail=classification.detail,
            http_status=200,
            request_reference=reference,
        )


class UnavailableSimulationNinProvider:
    """DEVELOPMENT SIMULATION: the service is always unavailable."""

    PROVIDER_CODE = "LOCAL_SIMULATION_UNAVAILABLE"
    IS_OFFICIAL = False

    def configuration_problems(self) -> list[str]:
        return []

    def lookup(self, nin: str) -> LookupOutcome:
        return LookupOutcome(LookupKind.UNAVAILABLE, detail="simulated_outage", retryable=True)


#: Deprecated names kept so an older local `.env` keeps loading; both are
#: simulations and are refused in staging and production like every other.
LocalStubNinProvider = LocalSimulationNinProvider
UnavailableNinProvider = UnavailableSimulationNinProvider


def get_nin_provider() -> NinLookupProvider:
    from django.conf import settings

    module_path, _, class_name = settings.NIN_PROVIDER_BACKEND.rpartition(".")
    module = import_module(module_path)
    provider_class = getattr(module, class_name)
    return provider_class()


def provider_readiness() -> tuple[bool, bool, list[str]]:
    """(is_official, is_ready, problems) for the configured backend.

    Never raises and never echoes a configured value. Used by
    `release_readiness` and the deploy check.
    """
    try:
        provider = get_nin_provider()
    except Exception:  # noqa: BLE001 - an unloadable backend is reported, never raised
        return False, False, ["NIN_PROVIDER_BACKEND does not load."]
    is_official = bool(getattr(provider, "IS_OFFICIAL", False))
    problems = list(provider.configuration_problems())
    if not is_official:
        problems.append("The configured NIN backend is not the official ministry adapter.")
    return is_official, is_official and not problems, problems
