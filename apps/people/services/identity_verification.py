"""Post-submission identity verification: start, durable jobs and the worker (IDV-2, ADR-0026).

Flow (A13-14):

1. `start_identity_verification` runs INSIDE the final-submission transaction.
   It creates the `IdentityVerification`, revision 1 and, for the Algerian NIN
   route, a durable `IdentityVerificationJob`, so the submission and its work
   item commit or roll back together. No provider is called here.
2. After commit, a robust `on_commit` callback tries to enqueue the job. A
   broker error is swallowed and logged by class name only: the submission is
   already saved, and the sweeper (`dispatch_due_identity_jobs`, beat) or the
   `run_identity_verification_jobs` command recovers the job later.
3. `process_identity_job` (the worker) claims the job with a lease, calls the
   provider OUTSIDE any database transaction, inside a bounded concurrency
   slot, then applies the result in a new transaction -- only to the exact
   revision it checked, and only if nothing changed meanwhile.

Lock order everywhere (IDV-C1):

1. the Person row (`FOR NO KEY UPDATE`, `lock_person_identity`), taken first
   by every command that can change which identifier a case uses or whether
   it is verified across the person's cases -- the submission start, every
   staff command and the participant resubmission. It serializes the identity
   lifecycle of one person without blocking unrelated rows that merely refer
   to the person;
2. the Registration row(s), then the IdentityVerification row(s) -- the
   command's own case first, then the other cases sharing its identifier, by
   registration id. The worker and the closure step take these two as well,
   in this order (the worker inserts rows referring to the Registration, so
   it must never hold the case row while waiting for the Registration);
3. the identifier advisory locks (class 6, every active blind-index version);
4. the identifier rows.

Nothing takes an advisory lock and then waits for a row, and only holders of
the Person lock wait for a second registration, so the order cannot
deadlock.

Shared identifiers (R-IDV-01): identifiers belong to the Person and are
event-independent, so one person's registrations in different events use the
same identifier. Replacing it in one case rebinds every other current case
that uses it, in the same transaction (`rebind_cases_after_replacement`); a
replaced, revoked or expired identifier is never made VERIFIED again.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.concurrency import (
    ADVISORY_LOCK_CLASS_OFFICIAL_IDENTIFIER,
    acquire_advisory_locks,
    lock_key,
)
from apps.core.crypto import compute_blind_index, compute_blind_indexes_for_active_versions
from apps.core.crypto import get_key_provider as _get_key_provider
from apps.people.identity_contract import (
    Comparison,
    LookupKind,
    evaluate_identity_match,
    normalize_name_for_comparison,
)
from apps.people.models import (
    IdentifierStatus,
    IdentifierType,
    IdentityDecision,
    IdentityDecisionAction,
    IdentityIdentifier,
    IdentityJobStatus,
    IdentityReasonCode,
    IdentityRevision,
    IdentityRevisionSource,
    IdentityRoute,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationAttempt,
    IdentityVerificationJob,
    IdentityVerificationMethod,
    IdentityVerificationResult,
    VerificationSource,
)
from apps.people.nin_provider import LookupOutcome, get_nin_provider

logger = logging.getLogger("asc2026.identity")

ALGERIA_COUNTRY_CODE = "DZ"

#: Advisory-lock classes for provider concurrency slots (session level, held
#: only around one provider call, never inside a transaction).
ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT = 8

#: Public statuses of a registration whose identity work is still meaningful.
#: A withdrawn or not-approved registration neither runs provider checks nor
#: counts as a pending duplicate.
ACTIVE_PUBLIC_STATUSES = (
    "SUBMITTED",
    "UNDER_REVIEW",
    "ADDITIONAL_INFORMATION_REQUIRED",
    "APPROVED",
)


class IdentityVerificationError(Exception):
    """Base of the controlled refusals raised by the identity services."""


#: Identifier statuses that may still be verified. A replaced, revoked or
#: expired identifier is history and is never verified again (R-IDV-01).
VERIFIABLE_IDENTIFIER_STATUSES = (IdentifierStatus.DECLARED, IdentifierStatus.VERIFIED)


def identifier_is_bound(identifier) -> bool:
    """IDV-Q-C1: an identifier that is VERIFIED, or that any submitted identity
    revision uses, records what was declared and checked. Its validity
    (`expires_at`) is never changed in place: one person's identifiers are
    shared across events, and since IDV-Q1 the eligibility of every case bound
    to it reads that expiry. A new draft that declares another expiry gets a
    new identifier row; a correction replaces the identifier and rebinds the
    person's other cases explicitly (IDV-C1). Only an identifier that nothing
    submitted uses yet (a draft's own) may still be edited."""
    if identifier.status == IdentifierStatus.VERIFIED:
        return True
    return IdentityRevision.objects.filter(identifier_id=identifier.pk).exists()


def supersede_same_person_declarations(
    verification, identifier, *, actor_user=None, correlation_id: str = ""
) -> None:
    """Before `identifier` is verified, retire the person's OLDER verified
    identifiers of the same document (same type, country and number, another
    row: an earlier declaration with another expiry, IDV-Q-C1).

    Both cannot be VERIFIED (the partial unique index), and the older one must
    never be extended or shortened silently. So it becomes REPLACED and the
    person's other cases bound to it are rebound to `identifier` (IDV-C1): an
    open or verified one returns to manual review with a recorded decision, so
    its eligibility changes only through its own authorized verification.

    MUST run inside the verifying transaction, after the Person lock and the
    case's own Registration and case locks, and before the identifier advisory
    locks (lock order, module docstring).
    """
    from apps.people.selectors import identifiers_for_value

    older = list(
        identifiers_for_value(
            identifier_type=identifier.identifier_type,
            country_code_id=identifier.country_code_id,
            raw_value=identifier.value_encrypted,
        )
        .filter(person_id=verification.person_id, status=IdentifierStatus.VERIFIED)
        .exclude(pk=identifier.pk)
        .order_by("created_at")
    )
    if not older:
        return
    siblings = []
    for old in older:
        siblings.extend(
            lock_cases_sharing_identifier(old.pk, exclude_verification_id=verification.pk)
        )
    lock_identifier_values(
        identifier.identifier_type, identifier.country_code_id, [identifier.value_encrypted]
    )
    IdentityIdentifier.objects.filter(
        pk__in=[old.pk for old in older], status=IdentifierStatus.VERIFIED
    ).update(status=IdentifierStatus.REPLACED, updated_at=timezone.now())
    rebind_cases_after_replacement(
        siblings,
        replacement=identifier,
        changed_field="identity_document",
        origin=verification,
        actor_user=actor_user,
        correlation_id=correlation_id,
    )


def lock_person_identity(person_id) -> None:
    """The per-person identity lock (lock order, step 1): the Person row,
    `FOR NO KEY UPDATE`, so rows that only refer to the person are never
    blocked. MUST run inside the transaction it protects, before any
    Registration or IdentityVerification row lock."""
    from apps.people.models import Person

    Person.objects.select_for_update(no_key=True).only("pk").get(pk=person_id)


def lock_registration_rows(registration_ids) -> None:
    """Lock Registration rows (lock order, step 2) by id, `FOR NO KEY UPDATE`."""
    from apps.registrations.models import Registration

    list(
        Registration.objects.select_for_update(no_key=True)
        .filter(pk__in=list(registration_ids))
        .order_by("pk")
        .values_list("pk", flat=True)
    )


# ---------------------------------------------------------------------------
# Fingerprints and identifier locks
# ---------------------------------------------------------------------------


def _canonical_identity(*, route, identifier, given_names, family_name, birth_date) -> str:
    return "|".join(
        [
            "IDV-REVISION-V1",
            route,
            identifier.identifier_type,
            identifier.country_code_id,
            identifier.value_encrypted,  # decrypted at the attribute level
            given_names or "",
            family_name or "",
            birth_date.isoformat() if birth_date else "",
        ]
    )


def identity_fingerprint(
    *, route, identifier, given_names, family_name, birth_date, key_version: int | None = None
) -> tuple[str, int]:
    """A keyed HMAC of the canonical identity, with its key version."""
    provider = _get_key_provider()
    version = key_version if key_version is not None else provider.current_blind_index_key_version()
    canonical = _canonical_identity(
        route=route,
        identifier=identifier,
        given_names=given_names,
        family_name=family_name,
        birth_date=birth_date,
    )
    return compute_blind_index(canonical, version=version, provider=provider), version


def lock_identifier_values(identifier_type: str, country_code_id: str, raw_values) -> None:
    """Take the same transaction-level advisory locks as
    `apps.people.services.create_identity_identifier`, for every active
    blind-index version of every value. MUST run inside the transaction whose
    duplicate decision the lock protects."""
    from apps.people.services import normalize_identifier_value

    pairs = []
    for raw_value in raw_values:
        normalized = normalize_identifier_value(raw_value)
        scoped = f"{identifier_type}:{country_code_id.upper()}:{normalized}"
        for digest in compute_blind_indexes_for_active_versions(scoped).values():
            pairs.append(lock_key(ADVISORY_LOCK_CLASS_OFFICIAL_IDENTIFIER, bytes.fromhex(digest)))
    acquire_advisory_locks(connection, pairs)


@dataclass(frozen=True)
class ConflictSummary:
    """Other persons' active claims on the same identifier. Holds ids only."""

    verified_identifier_ids: tuple = ()
    pending_verification_ids: tuple = ()
    registration_ids: tuple = field(default=())

    @property
    def exists(self) -> bool:
        return bool(self.verified_identifier_ids or self.pending_verification_ids)


def find_cross_person_conflicts(
    *, identifier_type: str, country_code_id: str, raw_value: str, person_id
) -> ConflictSummary:
    """Duplicates of `raw_value` held by another Person (IDV-07, A13-14).

    Scope: identifiers belong to a Person and are event-independent, so the
    check spans every event. A VERIFIED identifier of another person always
    conflicts. A DECLARED one conflicts while it is the current identifier of
    a submitted, still active registration whose identity is pending, under
    manual review or returned for correction. Drafts never conflict; the same
    Person never conflicts with itself. Callers that act on the answer hold
    `lock_identifier_values` first.
    """
    from apps.people.selectors import identifiers_for_value

    candidates = (
        identifiers_for_value(
            identifier_type=identifier_type, country_code_id=country_code_id, raw_value=raw_value
        )
        .exclude(person_id=person_id)
        .exclude(
            status__in=[
                IdentifierStatus.REPLACED,
                IdentifierStatus.REVOKED,
                IdentifierStatus.EXPIRED,
            ]
        )
    )
    verified_ids = tuple(
        candidates.filter(status=IdentifierStatus.VERIFIED).values_list("pk", flat=True)
    )
    pending = IdentityVerification.objects.filter(
        current_revision__identifier__in=candidates.filter(status=IdentifierStatus.DECLARED),
        status__in=IdentityStatus.open_statuses(),
        registration__public_status__in=ACTIVE_PUBLIC_STATUSES,
    ).values_list("pk", "registration_id")
    pending = list(pending)
    registration_ids = {registration_id for _pk, registration_id in pending}
    if verified_ids:
        registration_ids |= set(
            IdentityVerification.objects.filter(
                current_revision__identifier_id__in=verified_ids
            ).values_list("registration_id", flat=True)
        )
    return ConflictSummary(
        verified_identifier_ids=verified_ids,
        pending_verification_ids=tuple(pk for pk, _registration_id in pending),
        registration_ids=tuple(sorted(registration_ids, key=str)),
    )


# ---------------------------------------------------------------------------
# Start (inside the submission transaction)
# ---------------------------------------------------------------------------


def _audit(
    action_code,
    *,
    verification,
    result="SUCCESS",
    actor_user=None,
    actor_person=None,
    reason_code="",
    after=None,
    correlation_id="",
) -> None:
    if actor_user is not None:
        actor_type = "OPERATIONAL_USER"
    elif actor_person is not None:
        actor_type = "PARTICIPANT"
    else:
        actor_type = "SYSTEM"
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type=actor_type,
            actor_user_id=getattr(actor_user, "pk", None),
            actor_person_id=getattr(actor_person, "pk", None),
            action_code=action_code,
            target_type="IdentityVerification",
            target_uuid=verification.pk,
            event_edition_id=verification.event_edition_id,
            result=result,
            reason_code=reason_code or None,
            after_summary=after,
            correlation_id=correlation_id,
        )
    )


def current_identifier_for(person, route: str):
    """The identifier the route checks: the person's current Algerian NIN, or
    their most recent passport. REPLACED identifiers never count."""
    queryset = person.identity_identifiers.exclude(status=IdentifierStatus.REPLACED)
    if route == IdentityRoute.NIN:
        queryset = queryset.filter(
            identifier_type=IdentifierType.NIN, country_code_id=ALGERIA_COUNTRY_CODE
        )
    else:
        queryset = queryset.filter(identifier_type=IdentifierType.PASSPORT)
    return queryset.order_by("-created_at").first()


def route_for_nationality(nationality_code_id: str | None) -> str:
    """A13-01, A13-02: Algerian nationals use the NIN route; everyone else the
    passport route. Never chosen by the participant. (The NIN exemption route
    of IDV-Q3 replaces the NIN route only through a staff grant, at
    submission: `start_identity_verification`.)"""
    return (
        IdentityRoute.NIN if nationality_code_id == ALGERIA_COUNTRY_CODE else IdentityRoute.PASSPORT
    )


def _profile(registration):
    from apps.registrations.models import RegistrationProfile

    return RegistrationProfile.objects.get(registration=registration)


def create_revision(
    *,
    verification,
    identifier,
    profile,
    source,
    changed_fields=(),
    created_by_user=None,
    created_by_person=None,
) -> IdentityRevision:
    latest = (
        IdentityRevision.objects.filter(verification=verification)
        .order_by("-number")
        .values_list("number", flat=True)
        .first()
    )
    number = (latest or 0) + 1
    fingerprint, key_version = identity_fingerprint(
        route=verification.route,
        identifier=identifier,
        given_names=profile.submitted_given_names,
        family_name=profile.submitted_family_name,
        birth_date=profile.date_of_birth,
    )
    return IdentityRevision.objects.create(
        verification=verification,
        number=number,
        source=source,
        identifier=identifier,
        fingerprint=fingerprint,
        fingerprint_key_version=key_version,
        changed_fields=list(changed_fields),
        created_by_user=created_by_user,
        created_by_person=created_by_person,
    )


def schedule_job(*, verification, revision, delay_seconds: int = 0) -> IdentityVerificationJob:
    """Persist the durable work item for `revision` in the CURRENT transaction
    and register a robust after-commit dispatch. Idempotent per revision."""
    job, _created = IdentityVerificationJob.objects.get_or_create(
        revision=revision,
        defaults={
            "verification": verification,
            "next_attempt_at": timezone.now() + timedelta(seconds=delay_seconds),
        },
    )
    if getattr(settings, "IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT", True):
        transaction.on_commit(partial(dispatch_identity_job, str(job.pk)), robust=True)
    return job


def dispatch_identity_job(job_id: str) -> bool:
    """Enqueue one job on the broker. Never raises: an outage leaves the durable
    job PENDING for the sweeper (ASYNC-01). Logs the error class only."""
    from apps.people.tasks import process_identity_verification_job_task

    try:
        process_identity_verification_job_task.delay(job_id)
    except Exception as exc:  # noqa: BLE001 - the saved submission must never fail here
        logger.warning(
            "identity verification dispatch deferred to the sweeper",
            extra={"error_class": type(exc).__name__},
        )
        return False
    return True


def discard_open_jobs(verification, *, except_revision=None) -> int:
    """Mark the verification's not-yet-finished jobs stale (a newer revision or
    a staff decision supersedes them). A job already running keeps its lease
    but its result will be discarded by the revision check."""
    queryset = IdentityVerificationJob.objects.filter(
        verification=verification, status=IdentityJobStatus.PENDING
    )
    if except_revision is not None:
        queryset = queryset.exclude(revision=except_revision)
    return queryset.update(
        status=IdentityJobStatus.DISCARDED_STALE,
        completed_at=timezone.now(),
        last_outcome="SUPERSEDED",
    )


# ---------------------------------------------------------------------------
# Shared identifiers (R-IDV-01) and closed registrations (R-IDV-06)
# ---------------------------------------------------------------------------


def lock_cases_sharing_identifier(identifier_id, *, exclude_verification_id) -> list:
    """Lock every OTHER case whose current revision uses `identifier_id`:
    their Registration rows, then their case rows, by registration id (lock
    order, step 2). The caller holds the Person lock, so the set cannot grow
    meanwhile (every writer of a current revision takes it), and it MUST call
    this before any identifier advisory lock."""
    rows = list(
        IdentityVerification.objects.filter(current_revision__identifier_id=identifier_id)
        .exclude(pk=exclude_verification_id)
        .order_by("registration_id")
        .values_list("pk", "registration_id")
    )
    if not rows:
        return []
    lock_registration_rows(registration_id for _pk, registration_id in rows)
    return list(
        IdentityVerification.objects.select_for_update(of=("self",))
        .select_related("registration", "current_revision__identifier")
        .filter(pk__in=[pk for pk, _registration_id in rows])
        .order_by("registration_id")
    )


def rebind_cases_after_replacement(
    cases,
    *,
    replacement,
    changed_field: str,
    origin,
    actor_user=None,
    actor_person=None,
    correlation_id: str = "",
) -> None:
    """Point the person's other cases at the replacement identifier.

    Each gets a new revision (`LINKED_IDENTITY_CHANGE`) and a new version, so
    a decision prepared before the change is refused as stale. An open or
    verified case goes to manual review (`IDENTITY_DATA_CHANGED`) with its
    verification source cleared: a verification of the replaced value is
    never kept. A returned case stays returned (the participant answers the
    request with the current identifier); a final rejection stays as decided.
    Pending checks are discarded; history is kept.
    """
    now = timezone.now()
    for case in cases:
        if case.status == IdentityStatus.REJECTED:
            continue
        revision = create_revision(
            verification=case,
            identifier=replacement,
            profile=_profile(case.registration),
            source=IdentityRevisionSource.LINKED_IDENTITY_CHANGE,
            changed_fields=[changed_field],
            created_by_user=actor_user,
            created_by_person=actor_person,
        )
        discard_open_jobs(case)
        from_status = case.status
        case.current_revision = revision
        if case.status != IdentityStatus.RETURNED_FOR_CORRECTION:
            case.status = IdentityStatus.MANUAL_REVIEW
            case.reason_code = IdentityReasonCode.IDENTITY_DATA_CHANGED
            case.verification_source = ""
            case.decided_at = None
            case.decided_by = None
        case.status_changed_at = now
        case.version += 1
        case.save()
        IdentityDecision.objects.create(
            verification=case,
            revision=revision,
            action=IdentityDecisionAction.LINKED_IDENTITY_CHANGE,
            from_status=from_status,
            to_status=case.status,
            actor_user=actor_user,
            actor_person=actor_person,
        )
        _audit(
            action_codes.IDENTITY_LINKED_CASE_UPDATED,
            verification=case,
            actor_user=actor_user,
            actor_person=actor_person,
            reason_code=case.reason_code,
            after={
                "status": case.status,
                "revision": revision.number,
                "changed": changed_field,
                "origin_case": str(origin.pk),
            },
            correlation_id=correlation_id,
        )


def close_identity_work_for_registration(
    registration, *, actor_user=None, actor_person=None, correlation_id: str = ""
):
    """End the identity work of a Registration that was just closed
    (withdrawal, operational cancellation, NOT_APPROVED decision).

    MUST run inside the transaction that closed it, after locking its row
    (lock order, step 2). Pending checks are discarded. A pending case or an
    open correction request goes to manual review (`REGISTRATION_CLOSED`), so
    the participant no longer sees a correction and a stale resubmission is
    refused; an authorized reopening later finds the case waiting for staff.
    A decided case (verified, rejected) keeps its decision. Never changes the
    Registration itself.
    """
    verification = (
        IdentityVerification.objects.select_for_update(of=("self",))
        .filter(registration_id=registration.pk)
        .first()
    )
    if verification is None:
        return None
    discard_open_jobs(verification)
    if verification.status not in (IdentityStatus.PENDING, IdentityStatus.RETURNED_FOR_CORRECTION):
        return verification
    from_status = verification.status
    verification.status = IdentityStatus.MANUAL_REVIEW
    verification.reason_code = IdentityReasonCode.REGISTRATION_CLOSED
    verification.status_changed_at = timezone.now()
    verification.version += 1
    verification.save()
    IdentityDecision.objects.create(
        verification=verification,
        revision=verification.current_revision,
        action=IdentityDecisionAction.REGISTRATION_CLOSED,
        from_status=from_status,
        to_status=verification.status,
        reason_code=registration.public_status,
        actor_user=actor_user,
        actor_person=actor_person,
    )
    _audit(
        action_codes.IDENTITY_CLOSED_WITH_REGISTRATION,
        verification=verification,
        actor_user=actor_user,
        actor_person=actor_person,
        reason_code=IdentityReasonCode.REGISTRATION_CLOSED,
        after={"status": verification.status, "registration_status": registration.public_status},
        correlation_id=correlation_id,
    )
    return verification


def start_identity_verification(*, registration, correlation_id: str = "") -> IdentityVerification:
    """Create the identity verification of a registration being submitted.

    MUST run inside the submission transaction, after the Registration row is
    locked and the completeness guard passed. Idempotent: an existing
    verification is returned unchanged.
    """
    existing = IdentityVerification.objects.filter(registration=registration).first()
    if existing is not None:
        return existing
    person = registration.person
    # A correction of this person's identifier elsewhere either committed
    # before (the current identifier below is its replacement) or waits for
    # this submission and then sees and rebinds this new case too.
    lock_person_identity(person.pk)
    profile = _profile(registration)
    route = route_for_nationality(profile.nationality_code_id)
    # IDV-Q3: a staff-granted NIN exemption whose declared document this
    # submission uses turns the Algerian route into the documentary one (lock
    # order: Registration, Person, then the exemption row). No NIN exists or
    # is created for it, and no ministry job is ever scheduled.
    exemption = None
    if route == IdentityRoute.NIN:
        from apps.people.services.nin_exemption import consume_for_submission

        exemption = consume_for_submission(registration, correlation_id=correlation_id)
        if exemption is not None:
            route = IdentityRoute.NIN_EXEMPTION
    identifier = (
        exemption.document_identifier
        if exemption is not None
        else current_identifier_for(person, route)
    )
    if identifier is None:  # pragma: no cover - the completeness guard requires it
        raise IdentityVerificationError("The identity identifier is missing.")
    now = timezone.now()
    verification = IdentityVerification.objects.create(
        registration=registration,
        person=person,
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
        nationality_id=profile.nationality_code_id,
        route=route,
        status=IdentityStatus.PENDING,
        submitted_at=now,
        status_changed_at=now,
    )
    revision = create_revision(
        verification=verification,
        identifier=identifier,
        profile=profile,
        source=IdentityRevisionSource.SUBMISSION,
    )
    verification.current_revision = revision

    lock_identifier_values(
        identifier.identifier_type, identifier.country_code_id, [identifier.value_encrypted]
    )
    conflicts = find_cross_person_conflicts(
        identifier_type=identifier.identifier_type,
        country_code_id=identifier.country_code_id,
        raw_value=identifier.value_encrypted,
        person_id=person.pk,
    )
    if conflicts.exists:
        verification.status = IdentityStatus.MANUAL_REVIEW
        verification.reason_code = IdentityReasonCode.DUPLICATE_IDENTIFIER
    elif route == IdentityRoute.PASSPORT:
        # A13-02: every foreign participant is reviewed manually; no ministry job.
        verification.status = IdentityStatus.MANUAL_REVIEW
        verification.reason_code = IdentityReasonCode.FOREIGN_PASSPORT_REVIEW
    elif route == IdentityRoute.NIN_EXEMPTION:
        # IDV-Q3: manual documentary review only; no ministry job.
        verification.status = IdentityStatus.MANUAL_REVIEW
        verification.reason_code = IdentityReasonCode.NIN_EXEMPTION_REVIEW
    verification.save(update_fields=["current_revision", "status", "reason_code", "updated_at"])
    if verification.status == IdentityStatus.PENDING and route == IdentityRoute.NIN:
        schedule_job(verification=verification, revision=revision)
    _audit(
        action_codes.IDENTITY_VERIFICATION_STARTED,
        verification=verification,
        actor_person=person,
        reason_code=verification.reason_code,
        after={"route": route, "status": verification.status},
        correlation_id=correlation_id,
    )
    return verification


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _setting(name: str, default):
    return getattr(settings, name, default)


def retry_delays() -> tuple[int, ...]:
    return tuple(
        int(value)
        for value in _setting("IDENTITY_VERIFICATION_RETRY_DELAYS_SECONDS", (60, 300, 900, 3600))
    )


def max_attempts() -> int:
    return len(retry_delays()) + 1


def _require_no_open_transaction() -> None:
    """Provider calls never run inside a database transaction (A13-14). The
    only tolerated blocks are a test case's own wrapping transactions."""
    if any(not getattr(block, "_from_testcase", False) for block in connection.atomic_blocks):
        raise RuntimeError("A provider call must not run inside a database transaction.")


@contextmanager
def provider_slot():
    """Hold one of `IDENTITY_PROVIDER_MAX_CONCURRENCY` session-level advisory
    locks around a provider call, across every worker process. Yields False
    when every slot is busy."""
    slots = max(1, int(_setting("IDENTITY_PROVIDER_MAX_CONCURRENCY", 4)))
    acquired = None
    with connection.cursor() as cursor:
        for slot in range(slots):
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT, slot],
            )
            if cursor.fetchone()[0]:
                acquired = slot
                break
    try:
        yield acquired is not None
    finally:
        if acquired is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT, acquired],
                )


def _stale_reason(job, verification, registration) -> str:
    if verification.current_revision_id != job.revision_id:
        return "revision_superseded"
    if verification.status != IdentityStatus.PENDING:
        return "status_changed"
    if registration.public_status not in ACTIVE_PUBLIC_STATUSES:
        return "registration_closed"
    return ""


def _fingerprint_still_current(revision, profile, *, identifier=None) -> bool:
    """True while `revision` still describes the current identity: its
    identifier may still be verified and the keyed fingerprint of route,
    identifier, names and birth date is unchanged. Pass the identifier row as
    locked for the final write; a cached object never proves freshness."""
    identifier = identifier if identifier is not None else revision.identifier
    if identifier.pk != revision.identifier_id:
        return False
    if identifier.status not in VERIFIABLE_IDENTIFIER_STATUSES:
        return False
    try:
        fingerprint, _version = identity_fingerprint(
            route=revision.verification.route,
            identifier=identifier,
            given_names=profile.submitted_given_names,
            family_name=profile.submitted_family_name,
            birth_date=profile.date_of_birth,
            key_version=revision.fingerprint_key_version,
        )
    except Exception:  # noqa: BLE001 - a retired key cannot prove the identity is current
        return False
    return fingerprint == revision.fingerprint


def _safe_lookup(provider, nin: str) -> LookupOutcome:
    try:
        outcome = provider.lookup(nin)
    except Exception:  # noqa: BLE001 - no adapter exception (or its text) may escape
        return LookupOutcome(LookupKind.UNAVAILABLE, detail="adapter_error", retryable=True)
    if not isinstance(outcome, LookupOutcome):
        return LookupOutcome(LookupKind.INVALID_RESPONSE, detail="adapter_contract")
    return outcome


_RESULT_FOR_REASON = {
    "": IdentityVerificationResult.MATCH,
    IdentityReasonCode.DUPLICATE_IDENTIFIER: IdentityVerificationResult.MATCH,
    IdentityReasonCode.NOT_FOUND: IdentityVerificationResult.NO_MATCH,
    IdentityReasonCode.DATA_MISMATCH: IdentityVerificationResult.NO_MATCH,
    IdentityReasonCode.PROVIDER_IDENTITY_MISMATCH: IdentityVerificationResult.NO_MATCH,
    IdentityReasonCode.AMBIGUOUS_DATE: IdentityVerificationResult.INCONCLUSIVE,
    IdentityReasonCode.PRESUME_FLAG_UNRECOGNIZED: IdentityVerificationResult.INCONCLUSIVE,
    IdentityReasonCode.INVALID_RESPONSE: IdentityVerificationResult.INCONCLUSIVE,
    IdentityReasonCode.PROVIDER_UNAVAILABLE: IdentityVerificationResult.UNAVAILABLE,
    IdentityReasonCode.PROVIDER_AUTH_ERROR: IdentityVerificationResult.ERROR,
    IdentityReasonCode.PROVIDER_NOT_CONFIGURED: IdentityVerificationResult.ERROR,
}

_REASON_FOR_KIND = {
    LookupKind.NOT_FOUND: IdentityReasonCode.NOT_FOUND,
    LookupKind.INVALID_RESPONSE: IdentityReasonCode.INVALID_RESPONSE,
    LookupKind.NOT_CONFIGURED: IdentityReasonCode.PROVIDER_NOT_CONFIGURED,
    LookupKind.AUTH_ERROR: IdentityReasonCode.PROVIDER_AUTH_ERROR,
    LookupKind.UNAVAILABLE: IdentityReasonCode.PROVIDER_UNAVAILABLE,
}


def _record_attempt(
    *,
    job,
    verification,
    outcome,
    provider,
    attempt_number,
    duration_ms,
    evaluation,
    reason_code,
    applied,
) -> IdentityVerificationAttempt:
    retain = evaluation is not None and evaluation.retained_family_name_latin and applied
    return IdentityVerificationAttempt.objects.create(
        identity_identifier=job.revision.identifier,
        registration_id=verification.registration_id,
        method=IdentityVerificationMethod.EXTERNAL_SERVICE,
        provider_code=getattr(provider, "PROVIDER_CODE", "UNKNOWN")[:64],
        request_reference=(outcome.request_reference or "")[:64],
        result=_RESULT_FOR_REASON.get(reason_code, IdentityVerificationResult.INCONCLUSIVE),
        attempted_at=timezone.now(),
        revision=job.revision,
        job=job,
        attempt_number=attempt_number,
        outcome=outcome.kind,
        reason_code=reason_code,
        detail_code=(outcome.detail or "")[:40],
        is_official_provider=bool(getattr(provider, "IS_OFFICIAL", False)),
        applied=applied,
        http_status=outcome.http_status if isinstance(outcome.http_status, int) else None,
        duration_ms=duration_ms,
        comparison=dict(evaluation.comparison) if evaluation is not None else {},
        presume_flag=evaluation.presume if evaluation is not None else "",
        policy_basis=evaluation.policy_basis if evaluation is not None else "",
        official_family_name_latin=evaluation.retained_family_name_latin if retain else "",
        official_given_names_latin=evaluation.retained_given_names_latin if retain else "",
        official_birth_date=(
            evaluation.retained_birth_date.isoformat()
            if retain and evaluation.retained_birth_date
            else ""
        ),
        official_birth_date_text=evaluation.retained_birth_date_text if retain else "",
    )


def _claim(job_id, token, now):
    """Claim a due job, or recover an IN_PROGRESS job whose lease expired.

    Only the job row is locked (`of=("self",)`, skip locked), never a case or
    a Registration row (lock order, module docstring). Returns `(job, state)`:

    * `claimed`: a new attempt (attempts + 1) under a new lease;
    * `exhaust`: the attempt budget is already used (handled failures and
      abandoned leases count alike, R-IDV-04); nothing changes here and
      `_finalize_without_attempt` stops the job and routes the case;
    * `stale:identity_changed`: the identity changed outside this case; also
      finalized by `_finalize_without_attempt`;
    * `stale:<reason>`: revision superseded, case decided or registration
      closed; the job is discarded here (the case needs no change);
    * `busy` or `not_due`: nothing to do.
    """
    with transaction.atomic(durable=True):
        job = (
            IdentityVerificationJob.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("revision__identifier", "verification__registration")
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            return None, "busy"
        due = job.status == IdentityJobStatus.PENDING and job.next_attempt_at <= now
        expired = (
            job.status == IdentityJobStatus.IN_PROGRESS
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if not (due or expired):
            return None, "not_due"
        verification = job.verification
        stale = _stale_reason(job, verification, verification.registration)
        if stale:
            job.status = IdentityJobStatus.DISCARDED_STALE
            job.completed_at = now
            job.last_outcome = "STALE"
            job.lease_token = None
            job.lease_expires_at = None
            job.save()
            return job, f"stale:{stale}"
        if not _fingerprint_still_current(job.revision, _profile(verification.registration)):
            # Never check stale data. The case is routed in its own
            # transaction, in the lock order: this one holds a job row only.
            return job, "stale:identity_changed"
        if job.attempts >= max_attempts():
            return job, "exhaust"
        job.status = IdentityJobStatus.IN_PROGRESS
        job.attempts += 1
        job.lease_token = token
        job.lease_expires_at = now + timedelta(
            seconds=int(_setting("IDENTITY_VERIFICATION_LEASE_SECONDS", 180))
        )
        job.save()
        return job, "claimed"


def _lock_job_and_case(job_id):
    """Lock a job's Registration, case and job rows, in the lock order."""
    verification_id, registration_id = (
        IdentityVerificationJob.objects.filter(pk=job_id)
        .values_list("verification_id", "verification__registration_id")
        .get()
    )
    lock_registration_rows([registration_id])
    verification = (
        IdentityVerification.objects.select_for_update(of=("self",))
        .select_related("registration")
        .get(pk=verification_id)
    )
    job = (
        IdentityVerificationJob.objects.select_for_update(of=("self",))
        .select_related("revision__identifier")
        .get(pk=job_id)
    )
    return verification, job


def _finalize_without_attempt(job_id, *, outcome: str) -> str:
    """Stop a job without a provider call and route its case, atomically.

    `outcome` is `exhaust` (every attempt of the budget was claimed; the job is
    EXHAUSTED and the case goes to manual review with
    `PROVIDER_ATTEMPTS_EXHAUSTED`) or `identity_changed` (the job is discarded
    and the case goes to manual review with `IDENTITY_DATA_CHANGED`). Every
    condition is checked again under the locks; `not_due` means another worker
    or a command handled the job first. The lease is always cleared.
    """
    now = timezone.now()
    exhaust = outcome == "exhaust"
    with transaction.atomic(durable=True):
        verification, job = _lock_job_and_case(job_id)
        due = job.status == IdentityJobStatus.PENDING and job.next_attempt_at <= now
        expired = (
            job.status == IdentityJobStatus.IN_PROGRESS
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if not (due or expired):
            return "not_due"
        if exhaust and job.attempts < max_attempts():
            return "not_due"
        if not exhaust and _fingerprint_still_current(
            job.revision, _profile(verification.registration)
        ):
            return "not_due"
        job.status = IdentityJobStatus.EXHAUSTED if exhaust else IdentityJobStatus.DISCARDED_STALE
        job.completed_at = now
        job.last_outcome = "ATTEMPTS_EXHAUSTED" if exhaust else "STALE"
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        reason = (
            IdentityReasonCode.PROVIDER_ATTEMPTS_EXHAUSTED
            if exhaust
            else IdentityReasonCode.IDENTITY_DATA_CHANGED
        )
        if (
            verification.status == IdentityStatus.PENDING
            and verification.current_revision_id == job.revision_id
        ):
            verification.status = IdentityStatus.MANUAL_REVIEW
            verification.reason_code = reason
            verification.status_changed_at = now
            verification.version += 1
            verification.save()
        _audit(
            action_codes.IDENTITY_PROVIDER_ATTEMPTS_EXHAUSTED
            if exhaust
            else action_codes.IDENTITY_PROVIDER_RESULT_DISCARDED,
            verification=verification,
            reason_code=reason if exhaust else "identity_changed",
            after={"status": verification.status, "attempts": job.attempts},
        )
        return "exhausted" if exhaust else "stale"


def _release_unattempted(job_id, token) -> None:
    """Give the claim back when no provider slot was free: a busy slot is not
    an attempt (R-IDV-04)."""
    with transaction.atomic(durable=True):
        job = IdentityVerificationJob.objects.select_for_update().get(pk=job_id)
        if job.lease_token != token or job.status != IdentityJobStatus.IN_PROGRESS:
            return
        job.status = IdentityJobStatus.PENDING
        job.attempts = max(0, job.attempts - 1)
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = timezone.now() + timedelta(
            seconds=int(_setting("IDENTITY_PROVIDER_SLOT_RETRY_SECONDS", 15))
        )
        job.save()


def process_identity_job(job_id, *, provider=None) -> str:
    """Process one durable job. Returns a short outcome label for callers and
    tests. Safe under duplicate delivery and concurrent workers."""
    _require_no_open_transaction()
    token = uuid.uuid4()
    job, state = _claim(job_id, token, timezone.now())
    if state == "exhaust":
        return _finalize_without_attempt(job_id, outcome="exhaust")
    if state == "stale:identity_changed":
        return _finalize_without_attempt(job_id, outcome="identity_changed")
    if state.startswith("stale:"):
        return "stale"
    if job is None:
        return state
    attempt_number = job.attempts
    nin = job.revision.identifier.value_encrypted  # decrypted; never logged
    provider = provider or get_nin_provider()

    if not getattr(provider, "IS_OFFICIAL", False) and not _setting(
        "IDENTITY_ALLOW_SIMULATED_PROVIDER", False
    ):
        # Defence in depth behind the startup validation (A13-06).
        outcome, duration_ms = (
            LookupOutcome(LookupKind.NOT_CONFIGURED, detail="simulation_refused"),
            0,
        )
    else:
        with provider_slot() as acquired:
            if not acquired:
                _release_unattempted(job_id, token)
                return "deferred"
            _require_no_open_transaction()
            started = time.monotonic()
            outcome = _safe_lookup(provider, nin)
            duration_ms = int((time.monotonic() - started) * 1000)
    return _apply_lookup_result(
        job_id=job.pk,
        token=token,
        outcome=outcome,
        provider=provider,
        attempt_number=attempt_number,
        duration_ms=duration_ms,
    )


#: Name fields that may take the official form, with the evaluation field
#: holding the retained official value.
_OFFICIAL_NAME_FIELDS = (
    ("given_names", "retained_given_names_latin"),
    ("family_name", "retained_family_name_latin"),
)


def _apply_official_name_form(verification, revision, profile, evaluation) -> list[str]:
    """R-IDV-02 (A13-04, IDV-03): set a name that differs from the official
    Latin form only by case or spacing to the official form.

    Runs in the transaction that verifies the identity, after the final
    identifier update. Only `normalized_match` fields change; the official
    value must pass the project's Latin-name rule and still compare equal to
    the current value, or nothing changes at all. The change is RECORDED: a new
    revision (`OFFICIAL_NAME_NORMALIZATION`) whose fingerprint covers the new
    names becomes the current one, so later freshness checks keep passing. The
    submitted originals remain in the immutable submission snapshot. The birth
    date is never touched. Returns the changed field names.
    """
    from apps.core.text_rules import TextRuleError, normalize_latin_name

    changed: dict[str, str] = {}
    for field_name, retained in _OFFICIAL_NAME_FIELDS:
        if evaluation.comparison.get(field_name) != Comparison.NORMALIZED_MATCH:
            continue
        current = getattr(profile, f"submitted_{field_name}")
        try:
            official = normalize_latin_name(getattr(evaluation, retained))
        except TextRuleError:
            return []
        if normalize_name_for_comparison(official) != normalize_name_for_comparison(current):
            return []
        if official != current:
            changed[field_name] = official
    if not changed:
        return []
    for field_name, value in changed.items():
        setattr(profile, f"submitted_{field_name}", value)
    profile.submitted_full_name = (
        f"{profile.submitted_given_names} {profile.submitted_family_name}".strip()
    )
    profile.save(
        update_fields=[
            *(f"submitted_{field_name}" for field_name in changed),
            "submitted_full_name",
            "updated_at",
        ]
    )
    verification.current_revision = create_revision(
        verification=verification,
        identifier=revision.identifier,
        profile=profile,
        source=IdentityRevisionSource.OFFICIAL_NAME_NORMALIZATION,
        changed_fields=list(changed),
    )
    return list(changed)


def _apply_lookup_result(*, job_id, token, outcome, provider, attempt_number, duration_ms) -> str:
    from apps.registrations.models import RegistrationProfile

    now = timezone.now()
    with transaction.atomic(durable=True):
        verification, job = _lock_job_and_case(job_id)
        registration = verification.registration
        profile = RegistrationProfile.objects.get(registration_id=verification.registration_id)
        lease_lost = job.lease_token != token or job.status != IdentityJobStatus.IN_PROGRESS
        stale = (
            "lease_lost"
            if lease_lost
            else _stale_reason(job, verification, registration)
            or ("identity_changed" if not _fingerprint_still_current(job.revision, profile) else "")
        )

        evaluation = None
        if outcome.kind == LookupKind.FOUND and outcome.facts is not None and not stale:
            evaluation = evaluate_identity_match(
                requested_nin=job.revision.identifier.value_encrypted,
                submitted_given_names=profile.submitted_given_names,
                submitted_family_name=profile.submitted_family_name,
                submitted_birth_date=profile.date_of_birth,
                facts=outcome.facts,
            )
        final_reason = ""
        if evaluation is not None and evaluation.verified:
            # The final write: a fresh, locked identifier row, the revision
            # fingerprint against it, the duplicate recheck, a conditional
            # update (R-IDV-01).
            final_reason = verify_identifier_or_conflict(
                verification, job.revision.identifier, revision=job.revision, profile=profile
            )
            if final_reason == IdentityReasonCode.IDENTITY_DATA_CHANGED:
                stale, evaluation = "identity_changed", None

        if stale == "identity_changed" and verification.current_revision_id == job.revision_id:
            if verification.status == IdentityStatus.PENDING:
                verification.status = IdentityStatus.MANUAL_REVIEW
                verification.reason_code = IdentityReasonCode.IDENTITY_DATA_CHANGED
                verification.status_changed_at = now
                verification.version += 1
                verification.save()
        if stale:
            _record_attempt(
                job=job,
                verification=verification,
                outcome=outcome,
                provider=provider,
                attempt_number=attempt_number,
                duration_ms=duration_ms,
                evaluation=None,
                reason_code="",
                applied=False,
            )
            if not lease_lost:
                job.status = IdentityJobStatus.DISCARDED_STALE
                job.completed_at = now
                job.last_outcome = "STALE"
                job.lease_token = None
                job.lease_expires_at = None
                job.save()
            _audit(
                action_codes.IDENTITY_PROVIDER_RESULT_DISCARDED,
                verification=verification,
                reason_code=stale,
                after={"outcome": outcome.kind},
            )
            return "stale"

        # Transient failures: bounded retries with backoff, verification stays PENDING.
        if outcome.kind in (LookupKind.UNAVAILABLE, LookupKind.AUTH_ERROR) and outcome.retryable:
            if job.attempts < max_attempts():
                delay = retry_delays()[min(job.attempts - 1, len(retry_delays()) - 1)]
                _record_attempt(
                    job=job,
                    verification=verification,
                    outcome=outcome,
                    provider=provider,
                    attempt_number=attempt_number,
                    duration_ms=duration_ms,
                    evaluation=None,
                    reason_code=_REASON_FOR_KIND[outcome.kind],
                    applied=True,
                )
                job.status = IdentityJobStatus.PENDING
                job.next_attempt_at = now + timedelta(seconds=delay)
                job.lease_token = None
                job.lease_expires_at = None
                job.last_outcome = outcome.kind
                job.save()
                _audit(
                    action_codes.IDENTITY_PROVIDER_RETRY_SCHEDULED,
                    verification=verification,
                    reason_code=_REASON_FOR_KIND[outcome.kind],
                    after={"attempt": attempt_number, "delay_seconds": delay},
                )
                return "retry_scheduled"

        source = ""
        if outcome.kind == LookupKind.FOUND and evaluation is not None:
            reason = evaluation.reason_code
            if evaluation.verified:
                reason = final_reason
                if not reason:
                    source = (
                        VerificationSource.MINISTRY_API
                        if getattr(provider, "IS_OFFICIAL", False)
                        else VerificationSource.SIMULATED_API
                    )
        elif outcome.kind == LookupKind.FOUND:
            reason = IdentityReasonCode.INVALID_RESPONSE
        else:
            reason = _REASON_FOR_KIND.get(outcome.kind, IdentityReasonCode.INVALID_RESPONSE)

        _record_attempt(
            job=job,
            verification=verification,
            outcome=outcome,
            provider=provider,
            attempt_number=attempt_number,
            duration_ms=duration_ms,
            evaluation=evaluation,
            reason_code=reason,
            applied=True,
        )
        from_status = verification.status
        name_fields = (
            _apply_official_name_form(verification, job.revision, profile, evaluation)
            if source
            else []
        )
        verification.status = (
            IdentityStatus.API_VERIFIED if source else IdentityStatus.MANUAL_REVIEW
        )
        verification.reason_code = "" if source else reason
        verification.verification_source = source
        verification.status_changed_at = now
        if source:
            verification.decided_at = now
            verification.decided_by = None
        verification.version += 1
        verification.save()
        if name_fields:
            IdentityDecision.objects.create(
                verification=verification,
                revision=verification.current_revision,
                action=IdentityDecisionAction.OFFICIAL_NAME_APPLIED,
                from_status=from_status,
                to_status=verification.status,
                reason_code=source,
                requested_items=name_fields,
            )
            _audit(
                action_codes.IDENTITY_OFFICIAL_NAME_APPLIED,
                verification=verification,
                reason_code=source,
                after={"fields": name_fields, "revision": verification.current_revision.number},
            )
        exhausted = (
            outcome.kind in (LookupKind.UNAVAILABLE, LookupKind.AUTH_ERROR) and outcome.retryable
        )
        job.status = IdentityJobStatus.EXHAUSTED if exhausted else IdentityJobStatus.COMPLETED
        job.completed_at = now
        job.lease_token = None
        job.lease_expires_at = None
        job.last_outcome = outcome.kind
        job.save()
        _audit(
            action_codes.IDENTITY_PROVIDER_RESULT_APPLIED,
            verification=verification,
            reason_code=verification.reason_code,
            after={
                "outcome": outcome.kind,
                "status": verification.status,
                "source": source,
                "birth_date_comparison": (
                    evaluation.comparison.get("birth_date", "") if evaluation is not None else ""
                ),
                "official_name_fields": name_fields,
            },
        )
        return verification.status


def verify_identifier_or_conflict(verification, identifier, *, revision=None, profile=None) -> str:
    """Mark the identifier VERIFIED unless that would be wrong.

    Returns "" on success, `DUPLICATE_IDENTIFIER` when another person holds it
    (the final duplicate recheck under the identifier lock), or
    `IDENTITY_DATA_CHANGED` when it is no longer this person's verifiable
    identifier. The row is read again and locked AFTER the advisory lock (a
    cached object never establishes freshness); with `revision` and `profile`
    the revision fingerprint is checked against that row too; and the update
    itself is conditional on a verifiable status, so a replaced, revoked or
    expired identifier is never promoted (R-IDV-01). The partial unique index
    on verified identifiers is the database backstop for duplicates. Runs
    inside the caller's transaction, after its row locks.
    """
    lock_identifier_values(
        identifier.identifier_type, identifier.country_code_id, [identifier.value_encrypted]
    )
    fresh = (
        IdentityIdentifier.objects.select_for_update(no_key=True).filter(pk=identifier.pk).first()
    )
    if (
        fresh is None
        or fresh.person_id != verification.person_id
        or fresh.status not in VERIFIABLE_IDENTIFIER_STATUSES
    ):
        return IdentityReasonCode.IDENTITY_DATA_CHANGED
    if (
        revision is not None
        and profile is not None
        and not _fingerprint_still_current(revision, profile, identifier=fresh)
    ):
        return IdentityReasonCode.IDENTITY_DATA_CHANGED
    conflicts = find_cross_person_conflicts(
        identifier_type=fresh.identifier_type,
        country_code_id=fresh.country_code_id,
        raw_value=fresh.value_encrypted,
        person_id=verification.person_id,
    )
    if conflicts.exists:
        return IdentityReasonCode.DUPLICATE_IDENTIFIER
    try:
        with transaction.atomic():
            updated = IdentityIdentifier.objects.filter(
                pk=fresh.pk, status__in=VERIFIABLE_IDENTIFIER_STATUSES
            ).update(status=IdentifierStatus.VERIFIED, verified_at=timezone.now())
    except IntegrityError:
        return IdentityReasonCode.DUPLICATE_IDENTIFIER
    if updated != 1:
        return IdentityReasonCode.IDENTITY_DATA_CHANGED
    return ""


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def due_job_ids(limit: int = 100) -> list:
    now = timezone.now()
    bounded = max(1, min(int(limit), 1000))
    from django.db.models import Q

    return list(
        IdentityVerificationJob.objects.filter(
            Q(status=IdentityJobStatus.PENDING, next_attempt_at__lte=now)
            | Q(status=IdentityJobStatus.IN_PROGRESS, lease_expires_at__lte=now)
        )
        .order_by("next_attempt_at")
        .values_list("pk", flat=True)[:bounded]
    )


def run_due_identity_jobs(limit: int = 50, *, provider=None) -> dict[str, int]:
    """Process due jobs inline (the management command, and tests). Returns a
    count per outcome label."""
    counts: dict[str, int] = {}
    for job_id in due_job_ids(limit):
        label = process_identity_job(job_id, provider=provider)
        counts[label] = counts.get(label, 0) + 1
    return counts


def dispatch_due_identity_jobs(limit: int = 100) -> int:
    """Re-enqueue due jobs on the broker (beat). Returns how many were sent."""
    sent = 0
    for job_id in due_job_ids(limit):
        if dispatch_identity_job(str(job_id)):
            sent += 1
    return sent


def birth_date_was_skipped(attempt) -> bool:
    """True when the attempt verified without comparing the official date."""
    return (attempt.comparison or {}).get("birth_date") == Comparison.SKIPPED_PRESUMED
