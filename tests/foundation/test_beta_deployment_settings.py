"""Deployed settings: database identities, the migrate guard, broker TLS and SMTP.

Every settings import runs in a real subprocess with the documented synthetic,
non-secret environment of `scripts/check.py` (never `.env`, never a live
service), so the real module and its startup validation run as at startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from scripts.check import (
    BASE_DIR,
    SYNTHETIC_DEPLOY_ENV,
    SYNTHETIC_MIGRATION_ENV_EXTRA,
    synthetic_deploy_env,
)

#: A synthetic SMTP credential for the mapping test (never a real value).
SYNTHETIC_SMTP_VALUE = "synthetic-smtp-value"

_PRINT = """
import json, os, ssl, django
django.setup()
from django.conf import settings
from django.core.mail import get_connection
db = settings.DATABASES["default"]
connection = get_connection()
out = {
    "user": db["USER"],
    "password_is_runtime": db["PASSWORD"] == os.environ.get("DATABASE_PASSWORD"),
    "password_is_migration": db["PASSWORD"] == os.environ.get("DATABASE_MIGRATION_PASSWORD"),
    "role": settings.DATABASE_PROCESS_ROLE,
    "has_migration_password_setting": hasattr(settings, "DATABASE_MIGRATION_PASSWORD"),
    "broker_use_ssl": getattr(settings, "CELERY_BROKER_USE_SSL", None) is not None,
    "broker_cert_reqs_required": (
        getattr(settings, "CELERY_BROKER_USE_SSL", {}) or {}
    ).get("ssl_cert_reqs") == ssl.CERT_REQUIRED,
    "email_backend": settings.EMAIL_BACKEND,
    "smtp": [connection.host, connection.port, connection.use_tls, connection.use_ssl,
             connection.timeout, bool(connection.username)],
    "from": settings.DEFAULT_FROM_EMAIL,
    "scanner": settings.MALWARE_SCANNER_BACKEND,
    "allow_stubs": settings.MALWARE_SCANNER_ALLOW_LOCAL_STUBS,
}
from config.celery import app
out["celery_ssl_required"] = (
    (app.connection_for_write().ssl or {}).get("ssl_cert_reqs") == ssl.CERT_REQUIRED
)
print(json.dumps(out))
"""


def _run(settings_module: str, **changes) -> subprocess.CompletedProcess:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in SYNTHETIC_DEPLOY_ENV and key not in SYNTHETIC_MIGRATION_ENV_EXTRA
    }
    env = {**inherited, **synthetic_deploy_env(settings_module)}
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return subprocess.run(  # noqa: S603 - the project interpreter and a fixed script only
        [sys.executable, "-c", _PRINT],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _values(settings_module: str, **changes) -> dict:
    run = _run(settings_module, **changes)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", ["config.settings.staging", "config.settings.production"])
def test_runtime_processes_connect_with_the_runtime_identity_only(module) -> None:
    values = _values(module)
    assert values["user"] == SYNTHETIC_DEPLOY_ENV["DATABASE_USER"]
    assert values["password_is_runtime"] is True
    assert values["role"] == "runtime"
    assert values["has_migration_password_setting"] is False


@pytest.mark.parametrize("module", ["config.settings.staging", "config.settings.production"])
def test_a_runtime_process_refuses_to_start_with_the_migration_password(module) -> None:
    marker = "synthetic-migration-value-that-must-not-leak"
    run = _run(module, DATABASE_MIGRATION_PASSWORD=marker)
    assert run.returncode != 0
    assert "DATABASE_MIGRATION_PASSWORD must not be present in a runtime process" in run.stderr
    assert marker not in run.stderr


@pytest.mark.parametrize(
    "module", ["config.settings.staging_migration", "config.settings.production_migration"]
)
def test_the_migration_process_connects_as_the_owner_without_the_runtime_password(module) -> None:
    values = _values(module, DATABASE_PASSWORD=None)
    assert values["user"] == SYNTHETIC_DEPLOY_ENV["DATABASE_MIGRATION_USER"]
    assert values["password_is_migration"] is True
    assert values["role"] == "migration"


@pytest.mark.parametrize(
    "module", ["config.settings.staging_migration", "config.settings.production_migration"]
)
def test_the_migration_process_needs_the_migration_password(module) -> None:
    run = _run(module, DATABASE_MIGRATION_PASSWORD=None)
    assert run.returncode != 0
    assert "DATABASE_MIGRATION_PASSWORD" in run.stderr


def test_a_runtime_process_without_its_password_does_not_fall_back() -> None:
    run = _run("config.settings.staging", DATABASE_PASSWORD=None)
    assert run.returncode != 0
    assert "DATABASE_PASSWORD must be configured for the runtime role" in run.stderr


def test_the_migrate_command_refuses_a_runtime_settings_module(db) -> None:
    with override_settings(DATABASE_PROCESS_ROLE="runtime"):
        with pytest.raises(CommandError) as excinfo:
            call_command("migrate", verbosity=0)
        assert "staging_migration" in str(excinfo.value)
        # The read-only forms stay available.
        call_command("migrate", plan=True, verbosity=0)
        call_command("migrate", check_unapplied=True, verbosity=0)


def test_a_rediss_broker_verifies_certificates() -> None:
    values = _values("config.settings.staging")
    assert values["broker_use_ssl"] is True
    assert values["broker_cert_reqs_required"] is True
    assert values["celery_ssl_required"] is True


def test_a_broker_url_with_its_own_tls_options_keeps_them() -> None:
    values = _values(
        "config.settings.staging",
        CELERY_BROKER_URL="rediss://broker.example.invalid:6379/1?ssl_ca_certs=/etc/asc/ca.pem",
    )
    assert values["broker_use_ssl"] is False


def test_a_broker_url_that_weakens_tls_is_refused() -> None:
    run = _run(
        "config.settings.staging",
        CELERY_BROKER_URL="rediss://broker.example.invalid:6379/1?ssl_cert_reqs=none",
    )
    assert run.returncode != 0
    assert "CELERY_BROKER_URL must not disable TLS" in run.stderr


def test_smtp_is_configured_from_the_environment() -> None:
    values = _values(
        "config.settings.staging",
        EMAIL_HOST="smtp.internal.example.invalid",
        EMAIL_PORT="2587",
        EMAIL_USE_TLS="True",
        EMAIL_HOST_USER="asc-mailer",
        EMAIL_HOST_PASSWORD=SYNTHETIC_SMTP_VALUE,
        EMAIL_TIMEOUT_SECONDS="15",
        DEFAULT_FROM_EMAIL="ASC 2026 <no-reply@example.invalid>",
    )
    assert values["email_backend"] == "django.core.mail.backends.smtp.EmailBackend"
    assert values["smtp"] == ["smtp.internal.example.invalid", 2587, True, False, 15, True]
    assert values["from"] == "ASC 2026 <no-reply@example.invalid>"


def test_implicit_tls_smtp_is_supported() -> None:
    values = _values(
        "config.settings.staging", EMAIL_USE_TLS="False", EMAIL_USE_SSL="True", EMAIL_PORT="465"
    )
    assert values["smtp"][1:4] == [465, False, True]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"EMAIL_HOST": None}, "EMAIL_HOST must name the SMTP server"),
        ({"DEFAULT_FROM_EMAIL": None}, "DEFAULT_FROM_EMAIL must be the sender address"),
        (
            {"EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
            "EMAIL_BACKEND must be Django's SMTP backend",
        ),
        ({"EMAIL_USE_TLS": "False"}, "SMTP to a non-loopback EMAIL_HOST must use"),
    ],
)
def test_incomplete_or_unsafe_smtp_is_refused(changes, expected) -> None:
    run = _run("config.settings.staging", **changes)
    assert run.returncode != 0
    assert expected in run.stderr


def test_the_clamd_adapter_is_the_deployed_scanner_and_stubs_are_not_allowed() -> None:
    values = _values("config.settings.staging", MALWARE_SCANNER_BACKEND=None)
    assert values["scanner"] == "apps.documents.scanning.ClamdScanner"
    assert values["allow_stubs"] is False
    run = _run(
        "config.settings.staging",
        MALWARE_SCANNER_BACKEND="apps.documents.scanning.RejectingStubScanner",
    )
    assert run.returncode != 0
    assert "MALWARE_SCANNER_BACKEND must be the ClamAV clamd adapter" in run.stderr
