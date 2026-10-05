"""Phase 3 Prompt 5 (ADR-0022): lookup limits, anomaly signals, telemetry,
degraded-online state, the passive connection check, validation re-render,
query counts, the indexed verification path, and redaction.

All data is synthetic.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from django.conf import settings
from django.core.checks import run_checks
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry import observability
from apps.entry.models import (
    EntryOperatorSession,
    EntryReasonCode,
    EntryResult,
    VerificationMethod,
    VerificationSample,
)
from apps.entry.services.limits import (
    KIND_IDENTITY_LOOKUPS,
    KIND_INVALID_SCANS,
    EntryLookupThrottled,
)
from apps.entry.services.verification import (
    lookup_identity,
    lookup_reference,
    manual_search,
    verify_qr,
)
from apps.entry.tests import factories
from apps.entry.tests.test_views import _start

pytestmark = pytest.mark.django_db

SYNTHETIC_NIN = "109870123456789099"
UNTRUSTED = "synthetic-not-a-credential-000"


@pytest.fixture
def signed_in(client, operator, device_and_secret, layout):
    _start(client, operator, device_and_secret[1], layout.main)
    return client


def _anomalies():
    return AuditEvent.objects.filter(action_code=action_codes.ENTRY_ANOMALY_SIGNAL)


def _throttles():
    return AuditEvent.objects.filter(action_code=action_codes.ENTRY_LOOKUP_THROTTLED)


# ---------------------------------------------------------------------------
# Limits and anomaly signals
# ---------------------------------------------------------------------------


class TestInvalidScanLimit:
    def test_signal_at_threshold_then_refusal_at_maximum(self, checkpoint, key, settings):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 2
        settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 3
        first = verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        assert first.reason_code == EntryReasonCode.INVALID_CREDENTIAL
        assert not _anomalies().exists()
        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        assert _anomalies().count() == 1
        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        assert _anomalies().count() == 1  # deduplicated within the window
        with pytest.raises(EntryLookupThrottled) as caught:
            verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        assert caught.value.kind == KIND_INVALID_SCANS
        throttle = _throttles().get()
        assert throttle.reason_code == KIND_INVALID_SCANS
        assert throttle.actor_user_id == checkpoint.user.pk
        assert _anomalies().count() == 1

    def test_valid_scans_are_never_limited(self, checkpoint, active_pass, settings):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 1
        token = factories.token_for(active_pass)
        for _ in range(5):
            outcome = verify_qr(checkpoint=checkpoint, raw_token=token)
            assert outcome.result == EntryResult.ALLOWED
        assert not _anomalies().exists() and not _throttles().exists()

    def test_window_moves_on(self, checkpoint, key, settings):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 1
        from apps.entry.services.limits import enforce_lookup_budget

        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        now = timezone.now()
        with pytest.raises(EntryLookupThrottled):
            enforce_lookup_budget(checkpoint=checkpoint, method="QR", now=now)
        # The audit trail is append-only, so the clock moves instead: once
        # the window has passed, the same history no longer refuses.
        later = now + timedelta(seconds=settings.ENTRY_INVALID_SCAN_WINDOW_SECONDS + 5)
        enforce_lookup_budget(checkpoint=checkpoint, method="QR", now=later)

    def test_limit_is_per_operator_not_per_session(
        self, checkpoint, key, device, operator, layout, settings
    ):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 1
        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        # Restarting the checkpoint session must not reset the budget.
        restarted = factories.open_checkpoint(device=device, user=operator, zone=layout.main)
        with pytest.raises(EntryLookupThrottled):
            verify_qr(checkpoint=restarted, raw_token=UNTRUSTED)


class TestIdentityReferenceLimit:
    def test_nin_reference_and_manual_share_one_budget(
        self, supervisor_checkpoint, registration, settings
    ):
        settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD = 2
        settings.ENTRY_LOOKUP_MAX_PER_WINDOW = 3
        cp = supervisor_checkpoint
        lookup_identity(checkpoint=cp, method=VerificationMethod.NIN, raw_value=SYNTHETIC_NIN)
        lookup_reference(checkpoint=cp, raw_reference=registration.public_reference)
        assert _anomalies().filter(reason_code=KIND_IDENTITY_LOOKUPS).count() == 1
        manual_search(checkpoint=cp, raw_query="Synthetic")
        with pytest.raises(EntryLookupThrottled) as caught:
            lookup_reference(checkpoint=cp, raw_reference=registration.public_reference)
        assert caught.value.kind == KIND_IDENTITY_LOOKUPS
        assert caught.value.retry_after_seconds == settings.ENTRY_LOOKUP_WINDOW_SECONDS

    def test_qr_does_not_consume_the_identity_budget(
        self, checkpoint, active_pass, registration, settings
    ):
        settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD = 1
        settings.ENTRY_LOOKUP_MAX_PER_WINDOW = 1
        token = factories.token_for(active_pass)
        for _ in range(3):
            verify_qr(checkpoint=checkpoint, raw_token=token)
        lookup_reference(checkpoint=checkpoint, raw_reference=registration.public_reference)

    def test_permission_is_checked_before_the_budget(self, checkpoint, settings):
        """A method the operator may not use is refused as a permission
        failure, and never charged or throttled."""
        from apps.entry.services import EntryPermissionError

        settings.ENTRY_LOOKUP_MAX_PER_WINDOW = 1
        for _ in range(3):
            with pytest.raises(EntryPermissionError):
                manual_search(checkpoint=checkpoint, raw_query="Synthetic")
        assert not _throttles().exists()


class TestThrottleOverHttp:
    def test_throttled_lookup_is_429_with_retry_after_and_no_result(self, signed_in, key, settings):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW = 1
        signed_in.post(reverse("entry:verify-qr"), {"token": UNTRUSTED})
        response = signed_in.post(reverse("entry:verify-qr"), {"token": UNTRUSTED})
        assert response.status_code == 429
        assert response["Retry-After"] == str(settings.ENTRY_INVALID_SCAN_WINDOW_SECONDS)
        body = response.content.decode()
        assert 'id="entry-throttled"' in body
        assert 'id="entry-result"' not in body
        assert UNTRUSTED not in body


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


class TestVerificationSamples:
    def test_every_lookup_records_one_identifier_free_sample(
        self, supervisor_checkpoint, active_pass, registration
    ):
        cp = supervisor_checkpoint
        verify_qr(checkpoint=cp, raw_token=factories.token_for(active_pass))
        verify_qr(checkpoint=cp, raw_token=UNTRUSTED)
        lookup_reference(checkpoint=cp, raw_reference=registration.public_reference)
        manual_search(checkpoint=cp, raw_query="Synthetic")
        samples = list(VerificationSample.objects.order_by("id"))
        assert [s.method for s in samples] == ["QR", "QR", "REFERENCE", "MANUAL"]
        assert samples[0].result == EntryResult.ALLOWED
        assert samples[0].credential_code == "VALID"
        assert samples[1].reason_code == EntryReasonCode.INVALID_CREDENTIAL
        assert samples[1].credential_code == "MALFORMED"
        for sample in samples:
            assert sample.gate_id == cp.gate.pk and sample.device_id == cp.device.pk
            assert sample.latency_ms >= 0
            assert sample.mode == "ONLINE"
        field_names = {f.name for f in VerificationSample._meta.get_fields()}
        assert field_names == {
            "id",
            "occurred_at",
            "event_edition",
            "gate",
            "zone",
            "device",
            "method",
            "mode",
            "result",
            "reason_code",
            "credential_code",
            "latency_ms",
        }

    def test_a_telemetry_failure_never_changes_the_result(
        self, checkpoint, active_pass, monkeypatch
    ):
        from django.db import DatabaseError

        def broken(**kwargs):
            raise DatabaseError("synthetic telemetry failure")

        monkeypatch.setattr(VerificationSample.objects, "create", broken)
        outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass))
        assert outcome.result == EntryResult.ALLOWED
        assert AuditEvent.objects.filter(
            action_code=action_codes.ENTRY_VERIFICATION_PERFORMED
        ).exists()

    def test_metric_log_line_carries_no_sensitive_value(
        self, supervisor_checkpoint, active_pass, person, caplog
    ):
        from apps.people.models import IdentifierType

        factories.add_identifier(
            person=person, identifier_type=IdentifierType.NIN, country="DZ", value=SYNTHETIC_NIN
        )
        token = factories.token_for(active_pass)
        with caplog.at_level(logging.INFO, logger="asc2026"):
            verify_qr(checkpoint=supervisor_checkpoint, raw_token=token)
            lookup_identity(
                checkpoint=supervisor_checkpoint,
                method=VerificationMethod.NIN,
                raw_value=SYNTHETIC_NIN,
            )
            manual_search(checkpoint=supervisor_checkpoint, raw_query="Synthetic Participant")
        metric_records = [r for r in caplog.records if r.name == "asc2026.metrics"]
        assert len(metric_records) == 3
        blob = json.dumps([vars(r) for r in caplog.records], default=str)
        for secret in (token, SYNTHETIC_NIN, "Synthetic Participant", person.display_name):
            assert secret not in blob
        assert str(active_pass.registration_id) not in blob
        sample_blob = json.dumps(list(VerificationSample.objects.values()), default=str)
        for secret in (token, SYNTHETIC_NIN, "Synthetic Participant"):
            assert secret not in sample_blob

    def test_anomaly_log_and_audit_carry_counts_and_coordinates_only(
        self, checkpoint, key, settings, caplog
    ):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        with caplog.at_level(logging.WARNING, logger="asc2026.entry.security"):
            verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        record = next(r for r in caplog.records if r.name == "asc2026.entry.security")
        assert record.anomaly == KIND_INVALID_SCANS
        assert record.gate == checkpoint.gate.code
        signal = _anomalies().get()
        assert set(signal.after_summary) == {
            "kind",
            "count",
            "window_seconds",
            "threshold",
            "gate",
            # Phase 3 Prompt 8 (P8-05): the exact Gate identity the monitor
            # isolates on -- a checkpoint coordinate, not participant data.
            "gate_id",
            "zone",
            "device",
        }
        assert signal.after_summary["gate_id"] == str(checkpoint.gate.pk)
        assert UNTRUSTED not in json.dumps([vars(r) for r in caplog.records], default=str)

    def test_prune_removes_only_expired_samples(self, checkpoint, settings):
        settings.ENTRY_METRICS_RETENTION_DAYS = 7
        now = timezone.now()
        for age_days in (1, 8):
            VerificationSample.objects.create(
                occurred_at=now - timedelta(days=age_days),
                event_edition=checkpoint.event_edition,
                gate=checkpoint.gate,
                zone=checkpoint.zone,
                device=checkpoint.device,
                method="QR",
                result="ALLOWED",
                latency_ms=10,
            )
        assert observability.prune_verification_samples(now=now) == 1
        assert VerificationSample.objects.count() == 1


def _samples(checkpoint, latencies, result="ALLOWED", method="QR", reason=""):
    now = timezone.now()
    VerificationSample.objects.bulk_create(
        VerificationSample(
            occurred_at=now,
            event_edition=checkpoint.event_edition,
            gate=checkpoint.gate,
            zone=checkpoint.zone,
            device=checkpoint.device,
            method=method,
            result=result,
            reason_code=reason,
            latency_ms=latency,
        )
        for latency in latencies
    )


class TestGateHealth:
    def test_too_few_samples_is_healthy(self, checkpoint):
        _samples(checkpoint, [5000] * (settings.ENTRY_DEGRADED_MIN_SAMPLES - 1))
        assert not observability.gate_health(checkpoint).degraded
        assert observability.degraded_notice_for(checkpoint) == ""

    def test_slow_p95_is_degraded(self, checkpoint):
        _samples(checkpoint, [200] * 3 + [2500] * 3)
        health = observability.gate_health(checkpoint)
        assert health.slow and not health.failing and health.p95_ms == 2500
        assert "longer than usual" in observability.degraded_notice_for(checkpoint)

    def test_technical_error_burst_is_degraded(self, checkpoint):
        _samples(checkpoint, [50] * 4)
        _samples(checkpoint, [50] * 2, result=EntryResult.TECHNICAL_ERROR)
        assert observability.gate_health(checkpoint).failing
        assert "technical error" in observability.degraded_notice_for(checkpoint)

    def test_other_gates_do_not_affect_this_gate(self, checkpoint, layout):
        _samples(checkpoint, [2500] * 6)
        VerificationSample.objects.update(gate=layout.gate_b)
        assert not observability.gate_health(checkpoint).degraded

    def test_nearest_rank_percentile(self):
        assert observability.percentile([], 0.95) is None
        assert observability.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
        assert observability.percentile([10, 20, 30, 40], 0.5) == 20


class TestSnapshot:
    def test_snapshot_reports_every_required_metric_family(self, checkpoint):
        _samples(checkpoint, [100, 200, 300, 400])
        _samples(checkpoint, [50], result=EntryResult.DENIED, reason=EntryReasonCode.PASS_REVOKED)
        data = observability.snapshot(event_edition=checkpoint.event_edition)
        verification = data["verification"]
        assert verification["total"] == 5
        assert verification["p95_ms"] is not None and verification["within_target"]
        assert {row["result"] for row in verification["by_result"]} == {"ALLOWED", "DENIED"}
        assert verification["pass_failures"][0]["reason"] == EntryReasonCode.PASS_REVOKED
        assert verification["by_gate"][0]["gate"] == checkpoint.gate.code
        assert data["devices"]["enrolled"] == 1
        assert data["devices"]["open_checkpoint_sessions"] == 1
        assert set(data["queue"]) == {"pending", "failed", "oldest_pending_age_seconds"}
        assert "adjustments" in data["stock"] and "issuances_lost" in data["stock"]
        assert data["anomalies"]["signals"] == 0
        # Nothing identifying reaches the read model.
        blob = json.dumps(data, default=str)
        assert checkpoint.user.email_normalized not in blob

    def test_stale_devices_are_reported(self, checkpoint, settings):
        from apps.entry.models import EntryDevice

        EntryDevice.objects.update(
            last_seen_at=timezone.now() - timedelta(seconds=settings.ENTRY_DEVICE_STALE_SECONDS + 1)
        )
        health = observability.device_health(event_edition=checkpoint.event_edition)
        assert health["enrolled_stale"] == 1


# ---------------------------------------------------------------------------
# Passive connection status
# ---------------------------------------------------------------------------


class TestPassiveStatus:
    def test_online_state_word_only(self, signed_in):
        response = signed_in.get(reverse("entry:status"))
        assert response.status_code == 200
        assert response.json() == {"state": "online"}
        assert "no-store" in response["Cache-Control"] or "no-cache" in response["Cache-Control"]

    def test_status_never_refreshes_either_inactivity_clock(self, signed_in, operator):
        from apps.accounts import session_expiry

        past = timezone.now() - timedelta(minutes=3)
        EntryOperatorSession.objects.filter(user=operator).update(last_activity_at=past)
        session = signed_in.session
        session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = past.isoformat()
        session.save()
        assert signed_in.get(reverse("entry:status")).status_code == 200
        assert EntryOperatorSession.objects.get(user=operator).last_activity_at == past
        assert (
            signed_in.session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] == past.isoformat()
        )
        # An ordinary page request, by contrast, is activity.
        signed_in.get(reverse("entry:verify"))
        assert EntryOperatorSession.objects.get(user=operator).last_activity_at > past

    def test_idle_session_still_expires_under_polling(self, signed_in, operator, settings):
        past = timezone.now() - timedelta(seconds=settings.ENTRY_OPERATOR_INACTIVITY_SECONDS + 1)
        EntryOperatorSession.objects.filter(user=operator).update(last_activity_at=past)
        response = signed_in.get(reverse("entry:status"))
        assert response.status_code == 403
        assert response.json() == {"state": "session-ended"}
        assert EntryOperatorSession.objects.get(user=operator).ended_at is not None

    def test_signed_out_is_session_ended(self, client):
        response = client.get(reverse("entry:status"))
        assert response.status_code == 401
        assert response.json() == {"state": "session-ended"}

    def test_degraded_gate(self, signed_in, operator):
        from apps.entry.models import EntryDeviceSession

        device_session = EntryDeviceSession.objects.get(ended_at__isnull=True)
        VerificationSample.objects.bulk_create(
            VerificationSample(
                occurred_at=timezone.now(),
                event_edition_id=device_session.event_edition_id,
                gate_id=device_session.gate_id,
                zone_id=device_session.zone_id,
                device_id=device_session.device_id,
                method="QR",
                result="ALLOWED",
                latency_ms=4000,
            )
            for _ in range(6)
        )
        assert signed_in.get(reverse("entry:status")).json() == {"state": "degraded"}
        page = signed_in.get(reverse("entry:verify")).content.decode()
        assert 'id="entry-degraded-server"' in page
        assert 'data-state="degraded"' in page


# ---------------------------------------------------------------------------
# Screens: validation, scope, presentation
# ---------------------------------------------------------------------------


class TestVerifyScreen:
    def test_invalid_lookup_re_renders_with_field_errors_and_no_echo(self, signed_in):
        response = signed_in.post(
            reverse("entry:lookup-identity"), {"method": "PASSPORT", "value": "SYNTHPASS77"}
        )
        assert response.status_code == 400
        body = response.content.decode()
        assert 'id="error-summary"' in body
        assert 'aria-invalid="true"' in body
        assert "id_country_code_error" in body
        assert "SYNTHPASS77" not in body
        assert response.context["active_tab"] == "identity"
        # Nothing was looked up, audited, or measured for an invalid form.
        assert not AuditEvent.objects.filter(
            action_code=action_codes.ENTRY_IDENTITY_LOOKUP
        ).exists()
        assert not VerificationSample.objects.exists()

    def test_empty_qr_submission_is_a_specific_error(self, signed_in):
        response = signed_in.post(reverse("entry:verify-qr"), {"token": ""})
        assert response.status_code == 400
        assert "Scan the QR code, or type the code printed under it." in response.content.decode()

    def test_checkpoint_identity_and_device_scope_are_shown(self, signed_in, layout):
        body = signed_in.get(reverse("entry:verify")).content.decode()
        assert layout.gate_a.code in body
        assert "Device scope" in body
        assert "Digital Entry Pass QR" in body
        assert 'id="entry-connection"' in body
        assert "Offline verification" in body  # readiness placeholder
        assert "Not enabled" in body

    def test_every_result_has_a_distinct_icon_and_a_tone(self):
        from apps.entry.views import RESULT_PRESENTATION

        assert set(RESULT_PRESENTATION) == set(EntryResult.values)
        icons = [icon for _tone, icon, _heading in RESULT_PRESENTATION.values()]
        assert len(icons) == len(set(icons))
        for tone, _icon, heading in RESULT_PRESENTATION.values():
            assert tone in {"success", "info", "warning", "danger", "neutral"}
            assert str(heading)

    def test_result_page_carries_the_announcement_regions(self, signed_in, active_pass):
        response = signed_in.post(
            reverse("entry:verify-qr"), {"token": factories.token_for(active_pass)}
        )
        body = response.content.decode()
        assert 'id="entry-announcer-polite"' in body
        assert 'id="entry-announcer-assertive"' in body
        assert 'data-announce="polite"' in body
        assert "No reason" not in body


# ---------------------------------------------------------------------------
# Performance guards: query counts and the indexed path
# ---------------------------------------------------------------------------


class TestQueryBudget:
    """Guards against N+1 regressions on the hot path. The ceilings are
    today's measured counts plus a small margin; raising one needs a reason."""

    def test_qr_verification_query_ceiling(self, checkpoint, active_pass):
        token = factories.token_for(active_pass)
        verify_qr(checkpoint=checkpoint, raw_token=token)  # warm caches
        with CaptureQueriesContext(connection) as ctx:
            verify_qr(checkpoint=checkpoint, raw_token=token)
        # Measured 18 on 2026-09-23.
        assert len(ctx.captured_queries) <= 22, len(ctx.captured_queries)

    def test_reference_lookup_query_ceiling(self, checkpoint, registration, active_pass):
        lookup_reference(checkpoint=checkpoint, raw_reference=registration.public_reference)
        with CaptureQueriesContext(connection) as ctx:
            lookup_reference(checkpoint=checkpoint, raw_reference=registration.public_reference)
        # Measured 19 on 2026-09-23.
        assert len(ctx.captured_queries) <= 23, len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_event_size(
        self, checkpoint, active_pass, event, setup, staff
    ):
        token = factories.token_for(active_pass)
        verify_qr(checkpoint=checkpoint, raw_token=token)
        with CaptureQueriesContext(connection) as small:
            verify_qr(checkpoint=checkpoint, raw_token=token)
        for index in range(15):
            other = factories.make_registration(
                event=event, person=factories.make_person(f"Synthetic Filler {index}")
            )
            factories.assign(registration=other, setup=setup, actor=staff)
        with CaptureQueriesContext(connection) as large:
            verify_qr(checkpoint=checkpoint, raw_token=token)
        assert len(large.captured_queries) == len(small.captured_queries)

    def test_verify_page_query_ceiling(self, signed_in):
        signed_in.get(reverse("entry:verify"))
        with CaptureQueriesContext(connection) as ctx:
            assert signed_in.get(reverse("entry:verify")).status_code == 200
        # Measured 21 on 2026-09-23.
        assert len(ctx.captured_queries) <= 26, len(ctx.captured_queries)

    def test_qr_request_end_to_end_query_ceiling(self, signed_in, active_pass):
        token = factories.token_for(active_pass)
        signed_in.post(reverse("entry:verify-qr"), {"token": token})
        with CaptureQueriesContext(connection) as ctx:
            assert signed_in.post(reverse("entry:verify-qr"), {"token": token}).status_code == 200
        # Measured 34 on 2026-09-23 (session, checkpoint chain, verification,
        # audit, sample, limits, decision ticket, result projection).
        assert len(ctx.captured_queries) <= 40, len(ctx.captured_queries)


class TestIndexedPath:
    """The PostgreSQL planner uses an index for every lookup key on the
    verification path. `enable_seqscan = off` makes the planner prefer any
    usable index, so a plan still containing a Seq Scan proves none exists."""

    @staticmethod
    def _plan(sql: str, params) -> str:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("EXPLAIN " + sql, params)
            return "\n".join(row[0] for row in cursor.fetchall())

    def _assert_indexed(self, queryset, expected_index: str | None = None):
        sql, params = queryset.query.sql_with_params()
        plan = self._plan(sql, params)
        assert "Seq Scan" not in plan, plan
        assert "Index" in plan, plan
        if expected_index is not None:
            assert expected_index in plan, plan

    def test_case_insensitive_reference_lookup_uses_the_expression_index(self, registration):
        from apps.registrations.models import Registration

        self._assert_indexed(
            Registration.objects.filter(public_reference__iexact="ENT-SYNTH"),
            "reg_public_ref_upper_idx",
        )

    def test_credential_lookup_by_jti_is_indexed(self, active_pass):
        from apps.badges.models import DigitalEntryPass

        self._assert_indexed(DigitalEntryPass.objects.filter(jti=active_pass.jti))

    def test_rate_limit_counter_uses_the_actor_index(self, operator):
        qs = AuditEvent.objects.filter(
            actor_user_id=operator.pk,
            occurred_at__gte=timezone.now() - timedelta(minutes=5),
            action_code=action_codes.ENTRY_IDENTITY_LOOKUP,
        )
        self._assert_indexed(qs)

    def test_gate_health_uses_the_gate_index(self, layout):
        qs = VerificationSample.objects.filter(
            gate=layout.gate_a, occurred_at__gte=timezone.now() - timedelta(minutes=5)
        ).order_by("-occurred_at")
        self._assert_indexed(qs, "entry_sample_gate_idx")


class TestNoSynchronousExternalProvider:
    def test_verification_makes_no_outbound_network_call(
        self, supervisor_checkpoint, active_pass, registration, person, monkeypatch
    ):
        import socket

        from apps.people.models import IdentifierType

        factories.add_identifier(
            person=person, identifier_type=IdentifierType.NIN, country="DZ", value=SYNTHETIC_NIN
        )
        db_socket_create = socket.create_connection

        def forbidden(*args, **kwargs):  # pragma: no cover - fails the test if reached
            raise AssertionError(f"outbound connection attempted: {args!r}")

        monkeypatch.setattr(socket, "create_connection", forbidden)
        cp = supervisor_checkpoint
        verify_qr(checkpoint=cp, raw_token=factories.token_for(active_pass))
        lookup_identity(checkpoint=cp, method=VerificationMethod.NIN, raw_value=SYNTHETIC_NIN)
        lookup_reference(checkpoint=cp, raw_reference=registration.public_reference)
        manual_search(checkpoint=cp, raw_query="Synthetic")
        monkeypatch.setattr(socket, "create_connection", db_socket_create)


class TestSettingsChecks:
    def test_defaults_pass(self):
        assert [e for e in run_checks() if e.id and e.id.startswith("entry.")] == []

    def test_threshold_above_maximum_fails(self, settings):
        settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD = settings.ENTRY_LOOKUP_MAX_PER_WINDOW + 1
        assert "entry.E002" in {e.id for e in run_checks()}

    def test_status_path_must_stay_passive(self, settings):
        settings.OPERATIONAL_PASSIVE_PATHS = ()
        assert "entry.E004" in {e.id for e in run_checks()}


class TestObservabilityDashboard:
    def test_device_administrator_sees_metrics_without_identifiers(
        self, client, event, device_admin, checkpoint, active_pass, person
    ):
        from apps.entry.tests.test_views import _sign_in

        verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass))
        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED)
        _sign_in(client, device_admin)
        response = client.get(reverse("entry:observability", args=[event.pk]))
        assert response.status_code == 200
        body = response.content.decode()
        assert 'data-metric="p95"' in body
        assert response.context["data"]["verification"]["total"] == 2
        for secret in (person.display_name, active_pass.registration.public_reference, UNTRUSTED):
            assert secret not in body
        assert "no-store" in response["Cache-Control"] or "no-cache" in response["Cache-Control"]

    def test_checkpoint_operator_cannot_open_the_dashboard(self, client, event, operator):
        from apps.entry.tests.test_views import _sign_in

        _sign_in(client, operator)
        response = client.get(reverse("entry:observability", args=[event.pk]))
        assert response.status_code in (302, 403, 404)

    def test_other_event_is_not_found(self, client, event, other_event, device_admin):
        from apps.entry.tests.test_views import _sign_in

        _sign_in(client, device_admin)
        assert client.get(reverse("entry:observability", args=[other_event.pk])).status_code == 404

    def test_unknown_window_falls_back_to_the_default(self, client, event, device_admin):
        from apps.entry.tests.test_views import _sign_in

        _sign_in(client, device_admin)
        response = client.get(reverse("entry:observability", args=[event.pk]), {"window": "7"})
        assert response.context["window"] == settings.ENTRY_METRICS_DEFAULT_WINDOW_SECONDS

    def test_monitor_shows_labelled_attempts_and_gate_anomalies(
        self, client, supervisor, device_and_secret, layout, key, settings
    ):
        settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = 1
        _start(client, supervisor, device_and_secret[1], layout.main)
        client.post(reverse("entry:verify-qr"), {"token": UNTRUSTED})
        body = client.get(reverse("entry:monitor")).content.decode()
        assert "Repeated invalid scans" in body
        assert "Credential not valid" in body  # a label, not the raw code
        assert UNTRUSTED not in body


class TestCommands:
    def test_entry_metrics_prints_an_identifier_free_json_snapshot(
        self, checkpoint, active_pass, person
    ):
        from io import StringIO

        from django.core.management import call_command

        verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass))
        out = StringIO()
        call_command("entry_metrics", checkpoint.event_edition.code, stdout=out)
        data = json.loads(out.getvalue())
        assert data["verification"]["total"] == 1
        assert person.display_name not in out.getvalue()

    def test_entry_metrics_rejects_an_unknown_event(self):
        from django.core.management import CommandError, call_command

        with pytest.raises(CommandError):
            call_command("entry_metrics", "NOPE")

    def test_prune_task_is_idempotent(self, checkpoint):
        from apps.entry.tasks import prune_verification_samples_task

        assert prune_verification_samples_task() == 0
        assert prune_verification_samples_task() == 0
