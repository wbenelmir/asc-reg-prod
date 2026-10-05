"""P4-4-C4: regressions for independent-review findings R-C3-01 and R-C3-02.

* R-C3-01: a successful PING must not prove counter-write capability, clear a
  known write degradation, shorten the retry backoff or emit a recovery alert.
* R-C3-02: once a shared count works again, the limiter must be available
  even if the fallback failed earlier; a later shared failure with a still
  broken fallback must fail closed again.

Evidence boundary: the shared counters here are in-process stand-ins, NOT
Redis. They verify this project's decisions and states only. Real Redis
behaviour stays NOT_PROVEN (see `test_p4_4_c3_redis_integration.py`, opt-in).
Synthetic data only.
"""

from __future__ import annotations

import json
import logging

import pytest
from django.test import Client

from apps.core import issuance_counter, release_readiness
from apps.core.issuance_counter import (
    ALERT_DEGRADED,
    ALERT_RECOVERED,
    PROBE_KEY_LABEL,
    PROBE_TTL_SECONDS,
    STATE_DEGRADED,
    STATE_SHARED,
    STATE_UNAVAILABLE,
    STATE_UNVERIFIED,
    CounterUnavailable,
    IssuanceLimiter,
    LocalWindowCounter,
)

ALERTS = "asc2026.ops.alerts"
NOW = 1_900_000_000.0
RETRY = 15


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _StandIn:
    """In-process stand-in for the shared store (NOT Redis). `ping` always
    answers, as a Redis that refuses writes still does; `hit` fails while
    `write_failure` is set."""

    def __init__(self, write_failure=None):
        self.write_failure = write_failure
        self.hits: list[tuple[str, int]] = []
        self.counts: dict[str, int] = {}
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        return True

    def hit(self, key: str, ttl_seconds: int) -> int:
        self.hits.append((key, ttl_seconds))
        if self.write_failure is not None:
            raise self.write_failure
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


def _limiter(shared, clock, *, normal=6, fallback=2) -> IssuanceLimiter:
    return IssuanceLimiter(
        shared=shared,
        normal_limit=normal,
        fallback_limit=fallback,
        window_seconds=600,
        max_networks=100,
        retry_seconds=RETRY,
        alert_interval_seconds=1,  # never suppress here: every alert is visible
        clock=clock,
    )


def _alerts(caplog) -> list[str]:
    return [r.alert for r in caplog.records if r.name == ALERTS]


@pytest.fixture
def broken_fallback(monkeypatch):
    state = {"broken": True}
    original = LocalWindowCounter.hit

    def hit(self, bucket, window_index):
        if state["broken"]:
            raise MemoryError
        return original(self, bucket, window_index)

    monkeypatch.setattr(LocalWindowCounter, "hit", hit)
    return state


def _readyz(monkeypatch, limiter):
    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", lambda: limiter)
    response = Client().get("/readyz")
    return response.status_code, json.loads(response.content)


@pytest.fixture(autouse=True)
def _isolated_private_storage(settings, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = root


# ---------------------------------------------------------------------------
# R-C3-01: PING succeeds while writes are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        CounterUnavailable("rejected"),  # e.g. an ACL that refuses INCR
        CounterUnavailable("unexpected-reply"),
    ],
)
@pytest.mark.django_db
def test_ping_success_with_rejected_writes_never_reports_shared(monkeypatch, caplog, failure):
    clock = _Clock()
    shared = _StandIn(write_failure=failure)
    limiter = _limiter(shared, clock)
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        assert limiter.allow("net", now=NOW).counted_by == "fallback"
        for _ in range(5):  # probes inside the retry interval
            assert limiter.probe() == STATE_DEGRADED
        clock.now += RETRY  # a due probe: one capability write, which fails too
        assert limiter.probe() == STATE_DEGRADED
        status, body = _readyz(monkeypatch, limiter)
    assert (status, body["status"], body["challenge_counter"]) == (200, "degraded", "degraded")
    assert ALERT_RECOVERED not in _alerts(caplog)
    assert shared.pings == 0  # the limiter never relies on PING
    # A due capability write that fails is a recorded failure, not a recovery.
    assert _alerts(caplog).count(ALERT_DEGRADED) >= 2


@pytest.mark.django_db
def test_release_readiness_never_marks_a_write_refusing_counter_ready(monkeypatch, settings):
    settings.HUMAN_CHECK_COUNTER_STORE = "redis"
    limiter = _limiter(_StandIn(write_failure=CounterUnavailable("rejected")), _Clock())
    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", lambda: limiter)
    item = {i.key: i for i in release_readiness.assess_release_readiness().items}[
        "challenge_issuance_counter"
    ]
    assert item.status == release_readiness.BLOCKED


def test_probing_cannot_shorten_the_write_retry_backoff() -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("rejected"))
    limiter = _limiter(shared, clock)
    limiter.allow("net", now=NOW)
    attempts = len(shared.hits)
    for _ in range(50):
        limiter.probe()
        clock.now += 0.2  # 10 s of frequent readiness probes, inside the 15 s backoff
    assert len(shared.hits) == attempts  # no extra write attempt
    assert limiter.allow("net", now=NOW).counted_by == "fallback"
    assert len(shared.hits) == attempts
    clock.now += RETRY
    limiter.allow("net", now=NOW)  # the scheduled retry
    assert len(shared.hits) == attempts + 1


def test_a_due_probe_and_a_request_share_one_retry_slot() -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("rejected"))
    limiter = _limiter(shared, clock)
    limiter.allow("net", now=NOW)
    clock.now += RETRY
    limiter.probe()  # uses the retry slot
    attempts = len(shared.hits)
    limiter.allow("net", now=NOW)  # must not retry again in the same interval
    assert len(shared.hits) == attempts


def test_recovery_alerts_follow_only_a_completed_write(caplog) -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("connection"))
    limiter = _limiter(shared, clock)
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        limiter.allow("net", now=NOW)
        shared.write_failure = None
        limiter.probe()  # not due: no write, no recovery
        assert ALERT_RECOVERED not in _alerts(caplog)
        assert limiter.state() == STATE_DEGRADED
        clock.now += RETRY
        assert limiter.probe() == STATE_SHARED  # the capability write completed
        limiter.probe()
        limiter.allow("net", now=NOW)
    assert _alerts(caplog).count(ALERT_RECOVERED) == 1


# ---------------------------------------------------------------------------
# Startup evidence and the capability write
# ---------------------------------------------------------------------------


def test_an_untested_write_path_is_unverified_not_shared() -> None:
    shared = _StandIn()
    limiter = _limiter(shared, _Clock())
    assert limiter.state() == STATE_UNVERIFIED
    assert shared.hits == []


def test_the_capability_write_uses_a_dedicated_expiring_key_outside_any_budget() -> None:
    clock = _Clock()
    shared = _StandIn()
    limiter = _limiter(shared, clock, normal=1)
    assert limiter.probe() == STATE_SHARED
    key = f"{issuance_counter.KEY_NAMESPACE}:{PROBE_KEY_LABEL}"
    assert shared.hits == [(key, PROBE_TTL_SECONDS)]
    # It can never be an issuance key: no window suffix, and a non-hex label.
    assert key.count(":") == issuance_counter.KEY_NAMESPACE.count(":") + 1
    assert not set(PROBE_KEY_LABEL) <= set("0123456789abcdef")
    # Fresh evidence is reused: no write on every probe.
    for _ in range(10):
        limiter.probe()
    assert len(shared.hits) == 1
    # Many probes later, a network still gets its full budget.
    clock.now += RETRY
    for _ in range(10):
        limiter.probe()
        clock.now += RETRY
    assert limiter.allow("network-digest", now=NOW).allowed  # normal limit 1: the first is allowed
    assert not limiter.allow("network-digest", now=NOW).allowed


def test_a_failing_capability_write_at_startup_is_degraded(caplog) -> None:
    limiter = _limiter(_StandIn(write_failure=CounterUnavailable("rejected")), _Clock())
    with caplog.at_level(logging.WARNING, logger=ALERTS):
        assert limiter.probe() == STATE_DEGRADED
    assert _alerts(caplog) == [ALERT_DEGRADED]


@pytest.mark.django_db
def test_readyz_resolves_unverified_by_one_write(monkeypatch) -> None:
    shared = _StandIn()
    status, body = _readyz(monkeypatch, _limiter(shared, _Clock()))
    assert (status, body["status"], body["challenge_counter"]) == (200, "ok", "shared")
    assert len(shared.hits) == 1


# ---------------------------------------------------------------------------
# R-C3-02: both counters fail, then the shared counter works again
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_shared_recovery_after_both_counters_failed_is_consistent(
    monkeypatch, broken_fallback
) -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("connection"))
    limiter = _limiter(shared, clock, normal=2)
    assert limiter.allow("net", now=NOW) == issuance_counter.Decision(False, "refused")
    assert limiter.state() == STATE_UNAVAILABLE
    assert _readyz(monkeypatch, limiter)[0] == 503

    shared.write_failure = None
    clock.now += RETRY
    decisions = [limiter.allow("net", now=NOW) for _ in range(3)]
    assert [(d.allowed, d.counted_by) for d in decisions] == [
        (True, "shared"),
        (True, "shared"),
        (False, "shared"),  # the limit: a refusal by a working counter
    ]
    assert limiter.state() == STATE_SHARED
    status, body = _readyz(monkeypatch, limiter)
    assert (status, body["status"], body["challenge_counter"]) == (200, "ok", "shared")
    # Redis recovery does not claim that the fallback recovered.
    assert limiter.fallback_healthy() is False


@pytest.mark.django_db
def test_a_later_shared_failure_with_a_still_broken_fallback_fails_closed(
    monkeypatch, broken_fallback
) -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("connection"))
    limiter = _limiter(shared, clock)
    limiter.allow("net", now=NOW)  # both fail
    shared.write_failure = None
    clock.now += RETRY
    assert limiter.allow("net", now=NOW).counted_by == "shared"  # recovered

    shared.write_failure = CounterUnavailable("timeout")
    assert limiter.allow("net", now=NOW) == issuance_counter.Decision(False, "refused")
    assert limiter.state() == STATE_UNAVAILABLE
    status, body = _readyz(monkeypatch, limiter)
    assert (status, body["status"], body["challenge_counter"]) == (
        503,
        "unavailable",
        "unavailable",
    )


@pytest.mark.django_db
def test_a_working_fallback_after_shared_failure_is_degraded_again(
    monkeypatch, broken_fallback
) -> None:
    clock = _Clock()
    shared = _StandIn(write_failure=CounterUnavailable("connection"))
    limiter = _limiter(shared, clock)
    limiter.allow("net", now=NOW)  # both fail
    broken_fallback["broken"] = False
    assert limiter.allow("net", now=NOW).counted_by == "fallback"  # the fallback works again
    assert limiter.fallback_healthy() is True
    assert limiter.state() == STATE_DEGRADED


@pytest.mark.django_db
def test_a_counter_at_its_limit_stays_available(monkeypatch) -> None:
    limiter = _limiter(_StandIn(), _Clock(), normal=1)
    assert limiter.allow("net", now=NOW).allowed
    refused = limiter.allow("net", now=NOW)
    assert (refused.allowed, refused.counted_by) == (False, "shared")
    assert limiter.state() == STATE_SHARED
    status, body = _readyz(monkeypatch, limiter)
    assert (status, body["challenge_counter"]) == (200, "shared")
