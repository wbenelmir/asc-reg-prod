"""P4-4-C3 (CACHE-01): OPT-IN integration tests against a real Redis server.

Skipped unless `ASC_TEST_REDIS_URL` names an isolated, disposable Redis
database that the developer is authorized to use (never a shared, staging
broker or production service). While skipped, real shared-counter
concurrency, outage and recovery are NOT_PROVEN.

Every key is written under a random namespace for this run, and only keys
under that namespace are deleted afterwards. Nothing else is read, changed or
flushed. Example (an isolated local server, database 15):

    ASC_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \\
        uv run --env-file .env pytest apps/core/tests/test_p4_4_c3_redis_integration.py

The outage and recovery test runs a local TCP forwarder in front of the
server, so it needs a plain `redis://` URL (a TLS host name would not match
the forwarder's address); with `rediss://` it is skipped and the manual
staging procedure covers it.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import pytest

from apps.core.issuance_counter import (
    KEY_GRACE_SECONDS,
    PROBE_TTL_SECONDS,
    STATE_DEGRADED,
    STATE_SHARED,
    STATE_UNVERIFIED,
    IssuanceLimiter,
    RedisWindowCounter,
)

REDIS_URL = os.environ.get("ASC_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="NOT_PROVEN: set ASC_TEST_REDIS_URL to an isolated, disposable Redis database",
)

NOW = 1_900_000_000.0


@pytest.fixture
def namespace():
    import redis

    name = f"asc:test:p44c3:{uuid.uuid4().hex}:hc-issue"
    yield name
    client = redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    for key in client.scan_iter(match=f"{name}:*", count=500):
        client.delete(key)


def _limiter(namespace, *, url=REDIS_URL, normal=10, fallback=2, window=600, retry=60):
    return IssuanceLimiter(
        shared=RedisWindowCounter(url, connect_timeout=0.5, socket_timeout=0.5),
        normal_limit=normal,
        fallback_limit=fallback,
        window_seconds=window,
        max_networks=100,
        retry_seconds=retry,
        alert_interval_seconds=300,
        namespace=namespace,
    )


def _raw():
    import redis

    return redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


def test_concurrent_first_requests_initialize_one_key_with_an_expiry(namespace) -> None:
    limiter = _limiter(namespace, normal=10)
    barrier = threading.Barrier(32)

    def request(_index):
        barrier.wait()
        return limiter.allow("net", now=NOW)

    with ThreadPoolExecutor(max_workers=32) as pool:
        decisions = list(pool.map(request, range(32)))
    assert {d.counted_by for d in decisions} == {"shared"}
    assert sum(d.allowed for d in decisions) == 10
    key = f"{namespace}:net:{int(NOW // 600)}"
    assert int(_raw().get(key)) == 32
    assert 0 < _raw().ttl(key) <= 600 + KEY_GRACE_SECONDS


def test_separate_processes_share_one_exact_count(namespace) -> None:
    from apps.core.tests.p4_4_c3_redis_worker import hammer

    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        allowed = pool.starmap(hammer, [(REDIS_URL, namespace, "net", NOW, 25, 30)] * 4)
    assert sum(allowed) == 30
    assert int(_raw().get(f"{namespace}:net:{int(NOW // 600)}")) == 100


def test_window_boundaries_and_expiry_on_the_server(namespace) -> None:
    limiter = _limiter(namespace, normal=2, window=2)
    start = (NOW // 2) * 2
    assert [limiter.allow("net", now=start + 1.999).allowed for _ in range(3)] == [
        True,
        True,
        False,
    ]
    assert limiter.allow("net", now=start + 2).allowed  # the next window
    key = f"{namespace}:net:{int(start // 2)}"
    assert 0 < _raw().ttl(key) <= 2 + KEY_GRACE_SECONDS
    deadline = time.monotonic() + 2 + KEY_GRACE_SECONDS + 3
    while _raw().exists(key) and time.monotonic() < deadline:
        time.sleep(0.25)
    assert not _raw().exists(key)


def test_the_capability_write_proves_counting_on_the_server(namespace) -> None:
    """P4-4-C4 (R-C3-01): readiness evidence is a real INCR + EXPIRE on the
    dedicated probe key, which expires and never creates an issuance key."""
    limiter = _limiter(namespace)
    assert limiter.state() == STATE_UNVERIFIED
    assert limiter.probe() == STATE_SHARED
    probe_key = limiter.probe_key()
    assert int(_raw().get(probe_key)) == 1
    assert 0 < _raw().ttl(probe_key) <= PROBE_TTL_SECONDS
    keys = sorted(k.decode() for k in _raw().scan_iter(match=f"{namespace}:*", count=500))
    assert keys == [probe_key]


class _Forwarder:
    """A local TCP forwarder that can cut and restore the path to Redis."""

    def __init__(self, host: str, port: int):
        self.target = (host, port)
        self.open = True
        self._sockets: list[socket.socket] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self.open:
                client.close()
                continue
            upstream = socket.create_connection(self.target, timeout=2)
            self._sockets += [client, upstream]
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pump(source, sink):
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                try:
                    end.close()
                except OSError:
                    pass

    def cut(self):
        self.open = False
        for end in self._sockets:
            try:
                end.shutdown(socket.SHUT_RDWR)
                end.close()
            except OSError:
                pass
        self._sockets.clear()

    def restore(self):
        self.open = True

    def close(self):
        self.cut()
        self._listener.close()


def test_an_outage_falls_back_and_the_shared_counter_is_used_again_after_recovery(
    namespace,
) -> None:
    parts = urlsplit(REDIS_URL)
    if parts.scheme != "redis":
        pytest.skip("NOT_PROVEN here: the forwarder needs a plain redis:// test URL")
    forwarder = _Forwarder(parts.hostname, parts.port or 6379)
    try:
        netloc = f"127.0.0.1:{forwarder.port}"
        if parts.username or parts.password:
            netloc = f"{parts.netloc.rsplit('@', 1)[0]}@{netloc}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
        limiter = _limiter(namespace, url=url, normal=10, fallback=2, retry=1)
        assert limiter.allow("net", now=NOW).counted_by == "shared"
        forwarder.cut()
        started = time.monotonic()
        degraded = [limiter.allow("net", now=NOW) for _ in range(5)]
        assert time.monotonic() - started < 5  # bounded by the timeouts, no retries
        assert [d.counted_by for d in degraded] == ["fallback"] * 5
        assert [d.allowed for d in degraded] == [True, True, False, False, False]
        assert limiter.state() == STATE_DEGRADED
        forwarder.restore()
        time.sleep(1.2)  # the retry interval
        assert limiter.allow("net", now=NOW).counted_by == "shared"
        assert limiter.state() == STATE_SHARED
        # Requests admitted by the fallback are not in the shared count.
        assert int(_raw().get(f"{namespace}:net:{int(NOW // 600)}")) == 2
    finally:
        forwarder.close()
