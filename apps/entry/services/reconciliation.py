"""Offline reconciliation cases and the supervisor queue (Phase 4 Prompt 3,
ADR-0024, Schema §11.7, Flow §11.8-§11.9).

A case is opened -- in the SAME transaction as the synchronization outcome
that needs it -- for every operation a supervisor must look at. It keeps
apart what the device knew, what the server knew and what physically
happened, and it never rewrites the Entry Event: a supervisor's review is an
append-only `ReconciliationAction` (a note, or the closure, which requires a
note), taken under a row lock with the expected version (Schema §16.3-§16.4)
and audited without the note text.

Authorization is `entry.reconcile_offline_operation`, scoped like every
checkpoint permission: by event, venue and gate, through an
organization-UNscoped membership only (the P8-08 rule). A gate-scoped
supervisor sees and closes the cases of that gate only; an event-scoped one
sees the whole event. No path returns a case outside the actor's scope.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import (
    ReconciliationAction,
    ReconciliationActionType,
    ReconciliationCase,
    ReconciliationCaseOperation,
    ReconciliationCaseType,
    ReconciliationSeverity,
    ReconciliationStatus,
    SyncOperation,
    SyncOperationStatus,
)
from apps.entry.services import (
    EntryConcurrencyError,
    EntryPermissionError,
    EntryStateError,
    audit,
    new_public_id,
)

PERMISSION = "reconcile_offline_operation"
NOTE_MAX_LENGTH = 1000

_SECURITY_TYPES = frozenset(
    {
        ReconciliationCaseType.WRONG_EVENT,
        ReconciliationCaseType.PACKAGE_STALE,
        ReconciliationCaseType.PACKAGE_EXPIRED,
        ReconciliationCaseType.NON_OVERRIDEABLE_RESTRICTION,
        ReconciliationCaseType.POLICY_VIOLATION,
        ReconciliationCaseType.OPERATION_ID_REUSED,
        ReconciliationCaseType.SECURITY_REJECTION,
        ReconciliationCaseType.QUARANTINED_DEVICE,
        ReconciliationCaseType.CHAIN_BREAK,
    }
)
_ADVISORY_TYPES = frozenset(
    {
        ReconciliationCaseType.DUPLICATE_ADMISSION_ADVISORY,
        ReconciliationCaseType.SEQUENCE_GAP,
        ReconciliationCaseType.CLOCK_ANOMALY,
        ReconciliationCaseType.UNSUPPORTED_OPERATION,
    }
)


def severity_for(case_type: str) -> str:
    if case_type in _SECURITY_TYPES:
        return ReconciliationSeverity.SECURITY
    if case_type in _ADVISORY_TYPES:
        return ReconciliationSeverity.ADVISORY
    return ReconciliationSeverity.CONFLICT


# ---------------------------------------------------------------------------
# Opening cases (called by the synchronization service, inside its transaction)
# ---------------------------------------------------------------------------


def open_case(
    *,
    case_type: str,
    device,
    gate,
    sync_operation: SyncOperation | None = None,
    entry_event=None,
    related_entry_event=None,
    registration=None,
    device_known_state: dict | None = None,
    server_known_state: dict | None = None,
    operational_fact: dict | None = None,
    now=None,
) -> ReconciliationCase:
    """Open one case and link its operation. Caller holds the transaction."""
    now = now or timezone.now()
    case = ReconciliationCase.objects.create(
        public_id=new_public_id(),
        event_edition_id=device.event_edition_id,
        venue_id=gate.venue_id,
        gate=gate,
        device=device,
        case_type=case_type,
        severity=severity_for(case_type),
        sync_operation=sync_operation,
        entry_event=entry_event,
        related_entry_event=related_entry_event,
        registration=registration,
        device_known_state=device_known_state or {},
        server_known_state=server_known_state or {},
        operational_fact=operational_fact or {},
        opened_at=now,
    )
    if sync_operation is not None:
        link_operation(case, sync_operation, now=now)
    audit(
        action_code=action_codes.RECONCILIATION_CASE_OPENED,
        target_type="ReconciliationCase",
        target_uuid=case.pk,
        event_edition_id=device.event_edition_id,
        reason_code=case_type,
        actor_type="SYSTEM",
        after_summary={
            "case": case.public_id,
            "case_type": case_type,
            "severity": case.severity,
            "device": device.public_id,
            "gate_id": str(gate.pk),
            "operation": getattr(sync_operation, "public_id", ""),
            "entry_event": entry_event is not None,
        },
    )
    return case


def link_operation(case: ReconciliationCase, sync_operation: SyncOperation, *, now=None) -> None:
    ReconciliationCaseOperation.objects.get_or_create(
        case=case, sync_operation=sync_operation, defaults={"linked_at": now or timezone.now()}
    )


def open_quarantine_case(*, device, gate, sync_operation, now=None) -> ReconciliationCase:
    """One open quarantine case per device/checkpoint, extended per upload."""
    now = now or timezone.now()
    case = (
        ReconciliationCase.objects.select_for_update()
        .filter(
            device=device,
            gate=gate,
            case_type=ReconciliationCaseType.QUARANTINED_DEVICE,
            status=ReconciliationStatus.OPEN,
        )
        .order_by("opened_at")
        .first()
    )
    if case is None:
        return open_case(
            case_type=ReconciliationCaseType.QUARANTINED_DEVICE,
            device=device,
            gate=gate,
            sync_operation=sync_operation,
            device_known_state={"device_status": device.status},
            server_known_state={"device_status": device.status},
            operational_fact={"note": "UPLOADED_WHILE_NOT_OPERATIONAL"},
            now=now,
        )
    link_operation(case, sync_operation, now=now)
    return case


# ---------------------------------------------------------------------------
# Scoping (the supervisor queue)
# ---------------------------------------------------------------------------


def reconciliation_scopes(user) -> list[tuple]:
    """(event_edition_id, venue_id, gate_id) of every membership granting
    `entry.reconcile_offline_operation`; None means "every value"."""
    from apps.accounts.models import ScopedGroupMembership, ScopedGroupMembershipStatus

    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return []
    now = timezone.now()
    return list(
        ScopedGroupMembership.objects.filter(user=user, status=ScopedGroupMembershipStatus.ACTIVE)
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
        .filter(
            organization__isnull=True,
            group__permissions__content_type__app_label="entry",
            group__permissions__codename=PERMISSION,
        )
        .values_list("event_edition_id", "venue_id", "gate_id")
        .distinct()
    )


def _scope_q(scopes, *, prefix: str = "") -> Q | None:
    combined = None
    for event_id, venue_id, gate_id in scopes:
        q = Q()
        if event_id is not None:
            q &= Q(**{f"{prefix}event_edition_id": event_id})
        if venue_id is not None:
            q &= Q(**{f"{prefix}venue_id": venue_id})
        if gate_id is not None:
            q &= Q(**{f"{prefix}gate_id": gate_id})
        combined = q if combined is None else combined | q
    return combined


def cases_visible_to(user, *, event_edition=None):
    """Every case `user` may see, narrowed to one event when given. Fails
    closed: no scoped membership, no case."""
    queryset = ReconciliationCase.objects.select_related(
        "event_edition", "venue", "gate", "device", "registration__person", "sync_operation"
    )
    if event_edition is not None:
        queryset = queryset.filter(event_edition=event_edition)
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    if user.is_superuser:
        return queryset
    scope = _scope_q(reconciliation_scopes(user))
    if scope is None:
        return queryset.none()
    return queryset.filter(scope)


def may_reconcile(user, case: ReconciliationCase) -> bool:
    from apps.entry.policies import has_checkpoint_permission

    return has_checkpoint_permission(
        user,
        PERMISSION,
        event_edition_id=case.event_edition_id,
        venue_id=case.venue_id,
        gate_id=case.gate_id,
    )


def may_open_queue(user, *, event_edition) -> bool:
    """True if `user` may see at least part of this event's queue."""
    if getattr(user, "is_superuser", False) and user.is_active:
        return True
    return any(
        event_id is None or event_id == event_edition.pk
        for event_id, _venue, _gate in reconciliation_scopes(user)
    )


def record_denied_access(user, *, target_public_id: str, event_edition_id=None) -> None:
    """Audit an attempt to open or act on a case outside the actor's scope."""
    with transaction.atomic():
        audit(
            action_code=action_codes.RECONCILIATION_ACCESS_DENIED,
            actor=user,
            target_type="ReconciliationCase",
            event_edition_id=event_edition_id,
            result="DENIED",
            reason_code="OUT_OF_SCOPE",
            after_summary={"case": str(target_public_id)[:22]},
        )


# ---------------------------------------------------------------------------
# Supervisor actions (append-only, audited)
# ---------------------------------------------------------------------------


def record_action(
    *, case: ReconciliationCase, actor, action: str, note: str, expected_version: int, now=None
) -> ReconciliationAction:
    """Add a note to a case, or close it (a closure requires a note).

    Serialized by the case row lock and the expected version, so two
    supervisors can never both close one case; re-checks the actor's scope
    under the lock."""
    if action not in ReconciliationActionType.values:
        raise EntryStateError("Unknown reconciliation action.", code="INVALID_ACTION")
    note = (note or "").strip()
    if not note:
        raise EntryStateError("A note is required.", code="NOTE_REQUIRED")
    if len(note) > NOTE_MAX_LENGTH:
        raise EntryStateError("The note is too long.", code="NOTE_TOO_LONG")
    now = now or timezone.now()
    with transaction.atomic():
        locked = ReconciliationCase.objects.select_for_update().get(pk=case.pk)
        if not may_reconcile(actor, locked):
            audit(
                action_code=action_codes.RECONCILIATION_ACCESS_DENIED,
                actor=actor,
                target_type="ReconciliationCase",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="DENIED",
                reason_code="NOT_AUTHORIZED",
                after_summary={"case": locked.public_id, "action": action},
            )
            raise EntryPermissionError("You are not authorized to reconcile this case.")
        if locked.version != expected_version:
            raise EntryConcurrencyError("This case changed since you loaded it.")
        if locked.status != ReconciliationStatus.OPEN:
            raise EntryStateError("This case is already closed.", code="CASE_CLOSED")
        entry = ReconciliationAction.objects.create(
            case=locked, action=action, note_encrypted=note, actor=actor, created_at=now
        )
        if action == ReconciliationActionType.CLOSE:
            locked.status = ReconciliationStatus.CLOSED
            locked.closed_at = now
            locked.closed_by = actor
        locked.version += 1
        locked.save(update_fields=["status", "closed_at", "closed_by", "version"])
        audit(
            action_code=(
                action_codes.RECONCILIATION_CASE_CLOSED
                if action == ReconciliationActionType.CLOSE
                else action_codes.RECONCILIATION_NOTE_ADDED
            ),
            actor=actor,
            target_type="ReconciliationCase",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=locked.case_type,
            after_summary={
                "case": locked.public_id,
                "case_type": locked.case_type,
                "action": action,
                "note_recorded": True,
                "gate_id": str(locked.gate_id),
            },
        )
    return entry


# ---------------------------------------------------------------------------
# Recovery counts (TRD OFF-005)
# ---------------------------------------------------------------------------


def recovery_counts(operations) -> dict[str, int]:
    """OFF-005: uploaded, accepted, duplicate, rejected, conflict (plus
    pending and quarantined), over one SyncOperation queryset."""
    rows = dict(operations.values("status").annotate(n=Count("id")).values_list("status", "n"))
    duplicates = operations.aggregate(n=Sum("duplicate_submissions"))["n"] or 0
    return {
        "uploaded": sum(rows.values()),
        "accepted": rows.get(SyncOperationStatus.APPLIED, 0),
        "duplicate": int(duplicates),
        "rejected": rows.get(SyncOperationStatus.REJECTED, 0),
        "conflict": sum(
            rows.get(status, 0)
            for status in (
                SyncOperationStatus.CONFLICT,
                SyncOperationStatus.RECONCILIATION_REQUIRED,
                SyncOperationStatus.SECURITY_CONFLICT,
            )
        ),
        "quarantined": rows.get(SyncOperationStatus.QUARANTINED, 0),
        "pending": rows.get(SyncOperationStatus.PENDING, 0),
    }


def operations_visible_to(user, *, event_edition):
    """The event's synchronized operations within the actor's reconciliation
    scope (for the queue's OFF-005 counts)."""
    queryset = SyncOperation.objects.filter(event_edition=event_edition)
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    if user.is_superuser:
        return queryset
    # A SyncOperation carries its gate, and the venue through the gate.
    scope = None
    for event_id, venue_id, gate_id in reconciliation_scopes(user):
        q = Q()
        if event_id is not None:
            q &= Q(event_edition_id=event_id)
        if venue_id is not None:
            q &= Q(gate__venue_id=venue_id)
        if gate_id is not None:
            q &= Q(gate_id=gate_id)
        scope = q if scope is None else scope | q
    if scope is None:
        return queryset.none()
    return queryset.filter(scope)
