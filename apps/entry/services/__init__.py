"""Online entry services (Phase 3 Prompt 4, ADR-0021).

Every state change lives in a submodule of this package, inside an explicit
transaction, never in a view, a form, a model `save()`, or a signal:

* `devices`       -- registration, enrollment, scope versions, lifecycle;
* `sessions`      -- checkpoint (device) sessions and operator sessions;
* `restrictions`  -- person/context security restrictions;
* `access`        -- the pure admission evaluator (no writes);
* `verification`  -- QR / identity / reference / manual lookup;
* `decisions`     -- Entry Events and reason-coded overrides.

This module holds only the shared errors and small helpers. Nothing here
ever logs, audits, or returns a QR token, a NIN/passport value, a search
string, a device secret, or an activation code.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.middleware.correlation import get_correlation_id


class EntryServiceError(Exception):
    """Base for every entry-domain failure.

    The message is developer-facing English and is never rendered to a user
    (Phase 3 Prompt 8, P8-01). `code` is a stable, machine-readable
    refinement of the class that `apps.entry.presentation` maps to a
    localized message; services never translate.
    """

    code = ""

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class EntryPermissionError(EntryServiceError):
    """The acting user lacks the permission for this checkpoint action."""


class EntryConcurrencyError(EntryServiceError):
    """The caller's expected version is stale."""


class EntryStateError(EntryServiceError):
    """The current state does not permit the requested action."""


def new_public_id() -> str:
    """A random 128-bit URL-safe identifier (22 characters)."""
    return secrets.token_urlsafe(16)[:22]


def new_operation_id() -> str:
    return secrets.token_urlsafe(24)


def secret_digest(value: str) -> str:
    """SHA-256 hex digest of a high-entropy secret (device credential or
    activation code). Only ever applied to values drawn from `secrets`."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def audit(
    *,
    action_code: str,
    actor=None,
    target_type: str = "",
    target_uuid=None,
    event_edition_id=None,
    result: str = "SUCCESS",
    reason_code: str = "",
    after_summary: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> None:
    """Append one audit event in the caller's transaction."""
    if actor_type is None:
        actor_type = "OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM"
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type=actor_type,
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=target_type,
            target_uuid=target_uuid,
            event_edition_id=event_edition_id,
            result=result,
            reason_code=reason_code[:100],
            after_summary=after_summary,
            correlation_id=get_correlation_id() or "",
        )
    )
