"""`scripts/check.py all` must fail when the real PostgreSQL probe cannot
run; `all-no-db` is the explicitly named, non-acceptance alternative
(Prompt 2 correction §4).

Every `_run`-backed subprocess step (ruff, pytest, manage.py, pip-audit,
deploy) is monkeypatched to a fast, deterministic stand-in here -- this
test exercises the DB-requirement branching logic only, and deliberately
never spawns a real nested `pytest` process (which `cmd_all` would
otherwise do by shelling out to `pytest tests/foundation`).
"""

from __future__ import annotations

import argparse

import scripts.check as check


def _stub_everything_except_db(monkeypatch, *, run_db_probe_result):
    monkeypatch.setattr(check, "run_db_probe", lambda: run_db_probe_result)
    monkeypatch.setattr(
        check,
        "run_db_state",
        lambda: (
            1,
            {
                "is_clean_pre_prompt3_state": False,
                "django_migrations_table_exists": True,
                "applied_migrations": ["accounts.0001_initial"],
                "existing_tables": ["django_migrations"],
            },
        ),
    )
    monkeypatch.setattr(
        check,
        "run_audit_isolation_check",
        lambda: (1, {"trigger_enabled": True, "role_isolation_satisfied": False}),
    )
    # P4-4-C1: the release-readiness child process is stubbed like every
    # other subprocess step; it never affects the aggregate.
    monkeypatch.setattr(
        check, "run_release_readiness", lambda: (1, {"overall": "BLOCKED", "items": []})
    )
    monkeypatch.setattr(check, "_run", lambda description, command, env=None: True)
    monkeypatch.setattr(check, "cmd_deploy", lambda args: 0)
    monkeypatch.setattr(check, "check_gitignore_text_contract", lambda: [])
    monkeypatch.setattr(check, "check_env_file_is_excluded", lambda: [])
    monkeypatch.setattr(check, "check_env_example_placeholders_only", lambda: [])
    monkeypatch.setattr(check, "check_no_leaked_credentials", lambda: [])
    monkeypatch.setattr(check, "check_vendored_asset_checksums", lambda: [])


def test_all_fails_when_database_configuration_is_missing(monkeypatch) -> None:
    _stub_everything_except_db(
        monkeypatch,
        run_db_probe_result=(
            2,
            {"error": "missing required environment variable(s): DATABASE_HOST"},
        ),
    )

    exit_code = check.cmd_all(argparse.Namespace())

    assert exit_code == 1


def test_acceptance_never_enables_async_unsafe(monkeypatch):
    _stub_everything_except_db(monkeypatch, run_db_probe_result=(0, {}))
    calls = []

    def capture(description, command, env=None):
        calls.append((description, env))
        assert not env or not env.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        return True

    monkeypatch.setattr(check, "_run", capture)
    check.cmd_all(argparse.Namespace())
    assert any(description == "pytest tests/browser" for description, _env in calls)


def test_all_fails_when_the_connection_fails(monkeypatch) -> None:
    _stub_everything_except_db(
        monkeypatch,
        run_db_probe_result=(1, {"error": "connection failed", "host": "localhost"}),
    )

    assert check.cmd_all(argparse.Namespace()) == 1


def test_all_fails_on_wrong_major_version(monkeypatch) -> None:
    _stub_everything_except_db(
        monkeypatch,
        run_db_probe_result=(
            1,
            {"server_version": "PostgreSQL 16.4", "error": "server is not PostgreSQL 17.x"},
        ),
    )

    assert check.cmd_all(argparse.Namespace()) == 1


def test_all_succeeds_when_every_check_including_db_passes(monkeypatch) -> None:
    _stub_everything_except_db(
        monkeypatch,
        run_db_probe_result=(
            0,
            {
                "server_version": "PostgreSQL 17.11",
                "host": "localhost",
                "port": "5433",
                "database": "asc2026_dev",
                "role": "asc2026_app",
                "encoding": "UTF8",
            },
        ),
    )

    assert check.cmd_all(argparse.Namespace()) == 0


def test_all_no_db_succeeds_even_when_database_configuration_is_missing(monkeypatch) -> None:
    """The explicitly named DB-optional developer convenience -- never the
    Prompt 2 acceptance check -- tolerates a missing/unreachable database."""
    _stub_everything_except_db(
        monkeypatch,
        run_db_probe_result=(
            2,
            {"error": "missing required environment variable(s): DATABASE_HOST"},
        ),
    )

    exit_code = check.cmd_all_no_db(argparse.Namespace())

    assert exit_code == 0


def test_all_no_db_never_calls_run_db_probe_at_all(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(check, "run_db_probe", lambda: calls.append(1) or (0, {}))
    monkeypatch.setattr(check, "_run", lambda description, command, env=None: True)
    monkeypatch.setattr(check, "cmd_deploy", lambda args: 0)
    monkeypatch.setattr(check, "check_gitignore_text_contract", lambda: [])
    monkeypatch.setattr(check, "check_env_file_is_excluded", lambda: [])
    monkeypatch.setattr(check, "check_env_example_placeholders_only", lambda: [])
    monkeypatch.setattr(check, "check_no_leaked_credentials", lambda: [])
    monkeypatch.setattr(check, "check_vendored_asset_checksums", lambda: [])

    check.cmd_all_no_db(argparse.Namespace())

    assert calls == []
