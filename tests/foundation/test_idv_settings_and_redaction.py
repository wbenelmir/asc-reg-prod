"""IDV-2 configuration guards (STUB-01, A13-06), the deploy warning and log
redaction of NIN-bearing values (addendum §6). Configuration shape only: no
database, no network, no live service. Synthetic placeholders only.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys

import pytest
from django.test import override_settings

from apps.core.redaction import MASK, redact
from config.settings.validation import (
    DeploymentConfigurationError,
    validate_deployment_configuration,
)
from scripts.check import BASE_DIR, SYNTHETIC_DEPLOY_ENV
from tests.foundation.test_static_validation import _valid_snapshot

OFFICIAL = "apps.people.nin_provider.MinistryNinProvider"
DISABLED = "apps.people.nin_provider.DisabledNinProvider"

_COMPLETE_MINISTRY_API = {
    "base_url": "https://identity.example.invalid",
    "auth_path": "/api/auth/",
    "lookup_path_template": "/api/get/{nin}",
    "username": "__synthetic_user__",
    "password": "__synthetic_password__",
    "token_path": "token",
    "expiry_path": "",
    "expiry_format": "",
    "token_cache_seconds": 300,
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 10,
    "total_timeout_seconds": 20,
    "max_response_bytes": 65536,
    "ca_bundle": "",
}


def _snapshot(backend, *, allow_simulation=False, **ministry):
    return dataclasses.replace(
        _valid_snapshot(),
        nin_provider_backend=backend,
        official_nin_provider_backend=OFFICIAL,
        disabled_nin_provider_backend=DISABLED,
        identity_allow_simulated_provider=allow_simulation,
        ministry_api={**_COMPLETE_MINISTRY_API, **ministry},
    )


@pytest.mark.parametrize("backend", [OFFICIAL, DISABLED])
def test_the_official_and_the_disabled_backends_are_accepted(backend) -> None:
    validate_deployment_configuration(_snapshot(backend))


@pytest.mark.parametrize(
    "backend",
    [
        "apps.people.nin_provider.LocalSimulationNinProvider",
        "apps.people.nin_provider.LocalStubNinProvider",
        "apps.people.nin_provider.UnavailableNinProvider",
        "apps.people.nin_provider.UnavailableSimulationNinProvider",
        "tests.anything.FakeProvider",
        "",
    ],
)
def test_every_stub_and_simulation_is_refused_in_staging_and_production(backend) -> None:
    with pytest.raises(DeploymentConfigurationError) as caught:
        validate_deployment_configuration(_snapshot(backend))
    assert "NIN_PROVIDER_BACKEND" in str(caught.value)


def test_the_simulation_switch_is_refused_in_staging_and_production() -> None:
    with pytest.raises(DeploymentConfigurationError) as caught:
        validate_deployment_configuration(_snapshot(DISABLED, allow_simulation=True))
    assert "IDENTITY_ALLOW_SIMULATED_PROVIDER" in str(caught.value)


def test_missing_adapter_values_do_not_block_startup() -> None:
    """The authentication contract is open (API-01): the adapter stays
    unavailable until configured, which readiness reports; startup works."""
    validate_deployment_configuration(
        _snapshot(OFFICIAL, username="", password="", token_path="", base_url="")
    )


@pytest.mark.parametrize(
    ("ministry", "fragment"),
    [
        ({"base_url": "http://identity.example.invalid"}, "MINISTRY_NIN_API_BASE_URL"),
        ({"token_path": "not a path"}, "MINISTRY_NIN_API_AUTH_TOKEN_PATH"),
        ({"read_timeout_seconds": 0}, "MINISTRY_NIN_API_READ_TIMEOUT_SECONDS"),
        ({"expiry_path": "exp"}, "MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT"),
    ],
)
def test_malformed_adapter_values_fail_startup_without_echoing_them(ministry, fragment) -> None:
    with pytest.raises(DeploymentConfigurationError) as caught:
        validate_deployment_configuration(_snapshot(OFFICIAL, **ministry))
    message = str(caught.value)
    assert fragment in message
    assert "__synthetic_password__" not in message
    assert "identity.example.invalid" not in message


def _deploy_check(settings_module: str, **changes) -> subprocess.CompletedProcess:
    env = {
        name: value
        for name, value in os.environ.items()
        # Independent of whatever a developer's local environment exports.
        if not name.startswith(("NIN_PROVIDER_", "MINISTRY_NIN_API_", "IDENTITY_"))
        or name.startswith("IDENTITY_ENCRYPTION_")
        or name.startswith("IDENTITY_BLIND_INDEX_")
    }
    env.update({**SYNTHETIC_DEPLOY_ENV, "DJANGO_SETTINGS_MODULE": settings_module})
    env.update(changes)
    return subprocess.run(  # noqa: S603 - the project interpreter and manage.py only
        [sys.executable, "manage.py", "check", "--deploy", f"--settings={settings_module}"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "settings_module", ["config.settings.staging", "config.settings.production"]
)
def test_real_settings_refuse_the_simulation_at_startup(settings_module) -> None:
    result = _deploy_check(
        settings_module, NIN_PROVIDER_BACKEND="apps.people.nin_provider.LocalSimulationNinProvider"
    )
    assert result.returncode != 0
    assert "NIN_PROVIDER_BACKEND" in result.stderr + result.stdout


@pytest.mark.parametrize(
    "settings_module", ["config.settings.staging", "config.settings.production"]
)
def test_real_settings_default_to_the_disabled_backend_with_a_deploy_warning(
    settings_module,
) -> None:
    result = _deploy_check(settings_module)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "people.W001" in result.stdout + result.stderr


def test_the_deploy_check_is_silent_for_a_complete_official_adapter() -> None:
    from apps.people.checks import nin_provider_readiness

    with override_settings(
        NIN_PROVIDER_BACKEND=OFFICIAL,
        MINISTRY_NIN_API_BASE_URL="https://identity.example.invalid",
        MINISTRY_NIN_API_USERNAME="__synthetic_user__",
        MINISTRY_NIN_API_PASSWORD="__synthetic_password__",
        MINISTRY_NIN_API_AUTH_TOKEN_PATH="token",
    ):
        assert nin_provider_readiness() == []
    with override_settings(NIN_PROVIDER_BACKEND=OFFICIAL, MINISTRY_NIN_API_PASSWORD=""):
        warnings = nin_provider_readiness()
    assert [warning.id for warning in warnings] == ["people.W001"]
    assert "__synthetic" not in warnings[0].msg


# ---------------------------------------------------------------------------
# Redaction (addendum §6, AS-16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "GET https://identity.example.invalid/api/get/990000000000000010 failed",
        "proxy: GET /api/get/990000000000000010?x=1 HTTP/1.1 502",
        "ConnectionError('/api/get/009900000000000044')",
        "lookup for 990000000000000010 timed out",
    ],
)
def test_nin_bearing_paths_and_values_are_masked(text) -> None:
    redacted = redact(text)
    assert "990000000000000010" not in redacted
    assert "009900000000000044" not in redacted
    assert MASK in redacted


def test_other_numbers_are_not_masked() -> None:
    assert redact("17 digits 12345678901234567 and 19 digits 1234567890123456789") == (
        "17 digits 12345678901234567 and 19 digits 1234567890123456789"
    )


def test_a_bearer_header_is_masked() -> None:
    # Assembled at run time so the source holds no bearer-header shape.
    token = "_".join(["__synthetic", "token", "one__"])
    assert token not in redact("Authorization: Bearer " + token)


def test_the_ministry_credentials_are_masked_by_value(monkeypatch) -> None:
    monkeypatch.setenv("MINISTRY_NIN_API_PASSWORD", "__synthetic_ministry_password_value__")
    monkeypatch.setenv("MINISTRY_NIN_API_USERNAME", "__synthetic_ministry_user_value__")
    redacted = redact(
        "auth failed for __synthetic_ministry_user_value__ / __synthetic_ministry_password_value__"
    )
    assert "__synthetic_ministry_password_value__" not in redacted
    assert "__synthetic_ministry_user_value__" not in redacted


def test_a_formatted_exception_with_a_lookup_path_is_masked() -> None:
    from apps.core.logging_config import StructuredFormatter

    try:
        raise OSError("connect failed: /api/get/990000000000000010")
    except OSError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "lookup failed", None, sys.exc_info()
        )
    rendered = StructuredFormatter().format(record)
    assert "990000000000000010" not in rendered
