"""Temporary external-security operational-account foundations (Phase 2
Prompt 4 §7).

Deliberately narrow: creates and manages the ACCOUNT and its SCOPE only.
Grants no permission, no entry/gate/scan capability, and no implicit
inheritance of any unrelated operational permission -- an account created
here starts (and stays, unless an administrator separately and explicitly
grants a permission to `EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME`) with zero
functional capability.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.apps import EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME
from apps.accounts.models import (
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.communications.purposes import CommunicationPurpose
from apps.communications.services import queue_communication


class ExternalSecurityAccountError(Exception):
    """Raised for an invalid temporary external-security account operation."""


def _validate_future_expiry(expires_at, *, upper_bound=None) -> None:
    if expires_at is None or expires_at <= timezone.now():
        raise ExternalSecurityAccountError(
            "A temporary external-security account requires a future expires_at."
        )
    if upper_bound is not None and expires_at > upper_bound:
        raise ExternalSecurityAccountError("Membership expiry cannot exceed account expiry.")


def _validate_gate_scope(gate, event_edition) -> None:
    """A gate scope is only meaningful inside that gate's own event edition."""
    if gate is None:
        return
    if event_edition is None or gate.venue.event_edition_id != event_edition.pk:
        raise ExternalSecurityAccountError(
            "A gate scope requires the matching event edition scope."
        )


def _gate_scope_fields(gate) -> dict:
    if gate is None:
        return {}
    return {"venue_id": gate.venue_id, "gate": gate}


def create_external_security_account(
    *,
    email: str,
    display_name: str,
    expires_at,
    created_by,
    event_edition=None,
    organization=None,
    gate=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> OperationalUser:
    """Create one temporary `EXTERNAL_SECURITY` account with an explicit
    expiry and, where approved, an event/organization scope -- carrying
    NO permission by default (Phase 2 Prompt 4 §7 "deny-by-default
    permissions").
    """
    _validate_future_expiry(expires_at)
    _validate_gate_scope(gate, event_edition)
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        user = OperationalUser.objects.create_user(
            email=email,
            password=None,
            display_name=display_name,
            account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
            status=OperationalUserStatus.ACTIVE,
            active_from=timezone.now(),
            active_until=expires_at,
        )
        group_name = EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME
        from django.contrib.auth.models import Group

        group = Group.objects.get(name=group_name)
        ScopedGroupMembership.objects.create(
            user=user,
            group=group,
            event_edition=event_edition,
            organization=organization,
            **_gate_scope_fields(gate),
            active_from=timezone.now(),
            active_until=expires_at,
            granted_by=created_by,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(created_by, "pk", None),
                action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_CREATED,
                target_type="OperationalUser",
                target_uuid=user.pk,
                event_edition_id=getattr(event_edition, "pk", None),
                result="SUCCESS",
                after_summary={"expires_at": expires_at.isoformat()},
                correlation_id=correlation_id,
            )
        )
        queue_communication(
            purpose_code=CommunicationPurpose.ACCOUNT_ACCESS,
            event_edition=event_edition,
            person=None,
            language="en",
            destination=user.email_normalized,
            context={"display_name": display_name},
            idempotency_key=f"account-access:{user.pk}",
        )
    return user


def change_external_security_account_scope(
    *,
    user: OperationalUser,
    actor,
    event_edition=None,
    organization=None,
    gate=None,
    expires_at=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ScopedGroupMembership:
    """Close every current scoped membership for this account's temporary
    group and open a new one with the new scope -- never overwrites the
    prior membership row (Phase 2 Prompt 4 "scope change" audit evidence).
    """
    if user.account_type != OperationalUserAccountType.EXTERNAL_SECURITY:
        raise ExternalSecurityAccountError("Not a temporary external-security account.")
    _validate_gate_scope(gate, event_edition)
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    with transaction.atomic():
        locked_user = OperationalUser.objects.select_for_update().get(pk=user.pk)
        if locked_user.account_type != OperationalUserAccountType.EXTERNAL_SECURITY:
            raise ExternalSecurityAccountError("Not a temporary external-security account.")
        membership_expiry = expires_at or locked_user.active_until
        _validate_future_expiry(membership_expiry, upper_bound=locked_user.active_until)
        ScopedGroupMembership.objects.filter(
            user=locked_user, group__name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME
        ).filter(Q(active_until__isnull=True) | Q(active_until__gt=now)).update(active_until=now)
        from django.contrib.auth.models import Group

        group = Group.objects.get(name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME)
        membership = ScopedGroupMembership.objects.create(
            user=locked_user,
            group=group,
            event_edition=event_edition,
            organization=organization,
            **_gate_scope_fields(gate),
            active_from=now,
            active_until=membership_expiry,
            granted_by=actor,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_SCOPE_CHANGED,
                target_type="OperationalUser",
                target_uuid=locked_user.pk,
                event_edition_id=getattr(event_edition, "pk", None),
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return membership


def activate_external_security_account(
    *,
    user: OperationalUser,
    actor,
    expires_at,
    event_edition=None,
    organization=None,
    gate=None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> OperationalUser:
    """Reactivate a temporary account with a fresh bounded scope and expiry."""
    _validate_future_expiry(expires_at)
    _validate_gate_scope(gate, event_edition)
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = timezone.now()
    with transaction.atomic():
        locked_user = OperationalUser.objects.select_for_update().get(pk=user.pk)
        if locked_user.account_type != OperationalUserAccountType.EXTERNAL_SECURITY:
            raise ExternalSecurityAccountError("Not a temporary external-security account.")
        ScopedGroupMembership.objects.filter(
            user=locked_user, group__name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME
        ).filter(Q(active_until__isnull=True) | Q(active_until__gt=now)).update(active_until=now)
        locked_user.status = OperationalUserStatus.ACTIVE
        locked_user.active_from = now
        locked_user.active_until = expires_at
        locked_user.save(update_fields=["status", "active_from", "active_until", "updated_at"])
        from django.contrib.auth.models import Group

        ScopedGroupMembership.objects.create(
            user=locked_user,
            group=Group.objects.get(name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME),
            event_edition=event_edition,
            organization=organization,
            **_gate_scope_fields(gate),
            active_from=now,
            active_until=expires_at,
            granted_by=actor,
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_ACTIVATED,
                target_type="OperationalUser",
                target_uuid=locked_user.pk,
                event_edition_id=getattr(event_edition, "pk", None),
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
    return locked_user


def deactivate_external_security_account(
    *,
    user: OperationalUser,
    actor,
    reason: str,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> OperationalUser:
    if not reason:
        raise ExternalSecurityAccountError("Deactivation requires a reason.")
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    with transaction.atomic():
        locked_user = OperationalUser.objects.select_for_update().get(pk=user.pk)
        if locked_user.account_type != OperationalUserAccountType.EXTERNAL_SECURITY:
            raise ExternalSecurityAccountError("Not a temporary external-security account.")
        now = timezone.now()
        locked_user.status = OperationalUserStatus.SUSPENDED
        locked_user.save(update_fields=["status", "updated_at"])
        ScopedGroupMembership.objects.filter(
            user=locked_user, group__name=EXTERNAL_SECURITY_TEMPORARY_GROUP_NAME
        ).filter(Q(active_until__isnull=True) | Q(active_until__gt=now)).update(active_until=now)
        # "Easy to disable" (AUTHZ-003): a deactivated account also loses
        # every entry operator session it holds, immediately.
        from apps.entry.services.sessions import end_operator_sessions_for_user

        end_operator_sessions_for_user(user=locked_user, reason="ACCOUNT_DEACTIVATED", now=now)
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_DEACTIVATED,
                target_type="OperationalUser",
                target_uuid=locked_user.pk,
                result="SUCCESS",
                reason_code=reason[:100],
                correlation_id=correlation_id,
            )
        )
    return locked_user


def expire_lapsed_temporary_accounts(
    *, now=None, limit: int = 500, audit_recorder: AuditRecorder | None = None
) -> int:
    """Persist EXPIRED for temporary external-security accounts past `active_until`.

    Phase 3 Prompt 4 "automatic temporary-account expiry". Correctness never
    depends on this sweep having run: `OperationalUser.is_active` already
    fails closed the instant `active_until` passes, so an expired account
    cannot authenticate or keep a session. This sweep makes the expiry
    DURABLE and VISIBLE -- the status becomes EXPIRED, every still-open
    scoped membership is closed, every open entry operator session is ended,
    and one audit event is written per account.

    Idempotent and retry-safe: an already-EXPIRED account is never touched
    again, and each row is re-checked under a row lock.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    now = now or timezone.now()
    candidate_ids = list(
        OperationalUser.objects.filter(
            account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
            status=OperationalUserStatus.ACTIVE,
            active_until__isnull=False,
            active_until__lte=now,
        )
        .order_by("active_until")
        .values_list("pk", flat=True)[:limit]
    )
    expired = 0
    for user_id in candidate_ids:
        with transaction.atomic():
            locked_user = OperationalUser.objects.select_for_update().filter(pk=user_id).first()
            if (
                locked_user is None
                or locked_user.status != OperationalUserStatus.ACTIVE
                or locked_user.active_until is None
                or locked_user.active_until > now
            ):
                continue
            locked_user.status = OperationalUserStatus.EXPIRED
            locked_user.save(update_fields=["status", "updated_at"])
            ScopedGroupMembership.objects.filter(user=locked_user).filter(
                Q(active_until__isnull=True) | Q(active_until__gt=now)
            ).update(active_until=now)
            from apps.entry.services.sessions import end_operator_sessions_for_user

            end_operator_sessions_for_user(user=locked_user, reason="ACCOUNT_EXPIRED", now=now)
            audit_recorder.record(
                AuditRecord(
                    actor_type="SYSTEM",
                    action_code=action_codes.EXTERNAL_SECURITY_ACCOUNT_EXPIRED,
                    target_type="OperationalUser",
                    target_uuid=locked_user.pk,
                    result="SUCCESS",
                    after_summary={"active_until": locked_user.active_until.isoformat()},
                )
            )
            expired += 1
    return expired
