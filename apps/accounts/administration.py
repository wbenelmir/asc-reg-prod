"""Staff account administration: accounts, status, validity, credential setup
links and scoped roles.

The single service boundary of the operations area `/ops/staff-accounts/`
and of `manage.py bootstrap_account_administrator`. Views never write these
models themselves.

Authorization (checked here on every command, not only by the views):

* the actor needs `accounts.manage_operational_accounts` through an
  effective `ScopedGroupMembership` (or is a superuser);
* the actor's ADMINISTRATION SCOPE is the event/organization scope of those
  memberships. A role can be granted, changed or revoked only in a scope that
  one of them covers (an empty event or organization on the actor's
  membership covers every value of that dimension; the reverse never holds);
* an account's status, validity and credentials can be changed only when the
  actor's scope covers every current membership of that account, so an
  event administrator cannot disable a colleague who works across events;
* nobody changes their own account, status or roles (no self-escalation);
* a superuser account is administered only by a superuser, and this module
  never sets `is_staff` or `is_superuser`;
* the last effective unrestricted account administrator can never be
  removed, suspended, disabled, expired or narrowed. Every command that could
  change that number takes one transaction-scoped advisory lock first, so two
  simultaneous removals cannot both pass.

Credentials: an account is created INVITED without a usable password.
`issue_credential_setup` sends a single-use, expiring link (only its SHA-256
digest is stored, a newer link replaces an older one) directly by email after
commit, like the participant OTP, so the link is never stored in a message
row, an audit event or a log. Completing it sets the password only: it never
activates the account and never signs anyone in. Creating, activating and
granting access stay three separate actions.

Audit summaries carry account ids, statuses, role keys, scope ids and dates,
never an email address, a password, a token or a link.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts import roles
from apps.accounts.models import (
    CredentialSetupPurpose,
    CredentialSetupToken,
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
    ScopedGroupMembershipStatus,
)
from apps.accounts.policies import effective_scoped_memberships, has_scoped_permission
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.concurrency import ADVISORY_LOCK_CLASS_ACCOUNT_ADMINISTRATION

MANAGE_PERMISSION = "accounts.manage_operational_accounts"
_MANAGE_APP_LABEL, _, _MANAGE_CODENAME = MANAGE_PERMISSION.partition(".")

#: Account types the administration area may create. External security
#: accounts must carry an end of validity.
CREATABLE_ACCOUNT_TYPES = (
    OperationalUserAccountType.INTERNAL,
    OperationalUserAccountType.ORGANIZATION,
    OperationalUserAccountType.SUPPORT,
    OperationalUserAccountType.EXTERNAL_SECURITY,
)

REASON_MIN_LENGTH = 3
REASON_MAX_LENGTH = 300

#: Status commands and the statuses they may start from.
ACTIVATE = "activate"
SUSPEND = "suspend"
DISABLE = "disable"
_STATUS_COMMANDS = {
    ACTIVATE: (
        OperationalUserStatus.ACTIVE,
        (
            OperationalUserStatus.INVITED,
            OperationalUserStatus.SUSPENDED,
            OperationalUserStatus.EXPIRED,
            OperationalUserStatus.DISABLED,
        ),
    ),
    SUSPEND: (
        OperationalUserStatus.SUSPENDED,
        (OperationalUserStatus.INVITED, OperationalUserStatus.ACTIVE),
    ),
    DISABLE: (
        OperationalUserStatus.DISABLED,
        (
            OperationalUserStatus.INVITED,
            OperationalUserStatus.ACTIVE,
            OperationalUserStatus.SUSPENDED,
            OperationalUserStatus.EXPIRED,
        ),
    ),
}


class AccountAdministrationError(Exception):
    """A refused administration command. `code` is stable and mapped to a
    localized message by the views; the message is developer-facing."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# ---------------------------------------------------------------------------
# Scope and authorization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdministrationScope:
    """The (event, organization) pairs the actor administers; None = every."""

    unrestricted: bool
    pairs: frozenset[tuple]

    def covers(self, event_edition_id, organization_id) -> bool:
        if self.unrestricted:
            return True
        return any(
            (event is None or event == event_edition_id)
            and (organization is None or organization == organization_id)
            for event, organization in self.pairs
        )


def administration_scope(actor) -> AdministrationScope:
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        return AdministrationScope(False, frozenset())
    if actor.is_superuser:
        return AdministrationScope(True, frozenset())
    pairs = frozenset(
        effective_scoped_memberships(actor)
        .filter(
            group__permissions__content_type__app_label=_MANAGE_APP_LABEL,
            group__permissions__codename=_MANAGE_CODENAME,
        )
        .values_list("event_edition_id", "organization_id")
        .distinct()
    )
    return AdministrationScope((None, None) in pairs, pairs)


def is_account_administrator(actor) -> bool:
    return has_scoped_permission(actor, MANAGE_PERMISSION)


def _current_memberships(user, *, now=None):
    """Memberships that are, or will become, effective: not ended, not
    suspended. Ended rows stay as history."""
    now = now or timezone.now()
    return ScopedGroupMembership.objects.filter(
        user=user, status=ScopedGroupMembershipStatus.ACTIVE
    ).filter(Q(active_until__isnull=True) | Q(active_until__gt=now))


def can_administer_account(actor, target, scope: AdministrationScope | None = None) -> bool:
    """Whether `actor` may change `target`'s account (status, validity,
    credentials): never themselves, a superuser only by a superuser, and
    only when the actor's scope covers every current membership of target."""
    if target is None or getattr(actor, "pk", None) is None or actor.pk == target.pk:
        return False
    scope = scope or administration_scope(actor)
    if not (scope.unrestricted or scope.pairs):
        return False
    if target.is_superuser and not actor.is_superuser:
        return False
    return all(
        scope.covers(event_id, organization_id)
        for event_id, organization_id in _current_memberships(target).values_list(
            "event_edition_id", "organization_id"
        )
    )


def _covered_memberships_q(scope: AdministrationScope, prefix: str = "") -> Q:
    """A filter matching the memberships whose (event, organization) the
    scope covers. An empty event or organization on a membership ("every")
    is covered only by an administrator without that limit."""
    if scope.unrestricted:
        return Q()
    condition = Q(pk__in=[])
    for event_id, organization_id in scope.pairs:
        pair = Q()
        if event_id is not None:
            pair &= Q(**{f"{prefix}event_edition_id": event_id})
        if organization_id is not None:
            pair &= Q(**{f"{prefix}organization_id": organization_id})
        condition |= pair
    return condition


def visible_memberships(scope: AdministrationScope, queryset=None):
    """The memberships (current and past) an administrator may read."""
    queryset = ScopedGroupMembership.objects.all() if queryset is None else queryset
    if scope.unrestricted:
        return queryset
    if not scope.pairs:
        return queryset.none()
    return queryset.filter(_covered_memberships_q(scope))


def visible_accounts(actor, scope: AdministrationScope | None = None):
    """The accounts an administrator may list, search, count and open.

    * An administrator without an event or organization limit reads every
      account.
    * A scoped administrator reads an account that has at least one
      membership (current or past) inside their scope, and an account they
      created themselves while it has no current membership at all (so a new
      account stays reachable for its first role). Other new or unassigned
      accounts are read and assigned by an administrator without limits.

    An administrator always reads their own account (and still cannot change
    it)."""
    scope = scope or administration_scope(actor)
    accounts = OperationalUser.objects.all()
    if scope.unrestricted:
        return accounts
    if not scope.pairs:
        return accounts.none()
    covered = ScopedGroupMembership.objects.filter(user=OuterRef("pk")).filter(
        _covered_memberships_q(scope)
    )
    current = ScopedGroupMembership.objects.filter(
        user=OuterRef("pk"), status=ScopedGroupMembershipStatus.ACTIVE
    ).filter(Q(active_until__isnull=True) | Q(active_until__gt=timezone.now()))
    return accounts.filter(
        Q(Exists(covered))
        | Q(pk=getattr(actor, "pk", None))
        | (Q(created_by_id=getattr(actor, "pk", None)) & ~Q(Exists(current)))
    )


def _require_administrator(actor) -> AdministrationScope:
    """The actor's scope, read from the database NOW (a concurrently
    suspended administrator is refused even if their request began before)."""
    fresh = OperationalUser.objects.filter(pk=getattr(actor, "pk", None)).first()
    if fresh is None:
        raise AccountAdministrationError("NOT_PERMITTED", "Not an account administrator.")
    scope = administration_scope(fresh)
    if not (scope.unrestricted or scope.pairs):
        raise AccountAdministrationError("NOT_PERMITTED", "Not an account administrator.")
    return scope


def _require_account_authority(actor, target, scope) -> None:
    if getattr(actor, "pk", None) == target.pk:
        raise AccountAdministrationError("SELF_CHANGE", "Administrators never change themselves.")
    if not can_administer_account(actor, target, scope):
        raise AccountAdministrationError(
            "SCOPE_NOT_DELEGATABLE", "The account has access outside your scope."
        )


# ---------------------------------------------------------------------------
# Last-administrator protection
# ---------------------------------------------------------------------------


def _lock_administration() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", [ADVISORY_LOCK_CLASS_ACCOUNT_ADMINISTRATION, 0]
        )


def unrestricted_administrator_ids(*, now=None) -> set:
    """Accounts that can administer every account: active superusers, and
    active accounts with an effective account-administration membership not
    narrowed to an event, organization, venue or gate."""
    now = now or timezone.now()
    active = (
        OperationalUser.objects.filter(status=OperationalUserStatus.ACTIVE)
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
    )
    superusers = set(active.filter(is_superuser=True).values_list("pk", flat=True))
    members = set(
        ScopedGroupMembership.objects.filter(
            user__in=active,
            status=ScopedGroupMembershipStatus.ACTIVE,
            event_edition__isnull=True,
            organization__isnull=True,
            venue__isnull=True,
            gate__isnull=True,
            group__permissions__content_type__app_label=_MANAGE_APP_LABEL,
            group__permissions__codename=_MANAGE_CODENAME,
        )
        .filter(Q(active_from__isnull=True) | Q(active_from__lte=now))
        .filter(Q(active_until__isnull=True) | Q(active_until__gt=now))
        .values_list("user_id", flat=True)
    )
    return superusers | members


def _require_an_administrator_remains(*, before: set) -> None:
    """Refuse (inside the caller's transaction, so everything rolls back)
    when this command removed the last unrestricted administrator."""
    if before and not unrestricted_administrator_ids():
        raise AccountAdministrationError(
            "LAST_ADMINISTRATOR", "The last account administrator cannot be removed."
        )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(
    *,
    action_code: str,
    actor,
    target_uuid,
    result: str = "SUCCESS",
    reason_code: str = "",
    before: dict | None = None,
    after: dict | None = None,
    event_edition_id=None,
    correlation_id: str = "",
) -> None:
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type="OperationalUser",
            target_uuid=target_uuid,
            event_edition_id=event_edition_id,
            result=result,
            reason_code=reason_code[:100] or None,
            before_summary=before,
            after_summary=after,
            correlation_id=correlation_id,
        )
    )


def record_refusal(*, actor, target_uuid, code: str, correlation_id: str = "") -> None:
    """Audit a refused command in its own transaction (the refused one rolled
    back): who tried what on which account, with the refusal code only."""
    with transaction.atomic():
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_ACTION_REFUSED,
            actor=actor,
            target_uuid=target_uuid,
            result="DENIED",
            reason_code=code,
            correlation_id=correlation_id,
        )


def _normalize_reason(reason: str) -> str:
    reason = " ".join((reason or "").split())
    if not REASON_MIN_LENGTH <= len(reason) <= REASON_MAX_LENGTH:
        raise AccountAdministrationError("REASON_REQUIRED", "A reason is required.")
    return reason


def _iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def _validate_window(active_from, active_until, *, account_type: str) -> None:
    if active_from and active_until and active_until <= active_from:
        raise AccountAdministrationError("INVALID_VALIDITY", "The end must follow the start.")
    if account_type == OperationalUserAccountType.EXTERNAL_SECURITY:
        if active_until is None or active_until <= timezone.now():
            raise AccountAdministrationError(
                "EXPIRY_REQUIRED", "A temporary external-security account needs a future end."
            )


def create_account(
    *,
    actor,
    email: str,
    display_name: str,
    account_type: str,
    active_from: datetime | None = None,
    active_until: datetime | None = None,
    correlation_id: str = "",
) -> OperationalUser:
    """Create an INVITED account without a usable password, roles or session.
    Activation, credentials and roles are separate commands."""
    _require_administrator(actor)
    if account_type not in CREATABLE_ACCOUNT_TYPES:
        raise AccountAdministrationError("INVALID_ACCOUNT_TYPE", "Unknown account type.")
    display_name = " ".join((display_name or "").split())[:200]
    if not display_name:
        raise AccountAdministrationError("NAME_REQUIRED", "A display name is required.")
    _validate_window(active_from, active_until, account_type=account_type)
    email_normalized = OperationalUser.objects.normalize_email(email or "").strip().lower()
    if "@" not in email_normalized:
        raise AccountAdministrationError("EMAIL_INVALID", "A valid email address is required.")
    try:
        with transaction.atomic():
            user = OperationalUser.objects.create_user(
                email=email_normalized,
                password=None,
                display_name=display_name,
                account_type=account_type,
                status=OperationalUserStatus.INVITED,
                active_from=active_from,
                active_until=active_until,
                created_by=actor if getattr(actor, "pk", None) else None,
            )
            _audit(
                action_code=action_codes.STAFF_ACCOUNT_CREATED,
                actor=actor,
                target_uuid=user.pk,
                after={
                    "status": user.status,
                    "account_type": account_type,
                    "active_from": _iso(active_from),
                    "active_until": _iso(active_until),
                },
                correlation_id=correlation_id,
            )
    except IntegrityError:
        raise AccountAdministrationError("EMAIL_TAKEN", "An account uses this email.") from None
    return user


def _lock_target(target_id) -> OperationalUser:
    return OperationalUser.objects.select_for_update().get(pk=target_id)


def update_account(
    *,
    actor,
    target,
    display_name: str,
    active_from: datetime | None,
    active_until: datetime | None,
    reason: str,
    correlation_id: str = "",
) -> OperationalUser:
    """Change the display name and the account's own validity window. An end
    of validity also ends every role later than it (an account never outlives
    its own window through a role); a past end takes effect immediately."""
    _require_administrator(actor)
    reason = _normalize_reason(reason)
    display_name = " ".join((display_name or "").split())[:200]
    if not display_name:
        raise AccountAdministrationError("NAME_REQUIRED", "A display name is required.")
    with transaction.atomic():
        _lock_administration()
        scope = _require_administrator(actor)
        before_admins = unrestricted_administrator_ids()
        locked = _lock_target(target.pk)
        _require_account_authority(actor, locked, scope)
        _validate_window(active_from, active_until, account_type=locked.account_type)
        before = {
            "active_from": _iso(locked.active_from),
            "active_until": _iso(locked.active_until),
        }
        locked.display_name = display_name
        locked.active_from = active_from
        locked.active_until = active_until
        locked.save(update_fields=["display_name", "active_from", "active_until", "updated_at"])
        if active_until is not None:
            _current_memberships(locked).filter(
                Q(active_until__isnull=True) | Q(active_until__gt=active_until)
            ).update(active_until=active_until)
        _require_an_administrator_remains(before=before_admins)
        _end_sessions_if_inactive(locked)
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_UPDATED,
            actor=actor,
            target_uuid=locked.pk,
            reason_code=reason,
            before=before,
            after={"active_from": _iso(active_from), "active_until": _iso(active_until)},
            correlation_id=correlation_id,
        )
    return locked


def _end_sessions_if_inactive(user) -> None:
    """An account that can no longer be used loses its entry operator
    sessions at once; its web sessions end on their next request
    (`OperationalSessionExpiryMiddleware` re-checks `is_active`)."""
    if not user.is_active:
        from apps.entry.services.sessions import end_operator_sessions_for_user

        end_operator_sessions_for_user(user=user, reason="ACCOUNT_DEACTIVATED", now=timezone.now())


def change_status(
    *, actor, target, command: str, reason: str, correlation_id: str = ""
) -> OperationalUser:
    """Activate, suspend or disable an account (nothing is ever deleted).

    Suspension and disablement take effect on the account's next request and
    end its entry operator sessions now. Activation never grants a role and
    never sets a password."""
    if command not in _STATUS_COMMANDS:
        raise AccountAdministrationError("INVALID_STATE", "Unknown status command.")
    _require_administrator(actor)
    reason = _normalize_reason(reason)
    new_status, allowed_from = _STATUS_COMMANDS[command]
    with transaction.atomic():
        _lock_administration()
        scope = _require_administrator(actor)
        before_admins = unrestricted_administrator_ids()
        locked = _lock_target(target.pk)
        _require_account_authority(actor, locked, scope)
        if locked.status == new_status:
            return locked
        if locked.status not in allowed_from:
            raise AccountAdministrationError("INVALID_STATE", "Not possible in this status.")
        previous = locked.status
        locked.status = new_status
        locked.save(update_fields=["status", "updated_at"])
        _require_an_administrator_remains(before=before_admins)
        _end_sessions_if_inactive(locked)
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_STATUS_CHANGED,
            actor=actor,
            target_uuid=locked.pk,
            reason_code=reason,
            before={"status": previous},
            after={"status": new_status},
            correlation_id=correlation_id,
        )
    return locked


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def _validate_role_scope(role: roles.StaffRole, *, event, organization, venue, gate) -> None:
    if role.global_only and any(value is not None for value in (event, organization, venue, gate)):
        raise AccountAdministrationError(
            "INVALID_SCOPE", "This role is granted only for every event and organization."
        )
    if organization is not None and roles.ORGANIZATION not in role.scopes:
        raise AccountAdministrationError(
            "INVALID_SCOPE", "This role cannot be limited to an organization."
        )
    if (venue is not None or gate is not None) and roles.CHECKPOINT not in role.scopes:
        raise AccountAdministrationError(
            "INVALID_SCOPE", "This role cannot be limited to a checkpoint."
        )
    if gate is not None:
        venue = venue or gate.venue
        if gate.venue_id != venue.pk:
            raise AccountAdministrationError("INVALID_SCOPE", "The gate belongs to another venue.")
    if venue is not None and (event is None or venue.event_edition_id != event.pk):
        raise AccountAdministrationError(
            "INVALID_SCOPE", "A checkpoint scope needs the event of that venue."
        )


def grant_role(
    *,
    actor,
    target,
    role_key: str,
    event=None,
    all_events: bool = False,
    organization=None,
    venue=None,
    gate=None,
    active_from: datetime | None = None,
    active_until: datetime | None = None,
    reason: str,
    correlation_id: str = "",
) -> ScopedGroupMembership:
    """Grant one catalogue role in one explicit scope.

    An empty event is never implied: `all_events=True` must be passed on
    purpose (and is delegatable only by an administrator without an event
    limit). An empty organization means "every organization" and likewise
    needs an administrator without an organization limit. The membership
    never outlives the account's own validity."""
    role = roles.ROLES_BY_KEY.get(role_key)
    if role is None:
        raise AccountAdministrationError("INVALID_ROLE", "Unknown role.")
    if event is None and not all_events:
        raise AccountAdministrationError("EVENT_REQUIRED", "Choose an event, or all events.")
    if event is not None and all_events:
        raise AccountAdministrationError("INVALID_SCOPE", "Choose one event or all events.")
    if gate is not None and venue is None:
        venue = gate.venue
    _validate_role_scope(role, event=event, organization=organization, venue=venue, gate=gate)
    scope = _require_administrator(actor)
    event_id = getattr(event, "pk", None)
    organization_id = getattr(organization, "pk", None)
    if not scope.covers(event_id, organization_id):
        raise AccountAdministrationError(
            "SCOPE_NOT_DELEGATABLE", "This scope is outside your own administration scope."
        )
    reason = _normalize_reason(reason)
    now = timezone.now()
    with transaction.atomic():
        _lock_administration()
        if not _require_administrator(actor).covers(event_id, organization_id):
            raise AccountAdministrationError(
                "SCOPE_NOT_DELEGATABLE", "This scope is outside your own administration scope."
            )
        locked = _lock_target(target.pk)
        if getattr(actor, "pk", None) == locked.pk:
            raise AccountAdministrationError(
                "SELF_CHANGE", "Administrators never change themselves."
            )
        if locked.is_superuser and not actor.is_superuser:
            raise AccountAdministrationError("SCOPE_NOT_DELEGATABLE", "A superuser account.")
        if locked.status == OperationalUserStatus.DISABLED:
            raise AccountAdministrationError("INVALID_STATE", "The account is disabled.")
        start = active_from or now
        end = active_until
        if locked.active_until is not None and (end is None or end > locked.active_until):
            end = locked.active_until
        if locked.account_type == OperationalUserAccountType.EXTERNAL_SECURITY and end is None:
            raise AccountAdministrationError(
                "EXPIRY_REQUIRED", "A temporary external-security role needs an end."
            )
        if end is not None and end <= start:
            raise AccountAdministrationError("INVALID_VALIDITY", "The end must follow the start.")
        membership = ScopedGroupMembership.objects.create(
            user=locked,
            group=Group.objects.get(name=role.group_name),
            event_edition=event,
            organization=organization,
            venue=venue,
            gate=gate,
            active_from=start,
            active_until=end,
            status=ScopedGroupMembershipStatus.ACTIVE,
            granted_by=actor,
            reason=reason,
        )
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_ROLE_GRANTED,
            actor=actor,
            target_uuid=locked.pk,
            event_edition_id=event_id,
            reason_code=reason,
            after={
                "membership": str(membership.pk),
                "role": role.key,
                "event": str(event_id) if event_id else None,
                "organization": str(organization_id) if organization_id else None,
                "venue": str(venue.pk) if venue is not None else None,
                "gate": str(gate.pk) if gate is not None else None,
                "active_from": _iso(start),
                "active_until": _iso(end),
            },
            correlation_id=correlation_id,
        )
    return membership


def revoke_role(
    *, actor, target, membership_id, reason: str, correlation_id: str = ""
) -> ScopedGroupMembership:
    """End one membership now (it stays as history). Takes effect on the
    account's next request; refused when it is the last unrestricted
    account administration membership."""
    _require_administrator(actor)
    reason = _normalize_reason(reason)
    now = timezone.now()
    with transaction.atomic():
        _lock_administration()
        scope = _require_administrator(actor)
        before_admins = unrestricted_administrator_ids()
        locked = _lock_target(target.pk)
        if getattr(actor, "pk", None) == locked.pk:
            raise AccountAdministrationError(
                "SELF_CHANGE", "Administrators never change themselves."
            )
        membership = (
            ScopedGroupMembership.objects.select_for_update()
            .filter(pk=membership_id, user=locked)
            .first()
        )
        if membership is None:
            raise AccountAdministrationError("NOT_FOUND", "No such role on this account.")
        if not scope.covers(membership.event_edition_id, membership.organization_id):
            raise AccountAdministrationError(
                "SCOPE_NOT_DELEGATABLE", "This role is outside your administration scope."
            )
        if membership.active_until is not None and membership.active_until <= now:
            return membership
        previous_end = membership.active_until
        membership.active_until = now
        membership.save(update_fields=["active_until", "updated_at"])
        _require_an_administrator_remains(before=before_admins)
        role = roles.role_for_group_name(membership.group.name)
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_ROLE_REVOKED,
            actor=actor,
            target_uuid=locked.pk,
            event_edition_id=membership.event_edition_id,
            reason_code=reason,
            before={"membership": str(membership.pk), "active_until": _iso(previous_end)},
            after={
                "role": role.key if role else membership.group.name,
                "active_until": _iso(membership.active_until),
            },
            correlation_id=correlation_id,
        )
    return membership


# ---------------------------------------------------------------------------
# Credential setup and reset links
# ---------------------------------------------------------------------------

#: Session key holding the token id between the link and the password form.
SETUP_SESSION_KEY = "staff_credential_setup_token"


def _digest(raw_link: str) -> str:
    return hashlib.sha256(raw_link.encode("utf-8")).hexdigest()


def setup_url(raw_link: str) -> str:
    from django.urls import reverse

    path = reverse("accounts:credential-setup-start", kwargs={"token": raw_link})
    return f"{settings.PUBLIC_BASE_URL}{path}"


def _new_token(user, *, purpose: str, created_by, now) -> tuple[CredentialSetupToken, str]:
    CredentialSetupToken.objects.filter(
        user=user, used_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now)
    raw = secrets.token_urlsafe(32)
    token = CredentialSetupToken.objects.create(
        user=user,
        purpose=purpose,
        token_digest=_digest(raw),
        created_by=created_by,
        expires_at=now + timedelta(seconds=int(settings.OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS)),
    )
    return token, raw


def deliver_setup_link(*, email: str, raw_link: str, purpose: str) -> None:
    """Send the link by email, in the active language. Called after commit;
    the link exists only in this message and in the recipient's mailbox."""
    from django.core.mail import send_mail
    from django.utils.translation import gettext as _

    hours = max(1, int(settings.OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS) // 3600)
    if purpose == CredentialSetupPurpose.RESET:
        subject = _("Reset the password of your ASC 2026 staff account")
        intro = _("A password reset was requested for your ASC 2026 staff account.")
    else:
        subject = _("Set the password of your ASC 2026 staff account")
        intro = _("A staff account was created for you on the ASC 2026 platform.")
    body = "\n\n".join(
        [
            intro,
            _(
                "Open this link to choose your password. It works once and expires in "
                "%(hours)s hours:"
            )
            % {"hours": hours},
            setup_url(raw_link),
            _(
                "After that, sign in with your email address and password. Your account opens "
                "only once an administrator has activated it and given it a role. If you did "
                "not expect this message, ignore it."
            ),
        ]
    )
    send_mail(subject=subject, message=body, from_email=None, recipient_list=[email])


def record_delivery_failure(
    *, user_id, raw_link: str, error: BaseException, correlation_id=""
) -> None:
    """The email carrying `raw_link` could not be sent: revoke that link (an
    ambiguous failure may still have delivered it -- a dead link is the safe
    side) and audit the failure with the error class only, never the link,
    the address or the error text."""
    with transaction.atomic():
        CredentialSetupToken.objects.filter(
            token_digest=_digest(raw_link), used_at__isnull=True, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        _audit(
            action_code=action_codes.STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED,
            actor=None,
            target_uuid=user_id,
            result="FAILURE",
            reason_code=type(error).__name__,
            correlation_id=correlation_id,
        )


def _deliver_or_record_failure(
    failures: list, *, user_id, email, raw_link, purpose, correlation_id
):
    try:
        deliver_setup_link(email=email, raw_link=raw_link, purpose=purpose)
    except Exception as exc:  # any transport error: report it, never leave a live unsent link
        record_delivery_failure(
            user_id=user_id, raw_link=raw_link, error=exc, correlation_id=correlation_id
        )
        failures.append(type(exc).__name__)


def issue_credential_setup(
    *, actor, target, purpose: str, correlation_id: str = ""
) -> CredentialSetupToken:
    """Send a new single-use setup (invitation) or reset link to the account's
    own email address. Replaces any earlier open link. Never changes the
    current password, the status or the roles.

    The email is sent after commit. When it cannot be sent, the new link is
    revoked, the failure is audited and `DELIVERY_FAILED` is raised, so the
    administrator is told the truth and simply sends another link."""
    if purpose not in CredentialSetupPurpose.values:
        raise AccountAdministrationError("INVALID_PURPOSE", "Unknown link purpose.")
    scope = _require_administrator(actor)
    failures: list[str] = []
    with transaction.atomic():
        locked = _lock_target(target.pk)
        _require_account_authority(actor, locked, scope)
        if locked.status == OperationalUserStatus.DISABLED:
            raise AccountAdministrationError("INVALID_STATE", "The account is disabled.")
        now = timezone.now()
        token, raw = _new_token(locked, purpose=purpose, created_by=actor, now=now)
        _audit(
            action_code=action_codes.STAFF_CREDENTIAL_SETUP_ISSUED,
            actor=actor,
            target_uuid=locked.pk,
            after={"purpose": purpose, "expires_at": _iso(token.expires_at)},
            correlation_id=correlation_id,
        )
        transaction.on_commit(
            partial(
                _deliver_or_record_failure,
                failures,
                user_id=locked.pk,
                email=locked.email_normalized,
                raw_link=raw,
                purpose=purpose,
                correlation_id=correlation_id,
            )
        )
    if failures:
        raise AccountAdministrationError("DELIVERY_FAILED", "The setup link could not be sent.")
    return token


def find_open_token(raw_link: object, *, now=None) -> CredentialSetupToken | None:
    """The open, unexpired token of a link, or None (the same answer for an
    unknown, used, replaced or expired link)."""
    if not isinstance(raw_link, str) or not 20 <= len(raw_link) <= 128:
        return None
    now = now or timezone.now()
    return (
        CredentialSetupToken.objects.select_related("user")
        .filter(
            token_digest=_digest(raw_link),
            used_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=now,
        )
        .exclude(user__status=OperationalUserStatus.DISABLED)
        .first()
    )


def open_token_by_id(token_id, *, now=None) -> CredentialSetupToken | None:
    now = now or timezone.now()
    try:
        token_id = uuid.UUID(str(token_id))
    except ValueError:
        return None
    return (
        CredentialSetupToken.objects.select_related("user")
        .filter(pk=token_id, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now)
        .exclude(user__status=OperationalUserStatus.DISABLED)
        .first()
    )


def complete_credential_setup(
    *, token_id, password: str, correlation_id: str = ""
) -> OperationalUser:
    """Set the password through an open link, exactly once.

    Lock order (the same as every command that issues or replaces a link):
    the ACCOUNT first, then its token. The token is only read to find its
    account, then locked and re-checked under both locks -- so a replacement
    racing this completion either revokes it first (this one is refused) or
    waits until it is used (the replacement then leaves it as history); two
    submissions of one link set at most one password. Validators run against
    the account. Changing the password ends the account's other web sessions
    (Django's session hash)."""
    owner_id = (
        CredentialSetupToken.objects.filter(pk=token_id).values_list("user_id", flat=True).first()
    )
    if owner_id is None:
        raise AccountAdministrationError("LINK_INVALID", "This link is no longer valid.")
    with transaction.atomic():
        user = _lock_target(owner_id)
        token = (
            CredentialSetupToken.objects.select_for_update()
            .filter(pk=token_id, user_id=owner_id, used_at__isnull=True, revoked_at__isnull=True)
            .first()
        )
        now = timezone.now()
        if token is None or token.expires_at <= now:
            raise AccountAdministrationError("LINK_INVALID", "This link is no longer valid.")
        if user.status == OperationalUserStatus.DISABLED:
            raise AccountAdministrationError("LINK_INVALID", "This link is no longer valid.")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            error = AccountAdministrationError("PASSWORD_INVALID", "The password was refused.")
            error.messages = exc.messages
            raise error from None
        user.set_password(password)
        user.save(update_fields=["password", "updated_at"])
        token.used_at = now
        token.save(update_fields=["used_at"])
        _audit(
            action_code=action_codes.STAFF_CREDENTIAL_SETUP_COMPLETED,
            actor=None,
            target_uuid=user.pk,
            after={"purpose": token.purpose},
            correlation_id=correlation_id,
        )
    return user


# ---------------------------------------------------------------------------
# Initial administrator
# ---------------------------------------------------------------------------


def bootstrap_administrator(
    *, email: str, display_name: str, existing: bool = False, correlation_id: str = ""
) -> tuple[OperationalUser, str]:
    """The protected first-administrator setup (server-side command only).

    Refused when an unrestricted account administrator already exists. A new
    account is created ACTIVE without a usable password, given the Account
    Administrators role for every event and organization, and a single-use
    setup link is returned to the caller for delivery. `existing=True`
    promotes an existing, non-disabled account instead (its password is
    unchanged and the returned link is a reset link)."""
    email_normalized = OperationalUser.objects.normalize_email(email or "").strip().lower()
    if "@" not in email_normalized:
        raise AccountAdministrationError("EMAIL_INVALID", "A valid email address is required.")
    with transaction.atomic():
        _lock_administration()
        if unrestricted_administrator_ids():
            raise AccountAdministrationError(
                "ADMINISTRATOR_EXISTS", "An account administrator already exists."
            )
        user = (
            OperationalUser.objects.select_for_update()
            .filter(email_normalized=email_normalized)
            .first()
        )
        if user is not None and not existing:
            raise AccountAdministrationError("EMAIL_TAKEN", "An account uses this email.")
        if user is None and existing:
            raise AccountAdministrationError("NOT_FOUND", "No account uses this email.")
        if user is None:
            user = OperationalUser.objects.create_user(
                email=email_normalized,
                password=None,
                display_name=" ".join((display_name or email_normalized).split())[:200],
                account_type=OperationalUserAccountType.INTERNAL,
                status=OperationalUserStatus.ACTIVE,
            )
            purpose = CredentialSetupPurpose.INVITATION
        else:
            if user.status == OperationalUserStatus.DISABLED:
                raise AccountAdministrationError("INVALID_STATE", "The account is disabled.")
            user.status = OperationalUserStatus.ACTIVE
            user.save(update_fields=["status", "updated_at"])
            purpose = CredentialSetupPurpose.RESET
        ScopedGroupMembership.objects.create(
            user=user,
            group=Group.objects.get(name="Account Administrators"),
            status=ScopedGroupMembershipStatus.ACTIVE,
            active_from=timezone.now(),
            granted_by=user,
            reason="Initial account administrator (bootstrap command)",
        )
        _token, raw = _new_token(user, purpose=purpose, created_by=None, now=timezone.now())
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_ADMINISTRATOR_BOOTSTRAPPED,
            actor=None,
            target_uuid=user.pk,
            after={"existing": bool(existing), "purpose": purpose},
            correlation_id=correlation_id,
        )
    return user, raw


def resend_bootstrap_setup(*, email: str, correlation_id: str = "") -> tuple[OperationalUser, str]:
    """Recovery for a first administrator whose setup link never arrived.

    `bootstrap_administrator` commits the account before the email is sent;
    if sending fails, the bootstrap command refuses a second run because an
    administrator now exists, and nobody can use the operations area yet.
    This issues a NEW single-use link (the earlier one is revoked) for that
    same pending account only, and only while:

    * it is the account the bootstrap command set up (its audit event exists);
    * its setup was never completed (no usable password, no used link);
    * it is still active and still an administrator with full scope;
    * no other administrator with full scope and a password exists (one who
      does uses the operations area instead).

    It never creates or promotes an account and never changes a password."""
    email_normalized = OperationalUser.objects.normalize_email(email or "").strip().lower()
    with transaction.atomic():
        _lock_administration()
        user = (
            OperationalUser.objects.select_for_update()
            .filter(email_normalized=email_normalized)
            .first()
        )
        if user is None:
            raise AccountAdministrationError("NOT_FOUND", "No account uses this email.")
        from apps.audit.models import AuditEvent

        bootstrapped = AuditEvent.objects.filter(
            action_code=action_codes.STAFF_ACCOUNT_ADMINISTRATOR_BOOTSTRAPPED, target_uuid=user.pk
        ).exists()
        if not bootstrapped:
            raise AccountAdministrationError(
                "NOT_PENDING_BOOTSTRAP", "This account was not set up by the bootstrap command."
            )
        if (
            user.has_usable_password()
            or CredentialSetupToken.objects.filter(user=user, used_at__isnull=False).exists()
        ):
            raise AccountAdministrationError(
                "SETUP_COMPLETED", "This account already completed its setup."
            )
        administrators = unrestricted_administrator_ids()
        if user.pk not in administrators or user.status != OperationalUserStatus.ACTIVE:
            raise AccountAdministrationError(
                "NOT_PENDING_BOOTSTRAP", "This account is no longer the pending administrator."
            )
        others = OperationalUser.objects.filter(pk__in=administrators - {user.pk})
        if any(other.has_usable_password() for other in others):
            raise AccountAdministrationError(
                "ADMINISTRATOR_EXISTS", "Another account administrator can send links."
            )
        purpose = (
            CredentialSetupToken.objects.filter(user=user)
            .order_by("created_at")
            .values_list("purpose", flat=True)
            .first()
            or CredentialSetupPurpose.INVITATION
        )
        _token, raw = _new_token(user, purpose=purpose, created_by=None, now=timezone.now())
        _audit(
            action_code=action_codes.STAFF_ACCOUNT_ADMINISTRATOR_SETUP_RESENT,
            actor=None,
            target_uuid=user.pk,
            after={"purpose": purpose, "expires_at": _iso(_token.expires_at)},
            correlation_id=correlation_id,
        )
    return user, raw


# ---------------------------------------------------------------------------
# Presentation helpers (read only)
# ---------------------------------------------------------------------------

MEMBERSHIP_EFFECTIVE = "EFFECTIVE"
MEMBERSHIP_SCHEDULED = "SCHEDULED"
MEMBERSHIP_ENDED = "ENDED"
MEMBERSHIP_SUSPENDED = "SUSPENDED"


def membership_state(membership, *, now=None) -> str:
    now = now or timezone.now()
    if membership.status != ScopedGroupMembershipStatus.ACTIVE:
        return MEMBERSHIP_SUSPENDED
    if membership.active_until is not None and membership.active_until <= now:
        return MEMBERSHIP_ENDED
    if membership.active_from is not None and membership.active_from > now:
        return MEMBERSHIP_SCHEDULED
    return MEMBERSHIP_EFFECTIVE


ACCESS_NONE = "NO_ACCESS"
ACCESS_NOT_SET_UP = "NO_PASSWORD"
ACCESS_NO_ROLE = "NO_ROLE"
ACCESS_OK = "OK"


def effective_access(user, memberships, *, now=None) -> str:
    """Why an account can or cannot work now, separately from its status:
    not usable (status or validity), no password yet, no effective role, or
    usable."""
    now = now or timezone.now()
    if not user.is_active:
        return ACCESS_NONE
    if not user.has_usable_password():
        return ACCESS_NOT_SET_UP
    if not any(membership_state(m, now=now) == MEMBERSHIP_EFFECTIVE for m in memberships):
        return ACCESS_NO_ROLE
    return ACCESS_OK
