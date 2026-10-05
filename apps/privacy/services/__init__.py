"""Privacy-request lifecycle and Legal Hold commands (Phase 2 Prompt 5 §4.6).

No destructive retention/deletion job is implemented here or anywhere else
in this prompt's scope -- `RetentionCategory` and `LegalHold` are
foundations a FUTURE, separately-approved job would consult, never an
executor of one.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.policies import has_scoped_permission
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.privacy.models import LegalHold, PrivacyRequest, PrivacyRequestStatus

_TERMINAL_PRIVACY_REQUEST_STATUSES = frozenset(
    {
        PrivacyRequestStatus.COMPLETED,
        PrivacyRequestStatus.REJECTED,
        PrivacyRequestStatus.WITHDRAWN,
    }
)

_ALLOWED_PRIVACY_REQUEST_TRANSITIONS: dict[str, set[str]] = {
    PrivacyRequestStatus.RECEIVED: {
        PrivacyRequestStatus.IN_PROGRESS,
        PrivacyRequestStatus.REJECTED,
        PrivacyRequestStatus.WITHDRAWN,
    },
    PrivacyRequestStatus.IN_PROGRESS: {
        PrivacyRequestStatus.COMPLETED,
        PrivacyRequestStatus.REJECTED,
        PrivacyRequestStatus.WITHDRAWN,
    },
}


class InvalidPrivacyRequestTransitionError(Exception):
    """Raised when a `PrivacyRequest` status transition is not allowed."""


class LegalHoldError(Exception):
    """Raised for an invalid Legal Hold operation."""


class PrivacyAuthorizationError(Exception):
    """Raised when an operational actor lacks the required scoped permission."""


def _registration_scope(registration) -> tuple[object | None, object | None]:
    if registration is None:
        return None, None
    return registration.event_edition_id, registration.source_organization_id


def _require_privacy_permission(actor, codename: str, *, registration=None) -> None:
    event_id, organization_id = _registration_scope(registration)
    if not has_scoped_permission(
        actor,
        f"privacy.{codename}",
        event_edition_id=event_id,
        organization_id=organization_id,
    ):
        raise PrivacyAuthorizationError("The actor is not authorized for this privacy operation.")


def is_terminal_privacy_request_status(status: str) -> bool:
    return status in _TERMINAL_PRIVACY_REQUEST_STATUSES


def file_privacy_request(
    *,
    person,
    request_type: str,
    scope_description: str,
    reason: str = "",
    registration=None,
    received_at=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> PrivacyRequest:
    if not scope_description.strip():
        raise ValueError("A privacy request requires a scope description.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        privacy_request = PrivacyRequest.objects.create(
            person=person,
            registration=registration,
            request_type=request_type,
            scope_description=scope_description[:1000],
            reason=reason[:1000],
            received_at=received_at or timezone.now(),
            status=PrivacyRequestStatus.RECEIVED,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=getattr(person, "pk", None),
                action_code=action_codes.PRIVACY_REQUEST_RECEIVED,
                target_type="PrivacyRequest",
                target_uuid=privacy_request.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return privacy_request


def transition_privacy_request_status(
    privacy_request: PrivacyRequest,
    new_status: str,
    *,
    actor,
    resolution_note: str = "",
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> PrivacyRequest:
    _require_privacy_permission(
        actor, "change_privacyrequest", registration=privacy_request.registration
    )
    allowed = _ALLOWED_PRIVACY_REQUEST_TRANSITIONS.get(privacy_request.status, set())
    if new_status not in allowed:
        raise InvalidPrivacyRequestTransitionError(
            f"PrivacyRequest cannot move from {privacy_request.status} to {new_status}."
        )
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    extra_fields: dict[str, object] = {"handled_by": actor}
    if resolution_note:
        extra_fields["resolution_note"] = resolution_note[:1000]
    if new_status in _TERMINAL_PRIVACY_REQUEST_STATUSES:
        extra_fields["resolved_at"] = now
    with transaction.atomic():
        updated = PrivacyRequest.objects.filter(
            pk=privacy_request.pk, status=privacy_request.status
        ).update(status=new_status, **extra_fields)
        if updated == 0:
            raise InvalidPrivacyRequestTransitionError(
                "PrivacyRequest status changed concurrently; refusing a stale transition."
            )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.PRIVACY_REQUEST_STATUS_CHANGED,
                target_type="PrivacyRequest",
                target_uuid=privacy_request.pk,
                result="SUCCESS",
                after_summary={"status": new_status},
                correlation_id=correlation_id,
            )
        )
    privacy_request.status = new_status
    for field_name, value in extra_fields.items():
        setattr(privacy_request, field_name, value)
    return privacy_request


def place_legal_hold(
    *,
    registration,
    reason: str,
    placed_by,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> LegalHold:
    _require_privacy_permission(placed_by, "add_legalhold", registration=registration)
    if not reason.strip():
        raise LegalHoldError("Placing a Legal Hold requires a reason.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    try:
        with transaction.atomic():
            legal_hold = LegalHold.objects.create(
                registration=registration,
                reason=reason[:1000],
                placed_by=placed_by,
                placed_at=timezone.now(),
            )
    except IntegrityError:
        raise LegalHoldError("This Registration already has an active Legal Hold.") from None
    audit_recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(placed_by, "pk", None),
            action_code=action_codes.LEGAL_HOLD_PLACED,
            target_type="Registration",
            target_uuid=registration.pk,
            result="SUCCESS",
            correlation_id=correlation_id,
        )
    )
    return legal_hold


def release_legal_hold(
    *,
    legal_hold: LegalHold,
    released_by,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> LegalHold:
    _require_privacy_permission(
        released_by, "change_legalhold", registration=legal_hold.registration
    )
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    with transaction.atomic():
        updated = LegalHold.objects.filter(pk=legal_hold.pk, released_at__isnull=True).update(
            released_by=released_by, released_at=now
        )
        if updated == 0:
            raise LegalHoldError("This Legal Hold is already released.")
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(released_by, "pk", None),
                action_code=action_codes.LEGAL_HOLD_RELEASED,
                target_type="Registration",
                target_uuid=legal_hold.registration_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    legal_hold.released_by = released_by
    legal_hold.released_at = now
    return legal_hold
