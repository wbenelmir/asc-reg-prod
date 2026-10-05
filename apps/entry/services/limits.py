"""Abuse limits and anomaly signals for checkpoint lookups (Phase 3 Prompt 5, ADR-0022).

Two independent protections, both per named operator (the audit actor):

* **Identity-reference lookups** (NIN, passport, registration reference,
  controlled manual search): at most `ENTRY_LOOKUP_MAX_PER_WINDOW` per
  `ENTRY_LOOKUP_WINDOW_SECONDS`. These are the lookups that could be used to
  probe whether a person is registered; the Digital Entry Pass QR is not
  limited on volume.
* **Repeated invalid scans** (a QR that is not a trustworthy credential, or
  an unsupported format): at most `ENTRY_INVALID_SCAN_MAX_PER_WINDOW` per
  `ENTRY_INVALID_SCAN_WINDOW_SECONDS`. Valid scans never count toward this
  budget; once it is exhausted, that operator's QR verification is paused
  until the window moves on (other operators are unaffected).

Crossing the lower `*_ANOMALY_THRESHOLD` first raises an **anomaly signal**:
one audit event (`ENT_ANOMALY_SIGNAL`, deduplicated per operator, kind and
window) plus one WARNING log line for alerting. Exceeding the limit refuses
further lookups of that kind until the window moves on, audited as
`ENT_LOOKUP_THROTTLED`; the operator is told to call a supervisor.

Counting uses the audit trail itself -- every lookup is already audited --
through the indexed `(actor_user, occurred_at)` path, exactly like the
approved fallback-reference budget (`apps.badges.services`). As there, the
limit is a best-effort ceiling: concurrent requests by the SAME operator may
overshoot it by at most their concurrency, which a single checkpoint device
does not produce in practice. A limit never changes a verification result:
a refused lookup produces no result at all, never a false DENIED.

Nothing here reads or records a token, identity value, reference, or search
string -- only counts, the method, and checkpoint coordinates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import EntryReasonCode, VerificationMethod
from apps.entry.services import EntryServiceError, audit

logger = logging.getLogger("asc2026.entry.security")

IDENTITY_REFERENCE_METHODS: frozenset[str] = frozenset(
    {
        VerificationMethod.NIN,
        VerificationMethod.PASSPORT,
        VerificationMethod.REFERENCE,
        VerificationMethod.MANUAL,
    }
)
_IDENTITY_REFERENCE_ACTIONS = (
    action_codes.ENTRY_IDENTITY_LOOKUP,
    action_codes.ENTRY_REFERENCE_LOOKUP,
    action_codes.ENTRY_MANUAL_SEARCH,
)
INVALID_SCAN_REASONS: frozenset[str] = frozenset(
    {EntryReasonCode.INVALID_CREDENTIAL, EntryReasonCode.UNSUPPORTED_CREDENTIAL}
)

KIND_IDENTITY_LOOKUPS = "IDENTITY_REFERENCE_LOOKUPS"
KIND_INVALID_SCANS = "REPEATED_INVALID_SCANS"


class EntryLookupThrottled(EntryServiceError):
    """Too many lookups of one kind by this operator; retry later."""

    def __init__(self, kind: str, retry_after_seconds: int):
        super().__init__("Too many lookups. Wait, or call a supervisor.")
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class _Budget:
    kind: str
    window_seconds: int
    maximum: int
    anomaly_threshold: int


def _identity_budget() -> _Budget:
    return _Budget(
        kind=KIND_IDENTITY_LOOKUPS,
        window_seconds=int(settings.ENTRY_LOOKUP_WINDOW_SECONDS),
        maximum=int(settings.ENTRY_LOOKUP_MAX_PER_WINDOW),
        anomaly_threshold=int(settings.ENTRY_LOOKUP_ANOMALY_THRESHOLD),
    )


def _invalid_scan_budget() -> _Budget:
    return _Budget(
        kind=KIND_INVALID_SCANS,
        window_seconds=int(settings.ENTRY_INVALID_SCAN_WINDOW_SECONDS),
        maximum=int(settings.ENTRY_INVALID_SCAN_MAX_PER_WINDOW),
        anomaly_threshold=int(settings.ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD),
    )


def _recent(user, budget: _Budget, now):
    from apps.audit.models import AuditEvent

    return AuditEvent.objects.filter(
        actor_user_id=user.pk,
        occurred_at__gte=now - timedelta(seconds=budget.window_seconds),
    )


def _count(user, budget: _Budget, now) -> int:
    recent = _recent(user, budget, now)
    if budget.kind == KIND_IDENTITY_LOOKUPS:
        return recent.filter(action_code__in=_IDENTITY_REFERENCE_ACTIONS).count()
    return recent.filter(
        action_code=action_codes.ENTRY_VERIFICATION_PERFORMED,
        reason_code__in=list(INVALID_SCAN_REASONS),
    ).count()


def _budget_for_method(method: str) -> _Budget:
    return _identity_budget() if method in IDENTITY_REFERENCE_METHODS else _invalid_scan_budget()


def _coordinates(checkpoint) -> dict:
    # `gate` is a display label (unique only within a venue); `gate_id` is
    # the exact identity the supervisor monitor filters on (P8-05).
    return {
        "gate": checkpoint.gate.code,
        "gate_id": str(checkpoint.gate.pk),
        "zone": checkpoint.zone.code,
        "device": checkpoint.device.public_id,
    }


def enforce_lookup_budget(*, checkpoint, method: str, now=None) -> None:
    """Refuse the lookup when this operator has exhausted its budget.

    Called after the method permission check and before any lookup work.
    """
    now = now or timezone.now()
    budget = _budget_for_method(method)
    if _count(checkpoint.user, budget, now) < budget.maximum:
        return
    with transaction.atomic():
        audit(
            action_code=action_codes.ENTRY_LOOKUP_THROTTLED,
            actor=checkpoint.user,
            target_type="EntryDeviceSession",
            target_uuid=checkpoint.device_session.pk,
            event_edition_id=checkpoint.event_edition.pk,
            result="DENIED",
            reason_code=budget.kind,
            after_summary={
                "method": method,
                "kind": budget.kind,
                "window_seconds": budget.window_seconds,
                "limit": budget.maximum,
                **_coordinates(checkpoint),
            },
        )
    _signal(checkpoint, budget, count=budget.maximum, now=now)
    raise EntryLookupThrottled(budget.kind, budget.window_seconds)


def note_lookup_outcome(*, checkpoint, method: str, reason_code: str, now=None) -> None:
    """After an audited lookup: raise an anomaly signal on a threshold crossing."""
    now = now or timezone.now()
    if method in IDENTITY_REFERENCE_METHODS:
        budget = _identity_budget()
    elif method == VerificationMethod.QR and reason_code in INVALID_SCAN_REASONS:
        budget = _invalid_scan_budget()
    else:
        return
    count = _count(checkpoint.user, budget, now)
    if count >= budget.anomaly_threshold:
        _signal(checkpoint, budget, count=count, now=now)


def _signal(checkpoint, budget: _Budget, *, count: int, now) -> None:
    """One anomaly signal per operator, kind, and window (deduplicated)."""
    already = (
        _recent(checkpoint.user, budget, now)
        .filter(action_code=action_codes.ENTRY_ANOMALY_SIGNAL, reason_code=budget.kind)
        .exists()
    )
    if already:
        return
    with transaction.atomic():
        audit(
            action_code=action_codes.ENTRY_ANOMALY_SIGNAL,
            actor=checkpoint.user,
            target_type="EntryDeviceSession",
            target_uuid=checkpoint.device_session.pk,
            event_edition_id=checkpoint.event_edition.pk,
            result="SUCCESS",
            reason_code=budget.kind,
            after_summary={
                "kind": budget.kind,
                "count": count,
                "window_seconds": budget.window_seconds,
                "threshold": budget.anomaly_threshold,
                **_coordinates(checkpoint),
            },
        )
    logger.warning(
        "Entry anomaly signal raised.",
        extra={
            "anomaly": budget.kind,
            "count": count,
            "window_seconds": budget.window_seconds,
            **_coordinates(checkpoint),
        },
    )
