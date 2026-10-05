"""Prompt 3 CP 3a read-only pre-flight gate and CP 3b audit-isolation check.

Uses `psycopg` directly -- NOT Django's database/test-database machinery --
so running this proves the command itself never creates a table (not even
`django_migrations`), independent of whatever migration state this
developer's real `asc2026_dev` happens to be in when the suite runs.
"""

from __future__ import annotations

import os

import pytest

from scripts.check import (
    AUDIT_EVENT_TABLE,
    run_audit_isolation_check,
    run_db_state,
)

REQUIRED_VARS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)

_skip_without_db = pytest.mark.skipif(
    any(not os.environ.get(v) for v in REQUIRED_VARS),
    reason="DATABASE_* environment variables not set (run via `uv run --env-file .env pytest ...`)",
)


@_skip_without_db
def test_db_state_reports_sanitized_fields_only() -> None:
    exit_code, report = run_db_state()
    assert exit_code in (0, 1)  # 1 is expected once Prompt 3 migrations are applied
    assert "PostgreSQL 17" in report["server_version"]
    assert report["database"] == os.environ["DATABASE_NAME"]
    assert report["role"] == os.environ["DATABASE_USER"]
    assert report["encoding"] == "UTF8"
    assert "django_migrations_table_exists" in report
    assert "applied_migrations" in report
    assert "existing_tables" in report


@_skip_without_db
def test_db_state_report_never_contains_the_password() -> None:
    _exit_code, report = run_db_state()
    password = os.environ["DATABASE_PASSWORD"]
    assert password not in str(report)
    assert "password" not in report
    assert "connection_url" not in report
    assert "dsn" not in report


@_skip_without_db
def test_db_state_never_creates_the_django_migrations_table() -> None:
    """Read-only guarantee: running this against a real (even pristine) DB never
    creates `django_migrations` -- only `to_regclass()` existence checks are used."""
    import psycopg

    connection = psycopg.connect(
        host=os.environ["DATABASE_HOST"],
        port=os.environ["DATABASE_PORT"],
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.django_migrations') IS NOT NULL")
            (existed_before,) = cursor.fetchone()
    finally:
        connection.close()

    run_db_state()

    connection = psycopg.connect(
        host=os.environ["DATABASE_HOST"],
        port=os.environ["DATABASE_PORT"],
        dbname=os.environ["DATABASE_NAME"],
        user=os.environ["DATABASE_USER"],
        password=os.environ["DATABASE_PASSWORD"],
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.django_migrations') IS NOT NULL")
            (exists_after,) = cursor.fetchone()
    finally:
        connection.close()

    assert exists_after == existed_before


def test_db_state_reports_missing_configuration_cleanly(monkeypatch) -> None:
    for var in REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    exit_code, report = run_db_state()
    assert exit_code == 2
    assert "missing required environment variable" in report["error"]


@_skip_without_db
def test_audit_isolation_report_never_contains_the_password() -> None:
    _exit_code, report = run_audit_isolation_check()
    password = os.environ["DATABASE_PASSWORD"]
    assert password not in str(report)


@_skip_without_db
def test_audit_isolation_honestly_reports_two_separate_facts() -> None:
    """ADR-0008: local trigger presence and deployment-layer role isolation
    are two distinct facts, and this check must never conflate them."""
    _exit_code, report = run_audit_isolation_check()
    if "error" in report:
        pytest.skip(f"audit.0001 not yet migrated in this environment: {report['error']}")
    assert "trigger_ok__local_layer" in report
    assert "role_isolation_satisfied__deployment_layer" in report
    # This project's local setup is a single owning role (ADR-0008) -- the
    # deployment-layer fact is honestly expected to be unsatisfied here.
    assert report["role_is_table_owner"] is True
    assert report["role_isolation_satisfied__deployment_layer"] is False


def test_audit_isolation_reports_missing_configuration_cleanly(monkeypatch) -> None:
    for var in REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    exit_code, report = run_audit_isolation_check()
    assert exit_code == 2
    assert "missing required environment variable" in report["error"]


def test_audit_event_table_name_constant_matches_the_model() -> None:
    from apps.audit.models import AuditEvent

    assert AUDIT_EVENT_TABLE == AuditEvent._meta.db_table
