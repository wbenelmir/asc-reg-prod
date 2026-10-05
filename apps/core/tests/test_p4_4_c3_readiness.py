"""P4-4-C3 (CACHE-01): `/readyz` with the challenge-issuance counter.

The four required states: healthy shared counter, shared-counter outage with
a working fallback, unavailable protection, and the failure of another
required dependency while the counter is degraded. The shared counter is an
in-process stand-in, not Redis (see `test_p4_4_c3_issuance_counter.py`).
Synthetic data only.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client
from redis import exceptions as redis_exceptions

from apps.core import issuance_counter
from apps.core.issuance_counter import IssuanceLimiter, LocalWindowCounter

pytestmark = pytest.mark.django_db

LEAK = "rediss://redis-p44c3-ready.internal.invalid:6380/2 p44c3-leak-marker"


class _Shared:
    """In-process stand-in for the shared store (NOT Redis)."""

    def __init__(self, failure=None):
        self.failure = failure

    def hit(self, key, ttl_seconds):
        if self.failure:
            raise self.failure
        return 1


@pytest.fixture(autouse=True)
def _isolated_private_storage(settings, tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = root


def _use(monkeypatch, shared, clock=None) -> IssuanceLimiter:
    extra = {"clock": clock} if clock is not None else {}
    limiter = IssuanceLimiter(
        shared=shared,
        normal_limit=60,
        fallback_limit=20,
        window_seconds=600,
        max_networks=100,
        retry_seconds=15,
        alert_interval_seconds=300,
        **extra,
    )
    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", lambda: limiter)
    return limiter


def _get():
    response = Client().get("/readyz")
    return response, json.loads(response.content)


def _assert_sanitized(response) -> None:
    assert "no-store" in response["Cache-Control"]
    text = response.content.decode()
    for fragment in ("p44c3", "redis-p44c3", "6380", "rediss", "Error"):
        assert fragment not in text


def test_a_healthy_shared_counter_is_ready(monkeypatch) -> None:
    _use(monkeypatch, _Shared())
    response, body = _get()
    assert response.status_code == 200
    assert body == {
        "status": "ok",
        "database": "reachable",
        "storage": "reachable",
        "challenge_counter": "shared",
    }
    _assert_sanitized(response)


def test_a_shared_counter_outage_with_a_working_fallback_is_degraded_not_down(monkeypatch) -> None:
    _use(monkeypatch, _Shared(redis_exceptions.ConnectionError(LEAK)))
    response, body = _get()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["challenge_counter"] == "degraded"
    _assert_sanitized(response)


def test_unavailable_protection_is_not_ready(monkeypatch) -> None:
    limiter = _use(monkeypatch, _Shared(redis_exceptions.ConnectionError(LEAK)))

    def broken(self, bucket, window_index):
        raise MemoryError

    monkeypatch.setattr(LocalWindowCounter, "hit", broken)
    assert not limiter.allow("net", now=1_900_000_000.0).allowed
    response, body = _get()
    assert response.status_code == 503
    assert body["status"] == "unavailable"
    assert body["challenge_counter"] == "unavailable"
    _assert_sanitized(response)


@pytest.mark.parametrize(
    ("target", "component"),
    [
        ("apps.core.views._database_ready", "database"),
        ("apps.core.views._storage_ready", "storage"),
    ],
)
def test_a_degraded_counter_never_hides_another_failed_dependency(
    monkeypatch, target, component
) -> None:
    _use(monkeypatch, _Shared(redis_exceptions.TimeoutError(LEAK)))
    with patch(target, side_effect=RuntimeError(LEAK)):
        response, body = _get()
    assert response.status_code == 503
    assert body["status"] == "unavailable"
    assert body[component] == "unreachable"
    assert body["challenge_counter"] == "degraded"
    _assert_sanitized(response)


def test_a_healthy_counter_never_hides_another_failed_dependency(monkeypatch) -> None:
    _use(monkeypatch, _Shared())
    with patch("apps.core.views._database_ready", side_effect=RuntimeError(LEAK)):
        response, body = _get()
    assert response.status_code == 503
    assert body["status"] == "unavailable"
    _assert_sanitized(response)


def test_the_local_counter_is_never_reported_as_shared() -> None:
    response, body = _get()
    assert response.status_code == 200
    assert body["challenge_counter"] == "local"
    assert "shared" not in response.content.decode()


def test_the_probe_restores_the_shared_counter_after_recovery(monkeypatch) -> None:
    # P4-4-C4 (R-C3-01): recovery is shown after the retry interval, by a
    # completed capability write, never earlier.
    now = [1000.0]
    shared = _Shared(redis_exceptions.ConnectionError(LEAK))
    _use(monkeypatch, shared, clock=lambda: now[0])
    assert _get()[1]["challenge_counter"] == "degraded"
    shared.failure = None
    assert _get()[1]["challenge_counter"] == "degraded"  # still inside the backoff
    now[0] += 15
    response, body = _get()
    assert (response.status_code, body["status"], body["challenge_counter"]) == (
        200,
        "ok",
        "shared",
    )


def test_a_probe_that_raises_is_unavailable_without_detail(monkeypatch) -> None:
    def broken():
        raise RuntimeError(LEAK)

    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", broken)
    response, body = _get()
    assert response.status_code == 503
    assert body["challenge_counter"] == "unavailable"
    _assert_sanitized(response)
