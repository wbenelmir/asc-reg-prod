"""P4-4-C3: release readiness under the revised MFA-01 requirement and with
the CACHE-01 counter.

* Missing sign-in MFA is no longer a blocker, and is still never reported as
  implemented.
* The MFA step-up for sensitive operations and the shared challenge counter
  are separate items, assessed on their own facts.
* Removing the sign-in blocker never manufactures READY: legal, retention and
  every other unresolved prerequisite stay visible.

Synthetic data only.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import CommandError, call_command

from apps.accounts import mfa
from apps.core import issuance_counter, release_readiness
from apps.core.release_readiness import BLOCKED, NOT_ASSESSED, READY

pytestmark = pytest.mark.django_db

LOADABLE_STEP_UP = "apps.accounts.mfa.MfaStepUpBackend"


class _Probe:
    def __init__(self, state=None, error=None):
        self.state, self.error = state, error

    def probe(self):
        if self.error:
            raise self.error
        return self.state


def _items(report=None) -> dict:
    report = report or release_readiness.assess_release_readiness()
    return {item.key: item for item in report.items}


# ---------------------------------------------------------------------------
# Operational sign-in (MFA-01 revised, amendment A-11)
# ---------------------------------------------------------------------------


def test_the_revised_sign_in_requirement_is_stated_explicitly_and_truthfully() -> None:
    report = release_readiness.assess_release_readiness()
    item = _items(report)["operational_sign_in"]
    assert item.status == READY
    assert item.reference.startswith("MFA-01 revised (A-11)")
    text = " ".join(item.reasons)
    assert "email and password" in text
    assert "MFA is NOT enforced at operational sign-in" in text
    assert "does not report MFA as implemented" in text
    facts = report.as_dict()["facts"]
    assert facts["operational_sign_in_mfa_enforced"] is False
    assert facts["operational_sign_in_requirement"].startswith("EMAIL_AND_PASSWORD")


def test_the_enforcement_fact_is_unchanged() -> None:
    assert mfa.OPERATIONAL_SIGN_IN_MFA_ENFORCED is False


def test_removing_the_sign_in_blocker_does_not_manufacture_ready() -> None:
    report = release_readiness.assess_release_readiness()
    items = _items(report)
    assert items["legal_notices"].status == BLOCKED  # the seeded draft notices (C-09)
    assert items["retention"].status == BLOCKED  # OD-007
    assert report.overall == BLOCKED
    assert any("C-01" in entry for entry in report.not_covered)
    assert any("OD-006" in entry for entry in report.not_covered)


# ---------------------------------------------------------------------------
# Sensitive-operation step-up: unchanged by MFA-01, assessed on its own
# ---------------------------------------------------------------------------


def test_without_a_step_up_provider_the_emergency_wipe_is_reported_unavailable(settings) -> None:
    settings.MFA_BACKEND = None
    item = _items()["sensitive_operation_step_up"]
    assert item.status == BLOCKED
    text = " ".join(item.reasons)
    assert "emergency device wipe" in text and "fails closed" in text
    assert "does not authorize bypassing this step-up" in text


@pytest.mark.parametrize(
    ("backend", "reason"),
    [
        ("apps.accounts.tests.mfa_double.AcceptingStepUpBackend", "is a test double"),
        ("apps.p44c3.does_not_exist.Backend", "cannot be loaded"),
    ],
)
def test_a_test_double_or_unloadable_step_up_provider_is_blocked(settings, backend, reason) -> None:
    settings.MFA_BACKEND = backend
    item = _items()["sensitive_operation_step_up"]
    assert item.status == BLOCKED
    assert reason in item.reasons[0]
    assert "p44c3" not in " ".join(item.reasons) and "mfa_double" not in " ".join(item.reasons)


def test_a_loadable_step_up_provider_is_ready_but_not_claimed_to_work(settings) -> None:
    settings.MFA_BACKEND = LOADABLE_STEP_UP
    item = _items()["sensitive_operation_step_up"]
    assert item.status == READY
    assert "proven only by deployment evidence" in item.reasons[0]


# ---------------------------------------------------------------------------
# The shared challenge counter (CACHE-01)
# ---------------------------------------------------------------------------


def test_the_local_counter_is_a_release_blocker() -> None:
    item = _items()["challenge_issuance_counter"]
    assert item.status == BLOCKED
    assert "local development only" in item.reasons[0]


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (issuance_counter.STATE_SHARED, READY),
        (issuance_counter.STATE_DEGRADED, BLOCKED),
        (issuance_counter.STATE_UNAVAILABLE, BLOCKED),
    ],
)
def test_the_shared_counter_item_follows_its_probe(monkeypatch, settings, state, status) -> None:
    settings.HUMAN_CHECK_COUNTER_STORE = "redis"
    monkeypatch.setattr(issuance_counter, "get_issuance_limiter", lambda: _Probe(state))
    item = _items()["challenge_issuance_counter"]
    assert item.status == status
    if status == READY:
        assert "proven only by the staging verification" in item.reasons[0]


def test_a_counter_assessment_that_cannot_run_is_not_assessed(monkeypatch, settings) -> None:
    settings.HUMAN_CHECK_COUNTER_STORE = "redis"
    leak = "rediss://redis.p44c3.invalid:6380/2 p44c3-leak-marker"
    monkeypatch.setattr(
        issuance_counter, "get_issuance_limiter", lambda: _Probe(error=RuntimeError(leak))
    )
    report = release_readiness.assess_release_readiness()
    item = _items(report)["challenge_issuance_counter"]
    assert item.status == NOT_ASSESSED
    assert "p44c3" not in json.dumps(report.as_dict())


# ---------------------------------------------------------------------------
# The command output
# ---------------------------------------------------------------------------


def test_the_command_states_the_facts(capsys) -> None:
    with pytest.raises(CommandError) as caught:
        call_command("release_readiness")
    assert caught.value.returncode == 1
    out = capsys.readouterr().out
    assert "[READY] operational_sign_in" in out
    assert "operational_sign_in_mfa_enforced: False" in out
    assert "[BLOCKED] sensitive_operation_step_up" in out
    assert "[BLOCKED] challenge_issuance_counter" in out


def test_the_check_script_prints_well_formed_facts_only(capsys) -> None:
    from scripts.check import _print_release_readiness

    _print_release_readiness(
        {"overall": "BLOCKED", "items": [], "facts": {"operational_sign_in_mfa_enforced": False}}
    )
    _print_release_readiness({"overall": "BLOCKED", "items": [], "facts": ["not", "a", "dict"]})
    out = capsys.readouterr().out
    assert out.count("fact: ") == 1
    assert "fact: operational_sign_in_mfa_enforced = False" in out
