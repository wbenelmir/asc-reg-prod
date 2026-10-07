"""Phase 3 Prompt 8 (P8-05): monitor and metrics are isolated by Gate identity.

`events_gate_code_uq` makes a gate code unique only within a venue, so two
venues of ONE event edition may both have a gate `G1`. The supervisor
monitor and the observability dashboard used the code alone, so the two
`G1` gates' denied attempts, anomaly signals and latency metrics merged.
Every filter and grouping now uses the Gate primary key; the code is only a
display label. All data is synthetic.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit import action_codes
from apps.entry.models import EntryReasonCode, EntryResult
from apps.entry.observability import verification_summary
from apps.entry.selectors import recent_anomaly_signals, recent_denied_attempts
from apps.entry.services import audit
from apps.entry.services.limits import EntryLookupThrottled
from apps.entry.services.verification import verify_qr
from apps.entry.tests import factories
from apps.events.models import Gate, Venue, Zone

pytestmark = pytest.mark.django_db

UNTRUSTED_INPUT = "not-a-credential"


class TwinGate:
    """One venue with one entry gate coded `G1`, an operator and a supervisor."""

    def __init__(self, event, *, venue_code: str, device_admin):
        self.venue = Venue.objects.create(
            event_edition=event, code=venue_code, name=f"Venue {venue_code}"
        )
        self.zone = Zone.objects.create(venue=self.venue, code="MAIN", name="Main area")
        self.gate = Gate.objects.create(
            venue=self.venue, code="G1", name="Gate one", default_zone=self.zone
        )
        self.layout = _LayoutShim(self.venue, self.gate, self.zone)
        self.device, self.secret = factories.enroll_device(
            event=event, layout=self.layout, admin=device_admin, zones=[self.zone], gate=self.gate
        )
        self.operator = factories.make_user(
            f"operator.{venue_code.lower()}@example.test",
            group_name="Entry Operators",
            event=event,
            gate=self.gate,
        )
        self.supervisor = factories.make_user(
            f"supervisor.{venue_code.lower()}@example.test",
            group_name="Entry Supervisors",
            event=event,
            gate=self.gate,
        )

    def checkpoint(self, user=None):
        return factories.open_checkpoint(
            device=self.device, user=user or self.operator, zone=self.zone
        )


class _LayoutShim:
    def __init__(self, venue, gate, zone):
        self.venue = venue
        self.gate_a = gate
        self.main = zone
        self.hall = zone


@pytest.fixture
def twins(event, device_admin):
    return (
        TwinGate(event, venue_code="V1", device_admin=device_admin),
        TwinGate(event, venue_code="V2", device_admin=device_admin),
    )


def test_both_gates_really_share_the_code_within_one_event(twins, event):
    first, second = twins
    assert first.gate.code == second.gate.code == "G1"
    assert first.gate.pk != second.gate.pk
    assert first.venue.event_edition_id == second.venue.event_edition_id == event.pk


def test_denied_attempts_are_isolated_by_gate_identity(twins):
    first, second = twins
    first_checkpoint = first.checkpoint()
    second_checkpoint = second.checkpoint()

    for _ in range(2):
        verify_qr(checkpoint=first_checkpoint, raw_token=UNTRUSTED_INPUT)
    verify_qr(checkpoint=second_checkpoint, raw_token=UNTRUSTED_INPUT)

    first_rows = list(recent_denied_attempts(first_checkpoint))
    second_rows = list(recent_denied_attempts(second_checkpoint))
    assert len(first_rows) == 2
    assert len(second_rows) == 1
    assert {row.after_summary["gate_id"] for row in first_rows} == {str(first.gate.pk)}
    assert {row.after_summary["gate_id"] for row in second_rows} == {str(second.gate.pk)}
    # The human-readable code is still recorded separately, for display.
    assert {row.after_summary["gate"] for row in first_rows + second_rows} == {"G1"}


def test_throttling_and_anomaly_signals_are_isolated_by_gate_identity(twins, settings):
    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 2
    settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 3
    first, second = twins
    first_checkpoint = first.checkpoint()
    second_checkpoint = second.checkpoint()

    for _ in range(3):
        verify_qr(checkpoint=first_checkpoint, raw_token=UNTRUSTED_INPUT)
    with pytest.raises(EntryLookupThrottled):
        verify_qr(checkpoint=first_checkpoint, raw_token=UNTRUSTED_INPUT)

    first_signals = recent_anomaly_signals(first_checkpoint)
    assert {signal["throttled"] for signal in first_signals} == {True, False}
    assert recent_anomaly_signals(second_checkpoint) == []


def test_latency_and_error_metrics_are_grouped_by_gate_identity(twins, event):
    first, second = twins
    first_checkpoint = first.checkpoint()
    second_checkpoint = second.checkpoint()
    for _ in range(3):
        verify_qr(checkpoint=first_checkpoint, raw_token=UNTRUSTED_INPUT)
    verify_qr(checkpoint=second_checkpoint, raw_token=UNTRUSTED_INPUT)

    by_gate = verification_summary(event_edition=event, since=timezone.now() - timedelta(hours=1))[
        "by_gate"
    ]
    assert len(by_gate) == 2
    rows = {row["gate_id"]: row for row in by_gate}
    assert rows[str(first.gate.pk)]["count"] == 3
    assert rows[str(second.gate.pk)]["count"] == 1
    assert rows[str(first.gate.pk)]["technical_errors"] == 0
    # The codes are display labels only, qualified by venue for the reader.
    assert rows[str(first.gate.pk)]["gate"] == rows[str(second.gate.pk)]["gate"] == "G1"
    assert rows[str(first.gate.pk)]["label"] == "V1 / G1"
    assert rows[str(second.gate.pk)]["label"] == "V2 / G1"


def test_historical_rows_without_gate_identity_fail_closed(twins, event):
    """A row written before `gate_id` existed matches NEITHER `G1` gate --
    never a code-only fallback that could cross venues."""
    first, second = twins
    first_checkpoint = first.checkpoint()
    second_checkpoint = second.checkpoint()
    for action_code in (
        action_codes.ENTRY_VERIFICATION_PERFORMED,
        action_codes.ENTRY_ANOMALY_SIGNAL,
    ):
        audit(
            action_code=action_code,
            actor=first.operator,
            target_type="EntryDeviceSession",
            target_uuid=first_checkpoint.device_session.pk,
            event_edition_id=event.pk,
            result="SUCCESS",
            reason_code=EntryReasonCode.INVALID_CREDENTIAL,
            after_summary={
                "method": "QR",
                "result": EntryResult.DENIED,
                "reason": EntryReasonCode.INVALID_CREDENTIAL,
                "gate": "G1",
            },
        )
    for checkpoint in (first_checkpoint, second_checkpoint):
        assert list(recent_denied_attempts(checkpoint)) == []
        assert recent_anomaly_signals(checkpoint) == []


def _start(client, twin, user):
    staff_sign_in(client, user.email_normalized, factories.TEST_PASSWORD)
    client.cookies[settings.ENTRY_DEVICE_COOKIE_NAME] = twin.secret
    response = client.post(reverse("entry:home"), {"action": "start", "zone_id": str(twin.zone.pk)})
    assert response.status_code == 302


def test_the_supervisor_monitor_shows_only_its_own_gate(client, twins, settings):
    settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 2
    first, second = twins
    first_checkpoint = first.checkpoint()
    for _ in range(3):
        verify_qr(checkpoint=first_checkpoint, raw_token=UNTRUSTED_INPUT)

    # The other venue's supervisor, at the other `G1`, sees none of it.
    _start(client, second, second.supervisor)
    monitor = client.get(reverse("entry:monitor"))
    assert monitor.status_code == 200
    assert monitor.context["denied_attempts"] == []
    assert monitor.context["anomalies"] == []

    # The supervisor at the gate where it happened sees all of it.
    client.logout()
    _start(client, first, first.supervisor)
    monitor = client.get(reverse("entry:monitor"))
    assert monitor.status_code == 200
    assert len(monitor.context["denied_attempts"]) == 3
    assert len(monitor.context["anomalies"]) == 1
