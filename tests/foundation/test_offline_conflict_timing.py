"""Temporal conflict precedence; database behavior is covered by integration tests.

Exercise the production classifier while replacing only unrelated historical
access evaluation. No database locks, writes, signatures or browser behavior
are claimed by these tests.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.entry.services import offline_sync


@pytest.mark.parametrize(
    ("decision", "age_hours", "grant_hours", "band", "conflict", "outcome"),
    [
        ("ADMIT", 2, 3, "STALE", "PACKAGE_STALE", "ADMITTED_ON_STALE_DATA"),
        ("ADMIT", 2, 2, "STALE", "POLICY_VIOLATION", "GRANT_NOT_VALID"),
        ("DO_NOT_ADMIT", 2, 2, "STALE", "POLICY_VIOLATION", "GRANT_NOT_VALID"),
        ("ADMIT", 1, 1, "AGING", "POLICY_VIOLATION", "GRANT_NOT_VALID"),
        ("DO_NOT_ADMIT", 1, 1, "AGING", "POLICY_VIOLATION", "GRANT_NOT_VALID"),
        ("ADMIT", 8, 2, "EXPIRED", "PACKAGE_EXPIRED", "ADMITTED_ON_EXPIRED_DATA"),
        ("DO_NOT_ADMIT", 8, 2, "EXPIRED", "PACKAGE_EXPIRED", "DECISION_ON_EXPIRED_DATA"),
    ],
)
@pytest.mark.parametrize("boundary_seconds", [0, 1])
def test_temporal_rejection_precedence_retains_both_facts(
    monkeypatch, decision, age_hours, grant_hours, band, conflict, outcome, boundary_seconds
):
    cutoff = datetime(2026, 9, 28, 8, tzinfo=UTC)
    occurred = cutoff + timedelta(hours=age_hours, seconds=boundary_seconds)
    grant_expires = cutoff + timedelta(hours=grant_hours)
    package = SimpleNamespace(
        data_cutoff_at=cutoff,
        aging_at=cutoff + timedelta(minutes=15),
        stale_at=cutoff + timedelta(hours=2),
        expires_at=cutoff + timedelta(hours=8),
        status="READY",
        status_changed_at=cutoff,
    )
    row = SimpleNamespace(
        # The operation's identity: excluded from its own prior-admission
        # evidence (correction 5, D3 S1).
        pk="synthetic-operation",
        payload_json={
            "local": {"result": "ALLOWED"},
            "decision": decision,
            "override": None,
            "state": "OFFLINE_ACTIVE",
            "package": {"critical_delta_cutoff_at": int(cutoff.timestamp())},
        },
        digital_entry_pass=SimpleNamespace(status="ACTIVE", event_edition_id=1),
        package=package,
        grant=SimpleNamespace(issued_at=cutoff, expires_at=grant_expires, revoked_at=None),
        occurred_at=occurred,
        event_edition_id=1,
        event_edition=SimpleNamespace(pk=1),
        gate=SimpleNamespace(pk=1),
        zone=SimpleNamespace(pk=1),
    )
    evaluation = SimpleNamespace(pass_status_at="ACTIVE", as_json=lambda: {"result": "ALLOWED"})
    monkeypatch.setattr(offline_sync, "evaluate_at", lambda **_kwargs: evaluation)
    monkeypatch.setattr(offline_sync, "status_changed_at", lambda *_args: cutoff)

    result = offline_sync._classify(row, SimpleNamespace(event_edition_id=1), now=occurred)

    assert result.status == "SECURITY_CONFLICT"
    assert result.conflict_type == result.case_type == conflict
    assert result.outcome == outcome
    assert not result.create_event
    assert result.server["band_at_occurrence"] == band
    assert result.server["grant_expires_at"] == int(grant_expires.timestamp())
