"""Online entry observability (Phase 3 Prompt 5, ADR-0022).

Three responsibilities, all identifier-free:

1. **Recording** -- `record_verification()` measures one online lookup
   (server-side latency from the start of the lookup to its audited
   outcome), stores a `VerificationSample`, and emits one structured
   `asc2026.metrics` log line for an external metrics pipeline (TRD §23.1).
   Recording is best-effort: a telemetry failure is logged and swallowed and
   can never change, delay, or fail a verification.
2. **Gate health** -- `gate_health()` / `degraded_notice_for()` turn this
   gate's recent samples into the degraded-online state the checkpoint
   shows (UI/UX §9.7): slow verifications or a technical-error burst.
3. **Read models** for the operations dashboard and the `entry_metrics`
   command: verification latency percentiles and result codes, pass
   failures, device health, queue depth, stock exceptions, anomaly signals.

What never appears in any sample, log line, or read model here: a person,
registration, pass or credential identifier, token, identity value,
reference, search text, photograph, or operator identity. Only checkpoint
coordinates (event, gate, zone, device), methods, codes, counts, and times.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Aggregate, Count, FloatField, Max, Min, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.entry.models import (
    EntryDevice,
    EntryDeviceSession,
    EntryDeviceStatus,
    EntryReasonCode,
    EntryResult,
    VerificationMethod,
    VerificationSample,
)

logger = logging.getLogger(__name__)
metrics_logger = logging.getLogger("asc2026.metrics")

#: QR outcomes that are a failure of the presented pass itself (as opposed
#: to an access-rule or registration decision): the "pass failures" metric.
PASS_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        EntryReasonCode.INVALID_CREDENTIAL,
        EntryReasonCode.UNSUPPORTED_CREDENTIAL,
        EntryReasonCode.WRONG_EVENT,
        EntryReasonCode.PASS_REVOKED,
        EntryReasonCode.PASS_REPLACED,
        EntryReasonCode.PASS_SUSPENDED,
        EntryReasonCode.PASS_INACTIVE,
        EntryReasonCode.PASS_EXPIRED,
        EntryReasonCode.PASS_NOT_YET_VALID,
    }
)


# ---------------------------------------------------------------------------
# 1. Recording
# ---------------------------------------------------------------------------


def start_timer() -> float:
    return time.perf_counter()


def elapsed_ms(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def record_verification(
    *,
    checkpoint,
    method: str,
    result: str,
    reason_code: str,
    started: float,
    credential_code: str = "",
    now=None,
) -> None:
    """Store one sample and emit one metric log line. Never raises."""
    latency_ms = elapsed_ms(started)
    now = now or timezone.now()
    try:
        # A savepoint, so a telemetry failure can never poison a caller's
        # enclosing transaction.
        with transaction.atomic():
            VerificationSample.objects.create(
                occurred_at=now,
                event_edition_id=checkpoint.event_edition.pk,
                gate_id=checkpoint.gate.pk,
                zone_id=checkpoint.zone.pk,
                device_id=checkpoint.device.pk,
                method=method,
                result=result,
                reason_code=reason_code or "",
                credential_code=(credential_code or "")[:32],
                latency_ms=latency_ms,
            )
    except DatabaseError:
        logger.warning("Entry verification sample could not be stored.", exc_info=True)
    metrics_logger.info(
        "entry.verification",
        extra={
            "metric": "entry_verification",
            "method": method,
            "result": result,
            "reason": reason_code or "",
            "credential_code": credential_code or "",
            "latency_ms": latency_ms,
            "event": checkpoint.event_edition.code,
            "gate": checkpoint.gate.code,
            "zone": checkpoint.zone.code,
            "device": checkpoint.device.public_id,
            "origin": "cloud",
        },
    )


# ---------------------------------------------------------------------------
# 2. Gate health (degraded-online state)
# ---------------------------------------------------------------------------


def percentile(values, fraction: float) -> int | None:
    """Nearest-rank percentile of integer values (None when empty)."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class GateHealth:
    samples: int
    p95_ms: int | None
    technical_error_rate: float
    slow: bool
    failing: bool

    @property
    def degraded(self) -> bool:
        return self.slow or self.failing


def gate_health(checkpoint, *, now=None) -> GateHealth:
    """This gate's recent verification health, from at most 100 samples."""
    now = now or timezone.now()
    since = now - timedelta(seconds=int(settings.ENTRY_DEGRADED_WINDOW_SECONDS))
    rows = list(
        VerificationSample.objects.filter(gate_id=checkpoint.gate.pk, occurred_at__gte=since)
        .order_by("-occurred_at")
        .values_list("latency_ms", "result")[:100]
    )
    if len(rows) < int(settings.ENTRY_DEGRADED_MIN_SAMPLES):
        return GateHealth(len(rows), None, 0.0, slow=False, failing=False)
    p95 = percentile([latency for latency, _result in rows], 0.95)
    errors = sum(1 for _latency, result in rows if result == EntryResult.TECHNICAL_ERROR)
    rate = errors / len(rows)
    return GateHealth(
        samples=len(rows),
        p95_ms=p95,
        technical_error_rate=rate,
        slow=p95 is not None and p95 > int(settings.ENTRY_VERIFICATION_TARGET_P95_MS),
        failing=rate * 100 >= int(settings.ENTRY_DEGRADED_ERROR_PERCENT),
    )


def degraded_notice_for(checkpoint, *, now=None) -> str:
    """The localized degraded-online message for this gate, or ""."""
    health = gate_health(checkpoint, now=now)
    if health.failing:
        return _(
            "Several recent verifications at this gate failed with a technical error. "
            "Verify again, and call your supervisor if it continues."
        )
    if health.slow:
        return _(
            "Recent verifications at this gate took longer than usual. Wait for each "
            "result before deciding; never admit without one."
        )
    return ""


# ---------------------------------------------------------------------------
# 3. Read models (dashboard, `entry_metrics` command)
# ---------------------------------------------------------------------------


class PercentileCont(Aggregate):
    """PostgreSQL `percentile_cont(p) WITHIN GROUP (ORDER BY expr)`."""

    function = "percentile_cont"
    template = "%(function)s(%(percentile)s) WITHIN GROUP (ORDER BY %(expressions)s)"
    output_field = FloatField()

    def __init__(self, expression, percentile: float, **extra):
        if not 0 <= percentile <= 1:
            raise ValueError("percentile must be within [0, 1].")
        super().__init__(expression, percentile=float(percentile), **extra)


def _round(value) -> int | None:
    return None if value is None else int(round(value))


def verification_summary(*, event_edition, since, until=None) -> dict:
    """Latency percentiles, counts by method/result, and pass failures."""
    samples = VerificationSample.objects.filter(event_edition=event_edition, occurred_at__gte=since)
    if until is not None:
        samples = samples.filter(occurred_at__lt=until)
    overall = samples.aggregate(
        total=Count("id"),
        p50=PercentileCont("latency_ms", 0.5),
        p95=PercentileCont("latency_ms", 0.95),
        p99=PercentileCont("latency_ms", 0.99),
        max=Max("latency_ms"),
    )
    by_method = [
        {
            "method": row["method"],
            "label": VerificationMethod(row["method"]).label,
            "count": row["count"],
            "p50_ms": _round(row["p50"]),
            "p95_ms": _round(row["p95"]),
        }
        for row in samples.values("method")
        .annotate(
            count=Count("id"),
            p50=PercentileCont("latency_ms", 0.5),
            p95=PercentileCont("latency_ms", 0.95),
        )
        .order_by("method")
    ]
    by_result = [
        {"result": row["result"], "label": EntryResult(row["result"]).label, "count": row["count"]}
        for row in samples.values("result").annotate(count=Count("id")).order_by("-count")
    ]
    # Grouped by the exact Gate identity (P8-05). A gate code is unique only
    # within a venue, so grouping by code merged two venues' `G1` into one
    # row. The codes are carried as display labels only.
    by_gate = [
        {
            "gate_id": str(row["gate_id"]),
            "gate": row["gate__code"],
            "venue": row["gate__venue__code"],
            "label": f"{row['gate__venue__code']} / {row['gate__code']}",
            "count": row["count"],
            "p95_ms": _round(row["p95"]),
            "technical_errors": row["technical_errors"],
        }
        for row in samples.values("gate_id", "gate__code", "gate__venue__code")
        .annotate(
            count=Count("id"),
            p95=PercentileCont("latency_ms", 0.95),
            technical_errors=Count("id", filter=Q(result=EntryResult.TECHNICAL_ERROR)),
        )
        .order_by("gate__venue__code", "gate__code", "gate_id")
    ]
    pass_failures = [
        {
            "reason": row["reason_code"],
            "label": EntryReasonCode(row["reason_code"]).label,
            "credential_code": row["credential_code"],
            "count": row["count"],
        }
        for row in samples.filter(
            method=VerificationMethod.QR, reason_code__in=list(PASS_FAILURE_REASONS)
        )
        .values("reason_code", "credential_code")
        .annotate(count=Count("id"))
        .order_by("-count", "reason_code", "credential_code")
    ]
    target = int(settings.ENTRY_VERIFICATION_TARGET_P95_MS)
    p95 = _round(overall["p95"])
    return {
        "total": overall["total"],
        "p50_ms": _round(overall["p50"]),
        "p95_ms": p95,
        "p99_ms": _round(overall["p99"]),
        "max_ms": overall["max"],
        "target_p95_ms": target,
        "within_target": p95 is None or p95 <= target,
        "by_method": by_method,
        "by_result": by_result,
        "by_gate": by_gate,
        "pass_failures": pass_failures,
    }


def device_health(*, event_edition, now=None) -> dict:
    """Device counts by status, stale enrolled devices, open sessions."""
    now = now or timezone.now()
    stale_before = now - timedelta(seconds=int(settings.ENTRY_DEVICE_STALE_SECONDS))
    devices = EntryDevice.objects.filter(event_edition=event_edition)
    by_status = {
        row["status"]: row["count"] for row in devices.values("status").annotate(count=Count("id"))
    }
    # OFFLINE_READY devices operate online exactly like ENROLLED ones
    # (Phase 4 Prompt 2), so both count as enrolled here.
    enrolled = devices.filter(
        status__in=(EntryDeviceStatus.ENROLLED, EntryDeviceStatus.OFFLINE_READY)
    )
    open_sessions = EntryDeviceSession.objects.filter(
        event_edition=event_edition, ended_at__isnull=True, expires_at__gt=now
    )
    from apps.entry.models import OfflinePackage, OfflinePackageStatus

    ready_packages = OfflinePackage.objects.filter(
        event_edition=event_edition, status=OfflinePackageStatus.READY, expires_at__gt=now
    )
    return {
        "by_status": [
            {"status": value, "label": label, "count": by_status.get(value, 0)}
            for value, label in EntryDeviceStatus.choices
        ],
        "enrolled": by_status.get(EntryDeviceStatus.ENROLLED, 0)
        + by_status.get(EntryDeviceStatus.OFFLINE_READY, 0),
        # Offline readiness (Phase 4 Prompt 2): counts only.
        "offline_ready": by_status.get(EntryDeviceStatus.OFFLINE_READY, 0),
        "offline_packages_expiring_within_hour": ready_packages.filter(
            expires_at__lte=now + timedelta(hours=1)
        ).count(),
        "devices_reporting_unacknowledged": devices.filter(
            Q(reported_pending_operations__gt=0) | Q(reported_locked_operations__gt=0)
        ).count(),
        "enrolled_stale": enrolled.filter(
            Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=stale_before)
        ).count(),
        "stale_after_seconds": int(settings.ENTRY_DEVICE_STALE_SECONDS),
        "expiring_within_day": enrolled.filter(expires_at__lte=now + timedelta(days=1)).count(),
        "open_checkpoint_sessions": open_sessions.count(),
    }


def queue_depth(*, now=None) -> dict:
    """Reliable side-effect backlog: the transactional outbox (ADR-0008 family).

    The Celery broker's own queue length is an infrastructure metric of the
    broker (Redis) and is not read from the application.
    """
    from apps.core.models import OutboxEvent, OutboxEventStatus

    now = now or timezone.now()
    pending = OutboxEvent.objects.filter(status=OutboxEventStatus.PENDING)
    aggregates = pending.aggregate(count=Count("id"), oldest=Min("occurred_at"))
    oldest = aggregates["oldest"]
    return {
        "pending": aggregates["count"],
        "failed": OutboxEvent.objects.filter(status=OutboxEventStatus.FAILED).count(),
        "oldest_pending_age_seconds": (
            max(0, int((now - oldest).total_seconds())) if oldest is not None else None
        ),
    }


def stock_exceptions(*, event_edition) -> dict:
    """Generic badge stock exceptions -- quantities only, no participant data."""
    from apps.badges.models.stock import (
        BadgeIssuance,
        BadgeIssuanceStatus,
        BadgeStockEntryType,
        BadgeStockLedgerEntry,
        PrintBatch,
        StockAdjustmentReasonCode,
        StockReconciliation,
    )

    adjustments = BadgeStockLedgerEntry.objects.filter(
        event_edition=event_edition, entry_type=BadgeStockEntryType.ADJUSTMENT
    )
    by_reason = {
        row["reason_code"]: row
        for row in adjustments.values("reason_code").annotate(
            count=Count("id"), net=Sum("quantity_delta")
        )
    }
    discrepancies = StockReconciliation.objects.filter(event_edition=event_edition).exclude(
        discrepancy=0
    )
    ended = BadgeIssuance.objects.filter(
        registration__event_edition=event_edition,
        status__in=[
            BadgeIssuanceStatus.LOST,
            BadgeIssuanceStatus.VOIDED,
            BadgeIssuanceStatus.REPLACED,
        ],
    )
    ended_by_status = {
        row["status"]: row["count"] for row in ended.values("status").annotate(count=Count("id"))
    }
    return {
        "adjustments": [
            {
                "reason": value,
                "label": label,
                "count": by_reason.get(value, {}).get("count", 0),
                "net_quantity": by_reason.get(value, {}).get("net") or 0,
            }
            for value, label in StockAdjustmentReasonCode.choices
        ],
        "damaged_at_receipt": PrintBatch.objects.filter(event_edition=event_edition).aggregate(
            total=Sum("damaged_quantity")
        )["total"]
        or 0,
        "reconciliation_discrepancies": discrepancies.count(),
        "reconciliation_net_discrepancy": discrepancies.aggregate(net=Sum("discrepancy"))["net"]
        or 0,
        "issuances_lost": ended_by_status.get(BadgeIssuanceStatus.LOST, 0),
        "issuances_voided": ended_by_status.get(BadgeIssuanceStatus.VOIDED, 0),
        "issuances_replaced": ended_by_status.get(BadgeIssuanceStatus.REPLACED, 0),
    }


def anomaly_signals(*, event_edition, since, limit: int = 25) -> dict:
    """Recent anomaly signals and throttled lookups (checkpoint coordinates only)."""
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent

    events = AuditEvent.objects.filter(
        event_edition=event_edition,
        occurred_at__gte=since,
        action_code__in=[action_codes.ENTRY_ANOMALY_SIGNAL, action_codes.ENTRY_LOOKUP_THROTTLED],
    )
    counts = {
        (row["action_code"], row["reason_code"]): row["count"]
        for row in events.values("action_code", "reason_code").annotate(count=Count("id"))
    }
    recent = [
        {
            "occurred_at": row["occurred_at"],
            "type": "ANOMALY"
            if row["action_code"] == action_codes.ENTRY_ANOMALY_SIGNAL
            else "THROTTLED",
            "kind": row["reason_code"],
            "gate": (row["after_summary"] or {}).get("gate", ""),
            "device": (row["after_summary"] or {}).get("device", ""),
            "count": (row["after_summary"] or {}).get("count"),
        }
        for row in events.order_by("-occurred_at").values(
            "occurred_at", "action_code", "reason_code", "after_summary"
        )[:limit]
    ]
    return {
        "signals": sum(
            v for (code, _k), v in counts.items() if code == action_codes.ENTRY_ANOMALY_SIGNAL
        ),
        "throttled": sum(
            v for (code, _k), v in counts.items() if code == action_codes.ENTRY_LOOKUP_THROTTLED
        ),
        "recent": recent,
    }


def snapshot(*, event_edition, window_seconds: int | None = None, now=None) -> dict:
    """Every read model at once, for the dashboard and the metrics command."""
    now = now or timezone.now()
    window = int(window_seconds or settings.ENTRY_METRICS_DEFAULT_WINDOW_SECONDS)
    since = now - timedelta(seconds=window)
    return {
        "generated_at": now,
        "window_seconds": window,
        "verification": verification_summary(event_edition=event_edition, since=since),
        "devices": device_health(event_edition=event_edition, now=now),
        "queue": queue_depth(now=now),
        "stock": stock_exceptions(event_edition=event_edition),
        "anomalies": anomaly_signals(event_edition=event_edition, since=since),
    }


def prune_verification_samples(*, now=None) -> int:
    """Delete samples older than `ENTRY_METRICS_RETENTION_DAYS`."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=int(settings.ENTRY_METRICS_RETENTION_DAYS))
    deleted, _detail = VerificationSample.objects.filter(occurred_at__lt=cutoff).delete()
    return deleted


__all__ = [
    "PASS_FAILURE_REASONS",
    "GateHealth",
    "anomaly_signals",
    "degraded_notice_for",
    "device_health",
    "gate_health",
    "percentile",
    "prune_verification_samples",
    "queue_depth",
    "record_verification",
    "snapshot",
    "start_timer",
    "stock_exceptions",
    "verification_summary",
]
