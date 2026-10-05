"""P4-4-C1 (review finding R-03): release readiness is reported truthfully as
READY, BLOCKED or NOT_ASSESSED, separately from the local regression gate.

Before this correction the privacy checks returned no message when their
assessment failed, and `scripts/check.py all` ran `manage.py check` without a
database, so unresolved legal and retention facts were invisible there and
nothing reported the missing sign-in MFA. Synthetic data only.

P4-4-C3: owner decision MFA-01 was revised on 2026-10-01 (amendment A-11):
operational sign-in is email and password in staging and production, so the
former `operational_mfa` blocker is now `operational_sign_in`, READY under the
revised requirement while still stating that MFA is not enforced. The tests
below that pinned the blocker were rewritten to that requirement; the
step-up and counter items have their own tests in
`test_p4_4_c3_release_readiness.py`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.contrib.auth import SESSION_KEY
from django.core.checks import run_checks
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.test import Client
from django.urls import reverse

from apps.accounts import mfa
from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.core import issuance_counter, release_readiness
from apps.core.release_readiness import BLOCKED, NOT_ASSESSED, READY
from apps.privacy import checks as privacy_checks
from apps.privacy.models import RetentionCategory

pytestmark = pytest.mark.django_db

LEAK = "password=p44c1-synthetic host=db.p44c1.invalid"
STAFF_EMAIL = "p44c1-staff@example.test"
GOOD = "p44c1-correct-horse-battery"


#: A loadable path outside any `.tests.` module, for the READY path only. The
#: assessment checks loadability, never provider operation; the protocol class
#: itself verifies nothing, and the step-up boundary would refuse with it.
_LOADABLE_STEP_UP_PATH = "apps.accounts.mfa.MfaStepUpBackend"


class _HealthySharedCounter:
    """READY-path stand-in for a shared counter that answers. Not Redis."""

    def probe(self) -> str:
        return issuance_counter.STATE_SHARED


def _items(report) -> dict[str, release_readiness.ReadinessItem]:
    return {item.key: item for item in report.items}


def _all_text(report) -> str:
    return json.dumps(report.as_dict())


def _everything_resolved(monkeypatch, settings) -> None:
    """Every assessed item satisfied: the only way READY is reachable."""
    monkeypatch.setattr(privacy_checks, "legal_release_blockers", lambda: [])
    monkeypatch.setattr(privacy_checks, "retention_release_blockers", lambda: [])
    # P4-4-C3: the sign-in item is READY under the revised MFA-01 requirement
    # with MFA not enforced; the step-up provider and the shared counter are
    # separate items.
    settings.MFA_BACKEND = _LOADABLE_STEP_UP_PATH
    settings.HUMAN_CHECK_COUNTER_STORE = issuance_counter.STORE_REDIS
    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", _HealthySharedCounter)
    # IDV-2 (A13-06): the official ministry adapter with a complete, synthetic
    # configuration (placeholder values; no request is ever made here).
    settings.NIN_PROVIDER_BACKEND = settings.OFFICIAL_NIN_PROVIDER_BACKEND
    settings.MINISTRY_NIN_API_BASE_URL = "https://identity.example.test"
    settings.MINISTRY_NIN_API_USERNAME = "__synthetic_user__"
    settings.MINISTRY_NIN_API_PASSWORD = "__synthetic_password__"  # noqa: S105 - placeholder
    settings.MINISTRY_NIN_API_AUTH_TOKEN_PATH = "token"  # noqa: S105 - a JSON key path


# ---------------------------------------------------------------------------
# Operational sign-in (P44-F06): the MFA fact stays truthful; since P4-4-C3
# the revised MFA-01 requirement (email and password) is no release blocker
# ---------------------------------------------------------------------------


def test_the_mfa_fact_agrees_with_the_actual_sign_in_behaviour() -> None:
    """A password alone still creates an operational session, so the release
    assessment must say MFA is not enforced. When a reviewed integration makes
    a second factor necessary, this test forces the fact to change with it."""
    OperationalUser.objects.create_user(
        email=STAFF_EMAIL, password=GOOD, status=OperationalUserStatus.ACTIVE
    )
    client = Client(REMOTE_ADDR="198.51.100.77")
    response = client.post(
        reverse("accounts:operational-sign-in"), {"email": STAFF_EMAIL, "password": GOOD}
    )
    assert response.status_code == 302
    password_alone_creates_a_session = SESSION_KEY in client.session
    assert mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED is (not password_alone_creates_a_session)


def test_password_only_sign_in_is_the_revised_requirement_and_not_reported_as_mfa() -> None:
    # Until P4-4-C3 this item was `operational_mfa`, BLOCKED.
    report = release_readiness.assess_release_readiness()
    item = _items(report)["operational_sign_in"]
    assert item.status == READY
    assert "MFA is NOT enforced at operational sign-in" in " ".join(item.reasons)
    assert report.as_dict()["facts"]["operational_sign_in_mfa_enforced"] is False
    # Other prerequisites still block in the test environment.
    assert report.overall == BLOCKED


def test_the_remaining_items_are_still_assessed_when_legal_facts_are_resolved(
    monkeypatch,
) -> None:
    monkeypatch.setattr(privacy_checks, "legal_release_blockers", lambda: [])
    monkeypatch.setattr(privacy_checks, "retention_release_blockers", lambda: [])
    report = release_readiness.assess_release_readiness()
    statuses = {item.key: item.status for item in report.items}
    assert statuses == {
        "database_schema": READY,
        "legal_notices": READY,
        "retention": READY,
        "operational_sign_in": READY,
        "sensitive_operation_step_up": BLOCKED,
        "challenge_issuance_counter": BLOCKED,
        # IDV-2 (A13-06): the test settings use the development simulation.
        "identity_verification_provider": BLOCKED,
    }
    assert report.overall == BLOCKED


@pytest.mark.parametrize("backend", [None, "", "apps.p44c1.does_not_exist.Backend"])
def test_an_enforcement_flag_without_a_loadable_provider_is_still_blocked(
    monkeypatch, settings, backend
) -> None:
    monkeypatch.setattr(mfa, "OPERATIONAL_SIGN_IN_MFA_ENFORCED", True)
    settings.MFA_BACKEND = backend
    item = _items(release_readiness.assess_release_readiness())["operational_sign_in"]
    assert item.status == BLOCKED
    assert "p44c1" not in " ".join(item.reasons)


# ---------------------------------------------------------------------------
# Unresolved legal and retention facts
# ---------------------------------------------------------------------------


def test_the_seeded_draft_notices_are_unresolved_legal_facts() -> None:
    """The published drafts carry [TO BE CONFIRMED: ...] markers (C-09)."""
    item = _items(release_readiness.assess_release_readiness())["legal_notices"]
    assert item.status == BLOCKED
    assert item.reasons and all("unresolved item(s) in" in reason for reason in item.reasons)


def test_legal_reasons_are_counts_never_marker_content(monkeypatch) -> None:
    blockers = [
        ("PRIVACY_NOTICE", "en", "[TO BE CONFIRMED: p44c1 synthetic controller]"),
        ("PRIVACY_NOTICE", "en", "[TO BE CONFIRMED: p44c1 synthetic contact]"),
        ("TERMS", "ar", "missing effective version"),
    ]
    monkeypatch.setattr(privacy_checks, "legal_release_blockers", lambda: blockers)
    item = _items(release_readiness.assess_release_readiness())["legal_notices"]
    assert item.reasons == (
        "2 unresolved item(s) in PRIVACY_NOTICE (en)",
        "1 unresolved item(s) in TERMS (ar)",
    )
    assert "p44c1" not in " ".join(item.reasons)


def test_unresolved_retention_and_the_missing_job_block_release() -> None:
    RetentionCategory.objects.all().delete()
    RetentionCategory.objects.create(code="P44C1_PENDING", description="Synthetic category")
    item = _items(release_readiness.assess_release_readiness())["retention"]
    assert item.status == BLOCKED
    assert "1 retention category(ies) without an approved period" in item.reasons
    assert "no approved participant-data retention or purge job exists" in item.reasons
    assert "P44C1_PENDING" not in " ".join(item.reasons)


def test_approved_periods_alone_never_clear_the_missing_job() -> None:
    RetentionCategory.objects.all().delete()
    RetentionCategory.objects.create(
        code="P44C1_SET", description="Synthetic category", retention_period_days=30
    )
    item = _items(release_readiness.assess_release_readiness())["retention"]
    assert item.status == BLOCKED
    assert item.reasons == ("no approved participant-data retention or purge job exists",)


# ---------------------------------------------------------------------------
# Failed assessments are NOT_ASSESSED, never READY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["legal_release_blockers", "retention_release_blockers"])
def test_a_failed_assessment_is_not_assessed_and_never_ready(monkeypatch, settings, target) -> None:
    _everything_resolved(monkeypatch, settings)

    def broken():
        raise RuntimeError(LEAK)

    monkeypatch.setattr(privacy_checks, target, broken)
    report = release_readiness.assess_release_readiness()
    failed = [item for item in report.items if item.status == NOT_ASSESSED]
    assert len(failed) == 1
    assert failed[0].reasons == ("the assessment could not run",)
    assert report.overall == NOT_ASSESSED
    assert "p44c1" not in _all_text(report) and "RuntimeError" not in _all_text(report)


def test_an_unavailable_database_is_never_ready(monkeypatch, settings) -> None:
    _everything_resolved(monkeypatch, settings)
    from django.db import connection

    with patch.object(connection, "cursor", side_effect=OperationalError(LEAK)):
        report = release_readiness.assess_release_readiness()
    statuses = {item.key: item.status for item in report.items}
    assert statuses["database_schema"] == NOT_ASSESSED
    assert statuses["legal_notices"] == NOT_ASSESSED
    assert statuses["retention"] == NOT_ASSESSED
    assert report.overall == NOT_ASSESSED
    assert "p44c1" not in _all_text(report)


def test_a_schema_with_unapplied_migrations_is_not_assessed(monkeypatch, settings) -> None:
    """Observed on the development database during P4-4-C1: with the UX-3 data
    migration unapplied, the old notices had no marker, so legal facts read
    from that schema looked resolved. They must be NOT_ASSESSED instead."""
    _everything_resolved(monkeypatch, settings)
    from django.db.migrations.executor import MigrationExecutor

    monkeypatch.setattr(
        MigrationExecutor, "migration_plan", lambda self, targets: [("p44c1", False)]
    )
    report = release_readiness.assess_release_readiness()
    items = _items(report)
    assert items["database_schema"].status == NOT_ASSESSED
    assert items["database_schema"].reasons == (
        "1 migration(s) are not applied; this database does not represent the release schema",
    )
    for key in ("legal_notices", "retention"):
        assert items[key].status == NOT_ASSESSED
        assert items[key].reasons == (
            "not assessed: the database schema is unavailable or not current",
        )
    assert report.overall == NOT_ASSESSED


def test_ready_requires_every_item_and_still_disclaims_approval(monkeypatch, settings) -> None:
    _everything_resolved(monkeypatch, settings)
    report = release_readiness.assess_release_readiness()
    assert report.overall == READY
    body = report.as_dict()
    assert "not a legal, security or go-live approval" in body["note"]
    assert any("C-01" in entry for entry in body["not_covered"])


def test_the_assessment_writes_nothing() -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        release_readiness.assess_release_readiness()
    writes = [
        q["sql"]
        for q in captured.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
    ]
    assert writes == []


# ---------------------------------------------------------------------------
# The management command and the privacy system checks
# ---------------------------------------------------------------------------


def test_the_command_exits_non_zero_with_a_sanitized_json_report(capsys) -> None:
    with pytest.raises(CommandError) as caught:
        call_command("release_readiness", "--json")
    assert caught.value.returncode == 1
    body = json.loads(capsys.readouterr().out)
    assert body["overall"] == BLOCKED
    assert {item["key"] for item in body["items"]} == {
        "database_schema",
        "legal_notices",
        "retention",
        "operational_sign_in",
        "sensitive_operation_step_up",
        "challenge_issuance_counter",
        "identity_verification_provider",
    }


def test_the_command_reports_not_assessed_with_exit_code_two(monkeypatch, settings, capsys) -> None:
    def broken():
        raise RuntimeError(LEAK)

    _everything_resolved(monkeypatch, settings)
    monkeypatch.setattr(privacy_checks, "legal_release_blockers", broken)
    with pytest.raises(CommandError) as caught:
        call_command("release_readiness")
    assert caught.value.returncode == 2
    out = capsys.readouterr().out
    assert "Release readiness: NOT_ASSESSED" in out
    assert "Not covered by this assessment" in out
    assert "p44c1" not in out


@pytest.mark.parametrize(
    ("target", "check_id"),
    [("legal_release_blockers", "privacy.W003"), ("retention_release_blockers", "privacy.W004")],
)
def test_a_failed_privacy_check_is_reported_not_silent(monkeypatch, target, check_id) -> None:
    def broken():
        raise RuntimeError(LEAK)

    monkeypatch.setattr(privacy_checks, target, broken)
    messages = [m for m in run_checks(databases=["default"]) if m.id == check_id]
    assert len(messages) == 1
    assert "NOT_ASSESSED" in messages[0].msg and "never a release approval" in messages[0].msg
    assert "p44c1" not in messages[0].msg
    # The connection stays usable after the failed assessment.
    assert RetentionCategory.objects.count() >= 0


def test_without_a_database_the_privacy_checks_still_do_not_assess() -> None:
    assert privacy_checks.legal_notice_markers(databases=None) == []
    assert privacy_checks.retention_markers(databases=None) == []
