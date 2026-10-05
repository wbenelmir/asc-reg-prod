"""P4-4-C3 (owner decision CACHE-01): the shared challenge-issuance counter,
its bounded per-process fallback, degradation and recovery alerts, and the
challenge endpoint built on them.

Evidence boundary: no Redis server is available in this environment. The
shared counter here is `_SharedStandIn`, an in-process, thread-safe stand-in
that models the one atomic operation the adapter performs (INCR with an
expiry). These tests prove this project's decision logic and the adapter's
command sequence, NOT Redis semantics, cross-process behaviour against a real
server, or network failure modes. Those are covered by the opt-in
`test_p4_4_c3_redis_integration.py` (skipped without an isolated Redis) and the
manual staging guide. Synthetic data only.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.urls import reverse
from redis import exceptions as redis_exceptions

from apps.core import human_check, issuance_counter
from apps.core.issuance_counter import (
    ALERT_DEGRADED,
    ALERT_RECOVERED,
    ALERT_UNAVAILABLE,
    KEY_NAMESPACE,
    STATE_DEGRADED,
    STATE_LOCAL,
    STATE_SHARED,
    STATE_UNAVAILABLE,
    CounterUnavailable,
    IssuanceLimiter,
    LocalWindowCounter,
    RedisWindowCounter,
    failure_category,
    get_issuance_limiter,
)

ALERTS = "asc2026.ops.alerts"
WINDOW = 600
NOW = 1_900_000_000.0
#: Planted in failure messages; must never reach a log record or a response.
LEAK = "rediss://redis-p44c3.internal.invalid:6380/2 p44c3-leak-marker"


class _SharedStandIn:
    """In-process stand-in for the shared store (NOT Redis): an atomic
    increment per key under a lock, recording each expiry it is given."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.calls = 0
        self.failure: Exception | None = None

    def hit(self, key: str, ttl_seconds: int) -> int:
        with self._lock:
            self.calls += 1
            if self.failure is not None:
                raise self.failure
            self.counts[key] = self.counts.get(key, 0) + 1
            self.ttls[key] = ttl_seconds
            return self.counts[key]


class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _limiter(shared, *, normal=6, fallback=2, networks=100, clock=None, retry=15, alert=300):
    return IssuanceLimiter(
        shared=shared,
        normal_limit=normal,
        fallback_limit=fallback,
        window_seconds=WINDOW,
        max_networks=networks,
        retry_seconds=retry,
        alert_interval_seconds=alert,
        clock=clock or _Clock(),
    )


def _alert_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == ALERTS]


def _record_text(record: logging.LogRecord) -> str:
    from apps.core.logging_config import StructuredFormatter

    return StructuredFormatter().format(record)


# ---------------------------------------------------------------------------
# The per-process counter: windows, thread safety, bounded state
# ---------------------------------------------------------------------------


def test_the_local_counter_counts_per_network_and_window() -> None:
    counter = LocalWindowCounter(max_networks=10)
    assert [counter.hit("a", 5) for _ in range(3)] == [1, 2, 3]
    assert counter.hit("b", 5) == 1
    # A new window starts every network afresh and drops the old window.
    assert counter.hit("a", 6) == 1
    assert counter.snapshot() == {"window": 6, "networks": 1, "overflow": 0}


def test_a_request_stamped_with_an_earlier_window_counts_in_the_current_one() -> None:
    counter = LocalWindowCounter(max_networks=10)
    counter.hit("a", 7)
    assert counter.hit("a", 6) == 2  # never a fresh allowance
    assert counter.snapshot()["window"] == 7


def test_concurrent_increments_are_never_lost() -> None:
    counter = LocalWindowCounter(max_networks=10)
    with ThreadPoolExecutor(max_workers=16) as pool:
        counts = list(pool.map(lambda _i: counter.hit("same", 1), range(4000)))
    assert sorted(counts) == list(range(1, 4001))


def test_concurrent_first_requests_in_a_new_window_initialize_it_once() -> None:
    counter = LocalWindowCounter(max_networks=10)
    counter.hit("same", 1)
    barrier = threading.Barrier(12)

    def first_in_window_2(_i):
        barrier.wait()
        return counter.hit("same", 2)

    with ThreadPoolExecutor(max_workers=12) as pool:
        counts = list(pool.map(first_in_window_2, range(12)))
    assert sorted(counts) == list(range(1, 13))


def test_state_is_bounded_and_a_flood_of_networks_never_resets_a_count() -> None:
    limiter = _limiter(None, normal=3, networks=4)
    for _ in range(3):
        assert limiter.allow("victim", now=NOW).allowed
    assert not limiter.allow("victim", now=NOW).allowed
    flood = [limiter.allow(f"new-{index}", now=NOW).allowed for index in range(500)]
    # Three more networks fit the table; every further one shares one overflow
    # bucket with the same limit, so the flood gains only that limit.
    assert flood.count(True) == 3 + 3
    assert limiter.local.snapshot() == {
        "window": int(NOW // WINDOW),
        "networks": 4,
        "overflow": 497,
    }
    assert not limiter.allow("victim", now=NOW).allowed  # never evicted, never reset


# ---------------------------------------------------------------------------
# The shared counter (decision logic, with the in-process stand-in)
# ---------------------------------------------------------------------------


def test_processes_sharing_the_counter_admit_exactly_the_normal_limit() -> None:
    shared = _SharedStandIn()
    processes = [_limiter(shared, normal=6) for _ in range(3)]  # three "worker processes"

    def request(index):
        return processes[index % 3].allow("net", now=NOW)

    with ThreadPoolExecutor(max_workers=9) as pool:
        decisions = list(pool.map(request, range(30)))
    assert sum(d.allowed for d in decisions) == 6
    assert {d.counted_by for d in decisions} == {"shared"}
    assert all(p.state() == STATE_SHARED for p in processes)


def test_keys_are_namespaced_windowed_digests_with_an_expiry() -> None:
    shared = _SharedStandIn()
    limiter = _limiter(shared)
    digest = human_check.issuance_digest("192.0.2.80")
    limiter.allow(digest, now=NOW)
    limiter.allow(digest, now=NOW + WINDOW)  # the next window
    window = int(NOW // WINDOW)
    assert sorted(shared.counts) == [
        f"{KEY_NAMESPACE}:{digest}:{window}",
        f"{KEY_NAMESPACE}:{digest}:{window + 1}",
    ]
    assert set(shared.ttls.values()) == {WINDOW + issuance_counter.KEY_GRACE_SECONDS}
    assert not any("192.0.2.80" in key for key in shared.counts)


def test_the_window_boundary_starts_a_fresh_shared_count() -> None:
    shared = _SharedStandIn()
    limiter = _limiter(shared, normal=2)
    start = (NOW // WINDOW) * WINDOW
    assert [limiter.allow("n", now=start + WINDOW - 0.001).allowed for _ in range(3)] == [
        True,
        True,
        False,
    ]
    assert limiter.allow("n", now=start + WINDOW).allowed


# ---------------------------------------------------------------------------
# Outage, fallback, retry and recovery
# ---------------------------------------------------------------------------


def test_an_outage_applies_the_stricter_fallback_never_an_allowance() -> None:
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    limiter = _limiter(shared, normal=6, fallback=2)
    decisions = [limiter.allow("net", now=NOW) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, False, False, False]
    assert {d.counted_by for d in decisions} == {"fallback"}
    assert limiter.state() == STATE_DEGRADED


def test_the_fallback_bound_is_per_process() -> None:
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.TimeoutError(LEAK)
    processes = [_limiter(shared, normal=6, fallback=2) for _ in range(3)]
    admitted = sum(p.allow("net", now=NOW).allowed for p in processes for _ in range(10))
    assert admitted == 3 * 2  # N processes x the fallback limit


def test_a_failing_shared_counter_is_retried_once_per_interval_then_used_again(caplog) -> None:
    clock = _Clock()
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    limiter = _limiter(shared, normal=6, fallback=50, clock=clock, retry=15)
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        limiter.allow("net", now=NOW)
        calls_after_failure = shared.calls
        for _ in range(20):
            limiter.allow("net", now=NOW)  # inside the retry interval
        assert shared.calls == calls_after_failure
        clock.now += 15
        limiter.allow("net", now=NOW)  # one retry, still failing
        assert shared.calls == calls_after_failure + 1
        shared.failure = None  # Redis is back
        clock.now += 15
        decision = limiter.allow("net", now=NOW)
    assert decision.counted_by == "shared"
    assert limiter.state() == STATE_SHARED
    codes = [record.alert for record in _alert_records(caplog)]
    assert codes == [ALERT_DEGRADED, ALERT_RECOVERED]
    recovered = _alert_records(caplog)[-1]
    assert recovered.fallback_decisions == 22  # the first failure, 20 waiting, 1 failed retry
    assert recovered.degraded_seconds == 30


def test_requests_admitted_during_degradation_are_not_in_the_shared_count() -> None:
    """The documented bound for a window that overlaps an outage: the normal
    limit plus the fallback limit per process."""
    clock = _Clock()
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    limiter = _limiter(shared, normal=6, fallback=2, clock=clock)
    degraded = sum(limiter.allow("net", now=NOW).allowed for _ in range(5))
    shared.failure = None
    clock.now += 15
    recovered = sum(limiter.allow("net", now=NOW).allowed for _ in range(10))
    assert (degraded, recovered) == (2, 6)


def test_degradation_alerts_are_sanitized_and_rate_limited(caplog) -> None:
    clock = _Clock()
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    limiter = _limiter(shared, clock=clock, retry=1, alert=300)
    digest = human_check.issuance_digest("192.0.2.81")
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        for _ in range(10):
            limiter.allow(digest, now=NOW)
            clock.now += 10  # every request retries, every retry fails
        clock.now += 300
        limiter.allow(digest, now=NOW)
    records = _alert_records(caplog)
    assert [record.alert for record in records] == [ALERT_DEGRADED, ALERT_DEGRADED]
    assert records[0].transition is True and records[0].failure == "connection"
    assert records[1].transition is False and records[1].suppressed_since_last == 9
    assert records[0].levelname == "ERROR"
    for record in records:
        text = _record_text(record)
        for fragment in ("p44c3", "redis-p44c3", "6380", "192.0.2.81", digest, "ConnectionError"):
            assert fragment not in text


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (redis_exceptions.TimeoutError(LEAK), "timeout"),
        (redis_exceptions.ConnectionError(LEAK), "connection"),
        (redis_exceptions.ResponseError("OOM command not allowed " + LEAK), "rejected"),
        (TimeoutError(LEAK), "timeout"),
        (OSError(LEAK), "connection"),
        (ValueError(LEAK), "error"),
        (CounterUnavailable("configuration"), "configuration"),
    ],
)
def test_failure_categories_are_fixed_words(exc, category) -> None:
    assert failure_category(exc) == category


def test_when_no_counter_can_count_issuance_is_refused(caplog, monkeypatch) -> None:
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    limiter = _limiter(shared)

    def broken(self, bucket, window_index):
        raise MemoryError

    monkeypatch.setattr(LocalWindowCounter, "hit", broken)
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        decision = limiter.allow("net", now=NOW)
    assert decision == issuance_counter.Decision(False, "refused")
    assert limiter.state() == STATE_UNAVAILABLE
    assert ALERT_UNAVAILABLE in [record.alert for record in _alert_records(caplog)]


def test_a_failing_local_only_counter_also_refuses(monkeypatch) -> None:
    limiter = _limiter(None)

    def broken(self, bucket, window_index):
        raise MemoryError

    monkeypatch.setattr(LocalWindowCounter, "hit", broken)
    assert not limiter.allow("net", now=NOW).allowed
    assert limiter.state() == STATE_UNAVAILABLE


def test_the_readiness_probe_detects_failure_and_recovery() -> None:
    # P4-4-C4 (R-C3-01): the probe writes instead of pinging, reuses a fresh
    # write as evidence, and follows the retry schedule while degraded.
    clock = _Clock()
    shared = _SharedStandIn()
    limiter = _limiter(shared, clock=clock, retry=15)
    assert limiter.probe() == STATE_SHARED
    shared.failure = redis_exceptions.TimeoutError(LEAK)
    clock.now += 15  # the earlier write is no longer fresh evidence
    assert limiter.probe() == STATE_DEGRADED
    shared.failure = None
    assert limiter.probe() == STATE_DEGRADED  # not due yet: no write attempt
    clock.now += 15
    assert limiter.probe() == STATE_SHARED
    assert _limiter(None).probe() == STATE_LOCAL


# ---------------------------------------------------------------------------
# The Redis adapter: one transaction, bounded timeouts, no retries
# ---------------------------------------------------------------------------


class _RecordingPipeline:
    def __init__(self, replies):
        self.commands = []
        self.replies = replies

    def incr(self, key):
        self.commands.append(("INCR", key))

    def expire(self, key, seconds):
        self.commands.append(("EXPIRE", key, seconds))

    def execute(self):
        return self.replies


def _adapter(monkeypatch, replies):
    adapter = RedisWindowCounter(
        "rediss://redis.p44c3.example.invalid:6380/2", connect_timeout=0.5, socket_timeout=0.5
    )
    pipelines = []

    def pipeline(transaction=True):
        pipelines.append(transaction)
        pipe = _RecordingPipeline(replies)
        pipelines.append(pipe)
        return pipe

    monkeypatch.setattr(adapter._client, "pipeline", pipeline)
    return adapter, pipelines


def test_the_adapter_counts_in_one_transaction_and_always_sets_an_expiry(monkeypatch) -> None:
    adapter, pipelines = _adapter(monkeypatch, [3, True])
    assert adapter.hit("asc:abuse:v1:hc-issue:d:1", 605) == 3
    transaction, pipe = pipelines
    assert transaction is True  # MULTI ... EXEC
    assert pipe.commands == [
        ("INCR", "asc:abuse:v1:hc-issue:d:1"),
        ("EXPIRE", "asc:abuse:v1:hc-issue:d:1", 605),
    ]


@pytest.mark.parametrize("reply", [[None, True], ["3", True], [True, True], [0, True]])
def test_an_unexpected_reply_is_a_failure_not_an_allowance(monkeypatch, reply) -> None:
    adapter, _pipelines = _adapter(monkeypatch, reply)
    with pytest.raises(CounterUnavailable):
        adapter.hit("k", 605)


def test_the_adapter_has_bounded_timeouts_tls_verification_and_no_retries() -> None:
    adapter = RedisWindowCounter(
        "rediss://redis.p44c3.example.invalid:6380/2", connect_timeout=0.5, socket_timeout=0.25
    )
    pool = adapter._client.connection_pool
    connection = pool.make_connection()  # constructed, never connected
    assert pool.connection_kwargs["socket_connect_timeout"] == 0.5
    assert pool.connection_kwargs["socket_timeout"] == 0.25
    assert pool.connection_kwargs["db"] == 2
    assert connection.retry.get_retries() == 0
    assert type(connection).__name__ == "SSLConnection"
    import ssl

    assert connection.cert_reqs == ssl.CERT_REQUIRED and connection.check_hostname is True


def test_a_real_client_against_a_closed_local_port_degrades_within_the_timeouts() -> None:
    """Genuine network failure through redis-py, against a closed loopback port
    (no Redis server is involved, and nothing outside this machine is contacted)."""
    closed = RedisWindowCounter("redis://127.0.0.1:1/0", connect_timeout=0.3, socket_timeout=0.3)
    limiter = _limiter(closed, normal=6, fallback=2)
    started = time.monotonic()
    decisions = [limiter.allow("net", now=NOW) for _ in range(5)]
    elapsed = time.monotonic() - started
    assert [d.counted_by for d in decisions] == ["fallback"] * 5
    assert [d.allowed for d in decisions] == [True, True, False, False, False]
    assert elapsed < 3  # one bounded attempt; the others wait for the retry interval
    assert limiter.probe() == STATE_DEGRADED


# ---------------------------------------------------------------------------
# The process-wide limiter and the challenge endpoint
# ---------------------------------------------------------------------------


def test_local_development_counts_locally_without_any_redis() -> None:
    assert settings.HUMAN_CHECK_COUNTER_STORE == "local"
    limiter = get_issuance_limiter()
    assert limiter.shared is None and limiter.probe() == STATE_LOCAL


@override_settings(HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=7)
def test_the_limiter_follows_its_settings() -> None:
    assert get_issuance_limiter().normal_limit == 7


@override_settings(HUMAN_CHECK_COUNTER_STORE="redis", REDIS_URL=None)
def test_the_redis_store_without_a_url_degrades_instead_of_counting_per_process_normally() -> None:
    limiter = get_issuance_limiter()
    assert limiter.allow("net", now=NOW).counted_by == "fallback"
    assert limiter.state() == STATE_DEGRADED


@override_settings(
    HUMAN_CHECK_COUNTER_STORE="redis", REDIS_URL="rediss://counter.p44c3.invalid:notaport/2"
)
def test_a_malformed_redis_url_degrades_instead_of_failing_the_request() -> None:
    limiter = get_issuance_limiter()
    assert limiter.allow("net", now=NOW).counted_by == "fallback"
    assert limiter.state() == STATE_DEGRADED


@override_settings(
    HUMAN_CHECK_COUNTER_STORE="redis",
    REDIS_URL=None,
    HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW=6,
    HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW=2,
)
@pytest.mark.django_db
def test_refused_issuance_in_degraded_mode_creates_no_session() -> None:
    before = Session.objects.count()
    statuses = [
        Client().get(reverse("accounts:human-check-challenge")).status_code for _ in range(5)
    ]
    assert statuses == [200, 200, 429, 429, 429]
    assert Session.objects.count() == before + 2


@pytest.mark.django_db
def test_refused_issuance_when_no_counter_can_count_creates_no_session(monkeypatch) -> None:
    def broken(self, bucket, window_index):
        raise MemoryError

    monkeypatch.setattr(LocalWindowCounter, "hit", broken)
    before = Session.objects.count()
    client = Client()
    response = client.get(reverse("accounts:human-check-challenge"))
    assert response.status_code == 429
    assert response.json() == {"error": "try_again_later"}
    assert settings.SESSION_COOKIE_NAME not in response.cookies
    assert Session.objects.count() == before


@override_settings(
    HUMAN_CHECK_COUNTER_STORE="redis",
    REDIS_URL=None,
    OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator",
)
@pytest.mark.django_db
def test_altcha_and_otp_action_protections_are_unchanged_while_degraded() -> None:
    """The fallback limits only issuance; the OTP action keeps ALTCHA
    verification, session and action binding and one-time consumption."""
    from apps.core.testing import otp_request_data

    client = Client()
    data = otp_request_data("p44c3-degraded@example.com", client=client)
    assert get_issuance_limiter().state() == STATE_DEGRADED
    first = client.post(reverse("accounts:otp-request"), data)
    assert first.status_code == 302
    # The same solved payload again, in the same session: refused as a replay.
    assert client.post(reverse("accounts:otp-request"), data).status_code == 400
    # A payload solved for one session is refused in another.
    foreign = otp_request_data("p44c3-other@example.com", client=client)
    assert Client().post(reverse("accounts:otp-request"), foreign).status_code == 400
    with pytest.raises(human_check.HumanCheckFailed) as caught:
        human_check.verify_and_consume(
            data["human_check"], action=human_check.ACTION_OTP_REQUEST, session=client.session
        )
    assert caught.value.reason == "REPLAYED"


def test_the_recovery_switch_stays_off() -> None:
    assert settings.HUMAN_CHECK_ENABLED is True


def test_no_alert_field_can_carry_a_url(caplog) -> None:
    shared = _SharedStandIn()
    shared.failure = redis_exceptions.ConnectionError(LEAK)
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        _limiter(shared).allow("net", now=NOW)
    record = _alert_records(caplog)[0]
    payload = json.loads(_record_text(record))
    assert payload["alert"] == ALERT_DEGRADED
    assert "rediss" not in json.dumps(payload)
