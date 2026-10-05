"""P4-4-C3: the real staging and production settings under the revised MFA-01
requirement and with the CACHE-01 counter.

Each check imports `config.settings.staging` or `config.settings.production`
in a genuine subprocess with the documented synthetic environment of
`scripts/check.py` (never `.env`, never a live service), so the real settings
import and static validation run exactly as they would at startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from scripts.check import BASE_DIR, SYNTHETIC_DEPLOY_ENV, run_static_deploy_check

DEPLOY_MODULES = ("config.settings.staging", "config.settings.production")

_PRINT_SETTINGS = """
import json, django
django.setup()
from django.conf import settings
from apps.accounts import mfa
names = [
    "AUTHENTICATION_BACKENDS", "OPERATIONAL_SIGN_IN_WINDOW_SECONDS",
    "OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL", "OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK",
    "OPERATIONAL_SESSION_INACTIVITY_SECONDS", "OPERATIONAL_SESSION_ABSOLUTE_SECONDS",
    "PARTICIPANT_SESSION_INACTIVITY_SECONDS", "PARTICIPANT_SESSION_ABSOLUTE_SECONDS",
    "OTP_LIFETIME_SECONDS", "HUMAN_CHECK_ENABLED", "HUMAN_CHECK_COUNTER_STORE",
    "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW", "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW",
    "MFA_BACKEND",
]
values = {name: getattr(settings, name, None) for name in names}
values["SIGN_IN_MFA_ENFORCED"] = mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED
print(json.dumps(values, default=list))
"""


def _env(settings_module: str, **changes) -> dict[str, str]:
    env = {**os.environ, **SYNTHETIC_DEPLOY_ENV, "DJANGO_SETTINGS_MODULE": settings_module}
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _settings_in(settings_module: str, **changes) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - the project interpreter and a fixed script only
        [sys.executable, "-c", _PRINT_SETTINGS],
        cwd=BASE_DIR,
        env=_env(settings_module, **changes),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("settings_module", DEPLOY_MODULES)
def test_staging_and_production_start_without_a_sign_in_mfa_provider(settings_module) -> None:
    result = _settings_in(settings_module, MFA_BACKEND=None)
    assert result.returncode == 0, result.stderr[-2000:]
    values = json.loads(result.stdout.strip().splitlines()[-1])
    assert values["MFA_BACKEND"] is None
    assert values["SIGN_IN_MFA_ENFORCED"] is False
    # The same password sign-in path and its controls as everywhere else.
    assert values["AUTHENTICATION_BACKENDS"] == ["django.contrib.auth.backends.ModelBackend"]
    assert values["OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL"] == 10
    assert values["OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK"] == 50
    assert values["OPERATIONAL_SIGN_IN_WINDOW_SECONDS"] == 900
    assert values["OPERATIONAL_SESSION_INACTIVITY_SECONDS"] == 15 * 60
    assert values["OPERATIONAL_SESSION_ABSOLUTE_SECONDS"] == 8 * 60 * 60
    # Participants keep the email OTP and its human check.
    assert values["HUMAN_CHECK_ENABLED"] is True
    assert values["PARTICIPANT_SESSION_INACTIVITY_SECONDS"] == 30 * 60
    # CACHE-01: the shared counter, never the per-process one.
    assert values["HUMAN_CHECK_COUNTER_STORE"] == "redis"
    assert (
        values["HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW"]
        < values["HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW"]
    )


@pytest.mark.parametrize("settings_module", DEPLOY_MODULES)
def test_the_deploy_check_passes_without_mfa_and_names_the_unavailable_wipe(
    settings_module,
) -> None:
    env = _env(settings_module, MFA_BACKEND=None)
    result = subprocess.run(  # noqa: S603 - the project interpreter and manage.py only
        [sys.executable, "manage.py", "check", "--deploy", f"--settings={settings_module}"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    output = result.stdout + result.stderr
    assert "entry.W001" in output
    assert "emergency device wipe is unavailable" in output


@pytest.mark.parametrize("settings_module", DEPLOY_MODULES)
def test_with_a_step_up_provider_the_wipe_warning_is_absent(settings_module) -> None:
    result = run_static_deploy_check(settings_module)  # the synthetic env sets MFA_BACKEND
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "entry.W001" not in result.stdout + result.stderr


@pytest.mark.parametrize("settings_module", DEPLOY_MODULES)
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"REDIS_URL": "redis://synthetic.deploy-check.example.invalid:6379/0"}, "TLS"),
        (
            {"REDIS_URL": SYNTHETIC_DEPLOY_ENV["CELERY_BROKER_URL"]},
            "different Redis databases",
        ),
        (
            {
                "REDIS_URL": "rediss://synthetic.deploy-check.example.invalid:6379/0"
                "?ssl_cert_reqs=none"
            },
            "verification",
        ),
        ({"HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW": "60"}, "FALLBACK_MAX_PER_WINDOW"),
        ({"HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS": "60000"}, "SOCKET_TIMEOUT_MS"),
    ],
)
def test_startup_refuses_an_unsafe_counter_configuration(settings_module, changes, expected):
    result = _settings_in(settings_module, **changes)
    assert result.returncode != 0
    assert expected in result.stderr
    assert "synthetic.deploy-check.example.invalid" not in result.stderr
