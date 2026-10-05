"""Read-only PostgreSQL 17 probe (accepted plan §5.10).

Uses `psycopg` directly -- NOT Django's database/test-database machinery --
so running this test can never trigger a migration or create a table. It
genuinely connects to the configured database (normally `asc2026_dev`, when
run via `uv run --env-file .env pytest ...`) purely to read version/role/
encoding information.
"""

from __future__ import annotations

import os

import pytest

from scripts.check import (
    EXPECTED_DATABASE_ENCODING,
    EXPECTED_DATABASE_NAME,
    EXPECTED_DATABASE_USER,
    evaluate_db_probe_report,
    run_db_probe,
)

REQUIRED_VARS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)


@pytest.mark.skipif(
    any(not os.environ.get(v) for v in REQUIRED_VARS),
    reason="DATABASE_* environment variables not set (run via `uv run --env-file .env pytest ...`)",
)
def test_db_probe_reaches_postgresql_17() -> None:
    exit_code, report = run_db_probe()
    assert exit_code == 0, report
    assert "PostgreSQL 17" in report["server_version"]
    assert report["port"] == os.environ["DATABASE_PORT"]
    assert report["database"] == os.environ["DATABASE_NAME"]
    assert report["encoding"] == "UTF8"


@pytest.mark.skipif(
    any(not os.environ.get(v) for v in REQUIRED_VARS),
    reason="DATABASE_* environment variables not set (run via `uv run --env-file .env pytest ...`)",
)
def test_db_probe_report_never_contains_the_password() -> None:
    _exit_code, report = run_db_probe()
    password = os.environ["DATABASE_PASSWORD"]
    assert password not in str(report)


def test_db_probe_reports_missing_configuration_cleanly(monkeypatch) -> None:
    for var in REQUIRED_VARS:
        monkeypatch.delenv(var, raising=False)
    exit_code, report = run_db_probe()
    assert exit_code == 2
    assert "missing required environment variable" in report["error"]


def _valid_report() -> dict:
    return {
        "server_version": "PostgreSQL 17.11 on x86_64-windows",
        "host": "localhost",
        "port": "5433",
        "database": EXPECTED_DATABASE_NAME,
        "role": EXPECTED_DATABASE_USER,
        "encoding": EXPECTED_DATABASE_ENCODING,
    }


def test_evaluate_db_probe_report_accepts_the_approved_identity() -> None:
    assert evaluate_db_probe_report(_valid_report()) == []


def test_evaluate_db_probe_report_rejects_wrong_major_version() -> None:
    report = {**_valid_report(), "server_version": "PostgreSQL 16.4 on x86_64-windows"}
    problems = evaluate_db_probe_report(report)
    assert any("PostgreSQL 17.x" in p for p in problems)


def test_evaluate_db_probe_report_rejects_wrong_database_name() -> None:
    report = {**_valid_report(), "database": "some_other_database"}
    problems = evaluate_db_probe_report(report)
    assert any("some_other_database" in p and EXPECTED_DATABASE_NAME in p for p in problems)


def test_evaluate_db_probe_report_rejects_wrong_runtime_user() -> None:
    report = {**_valid_report(), "role": "some_other_role"}
    problems = evaluate_db_probe_report(report)
    assert any("some_other_role" in p and EXPECTED_DATABASE_USER in p for p in problems)


def test_evaluate_db_probe_report_rejects_wrong_encoding() -> None:
    report = {**_valid_report(), "encoding": "LATIN1"}
    problems = evaluate_db_probe_report(report)
    assert any("LATIN1" in p for p in problems)


def test_evaluate_db_probe_report_never_needs_or_sees_a_password() -> None:
    # Structural guarantee: the function's only input is the report dict,
    # which run_db_probe() never populates with a password or DSN.
    report = _valid_report()
    assert "password" not in report
    assert evaluate_db_probe_report(report) == []
