"""Child-process worker for `test_p4_4_c3_redis_integration.py` (P4-4-C3).

Imported by `multiprocessing` in a fresh interpreter, so it uses no Django
settings and no test fixture: each call builds its own limiter and Redis
client, exactly as a separate worker process would.
"""

from __future__ import annotations


def hammer(url: str, namespace: str, bucket: str, now: float, hits: int, normal: int) -> int:
    """Send `hits` issuance requests through a new process-local limiter that
    shares the Redis counter; return how many were allowed."""
    from apps.core.issuance_counter import IssuanceLimiter, RedisWindowCounter

    limiter = IssuanceLimiter(
        shared=RedisWindowCounter(url, connect_timeout=2.0, socket_timeout=2.0),
        normal_limit=normal,
        fallback_limit=1,
        window_seconds=600,
        max_networks=100,
        retry_seconds=60,
        alert_interval_seconds=300,
        namespace=namespace,
    )
    allowed = 0
    for _ in range(hits):
        decision = limiter.allow(bucket, now=now)
        if decision.counted_by != "shared":
            raise RuntimeError("the shared counter was not used")
        allowed += decision.allowed
    return allowed
