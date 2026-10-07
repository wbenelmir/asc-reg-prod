"""Ministry NIN service diagnostics (operations page `/ops/integrations/ministry-nin/`).

Three separate checks for an authorized technical operator, none of which
touches a participant:

1. **Configuration readiness** -- no network request. Which backend is
   configured, whether it is the official adapter, and the NAMES of missing or
   malformed settings (never a value, a host or a path).
2. **Authentication** -- one fresh authentication request through the
   official adapter (`MinistryNinProvider.probe_authentication`): never the
   cached token, and the token obtained is discarded. Success proves only that
   the authentication endpoint accepted the configured credentials.
3. **Lookup** -- one lookup of an operator-supplied, authorized test NIN
   through the same adapter as registrations (`lookup`), reusing its
   validation, transport, token cache and response parsing. It creates and
   changes nothing: no registration, person, identity case, decision, pass,
   badge or notification. Only the kind of answer is reported, never the
   identity data.

Every check needs `people.run_ministry_nin_diagnostics` through a GLOBAL
membership (no event, organization or checkpoint limit) or a superuser,
checked here on every entry point. Network checks are bounded by the shared
counter (`apps.core.issuance_counter`, its own namespace and limits), and only
one runs at a time across every process (a non-blocking advisory lock).

Each network check is audited (`IDV_NIN_DIAGNOSTIC_RUN`) with the operator,
the environment, the provider code, the action, the outcome, the HTTP status
and the duration -- never the NIN, a credential, a token, a body or identity
data. Nothing is logged.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.accounts.policies import effective_scoped_memberships
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.concurrency import ADVISORY_LOCK_CLASS_NIN_DIAGNOSTICS
from apps.people.identity_contract import NIN_PATTERN, LookupKind
from apps.people.nin_provider import (
    LookupOutcome,
    MinistryApiConfig,
    MinistryNinProvider,
    get_nin_provider,
)

PERMISSION = "people.run_ministry_nin_diagnostics"
_APP_LABEL, _, _CODENAME = PERMISSION.partition(".")

ACTION_AUTHENTICATION = "AUTHENTICATION"
ACTION_LOOKUP = "LOOKUP"

#: Outcome codes shown to the operator (each has a localized meaning).
NOT_OFFICIAL = "NOT_OFFICIAL"
NOT_CONFIGURED = "NOT_CONFIGURED"
AUTH_OK = "AUTH_OK"
AUTH_FAILED = "AUTH_FAILED"
IDENTITY_FOUND = "IDENTITY_FOUND"
IDENTITY_NOT_FOUND = "IDENTITY_NOT_FOUND"
UNAVAILABLE = "UNAVAILABLE"
REDIRECT_REFUSED = "REDIRECT_REFUSED"
MALFORMED = "MALFORMED"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
INVALID_INPUT = "INVALID_INPUT"
THROTTLED = "THROTTLED"
BUSY = "BUSY"

#: Outcomes that do not reach the provider at all.
LOCAL_OUTCOMES = frozenset({NOT_OFFICIAL, NOT_CONFIGURED, INVALID_INPUT, THROTTLED, BUSY})


class DiagnosticsDenied(Exception):
    """The user may not run the Ministry NIN diagnostics."""


def can_run(user) -> bool:
    """A superuser, or an effective membership granting the permission with
    no event, organization, venue or gate limit. An event- or
    organization-scoped role never opens this global page."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if user.is_superuser:
        return True
    return (
        effective_scoped_memberships(user)
        .filter(
            event_edition__isnull=True,
            organization__isnull=True,
            group__permissions__content_type__app_label=_APP_LABEL,
            group__permissions__codename=_CODENAME,
        )
        .exists()
    )


def _require(user) -> None:
    if not can_run(user):
        raise DiagnosticsDenied("Not authorized for the Ministry NIN diagnostics.")


def environment_label() -> str:
    """`local`, `test`, `staging`, `production`... from the settings module
    (the environment variable when settings are overridden in a test)."""
    module = (
        getattr(settings, "SETTINGS_MODULE", None) or os.environ.get("DJANGO_SETTINGS_MODULE") or ""
    )
    return module.rsplit(".", 1)[-1][:32] or "unknown"


# ---------------------------------------------------------------------------
# 1. Configuration readiness (no network)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readiness:
    backend_code: str
    is_official: bool
    #: Setting NAMES only (never a value): absent, and present but invalid.
    missing_settings: tuple[str, ...]
    invalid_settings: tuple[str, ...]
    #: Seconds left of THIS web process's cached token (metadata only).
    cached_token_seconds: int | None

    @property
    def ready(self) -> bool:
        return self.is_official and not (self.missing_settings or self.invalid_settings)


_SETTING_NAME = re.compile(r"\b(?:MINISTRY_NIN_API_[A-Z_]+|NIN_PROVIDER_BACKEND)\b")


def _names(problems) -> tuple[str, ...]:
    names: list[str] = []
    for problem in problems:
        for name in _SETTING_NAME.findall(problem):
            if name not in names:
                names.append(name)
    return tuple(names)


def readiness(user) -> Readiness:
    """No network request: the configured backend and the names of the
    settings that keep the official adapter from working."""
    _require(user)
    try:
        provider = get_nin_provider()
    except Exception:  # noqa: BLE001 - an unloadable backend is reported, never raised
        return Readiness("UNLOADABLE", False, (), ("NIN_PROVIDER_BACKEND",), None)
    code = getattr(provider, "PROVIDER_CODE", "UNKNOWN")
    if not isinstance(provider, MinistryNinProvider):
        return Readiness(code, False, (), (), None)
    config = MinistryApiConfig.from_settings()
    missing = _names(config.missing_problems())
    invalid = _names(config.malformed_problems())
    cached = None if (missing or invalid) else provider.cached_token_seconds_left()
    return Readiness(code, True, missing, invalid, cached)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    action: str
    outcome: str
    detail: str
    http_status: int | None
    duration_ms: int
    checked_at: datetime
    environment: str
    provider_code: str
    #: Authentication only: usable lifetime of the fresh token, in seconds.
    token_lifetime_seconds: int | None = None
    #: Whether a request was sent to the provider.
    reached_provider: bool = False


def _outcome_for(
    action: str, outcome: LookupOutcome | None, success_status: int | None = None
) -> tuple[str, str, int | None]:
    """Map a provider outcome onto the diagnostics taxonomy. A redirect and a
    provider rate limit are named by their HTTP status, whichever step met
    them; `identite: null` is a successful lookup that found no identity. A
    successful authentication keeps the status of its response."""
    if outcome is None:
        return AUTH_OK, "authenticated", success_status
    status = outcome.http_status
    detail = outcome.detail or ""
    if outcome.kind == LookupKind.NOT_CONFIGURED:
        return NOT_CONFIGURED, detail, status
    if status is not None and 300 <= status <= 399:
        return REDIRECT_REFUSED, detail, status
    if status == 429:
        return PROVIDER_RATE_LIMITED, detail, status
    if outcome.kind == LookupKind.FOUND:
        return IDENTITY_FOUND, detail, status
    if outcome.kind == LookupKind.NOT_FOUND:
        return IDENTITY_NOT_FOUND, detail, status
    if outcome.kind == LookupKind.AUTH_ERROR:
        # An unreadable authentication body is a malformed answer, not a
        # refusal of the credentials.
        return (MALFORMED if detail == "auth_response_invalid" else AUTH_FAILED), detail, status
    if outcome.kind == LookupKind.UNAVAILABLE:
        return UNAVAILABLE, detail, status
    return MALFORMED, detail, status


# ---------------------------------------------------------------------------
# Bounds: shared rate limit and one check at a time
# ---------------------------------------------------------------------------

_LIMITERS: dict = {}
_LIMITER_LOCK = threading.Lock()
_ENVIRONMENT_BUCKET = "environment"


def _limiter(kind: str):
    """Dedicated limiters on the SAME shared counter as the human checks (Redis
    in staging and production), each in its own namespace: one per operator
    and one for the whole environment."""
    with _LIMITER_LOCK:
        if kind not in _LIMITERS:
            from apps.core.issuance_counter import IssuanceLimiter, get_issuance_limiter

            limit = (
                settings.NIN_DIAGNOSTICS_MAX_PER_OPERATOR
                if kind == "operator"
                else settings.NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT
            )
            _LIMITERS[kind] = IssuanceLimiter(
                shared=get_issuance_limiter().shared,
                normal_limit=limit,
                fallback_limit=max(1, limit // 2),
                window_seconds=settings.NIN_DIAGNOSTICS_WINDOW_SECONDS,
                max_networks=64,
                retry_seconds=settings.HUMAN_CHECK_COUNTER_RETRY_SECONDS,
                alert_interval_seconds=settings.HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS,
                namespace=f"asc2026:nin-diagnostics:{kind}",
            )
        return _LIMITERS[kind]


def reset_limiters() -> None:
    """Forget the limiters (tests, settings changes)."""
    with _LIMITER_LOCK:
        _LIMITERS.clear()


def _within_limits(user) -> bool:
    now = time.time()
    operator = hashlib.sha256(f"nin-diagnostics:{user.pk}".encode()).hexdigest()[:32]
    if not _limiter("operator").allow(operator, now=now).allowed:
        return False
    return _limiter("environment").allow(_ENVIRONMENT_BUCKET, now=now).allowed


class _OneAtATime:
    """A session-level, non-blocking advisory lock (never waited for)."""

    def __enter__(self) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_CLASS_NIN_DIAGNOSTICS, 0]
            )
            self.acquired = bool(cursor.fetchone()[0])
        return self.acquired

    def __exit__(self, *exc) -> None:
        if self.acquired:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", [ADVISORY_LOCK_CLASS_NIN_DIAGNOSTICS, 0]
                )


# ---------------------------------------------------------------------------
# 2 and 3. Network checks
# ---------------------------------------------------------------------------


def _audit(user, result: CheckResult, correlation_id: str) -> None:
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=user.pk,
            action_code=action_codes.IDV_NIN_DIAGNOSTIC_RUN,
            target_type="MinistryNinService",
            target_uuid=None,
            result=(
                "SUCCESS"
                if result.outcome in (AUTH_OK, IDENTITY_FOUND, IDENTITY_NOT_FOUND)
                else "DENIED"
                if result.outcome in (THROTTLED, BUSY)
                else "FAILURE"
            ),
            reason_code=result.outcome,
            after_summary={
                "action": result.action,
                "environment": result.environment,
                "provider_code": result.provider_code,
                "outcome": result.outcome,
                "detail": result.detail,
                "http_status": result.http_status,
                "duration_ms": result.duration_ms,
                "reached_provider": result.reached_provider,
            },
            correlation_id=correlation_id,
        )
    )


def _run(user, action: str, probe, *, correlation_id: str) -> CheckResult:
    """Common frame of a network check: provider, bounds, timing, audit."""
    environment = environment_label()
    started = time.monotonic()

    def finish(outcome, detail="", status=None, *, lifetime=None, reached=False, code=""):
        result = CheckResult(
            action=action,
            outcome=outcome,
            detail=detail,
            http_status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            checked_at=timezone.now(),
            environment=environment,
            provider_code=code,
            token_lifetime_seconds=lifetime,
            reached_provider=reached,
        )
        _audit(user, result, correlation_id)
        return result

    try:
        provider = get_nin_provider()
    except Exception:  # noqa: BLE001 - reported as not configured
        return finish(NOT_CONFIGURED, "backend_unloadable", code="UNLOADABLE")
    code = getattr(provider, "PROVIDER_CODE", "UNKNOWN")
    if not isinstance(provider, MinistryNinProvider):
        return finish(NOT_OFFICIAL, "not_official_backend", code=code)
    if provider.configuration_problems():
        return finish(NOT_CONFIGURED, "not_configured", code=code)
    if not _within_limits(user):
        return finish(THROTTLED, "diagnostics_rate_limited", code=code)
    with _OneAtATime() as acquired:
        if not acquired:
            return finish(BUSY, "another_check_running", code=code)
        started = time.monotonic()
        outcome, lifetime, success_status = probe(provider)
    mapped, detail, status = _outcome_for(action, outcome, success_status)
    return finish(mapped, detail, status, lifetime=lifetime, reached=True, code=code)


def run_authentication_check(user, *, correlation_id: str = "") -> CheckResult:
    """One fresh authentication request (never the cached token)."""
    _require(user)

    def probe(provider):
        result = provider.probe_authentication()
        return result.failure, result.lifetime_seconds, result.http_status

    return _run(user, ACTION_AUTHENTICATION, probe, correlation_id=correlation_id)


def run_lookup_check(user, nin: str, *, correlation_id: str = "") -> CheckResult:
    """One lookup of an authorized test NIN. Validated first with the same
    18-digit rule as registrations; nothing is created or changed, and the
    identity data of the answer is discarded here."""
    _require(user)
    nin = (nin or "").strip()
    if NIN_PATTERN.fullmatch(nin) is None:
        result = CheckResult(
            action=ACTION_LOOKUP,
            outcome=INVALID_INPUT,
            detail="nin_format",
            http_status=None,
            duration_ms=0,
            checked_at=timezone.now(),
            environment=environment_label(),
            provider_code="",
        )
        return result  # nothing was sent; not audited (no provider contact)

    def probe(provider):
        outcome = provider.lookup(nin)
        # Keep only what the diagnostics show: kind, detail, status.
        return (
            LookupOutcome(outcome.kind, detail=outcome.detail, http_status=outcome.http_status),
            None,
            None,
        )

    return _run(user, ACTION_LOOKUP, probe, correlation_id=correlation_id)


def recent_checks(user, limit: int = 10) -> list[dict]:
    """The latest audited checks of this environment (sanitized metadata
    only), newest first. A recorded result is history, never current health."""
    _require(user)
    from apps.audit.models import AuditEvent

    events = (
        AuditEvent.objects.filter(action_code=action_codes.IDV_NIN_DIAGNOSTIC_RUN)
        .select_related("actor_user")
        .order_by("-occurred_at")[:limit]
    )
    rows = []
    for event in events:
        summary = event.after_summary or {}
        rows.append(
            {
                "occurred_at": event.occurred_at,
                "operator": getattr(event.actor_user, "email_normalized", "") or "",
                "action": summary.get("action", ""),
                "outcome": summary.get("outcome", event.reason_code),
                "http_status": summary.get("http_status"),
                "duration_ms": summary.get("duration_ms"),
                "environment": summary.get("environment", ""),
            }
        )
    return rows
