"""P4-4-C3: static deployment validation under the revised MFA-01 requirement
and for the CACHE-01 counter. Configuration shape only; no live service.
"""

from __future__ import annotations

import dataclasses

import pytest

from config.settings.validation import (
    DeploymentConfigurationError,
    validate_deployment_configuration,
)
from tests.foundation.test_static_validation import _valid_snapshot


def _counter_snapshot(**overrides):
    base = dataclasses.replace(
        _valid_snapshot(),
        human_check_counter_store="redis",
        human_check_challenge_max_per_window=60,
        human_check_challenge_fallback_max_per_window=20,
        human_check_counter_fallback_max_networks=10_000,
        human_check_counter_connect_timeout_ms=500,
        human_check_counter_socket_timeout_ms=500,
        human_check_counter_retry_seconds=15,
        human_check_counter_alert_interval_seconds=300,
    )
    return dataclasses.replace(base, **overrides)


def test_a_missing_mfa_backend_is_accepted_under_the_revised_requirement() -> None:
    validate_deployment_configuration(_counter_snapshot(mfa_backend=None))


def test_the_default_counter_configuration_is_valid() -> None:
    validate_deployment_configuration(_counter_snapshot())


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"human_check_counter_store": "local"}, "HUMAN_CHECK_COUNTER_STORE"),
        ({"redis_url": "redis://redis.example.invalid:6379/0"}, "TLS (rediss://)"),
        ({"redis_url": "rediss://redis.example.invalid:6379/1"}, "different Redis databases"),
        ({"redis_url": "rediss://REDIS.example.invalid/1"}, "different Redis databases"),
        ({"redis_url": "rediss://redis.example.invalid:6379?db=1"}, "different Redis databases"),
        (
            {"redis_url": "rediss://redis.example.invalid:6379/0?ssl_cert_reqs=none"},
            "verification",
        ),
        (
            {"redis_url": "rediss://redis.example.invalid:6379/0?ssl_check_hostname=false"},
            "verification",
        ),
        ({"redis_url": "rediss://redis.example.invalid:6379/zero"}, "numeric database"),
        ({"redis_url": "unix:///tmp/redis.sock"}, "numeric database"),
        ({"human_check_challenge_fallback_max_per_window": 60}, "lower than"),
        ({"human_check_challenge_fallback_max_per_window": 0}, "at least 1"),
        ({"human_check_challenge_max_per_window": 1}, "at least 2"),
        ({"human_check_counter_fallback_max_networks": 0}, "FALLBACK_MAX_NETWORKS"),
        ({"human_check_counter_connect_timeout_ms": 0}, "CONNECT_TIMEOUT_MS"),
        ({"human_check_counter_socket_timeout_ms": 10_000}, "SOCKET_TIMEOUT_MS"),
        ({"human_check_counter_retry_seconds": 0}, "RETRY_SECONDS"),
        ({"human_check_counter_alert_interval_seconds": 0}, "ALERT_INTERVAL_SECONDS"),
    ],
)
def test_an_unsafe_counter_configuration_is_refused(overrides, expected) -> None:
    with pytest.raises(DeploymentConfigurationError) as caught:
        validate_deployment_configuration(_counter_snapshot(**overrides))
    message = str(caught.value)
    assert expected in message
    assert "redis.example.invalid" not in message.lower()


def test_a_different_host_or_database_from_the_broker_is_accepted() -> None:
    for url in (
        "rediss://redis.example.invalid:6379/2",
        "rediss://counter.example.invalid:6379/1",
        "rediss://redis.example.invalid:6380/1",
        "rediss://redis.example.invalid:6379/2?ssl_cert_reqs=required",
    ):
        validate_deployment_configuration(_counter_snapshot(redis_url=url))
