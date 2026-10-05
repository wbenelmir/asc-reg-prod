"""Operational sign-in attempt limits and audit (P4-4 finding P44-F03, AF-AUTH-03).

Before P4-4 the staff password sign-in had no attempt limit and wrote no audit
event. This module adds both. It follows the project's database-backed
pattern (the fallback-reference and checkpoint lookup budgets): the audit
trail itself is the counter, so the limit holds without a shared cache.

* Every failed attempt is audited as `ACC_OPERATIONAL_SIGN_IN_FAILED` and
  every success as `ACC_OPERATIONAL_SIGN_IN_SUCCEEDED`. Neither carries the
  email address or the password. The audit target is a keyed fingerprint of
  the typed email (a UUID taken from the rate-limit HMAC, ADR-0007). It is
  computed the same way for known and unknown addresses, so the limit never
  reveals whether an account exists. The client network appears only as its
  keyed fingerprint.
* Email budget: at most `OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL` failures
  for one typed email per `OPERATIONAL_SIGN_IN_WINDOW_SECONDS`, counted since
  that email's last successful sign-in. This is exact under concurrency: a
  transaction-scoped advisory lock per email fingerprint serializes attempts
  for the same email, including the password check.
* Network budget: at most `OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK`
  failures from one client network in the same window, whatever the email.
  It is best effort under concurrency, like the other network budgets.
* Once a budget is spent the password is not checked at all, so a refused
  attempt is never an oracle. One `ACC_OPERATIONAL_SIGN_IN_THROTTLED` event is
  recorded per email fingerprint (email budget) or per network (network
  budget) and window, so a refused burst adds no further rows.

The counting queries use the `(target_type, target_uuid, occurred_at)` audit
index; the network count is bounded to this module's own target type.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.core.concurrency import (
    ADVISORY_LOCK_CLASS_OPERATIONAL_SIGN_IN,
    acquire_advisory_locks,
    lock_key,
)
from apps.core.crypto import compute_rate_limit_fingerprints_for_active_versions, get_key_provider

TARGET_TYPE = "OperationalSignIn"
#: Domain separation from the OTP recipient fingerprints, which use the same
#: rate-limit key family.
_EMAIL_DOMAIN = "operational-sign-in:"

REASON_EMAIL_BUDGET = "EMAIL_BUDGET"
REASON_NETWORK_BUDGET = "NETWORK_BUDGET"


@dataclass(frozen=True)
class SignInResult:
    user: object | None
    throttled: bool = False


def email_target_uuids(email: str) -> list[uuid.UUID]:
    """The audit target UUIDs of a typed email, one per active key version.

    Also the way an auditor finds the attempts made for a given address."""
    return [uuid.UUID(bytes=digest[:16]) for digest in _email_digests(email).values()]


def _email_digests(email: str) -> dict[int, bytes]:
    return compute_rate_limit_fingerprints_for_active_versions(
        _EMAIL_DOMAIN + email.strip().lower(), provider=get_key_provider()
    )


def _network_values(network_identity: str) -> tuple[list[str], str]:
    if not network_identity:
        return [], ""
    provider = get_key_provider()
    digests = compute_rate_limit_fingerprints_for_active_versions(
        network_identity, provider=provider
    )
    write = digests.get(provider.write_rate_limit_key_version(), b"").hex()
    return [digest.hex() for digest in digests.values()], write


def _email_failures(targets: list[uuid.UUID], since):
    from apps.audit.models import AuditEvent

    recent = AuditEvent.objects.filter(
        target_type=TARGET_TYPE, target_uuid__in=targets, occurred_at__gte=since
    )
    last_success = (
        recent.filter(action_code=action_codes.OPERATIONAL_SIGN_IN_SUCCEEDED)
        .order_by("-occurred_at")
        .values_list("occurred_at", flat=True)
        .first()
    )
    failures = recent.filter(action_code=action_codes.OPERATIONAL_SIGN_IN_FAILED)
    if last_success is not None:
        failures = failures.filter(occurred_at__gt=last_success)
    return failures.count(), recent


def _network_events(network_values: list[str], since):
    from apps.audit.models import AuditEvent

    return AuditEvent.objects.filter(
        target_type=TARGET_TYPE, occurred_at__gte=since, network_fingerprint__in=network_values
    )


def _network_failures(network_values: list[str], since) -> int:
    if not network_values:
        return 0
    return (
        _network_events(network_values, since)
        .filter(action_code=action_codes.OPERATIONAL_SIGN_IN_FAILED)
        .count()
    )


def _limit(name: str) -> int:
    """A misconfigured zero or negative limit must not lock every account."""
    return max(1, int(getattr(settings, name)))


def attempt_sign_in(
    request,
    *,
    email: str,
    password: str,
    network_identity: str,
    correlation_id: str = "",
    audit_recorder: AuditRecorder | None = None,
) -> SignInResult:
    """Check the budgets, then the password, and audit the outcome.

    Returns the authenticated user, or `None` with `throttled` telling a
    refused attempt from a wrong password. The caller performs `login()`.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    window = _limit("OPERATIONAL_SIGN_IN_WINDOW_SECONDS")
    email_digests = _email_digests(email)
    targets = [uuid.UUID(bytes=digest[:16]) for digest in email_digests.values()]
    write_target = uuid.UUID(
        bytes=email_digests[get_key_provider().write_rate_limit_key_version()][:16]
    )
    network_values, network_write = _network_values(network_identity)

    def record(action_code: str, result: str, *, user=None, reason: str = "", after=None):
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(user, "pk", None),
                action_code=action_code,
                target_type=TARGET_TYPE,
                target_uuid=write_target,
                result=result,
                reason_code=reason or None,
                after_summary=after,
                correlation_id=correlation_id,
                network_fingerprint=network_write,
            )
        )

    with transaction.atomic():
        acquire_advisory_locks(
            connection,
            [lock_key(ADVISORY_LOCK_CLASS_OPERATIONAL_SIGN_IN, d) for d in email_digests.values()],
        )
        now = timezone.now()
        since = now - timedelta(seconds=window)
        email_count, recent = _email_failures(targets, since)
        reason = ""
        if email_count >= _limit("OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL"):
            reason = REASON_EMAIL_BUDGET
        elif _network_failures(network_values, since) >= _limit(
            "OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK"
        ):
            reason = REASON_NETWORK_BUDGET
            recent = _network_events(network_values, since)
        if reason:
            already = recent.filter(
                action_code=action_codes.OPERATIONAL_SIGN_IN_THROTTLED, reason_code=reason
            ).exists()
            if not already:
                record(
                    action_codes.OPERATIONAL_SIGN_IN_THROTTLED,
                    "DENIED",
                    reason=reason,
                    after={"window_seconds": window},
                )
            return SignInResult(user=None, throttled=True)

        user = authenticate(request, username=email, password=password)
        if user is None:
            record(action_codes.OPERATIONAL_SIGN_IN_FAILED, "FAILURE")
        else:
            record(action_codes.OPERATIONAL_SIGN_IN_SUCCEEDED, "SUCCESS", user=user)
    return SignInResult(user=user)
