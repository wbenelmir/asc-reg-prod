"""Shared ALTCHA challenge-issuance counter with a bounded local fallback
(P4-4-C3, owner decision CACHE-01; ADR-0025).

`apps.core.human_check.challenge_issuance_allowed` asks this module whether
one more challenge may be issued to a client network in the current fixed
window. The caller passes a keyed digest of the network, never the address.

Counter stores (`settings.HUMAN_CHECK_COUNTER_STORE`):

* ``"redis"`` (staging and production): one counter per network and window in
  the Redis database named by `REDIS_URL`, shared by every process and host.
  Each request runs `INCR` and `EXPIRE` inside one MULTI/EXEC transaction, so
  the count is atomic and the key can never exist without an expiry. Django's
  `RedisCache` is deliberately not used: its `incr` is `EXISTS` then `INCR`,
  two separate commands, so a key that expires between them is re-created by
  `INCR` without any expiry.
* ``"local"`` (local development and tests only): a per-process counter with
  the normal limit. Static validation refuses it in staging and production.

When the shared counter fails (timeout, refused connection, a Redis error such
as an out-of-memory refusal, or an unexpected reply), the request is counted
by a per-process fallback with the stricter
`HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW`. The shared counter is tried
again after `HUMAN_CHECK_COUNTER_RETRY_SECONDS` (by one request at a time), and
used again as soon as it answers. Degradation and recovery are reported on the
`asc2026.ops.alerts` logger, rate-limited, with fixed fields only: never an
address, a URL, a credential or exception text. If neither counter can count,
issuance is refused (fail closed).

Bounds, with N processes alive in a window (each restart counts as a new
process because the fallback state is in memory):

* healthy: at most `HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW` per network and
  window, across all processes;
* degraded: at most N x the fallback limit per network and window. In a window
  that overlaps both states, the requests counted by the fallback are not in
  Redis, so the bound is the normal limit plus N x the fallback limit;
* memory: the fallback keeps only the current window, at most
  `HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS` networks. When that table is
  full, every further network shares one overflow counter with the fallback
  limit, so a flood of new addresses can neither grow memory nor reset another
  network's count.

State evidence (P4-4-C4, review findings R-C3-01 and R-C3-02):

* Only a completed counter write proves that the shared counter works: an
  issuance count, or the readiness capability write described below. A Redis
  `PING` is never used: Redis can answer it while it refuses `INCR` or
  `EXPIRE` (an ACL, a read-only replica, an out-of-memory refusal).
* Before any write, a configured shared counter is ``unverified``. `probe()`
  resolves that with one capability write.
* `probe()` follows the same retry schedule as requests. While the shared
  counter is degraded it writes at most once per retry interval, so probing
  can neither shorten the backoff nor add write attempts. While the counter
  is healthy, a successful write in the last retry interval is reused as
  evidence, so a probe does not write on every call.
* The capability write is the same atomic `INCR` + `EXPIRE` on one dedicated
  key, `<namespace>:readiness-probe` (TTL `PROBE_TTL_SECONDS`), shared by every
  process. That key has no window suffix and a non-hex label, so it can never
  be an issuance key (`<32 hex digest>:<window>`), and no issuance decision
  ever reads it. It needs the same permissions as issuance, expires by itself,
  and needs no cleanup.
* A recovery alert follows only a completed write after a known failure.
* A working shared counter is available whatever happened to the fallback
  earlier; the fallback's own health is kept separately (`fallback_healthy`)
  and applies again only when the shared counter fails.
* Reaching the per-network limit is a refusal by a working counter, never a
  dependency failure.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass

from django.conf import settings

alert_logger = logging.getLogger("asc2026.ops.alerts")

#: Redis key namespace of the challenge-issuance counter. Every key is
#: `<namespace>:<keyed network digest>:<window index>`.
KEY_NAMESPACE = "asc:abuse:v1:hc-issue"
#: Seconds a window key outlives its window, so a late request still counts.
KEY_GRACE_SECONDS = 5

STORE_REDIS = "redis"
STORE_LOCAL = "local"
STORES = (STORE_REDIS, STORE_LOCAL)

STATE_SHARED = "shared"  # the last shared counter write completed
STATE_UNVERIFIED = "unverified"  # shared counter configured; no write attempted yet
STATE_LOCAL = "local"  # no shared counter by configuration (local development, tests)
STATE_DEGRADED = "degraded"  # the shared counter fails; the stricter fallback counts
STATE_UNAVAILABLE = "unavailable"  # no counter can count; issuance is refused

#: The readiness capability key, under the issuance namespace (one key for all
#: processes), and its expiry. See the module docstring.
PROBE_KEY_LABEL = "readiness-probe"
PROBE_TTL_SECONDS = 60

ALERT_DEGRADED = "HUMAN_CHECK_COUNTER_DEGRADED"
ALERT_RECOVERED = "HUMAN_CHECK_COUNTER_RECOVERED"
ALERT_UNAVAILABLE = "HUMAN_CHECK_COUNTER_UNAVAILABLE"

_OVERFLOW = object()


class CounterUnavailable(Exception):
    """A counter could not count. Carries a fixed category, never details."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def failure_category(exc: BaseException) -> str:
    """A fixed word for an exception, safe to log. Never its text."""
    if isinstance(exc, CounterUnavailable):
        return exc.category
    try:
        from redis import exceptions as redis_exceptions
    except ImportError:  # pragma: no cover - redis is a locked dependency
        redis_exceptions = None
    if redis_exceptions is not None:
        if isinstance(exc, redis_exceptions.TimeoutError):
            return "timeout"
        if isinstance(exc, redis_exceptions.ConnectionError):
            return "connection"
        if isinstance(exc, redis_exceptions.ResponseError):
            return "rejected"
    if isinstance(exc, (TimeoutError, socket.timeout)):  # fmt: skip
        return "timeout"
    if isinstance(exc, OSError):
        return "connection"
    return "error"


class LocalWindowCounter:
    """A thread-safe fixed-window counter for one process, with bounded memory.

    Only the current window is kept; the first request of a new window drops
    the previous one. A request stamped with an earlier window (a clock step
    back) is counted in the current window, which can only make it stricter.
    """

    def __init__(self, max_networks: int):
        self.max_networks = max(1, int(max_networks))
        self._lock = threading.Lock()
        self._window: int | None = None
        self._counts: dict[str, int] = {}
        self._overflow = 0

    def hit(self, bucket: str, window_index: int) -> int:
        with self._lock:
            if self._window is None or window_index > self._window:
                self._window = window_index
                self._counts.clear()
                self._overflow = 0
            if bucket in self._counts or len(self._counts) < self.max_networks:
                count = self._counts.get(bucket, 0) + 1
                self._counts[bucket] = count
                return count
            self._overflow += 1
            return self._overflow

    def snapshot(self) -> dict:
        """Sizes only, for tests and diagnostics; never a digest."""
        with self._lock:
            return {
                "window": self._window,
                "networks": len(self._counts),
                "overflow": self._overflow,
            }

    def keys(self) -> list[str]:
        """The tracked bucket digests (tests only)."""
        with self._lock:
            return list(self._counts)


class RedisWindowCounter:
    """The shared counter: one atomic MULTI/EXEC of `INCR` and `EXPIRE` per hit.

    The client has bounded connect and socket timeouts and no retries: a
    retried `INCR` could count one request twice, and every retry would add
    latency to the public request. Pool and TLS settings come from the URL
    (`rediss://` verifies the certificate and the host name by default).

    P4-4-C4 (R-C3-01): there is deliberately no `ping()`. Answering `PING` does
    not show that Redis accepts the counter's writes.
    """

    def __init__(self, url: str, *, connect_timeout: float, socket_timeout: float):
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        self._client = redis.Redis.from_url(
            url,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
            retry=Retry(NoBackoff(), 0),
            health_check_interval=0,
        )

    def hit(self, key: str, ttl_seconds: int) -> int:
        pipe = self._client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, ttl_seconds)
        count, _expire_set = pipe.execute()
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise CounterUnavailable("unexpected-reply")
        return count


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: "shared", "local", "fallback" or "refused"
    counted_by: str


class IssuanceLimiter:
    """Chooses the counter for each request and tracks degradation.

    `clock` is a monotonic clock for the retry and alert intervals; the window
    index comes from the wall-clock `now` the caller passes, so every process
    and host agrees on window boundaries.
    """

    def __init__(
        self,
        *,
        shared,
        normal_limit: int,
        fallback_limit: int,
        window_seconds: int,
        max_networks: int,
        retry_seconds: int,
        alert_interval_seconds: int,
        namespace: str = KEY_NAMESPACE,
        clock=time.monotonic,
    ):
        self.shared = shared
        self.normal_limit = int(normal_limit)
        self.fallback_limit = int(fallback_limit)
        self.window_seconds = max(1, int(window_seconds))
        self.retry_seconds = max(1, int(retry_seconds))
        self.alert_interval_seconds = max(1, int(alert_interval_seconds))
        self.namespace = namespace
        self.local = LocalWindowCounter(max_networks)
        self._clock = clock
        self._lock = threading.Lock()
        self._degraded_since: float | None = None
        self._next_shared_attempt = 0.0
        self._fallback_decisions = 0
        self._fallback_refusals = 0
        self._fallback_broken = False
        #: Monotonic time of the last completed shared write, or None.
        self._last_shared_success: float | None = None
        self._last_alert: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    # -- state ---------------------------------------------------------------

    def state(self) -> str:
        """The state the last evidence supports (P4-4-C4).

        A working shared counter is decisive: its state does not depend on an
        earlier fallback failure (R-C3-02). The fallback's health matters only
        when the fallback is the counter in use."""
        with self._lock:
            if self.shared is None:
                return STATE_UNAVAILABLE if self._fallback_broken else STATE_LOCAL
            if self._degraded_since is None:
                if self._last_shared_success is None:
                    return STATE_UNVERIFIED
                return STATE_SHARED
            return STATE_UNAVAILABLE if self._fallback_broken else STATE_DEGRADED

    def fallback_healthy(self) -> bool:
        """False after the fallback failed, until it counts successfully again.
        Kept apart from the shared counter's state; never cleared by Redis."""
        with self._lock:
            return not self._fallback_broken

    def _shared_attempt_due(self) -> bool:
        with self._lock:
            if self._degraded_since is None:
                return True
            now = self._clock()
            if now < self._next_shared_attempt:
                return False
            # One request per interval tries the shared counter; the others
            # keep using the fallback meanwhile.
            self._next_shared_attempt = now + self.retry_seconds
            return True

    # -- alerts --------------------------------------------------------------

    def _alert(self, code: str, level: int, message: str, fields: dict) -> None:
        """Emit at most one alert per code and interval; count the rest."""
        now = self._clock()
        last = self._last_alert.get(code)
        if last is not None and now - last < self.alert_interval_seconds:
            self._suppressed[code] = self._suppressed.get(code, 0) + 1
            return
        self._last_alert[code] = now
        suppressed = self._suppressed.pop(code, 0)
        alert_logger.log(
            level,
            message,
            extra={"alert": code, "suppressed_since_last": suppressed, **fields},
        )

    def _record_shared_failure(self, exc: BaseException) -> None:
        category = failure_category(exc)
        with self._lock:
            now = self._clock()
            first = self._degraded_since is None
            if first:
                self._degraded_since = now
                self._fallback_decisions = 0
                self._fallback_refusals = 0
            self._next_shared_attempt = now + self.retry_seconds
            self._alert(
                ALERT_DEGRADED,
                logging.ERROR,
                "challenge issuance counter degraded: the shared counter is unavailable; "
                "the stricter per-process fallback limit applies",
                {
                    "failure": category,
                    "transition": first,
                    "degraded_seconds": int(now - self._degraded_since),
                    "fallback_limit": self.fallback_limit,
                    "window_seconds": self.window_seconds,
                    "fallback_decisions": self._fallback_decisions,
                },
            )

    def _record_shared_success(self) -> None:
        """Called only after a completed shared write (a count or the capability
        write). Never after a PING."""
        with self._lock:
            now = self._clock()
            self._last_shared_success = now
            if self._degraded_since is None:
                return
            fields = {
                "degraded_seconds": int(now - self._degraded_since),
                "fallback_decisions": self._fallback_decisions,
                "fallback_refusals": self._fallback_refusals,
            }
            self._degraded_since = None
            self._next_shared_attempt = 0.0
            self._alert(
                ALERT_RECOVERED,
                logging.WARNING,
                "challenge issuance counter recovered: the shared counter is used again",
                fields,
            )

    def _record_fallback_failure(self) -> None:
        with self._lock:
            self._fallback_broken = True
            self._alert(
                ALERT_UNAVAILABLE,
                logging.CRITICAL,
                "challenge issuance refused: no counter can count requests",
                {"window_seconds": self.window_seconds},
            )

    # -- decisions -----------------------------------------------------------

    def _key(self, bucket: str, window_index: int) -> str:
        return f"{self.namespace}:{bucket}:{window_index}"

    def _count_locally(self, bucket: str, window_index: int, limit: int) -> bool:
        count = self.local.hit(bucket, window_index)
        with self._lock:
            self._fallback_broken = False
        return count <= limit

    def allow(self, bucket: str, *, now: float) -> Decision:
        window_index = int(now // self.window_seconds)
        if self.shared is None:
            try:
                allowed = self._count_locally(bucket, window_index, self.normal_limit)
            except Exception:  # noqa: BLE001 - no counter: fail closed
                self._record_fallback_failure()
                return Decision(False, "refused")
            return Decision(allowed, "local")
        if self._shared_attempt_due():
            try:
                count = self.shared.hit(
                    self._key(bucket, window_index), self.window_seconds + KEY_GRACE_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - any failure degrades, never allows
                self._record_shared_failure(exc)
            else:
                self._record_shared_success()
                return Decision(count <= self.normal_limit, "shared")
        try:
            allowed = self._count_locally(bucket, window_index, self.fallback_limit)
        except Exception:  # noqa: BLE001 - no counter: fail closed
            self._record_fallback_failure()
            return Decision(False, "refused")
        with self._lock:
            self._fallback_decisions += 1
            if not allowed:
                self._fallback_refusals += 1
        return Decision(allowed, "fallback")

    def probe_key(self) -> str:
        return f"{self.namespace}:{PROBE_KEY_LABEL}"

    def _fresh_shared_evidence(self) -> bool:
        with self._lock:
            return (
                self._degraded_since is None
                and self._last_shared_success is not None
                and self._clock() - self._last_shared_success < self.retry_seconds
            )

    def probe(self) -> str:
        """Readiness (P4-4-C4): report the state that counter writes support.
        Never raises, and never uses PING.

        * No shared counter: the local or unavailable state, no contact.
        * Healthy, with a completed write in the last retry interval: shared,
          no contact.
        * Otherwise, when a shared attempt is due under the retry schedule: one
          capability write (`INCR` + `EXPIRE` on the probe key). Its success or
          failure is recorded exactly like an issuance count.
        * Degraded and not yet due: the degraded or unavailable state, no
          contact, so probing never shortens the backoff.
        """
        if self.shared is not None and not self._fresh_shared_evidence():
            if self._shared_attempt_due():
                try:
                    self.shared.hit(self.probe_key(), PROBE_TTL_SECONDS)
                except Exception as exc:  # noqa: BLE001 - sanitized by the state
                    self._record_shared_failure(exc)
                else:
                    self._record_shared_success()
        return self.state()


# ---------------------------------------------------------------------------
# The process-wide limiter, rebuilt when its settings change (tests)
# ---------------------------------------------------------------------------

_SETTING_NAMES = (
    "HUMAN_CHECK_COUNTER_STORE",
    "REDIS_URL",
    "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW",
    "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW",
    "HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS",
    "HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS",
    "HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS",
    "HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS",
    "HUMAN_CHECK_COUNTER_RETRY_SECONDS",
    "HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS",
)
_limiter_lock = threading.Lock()
_limiter: IssuanceLimiter | None = None
_limiter_signature: tuple | None = None


class _MisconfiguredSharedCounter:
    """Stands in when the Redis store is selected without a URL: every hit
    fails, so the stricter fallback applies and the degradation alert fires.
    Static validation refuses this configuration in staging and production."""

    def hit(self, key: str, ttl_seconds: int) -> int:
        raise CounterUnavailable("configuration")


def _build_limiter() -> IssuanceLimiter:
    store = getattr(settings, "HUMAN_CHECK_COUNTER_STORE", STORE_LOCAL)
    shared = None
    if store != STORE_LOCAL:
        url = getattr(settings, "REDIS_URL", None)
        shared = _MisconfiguredSharedCounter()
        if store == STORE_REDIS and url:
            try:
                shared = RedisWindowCounter(
                    url,
                    connect_timeout=int(settings.HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS) / 1000,
                    socket_timeout=int(settings.HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS) / 1000,
                )
            except Exception:  # noqa: BLE001 - a malformed URL degrades, never a 500
                shared = _MisconfiguredSharedCounter()
    return IssuanceLimiter(
        shared=shared,
        normal_limit=settings.HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW,
        fallback_limit=settings.HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW,
        window_seconds=settings.HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS,
        max_networks=settings.HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS,
        retry_seconds=settings.HUMAN_CHECK_COUNTER_RETRY_SECONDS,
        alert_interval_seconds=settings.HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS,
    )


def get_issuance_limiter() -> IssuanceLimiter:
    global _limiter, _limiter_signature
    signature = tuple(getattr(settings, name, None) for name in _SETTING_NAMES)
    with _limiter_lock:
        if _limiter is None or signature != _limiter_signature:
            _limiter = _build_limiter()
            _limiter_signature = signature
        return _limiter


def reset_issuance_limiter() -> None:
    """Forget the process-wide limiter and its counts (tests only)."""
    global _limiter, _limiter_signature
    with _limiter_lock:
        _limiter = None
        _limiter_signature = None
