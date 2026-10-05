"""Identity clearance for participation (owner decision IDV-Q1, 2026-10-02).

A participation APPROVED decision requires a current, valid, verified
identity for that registration, and so does every downstream eligibility
check (passes, physical badges, entry, offline packages), so an earlier
approval never makes an identity-rejected or invalidated registration
eligible. Identity and participation keep their own histories: nothing here
writes, and an identity change never rewrites a participation decision.

"Cleared" means all of:

* the registration has an identity case (a registration submitted before
  identity verification existed has none: `NO_IDENTITY_CASE`, refused, never
  assumed verified -- IDV-Q4 decides any backfill);
* the case is verified (`API_VERIFIED` or `MANUALLY_VERIFIED`);
* the identifier of its current revision is still `VERIFIED` (not declared,
  replaced, revoked or expired);
* the document has not expired (passports and identity cards that carry an
  expiry date; a NIN has none);
* the source is not the development simulation, unless
  `IDENTITY_ALLOW_SIMULATED_PROVIDER` is on (local and test only; staging and
  production refuse it at startup). A simulated result is never official
  evidence.

`identity_clearance` is the row check (optionally under a lock, for the
decision service); `identity_cleared_q` is the queryset twin used by the
shared eligibility predicate. A test pins that they agree.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.people.models import (
    IdentifierStatus,
    IdentityStatus,
    IdentityVerification,
    VerificationSource,
)


class ClearanceCode:
    CLEARED = "CLEARED"
    NO_IDENTITY_CASE = "NO_IDENTITY_CASE"
    IDENTITY_NOT_VERIFIED = "IDENTITY_NOT_VERIFIED"
    IDENTITY_REJECTED = "IDENTITY_REJECTED"
    IDENTIFIER_NOT_CURRENT = "IDENTIFIER_NOT_CURRENT"
    DOCUMENT_EXPIRED = "DOCUMENT_EXPIRED"
    SIMULATION_NOT_ACCEPTED = "SIMULATION_NOT_ACCEPTED"


#: Staff-facing explanations (never shown to the participant).
CLEARANCE_MESSAGES = {
    ClearanceCode.NO_IDENTITY_CASE: _(
        "This registration has no identity verification case (it was submitted before "
        "identity verification existed). It cannot be approved until its identity is verified."
    ),
    ClearanceCode.IDENTITY_NOT_VERIFIED: _(
        "The identity is not verified yet (pending, in manual review or returned for "
        "correction). Approval requires a verified identity."
    ),
    ClearanceCode.IDENTITY_REJECTED: _(
        "The identity was finally rejected. This registration cannot be approved."
    ),
    ClearanceCode.IDENTIFIER_NOT_CURRENT: _(
        "The verified identifier is no longer current (it was replaced or changed). "
        "The identity must be verified again."
    ),
    ClearanceCode.DOCUMENT_EXPIRED: _(
        "The verified identity document has expired. The identity must be verified again."
    ),
    ClearanceCode.SIMULATION_NOT_ACCEPTED: _(
        "The identity was verified only by the development simulation, which is not "
        "official evidence here."
    ),
}


@dataclass(frozen=True)
class IdentityClearance:
    code: str
    status: str = ""
    source: str = ""

    @property
    def cleared(self) -> bool:
        return self.code == ClearanceCode.CLEARED

    @property
    def message(self):
        return CLEARANCE_MESSAGES.get(self.code, "")


def simulated_source_accepted() -> bool:
    return bool(getattr(settings, "IDENTITY_ALLOW_SIMULATED_PROVIDER", False))


def accepted_sources() -> tuple[str, ...]:
    """Verification sources that may clear an identity in this environment."""
    return tuple(
        source
        for source in VerificationSource.values
        if source != VerificationSource.SIMULATED_API or simulated_source_accepted()
    )


def _today(today: datetime.date | None) -> datetime.date:
    return today if today is not None else timezone.now().date()


def evaluate_clearance(verification, *, today: datetime.date | None = None) -> IdentityClearance:
    """The clearance of an already-loaded case (or None). Reads only the
    objects it is given; callers that must be race-free load them locked."""
    if verification is None:
        return IdentityClearance(ClearanceCode.NO_IDENTITY_CASE)
    status, source = verification.status, verification.verification_source
    if status == IdentityStatus.REJECTED:
        return IdentityClearance(ClearanceCode.IDENTITY_REJECTED, status, source)
    if status not in IdentityStatus.verified_statuses():
        return IdentityClearance(ClearanceCode.IDENTITY_NOT_VERIFIED, status, source)
    revision = verification.current_revision if verification.current_revision_id else None
    identifier = revision.identifier if revision is not None else None
    if identifier is None or identifier.status != IdentifierStatus.VERIFIED:
        return IdentityClearance(ClearanceCode.IDENTIFIER_NOT_CURRENT, status, source)
    if identifier.expires_at is not None and identifier.expires_at <= _today(today):
        return IdentityClearance(ClearanceCode.DOCUMENT_EXPIRED, status, source)
    if source not in accepted_sources():
        return IdentityClearance(ClearanceCode.SIMULATION_NOT_ACCEPTED, status, source)
    return IdentityClearance(ClearanceCode.CLEARED, status, source)


def identity_clearance(
    registration_id, *, lock: bool = False, today: datetime.date | None = None
) -> IdentityClearance:
    """The clearance of one registration, read from the database.

    With `lock=True` the case row is locked `FOR NO KEY UPDATE` (the caller
    already holds the Registration row: lock order Registration, then case),
    so an identity command cannot change it before the caller commits. The
    identifier needs no lock of its own: every writer that replaces or
    re-verifies the identifier of a case holds that case's Registration row.
    """
    queryset = IdentityVerification.objects.select_related("current_revision__identifier").filter(
        registration_id=registration_id
    )
    if lock:
        queryset = queryset.select_for_update(no_key=True, of=("self",))
    return evaluate_clearance(queryset.first(), today=today)


def identity_cleared_q(prefix: str = "", *, today: datetime.date | None = None) -> Q:
    """Queryset twin of `identity_clearance(...).cleared` for Registration rows
    (`prefix` reaches the Registration from another model)."""
    case = f"{prefix}identity_verification__"
    identifier = f"{case}current_revision__identifier__"
    return Q(
        **{
            f"{case}status__in": IdentityStatus.verified_statuses(),
            f"{case}verification_source__in": accepted_sources(),
            f"{identifier}status": IdentifierStatus.VERIFIED,
        }
    ) & (
        Q(**{f"{identifier}expires_at__isnull": True})
        | Q(**{f"{identifier}expires_at__gt": _today(today)})
    )
