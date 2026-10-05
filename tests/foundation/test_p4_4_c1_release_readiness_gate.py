"""P4-4-C1 (review finding R-03): `scripts/check.py` keeps the local
regression result and release readiness apart, and never turns an unassessed
or malformed release result into READY.

Every subprocess is stubbed: no nested pytest, manage.py or database call.
"""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

import scripts.check as check

from .test_all_requires_db import _stub_everything_except_db

GOOD_PROBE = (0, {"server_version": "PostgreSQL 17.11", "database": "asc2026_dev"})


def _completed(stdout: str, returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize("status", ["BLOCKED", "NOT_ASSESSED"])
def test_a_passing_local_gate_reports_release_readiness_separately(
    monkeypatch, capsys, status
) -> None:
    _stub_everything_except_db(monkeypatch, run_db_probe_result=GOOD_PROBE)
    monkeypatch.setattr(check, "run_release_readiness", lambda: (2, {"overall": status}))

    assert check.cmd_all(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "LOCAL REGRESSION GATE: PASSED" in out
    assert f"RELEASE READINESS: {status}" in out
    assert "a passing local gate is never release readiness" in out


def test_a_ready_release_result_never_hides_a_failing_local_gate(monkeypatch, capsys) -> None:
    _stub_everything_except_db(monkeypatch, run_db_probe_result=GOOD_PROBE)
    monkeypatch.setattr(check, "run_release_readiness", lambda: (0, {"overall": "READY"}))
    monkeypatch.setattr(
        check, "_run", lambda description, command, env=None: description != "pytest"
    )

    assert check.cmd_all(argparse.Namespace()) == 1
    assert "LOCAL REGRESSION GATE: FAILED" in capsys.readouterr().out


def test_the_gate_runs_the_database_checks(monkeypatch) -> None:
    _stub_everything_except_db(monkeypatch, run_db_probe_result=GOOD_PROBE)
    commands = []
    monkeypatch.setattr(
        check, "_run", lambda description, command, env=None: commands.append(command) or True
    )
    check.cmd_all(argparse.Namespace())
    (django_check,) = [c for c in commands if c[1:3] == ["manage.py", "check"]]
    assert "--database=default" in django_check


def test_all_no_db_never_assesses_and_says_so(monkeypatch, capsys) -> None:
    _stub_everything_except_db(monkeypatch, run_db_probe_result=GOOD_PROBE)
    calls = []
    monkeypatch.setattr(check, "run_release_readiness", lambda: calls.append(1) or (0, {}))
    commands = []
    monkeypatch.setattr(
        check, "_run", lambda description, command, env=None: commands.append(command) or True
    )
    check.cmd_all_no_db(argparse.Namespace())
    assert calls == []
    assert "RELEASE READINESS: NOT_ASSESSED" in capsys.readouterr().out
    (django_check,) = [c for c in commands if c[1:3] == ["manage.py", "check"]]
    assert "--database=default" not in django_check


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 1),  # the command crashed before printing a report
        ("Traceback (most recent call last): ...", 1),
        (json.dumps({"overall": "READY"}), 1),  # status and exit code disagree
        (json.dumps({"overall": "BLOCKED"}), 0),
        (json.dumps({"overall": "APPROVED"}), 0),  # unknown status
        (json.dumps({"items": []}), 0),  # no overall status
    ],
)
def test_a_malformed_or_inconsistent_result_is_not_assessed(
    monkeypatch, stdout, returncode
) -> None:
    secret_stderr = "connection to host=db.p44c1.invalid password=p44c1 failed"
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda *args, **kwargs: _completed(stdout, returncode, secret_stderr),
    )
    exit_code, report = check.run_release_readiness()
    assert exit_code == 2
    assert report["overall"] == "NOT_ASSESSED"
    assert "p44c1" not in json.dumps(report)


@pytest.mark.parametrize(
    "overall",
    [[], {}, None, 0, 1, 2.5, True, False],
    ids=["empty-list", "empty-object", "null", "zero", "one", "float", "true", "false"],
)
@pytest.mark.parametrize("returncode", [0, 1, 2])
def test_a_non_string_overall_is_not_assessed_and_never_raises(
    monkeypatch, overall, returncode
) -> None:
    """P4-4-C2: an unhashable `overall` (a list or an object) used to raise
    TypeError at the status lookup instead of failing closed. Whatever its
    type, and whatever the child's exit code, it is NOT_ASSESSED."""
    secret_stdout = json.dumps({"overall": overall, "detail": "host=db.p44c2.invalid"})
    secret_stderr = "connection to host=db.p44c2.invalid password=p44c2 failed"
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda *args, **kwargs: _completed(secret_stdout, returncode, secret_stderr),
    )
    exit_code, report = check.run_release_readiness()
    assert exit_code == 2
    assert report == {
        "overall": "NOT_ASSESSED",
        "error": "the assessment result is inconsistent",
    }
    assert "p44c2" not in json.dumps(report)


@pytest.mark.parametrize("body", ["[]", "null", "0", '"READY"', "true"])
def test_a_report_that_is_not_an_object_is_not_assessed(monkeypatch, body) -> None:
    monkeypatch.setattr(
        check.subprocess, "run", lambda *args, **kwargs: _completed(body, 0, "p44c2")
    )
    exit_code, report = check.run_release_readiness()
    assert exit_code == 2
    assert report["overall"] == "NOT_ASSESSED"
    assert "p44c2" not in json.dumps(report)


@pytest.mark.parametrize(
    ("status", "returncode"), [("READY", 0), ("BLOCKED", 1), ("NOT_ASSESSED", 2)]
)
def test_each_valid_status_keeps_its_matching_exit_code(monkeypatch, status, returncode) -> None:
    body = {"overall": status, "items": [], "not_covered": [], "note": "n"}
    monkeypatch.setattr(
        check.subprocess, "run", lambda *args, **kwargs: _completed(json.dumps(body), returncode)
    )
    assert check.run_release_readiness() == (returncode, body)


def test_a_consistent_result_is_passed_through(monkeypatch) -> None:
    body = {"overall": "BLOCKED", "items": [], "not_covered": [], "note": "n"}
    monkeypatch.setattr(
        check.subprocess, "run", lambda *args, **kwargs: _completed(json.dumps(body), 1)
    )
    assert check.run_release_readiness() == (1, body)


def test_the_child_command_is_the_read_only_json_assessment(monkeypatch) -> None:
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return _completed(json.dumps({"overall": "BLOCKED"}), 1)

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    check.run_release_readiness()
    assert seen["command"][1:4] == ["manage.py", "release_readiness", "--json"]
