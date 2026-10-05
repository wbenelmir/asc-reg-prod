"""Entry Events and reason-coded overrides (Schema §11.4-§11.5, Flow §10.9-§10.10).

An Entry Event is written ONLY here, ONLY after an explicit operator
decision (Gate G7), and ONLY for a context that was verified moments before
under the same operator session. The verification is carried between the
two requests as a server-held `PendingVerification` -- the browser only
ever sees its random `operation_id`, never a registration identifier.

Five guarantees, each covered by tests:

1. **Idempotent.** `operation_id` is unique on `EntryEvent`. A retried
   decision returns the original event; the same identifier bound to a
   different command (e.g. Admit, then Do Not Admit) is a conflict.
2. **Re-evaluated, never trusted.** The context is re-assessed inside the
   decision transaction, under a row lock on the Registration. If anything
   material changed since verification -- a pass revoked, a restriction
   added, another gate admitting the same person a second earlier -- the
   decision is refused as STALE and the operator verifies again.
3. **No admission without an allowed result**, except through an override
   (also enforced by a database CHECK constraint).
4. **Overrides are bounded.** The supervisor needs `entry.override_entry`
   at this checkpoint; the reason must come from the configured catalogue
   and must cover EVERY blocker; fixed non-overrideable codes and
   non-overrideable restrictions are refused regardless of configuration.
5. **Short-lived.** A verification older than `ENTRY_DECISION_TICKET_SECONDS`
   is refused as STALE.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.badges.models import DigitalEntryPass
from apps.badges.services import OperationConflictError, command_fingerprint
from apps.entry.models import (
    ADMITTABLE_RESULTS,
    EntryDecision,
    EntryDecisionReason,
    EntryEvent,
    EntryEventType,
    EntryOverride,
    EntryOverrideReason,
)
from apps.entry.policies import checkpoint_permission
from apps.entry.services import (
    EntryPermissionError,
    EntryServiceError,
    EntryStateError,
    audit,
    new_operation_id,
)
from apps.entry.services.access import Assessment, assess_context
from apps.registrations.models import Registration

OVERRIDE_NOTE_MAX_LENGTH = 500
_ENTRY_LOCK_CLASSID = 0x454E5452  # "ENTR" -- distinct from every other classid


class StaleVerificationError(EntryServiceError):
    """The verification is too old, from another session, or no longer
    matches the context's current state. The operator must verify again."""


class OverrideNotPermittedError(EntryServiceError):
    """The override is outside the approved catalogue or fixed exclusions."""


@dataclass(frozen=True)
class PendingVerification:
    """Server-held link between a verification and its decision.

    Stored in the operator's server-side session keyed by `operation_id`;
    never rendered into the page except `operation_id` itself.
    """

    operation_id: str
    registration_id: str
    credential_id: str
    method: str
    identity_verified: bool | None
    operator_session_id: str
    verified_at: str
    signature: tuple

    def to_session(self) -> dict:
        data = asdict(self)
        data["signature"] = list(self.signature)
        return data

    @classmethod
    def from_session(cls, data: object) -> PendingVerification | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                operation_id=str(data["operation_id"]),
                registration_id=str(data["registration_id"]),
                credential_id=str(data["credential_id"]),
                method=str(data["method"]),
                identity_verified=data["identity_verified"],
                operator_session_id=str(data["operator_session_id"]),
                verified_at=str(data["verified_at"]),
                signature=tuple(
                    tuple(item) if isinstance(item, list) else item for item in data["signature"]
                ),
            )
        except (KeyError, TypeError, ValueError):  # fmt: skip
            return None


def pending_from_assessment(
    *, assessment: Assessment, checkpoint, method: str, identity_verified=None
) -> PendingVerification:
    return PendingVerification(
        operation_id=new_operation_id(),
        registration_id=str(assessment.registration.pk),
        credential_id=str(getattr(assessment.credential, "pk", "") or ""),
        method=method,
        identity_verified=identity_verified,
        operator_session_id=str(checkpoint.operator_session.pk),
        verified_at=timezone.now().isoformat(),
        signature=_normalize_signature(assessment.signature()),
    )


def _normalize_signature(signature: tuple) -> tuple:
    return tuple(tuple(item) if isinstance(item, list | tuple) else item for item in signature)


@dataclass(frozen=True)
class DecisionOutcome:
    entry_event: EntryEvent
    replayed: bool


# ---------------------------------------------------------------------------
# Shared guards
# ---------------------------------------------------------------------------


def _acquire_operation_lock(operation_id: str) -> None:
    from apps.core.concurrency import lock_key, order_lock_keys

    digest = hashlib.sha256(operation_id.encode("utf-8")).digest()
    with connection.cursor() as cursor:
        for classid, objid in order_lock_keys([lock_key(_ENTRY_LOCK_CLASSID, digest)]):
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [classid, objid])


def _replayed(operation_id: str, fingerprint: str) -> EntryEvent | None:
    existing = EntryEvent.objects.filter(operation_id=operation_id).first()
    if existing is None:
        return None
    if existing.command_fingerprint != fingerprint:
        raise OperationConflictError("This verification was already used for a different decision.")
    return existing


def _require_fresh(pending: PendingVerification, checkpoint, *, now) -> None:
    if pending.operator_session_id != str(checkpoint.operator_session.pk):
        raise StaleVerificationError("This verification belongs to another operator session.")
    try:
        verified_at = datetime.fromisoformat(pending.verified_at)
    except ValueError as exc:
        raise StaleVerificationError("This verification is not valid.") from exc
    if (now - verified_at).total_seconds() > settings.ENTRY_DECISION_TICKET_SECONDS:
        raise StaleVerificationError("This verification has expired. Verify again.")


def _reassess(pending: PendingVerification, checkpoint, *, now) -> Assessment:
    """Lock the context and evaluate it again, exactly as it was verified."""
    # `of=("self",)`: `person` is nullable, and PostgreSQL refuses FOR UPDATE
    # on the nullable side of an outer join. Only the Registration row is
    # the serialization point anyway -- two gates deciding for the same
    # context queue here, so the second always sees the first's event.
    registration = (
        Registration.objects.select_for_update(of=("self",))
        .select_related("event_edition", "person")
        .get(pk=pending.registration_id)
    )
    presented = None
    if pending.method == "QR" and pending.credential_id:
        presented = DigitalEntryPass.objects.select_related("event_edition").get(
            pk=pending.credential_id
        )
    return assess_context(
        registration=registration,
        checkpoint=checkpoint,
        presented_credential=presented,
        identity_verified=pending.identity_verified,
        now=now,
    )


def _reject(checkpoint, pending, *, action_code: str, reason: str) -> None:
    with transaction.atomic():
        audit(
            action_code=action_code,
            actor=checkpoint.user,
            target_type="Registration",
            target_uuid=pending.registration_id,
            event_edition_id=checkpoint.event_edition.pk,
            result="DENIED",
            reason_code=reason,
            after_summary={"gate": checkpoint.gate.code, "method": pending.method},
        )


def _event_summary(event: EntryEvent, checkpoint) -> dict:
    return {
        "result": event.result,
        "reason": event.reason_code,
        "advisories": list(event.advisory_codes),
        "decision": event.decision,
        "decision_reason": event.decision_reason_code,
        "method": event.verification_method,
        "gate": checkpoint.gate.code,
        "zone": checkpoint.zone.code,
        "device": checkpoint.device.public_id,
        "override": event.override_id is not None,
    }


def _create_event(
    *, checkpoint, pending, assessment, decision, decision_reason, fingerprint, now, override=None
) -> EntryEvent:
    return EntryEvent.objects.create(
        operation_id=pending.operation_id,
        command_fingerprint=fingerprint,
        event_edition=checkpoint.event_edition,
        registration=assessment.registration,
        digital_entry_pass=assessment.credential,
        badge_assignment=assessment.badge_assignment,
        gate=checkpoint.gate,
        zone=checkpoint.zone,
        device=checkpoint.device,
        device_session=checkpoint.device_session,
        operator_session=checkpoint.operator_session,
        operator_user=checkpoint.user,
        event_type=EntryEventType.ENTRY,
        verification_method=pending.method,
        result=assessment.result,
        reason_code=assessment.reason_code if assessment.blockers else "",
        advisory_codes=list(assessment.advisory_codes),
        decision=decision,
        decision_reason_code=decision_reason,
        prior_entry_event=assessment.prior_entry,
        override=override,
        occurred_at=now,
        recorded_at=timezone.now(),
    )


# ---------------------------------------------------------------------------
# Operator decision
# ---------------------------------------------------------------------------


def record_entry_decision(
    *, checkpoint, pending: PendingVerification, decision: str, decision_reason: str = "", now=None
) -> DecisionOutcome:
    now = now or timezone.now()
    if decision not in EntryDecision.values:
        raise EntryStateError("Unknown decision.")
    if decision == EntryDecision.ADMIT:
        decision_reason = ""
    else:
        decision_reason = decision_reason or EntryDecisionReason.FOLLOWS_RESULT
        if decision_reason not in EntryDecisionReason.values:
            raise EntryStateError("Unknown decision reason.")
    if not checkpoint_permission(checkpoint.user, "verify_entry", checkpoint):
        raise EntryPermissionError("You are not authorized to record entry here.")
    fingerprint = command_fingerprint(
        operation_type="ENTRY_DECISION",
        actor=checkpoint.user,
        target_type="Registration",
        target_id=pending.registration_id,
        parameters={
            "decision": decision,
            "decision_reason": decision_reason,
            "method": pending.method,
            "credential": pending.credential_id,
            "gate": str(checkpoint.gate.pk),
            "zone": str(checkpoint.zone.pk),
        },
    )
    with transaction.atomic():
        _acquire_operation_lock(pending.operation_id)
        existing = _replayed(pending.operation_id, fingerprint)
        if existing is not None:
            return DecisionOutcome(entry_event=existing, replayed=True)
    try:
        _require_fresh(pending, checkpoint, now=now)
    except StaleVerificationError:
        _reject(
            checkpoint, pending, action_code=action_codes.ENTRY_DECISION_REJECTED, reason="EXPIRED"
        )
        raise
    refusal: str | None = None
    with transaction.atomic():
        _acquire_operation_lock(pending.operation_id)
        existing = _replayed(pending.operation_id, fingerprint)
        if existing is not None:
            return DecisionOutcome(entry_event=existing, replayed=True)
        assessment = _reassess(pending, checkpoint, now=now)
        if _normalize_signature(assessment.signature()) != pending.signature:
            refusal = "CHANGED"
        elif decision == EntryDecision.ADMIT and assessment.result not in ADMITTABLE_RESULTS:
            refusal = "NOT_ADMITTABLE"
        else:
            event = _create_event(
                checkpoint=checkpoint,
                pending=pending,
                assessment=assessment,
                decision=decision,
                decision_reason=decision_reason,
                fingerprint=fingerprint,
                now=now,
            )
            audit(
                action_code=action_codes.ENTRY_EVENT_RECORDED,
                actor=checkpoint.user,
                target_type="EntryEvent",
                target_uuid=event.pk,
                event_edition_id=checkpoint.event_edition.pk,
                after_summary=_event_summary(event, checkpoint),
            )
            return DecisionOutcome(entry_event=event, replayed=False)
    _reject(checkpoint, pending, action_code=action_codes.ENTRY_DECISION_REJECTED, reason=refusal)
    if refusal == "NOT_ADMITTABLE":
        raise EntryStateError("This result does not permit admission.")
    raise StaleVerificationError("The situation changed since verification. Verify again.")


# ---------------------------------------------------------------------------
# Supervisor override
# ---------------------------------------------------------------------------


def available_override_reasons(*, checkpoint, assessment: Assessment):
    """Catalogue entries that could override THIS assessment -- empty when
    the assessment is not overrideable at all or the user may not override."""
    if not assessment.may_be_overridden:
        return []
    if not checkpoint_permission(checkpoint.user, "override_entry", checkpoint):
        return []
    blockers = set(assessment.blocker_codes)
    return [
        reason
        for reason in EntryOverrideReason.objects.filter(
            event_edition=checkpoint.event_edition, is_active=True
        ).order_by("code")
        if blockers <= set(reason.overridable_reason_codes or [])
    ]


def record_override(
    *, checkpoint, pending: PendingVerification, override_reason_id, note: str = "", now=None
) -> DecisionOutcome:
    now = now or timezone.now()
    if not checkpoint_permission(checkpoint.user, "override_entry", checkpoint):
        _reject(
            checkpoint,
            pending,
            action_code=action_codes.ENTRY_OVERRIDE_REJECTED,
            reason="NOT_AUTHORIZED",
        )
        raise EntryPermissionError("You are not authorized to override entry decisions here.")
    reason = EntryOverrideReason.objects.filter(
        pk=override_reason_id, event_edition=checkpoint.event_edition, is_active=True
    ).first()
    if reason is None:
        _reject(
            checkpoint,
            pending,
            action_code=action_codes.ENTRY_OVERRIDE_REJECTED,
            reason="UNKNOWN_REASON",
        )
        raise OverrideNotPermittedError("This override reason is not available.")
    note = (note or "").strip()[:OVERRIDE_NOTE_MAX_LENGTH]
    if reason.requires_note and not note:
        raise OverrideNotPermittedError("This override reason requires a note.")
    fingerprint = command_fingerprint(
        operation_type="ENTRY_OVERRIDE",
        actor=checkpoint.user,
        target_type="Registration",
        target_id=pending.registration_id,
        parameters={
            "reason": reason.code,
            "note_text": note,
            "method": pending.method,
            "credential": pending.credential_id,
            "gate": str(checkpoint.gate.pk),
            "zone": str(checkpoint.zone.pk),
        },
    )
    with transaction.atomic():
        _acquire_operation_lock(pending.operation_id)
        existing = _replayed(pending.operation_id, fingerprint)
        if existing is not None:
            return DecisionOutcome(entry_event=existing, replayed=True)
    try:
        _require_fresh(pending, checkpoint, now=now)
    except StaleVerificationError:
        _reject(
            checkpoint, pending, action_code=action_codes.ENTRY_OVERRIDE_REJECTED, reason="EXPIRED"
        )
        raise

    rejection: str | None = None
    with transaction.atomic():
        _acquire_operation_lock(pending.operation_id)
        existing = _replayed(pending.operation_id, fingerprint)
        if existing is not None:
            return DecisionOutcome(entry_event=existing, replayed=True)
        assessment = _reassess(pending, checkpoint, now=now)
        if _normalize_signature(assessment.signature()) != pending.signature:
            rejection = "CHANGED"
        elif not assessment.may_be_overridden:
            rejection = "NON_OVERRIDEABLE"
        elif not set(assessment.blocker_codes) <= set(reason.overridable_reason_codes or []):
            rejection = "NOT_COVERED"
        else:
            override = EntryOverride.objects.create(
                operation_id=pending.operation_id,
                event_edition=checkpoint.event_edition,
                registration=assessment.registration,
                digital_entry_pass=assessment.credential,
                original_result=assessment.result,
                original_reason_code=assessment.reason_code,
                reason=reason,
                reason_code=reason.code,
                note_encrypted=note,
                user=checkpoint.user,
                gate=checkpoint.gate,
                zone=checkpoint.zone,
                device=checkpoint.device,
                occurred_at=now,
            )
            event = _create_event(
                checkpoint=checkpoint,
                pending=pending,
                assessment=assessment,
                decision=EntryDecision.ADMIT,
                decision_reason="",
                fingerprint=fingerprint,
                now=now,
                override=override,
            )
            audit(
                action_code=action_codes.ENTRY_OVERRIDE_RECORDED,
                actor=checkpoint.user,
                target_type="EntryOverride",
                target_uuid=override.pk,
                event_edition_id=checkpoint.event_edition.pk,
                reason_code=reason.code,
                after_summary={
                    "original_result": assessment.result,
                    "blockers": sorted(assessment.blocker_codes),
                    "reason": reason.code,
                    "gate": checkpoint.gate.code,
                    "zone": checkpoint.zone.code,
                    "device": checkpoint.device.public_id,
                    "note_recorded": bool(note),
                },
            )
            audit(
                action_code=action_codes.ENTRY_EVENT_RECORDED,
                actor=checkpoint.user,
                target_type="EntryEvent",
                target_uuid=event.pk,
                event_edition_id=checkpoint.event_edition.pk,
                after_summary=_event_summary(event, checkpoint),
            )
            return DecisionOutcome(entry_event=event, replayed=False)
    _reject(checkpoint, pending, action_code=action_codes.ENTRY_OVERRIDE_REJECTED, reason=rejection)
    if rejection == "CHANGED":
        raise StaleVerificationError("The situation changed since verification. Verify again.")
    raise OverrideNotPermittedError("This result cannot be overridden with this reason.")
