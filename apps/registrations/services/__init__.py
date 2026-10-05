"""Registration domain services (Schema §5, §16.3-§16.5).

Deliberately NOT the complete registration workflow or public screens
(project scope) -- these are the Phase 1 foundations: concurrency-safe
event-scoped reference allocation, draft creation with deduplication-key
preservation, and idempotent immutable submission, each wrapped in one
atomic transaction with audit/outbox integration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.core.outbox.contracts import OutboxMessage, OutboxPublisher
from apps.core.outbox.persistent import PersistentOutboxPublisher
from apps.events.models import EventEdition
from apps.privacy.models import LegalDocumentVersionStatus
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
    RegistrationSubmission,
    RegistrationSubmissionKind,
    RegistrationSubmittedByType,
)

ALGERIA_COUNTRY_CODE = "DZ"


def allocate_registration_reference(event_edition: EventEdition) -> str:
    """Return the next event-scoped public reference, e.g. `ASC2026-R-000123`.

    MUST be called from inside an existing atomic transaction, ideally the
    same one that creates the Registration row. Locks the `EventEdition`
    row with `SELECT ... FOR UPDATE`, so concurrent callers for the same
    event are serialized and never observe/consume the same sequence
    number (Schema §3.1/§20: "Reference generation must be concurrency-safe").
    """
    locked = EventEdition.objects.select_for_update().get(pk=event_edition.pk)
    sequence = locked.next_registration_sequence
    locked.next_registration_sequence = sequence + 1
    locked.save(update_fields=["next_registration_sequence"])
    return f"{locked.code}-R-{sequence:06d}"


def require_channel_for_new_draft(event_edition: EventEdition, source_kind: str) -> None:
    """Refuse to create a draft of `source_kind` when the event's channel
    state does not accept it (UX-4, D-12).

    MUST run inside the creating transaction. Locks the event row with
    `FOR UPDATE`, the same lock `allocate_registration_reference` takes next,
    so a channel change that commits first is always seen here.
    """
    from apps.events.policies.registration_channels import (
        RegistrationChannelClosed,
        decide_for_source,
    )

    locked = EventEdition.objects.select_for_update().get(pk=event_edition.pk)
    decision = decide_for_source(locked, source_kind)
    if not decision.allowed:
        raise RegistrationChannelClosed(decision)


def require_channel_for_submission(registration: Registration) -> None:
    """Refuse a final submission that the event's channel state refuses.

    MUST run inside the submitting transaction. Reads the event under
    `FOR SHARE`: concurrent submissions do not wait for each other, a
    channel change waits for them, and a change that committed first wins.
    """
    from apps.events.policies.registration_channels import (
        RegistrationChannelClosed,
        decide_for_source,
        lock_event_for_channel_read,
    )

    event = lock_event_for_channel_read(registration.event_edition_id)
    decision = decide_for_source(event, registration.source_kind)
    if not decision.allowed:
        raise RegistrationChannelClosed(decision)


def compute_deduplication_key(
    *, event_edition_id: UUID, person_id: UUID | None, source_context_key: str
) -> str:
    """Derive the current-context deduplication key (Schema §5.4).

    Derived from event, resolved person, and source context -- never from
    email alone, so email is not the sole matching factor for a claimed
    account with multiple contact points.
    """
    person_component = str(person_id) if person_id else "unclaimed"
    raw = f"{event_edition_id}:{person_component}:{source_context_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DraftRegistrationResult:
    registration: Registration


def create_open_draft_registration(
    *,
    event_edition: EventEdition,
    source_context_key: str,
    preferred_language: str,
    person_id: UUID | None,
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
    correlation_id: str = "",
) -> DraftRegistrationResult:
    """Create a `DRAFT` `OPEN`-source Registration (Schema §5.3, §20 item 5).

    Wraps allocation, creation, audit, and outbox enqueue in one
    transaction: a rollback anywhere undoes the reference allocation, the
    row, and the outbox row together (Schema §16.5).
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        require_channel_for_new_draft(event_edition, RegistrationSourceKind.OPEN)
        public_reference = allocate_registration_reference(event_edition)
        deduplication_key = compute_deduplication_key(
            event_edition_id=event_edition.pk,
            person_id=person_id,
            source_context_key=source_context_key,
        )
        registration = Registration.objects.create(
            public_reference=public_reference,
            event_edition=event_edition,
            person_id=person_id,
            source_kind=RegistrationSourceKind.OPEN,
            source_context_key=source_context_key,
            deduplication_key=deduplication_key,
            public_status=RegistrationPublicStatus.DRAFT,
            internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
            preferred_language=preferred_language,
            current_step="identity",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="PARTICIPANT" if person_id else "SYSTEM",
                actor_person_id=person_id,
                action_code="REGISTRATION_DRAFT_CREATED",
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=event_edition.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="registration.draft_created",
                aggregate_type="Registration",
                aggregate_id=str(registration.pk),
                payload={"public_reference": public_reference},
            )
        )
    return DraftRegistrationResult(registration=registration)


def create_invited_draft_registration(
    *,
    event_edition: EventEdition,
    campaign,
    source_context_key: str,
    preferred_language: str,
    person_id: UUID | None,
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
    correlation_id: str = "",
) -> DraftRegistrationResult:
    """Create an `INVITATION`-source `DRAFT` Registration (Phase 2 Prompt 2, AF-ORG-02).

    Same allocate/create/audit/enqueue transaction shape as
    `create_open_draft_registration`. `source_organization` is set to the
    campaign's inviting organization and `invitation_campaign` preserves
    exact provenance on this Registration Context (INV-002) -- never
    inferred later from "the currently active link", which could rotate.
    `source_context_key` MUST be campaign-specific (e.g.
    `f"invitation-campaign:{campaign.pk}"`) so the SAME Person invited by
    TWO different campaigns/organizations gets two distinct
    `deduplication_key` values and therefore two independently valid
    Registration Contexts (Schema §5.4, BR-DUP-003).
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        require_channel_for_new_draft(event_edition, RegistrationSourceKind.INVITATION)
        public_reference = allocate_registration_reference(event_edition)
        deduplication_key = compute_deduplication_key(
            event_edition_id=event_edition.pk,
            person_id=person_id,
            source_context_key=source_context_key,
        )
        registration = Registration.objects.create(
            public_reference=public_reference,
            event_edition=event_edition,
            person_id=person_id,
            source_kind=RegistrationSourceKind.INVITATION,
            source_context_key=source_context_key,
            source_organization=campaign.organization,
            invitation_campaign=campaign,
            deduplication_key=deduplication_key,
            public_status=RegistrationPublicStatus.DRAFT,
            internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
            preferred_language=preferred_language,
            current_step="identity",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="PARTICIPANT" if person_id else "SYSTEM",
                actor_person_id=person_id,
                action_code="REGISTRATION_DRAFT_CREATED",
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=event_edition.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="registration.draft_created",
                aggregate_type="Registration",
                aggregate_id=str(registration.pk),
                payload={"public_reference": public_reference, "source_kind": "INVITATION"},
            )
        )
    return DraftRegistrationResult(registration=registration)


def create_on_behalf_draft_registration(
    *,
    event_edition: EventEdition,
    source_organization,
    created_on_behalf_by,
    source_context_key: str,
    preferred_language: str,
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
    correlation_id: str = "",
) -> DraftRegistrationResult:
    """Create an unclaimed `ON_BEHALF`-source `DRAFT` Registration (AF-ORG-03).

    `person_id` is always `None` at creation -- the Registration is
    unclaimed until a participant later verifies the intended email through
    the existing OTP flow and claims it (`claim_on_behalf_registration`,
    `apps.invitations.services`). Never marks the Registration submitted,
    never creates a legal `AcceptanceRecord`, never assigns a decision,
    role, badge, or access (Phase 2 Prompt 2 on-behalf requirements).
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        require_channel_for_new_draft(event_edition, RegistrationSourceKind.ON_BEHALF)
        public_reference = allocate_registration_reference(event_edition)
        deduplication_key = compute_deduplication_key(
            event_edition_id=event_edition.pk,
            person_id=None,
            source_context_key=source_context_key,
        )
        registration = Registration.objects.create(
            public_reference=public_reference,
            event_edition=event_edition,
            person_id=None,
            source_kind=RegistrationSourceKind.ON_BEHALF,
            source_context_key=source_context_key,
            source_organization=source_organization,
            created_on_behalf_by=created_on_behalf_by,
            deduplication_key=deduplication_key,
            public_status=RegistrationPublicStatus.DRAFT,
            internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
            preferred_language=preferred_language,
            current_step="identity",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(created_on_behalf_by, "pk", None),
                action_code=action_codes.ON_BEHALF_DRAFT_CREATED,
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=event_edition.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="registration.draft_created",
                aggregate_type="Registration",
                aggregate_id=str(registration.pk),
                payload={"public_reference": public_reference, "source_kind": "ON_BEHALF"},
            )
        )
    return DraftRegistrationResult(registration=registration)


class DeduplicationConflictError(Exception):
    """Raised when claiming a Registration would collide with an existing
    current-context deduplication key for the resolved Person.

    A real, if rare, possibility: the participant claiming this on-behalf
    draft may ALREADY have another current Registration Context whose
    `source_context_key` happens to canonicalize to the same key once
    `person_id` is no longer the "unclaimed" placeholder. Handled as a
    controlled conflict the caller can act on -- never an unhandled
    `IntegrityError`/HTTP 500 (Phase 2 Prompt 2 requirement).
    """


def attach_claimed_person_to_registration(
    *,
    registration: Registration,
    person,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> Registration:
    """Attach the resolved, verified Person to an on-behalf draft (AF-ORG-04).

    MUST be called from inside the SAME transaction that already holds a
    `select_for_update()` lock on `registration` (the caller,
    `apps.invitations.services.claim_on_behalf_registration`, acquires it) --
    this function itself only recomputes the deduplication key and saves,
    inside a nested `atomic()` savepoint so a unique-constraint collision
    rolls back ONLY this save, never the outer claim transaction.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    if registration.person_id is not None:
        raise ValueError("Registration is already claimed.")
    new_dedup_key = compute_deduplication_key(
        event_edition_id=registration.event_edition_id,
        person_id=person.pk,
        source_context_key=registration.source_context_key,
    )
    try:
        with transaction.atomic():
            registration.person_id = person.pk
            registration.deduplication_key = new_dedup_key
            registration.claimed_at = timezone.now()
            registration.save(
                update_fields=["person_id", "deduplication_key", "claimed_at", "updated_at"]
            )
    except IntegrityError:
        raise DeduplicationConflictError(
            "This claim would duplicate an existing current Registration Context."
        ) from None
    audit_recorder.record(
        AuditRecord(
            actor_type="PARTICIPANT",
            actor_person_id=person.pk,
            action_code=action_codes.ON_BEHALF_CLAIM_SUCCEEDED,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            result="SUCCESS",
            correlation_id=correlation_id,
        )
    )
    return registration


def create_delegation_draft_registration(
    *,
    event_edition: EventEdition,
    source_organization,
    source_context_key: str,
    preferred_language: str = "en",
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
    correlation_id: str = "",
) -> DraftRegistrationResult:
    """Create a `DELEGATION`-source `DRAFT` Registration for one applied CSV row.

    `source_context_key` MUST be row-specific (e.g.
    `f"delegation-row:{row.pk}"`) so re-applying the same batch is
    idempotent at the Registration-creation layer too, in addition to the
    caller's own `DelegationRow.registration_id` short-circuit.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        require_channel_for_new_draft(event_edition, RegistrationSourceKind.DELEGATION)
        public_reference = allocate_registration_reference(event_edition)
        deduplication_key = compute_deduplication_key(
            event_edition_id=event_edition.pk,
            person_id=None,
            source_context_key=source_context_key,
        )
        registration = Registration.objects.create(
            public_reference=public_reference,
            event_edition=event_edition,
            person_id=None,
            source_kind=RegistrationSourceKind.DELEGATION,
            source_context_key=source_context_key,
            source_organization=source_organization,
            deduplication_key=deduplication_key,
            public_status=RegistrationPublicStatus.DRAFT,
            internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
            preferred_language=preferred_language,
            current_step="identity",
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="SYSTEM",
                action_code="REGISTRATION_DRAFT_CREATED",
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=event_edition.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="registration.draft_created",
                aggregate_type="Registration",
                aggregate_id=str(registration.pk),
                payload={"public_reference": public_reference, "source_kind": "DELEGATION"},
            )
        )
    return DraftRegistrationResult(registration=registration)


def _canonical_snapshot_hash(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyKeyConflictError(Exception):
    """Raised when an idempotency key already identifies a submission for a
    DIFFERENT Registration (Prompt 6 P6-H-02 correction).

    Never returns or exposes the other Registration's submission -- the
    caller must treat this as a bounded domain error, not a successful
    idempotent retry. In practice this cannot occur through the canonical,
    Registration-derived key (`initial_submission_operation_key`), which is
    unique per Registration by construction; it guards against an
    externally supplied or hand-crafted key colliding across Registrations.
    """


def initial_submission_operation_key(registration_id) -> str:
    """The canonical, deterministic idempotency key for a Registration's
    INITIAL submission (Prompt 6 P6-H-02 correction).

    Derived ONLY from the Registration's own id -- never from a randomly
    generated, session-cached value -- so every request for the same
    Registration's first submission (a genuine double-click, a client
    retry after a timeout, or a fresh request arriving after the first
    already committed) converges on the SAME key, regardless of how many
    different requests raced to get here.
    """
    return f"registration-initial-submission:{registration_id}"


def record_registration_submission(
    *,
    registration: Registration,
    snapshot: dict[str, Any],
    submission_kind: str = RegistrationSubmissionKind.INITIAL,
    submitted_by_type: str = RegistrationSubmittedByType.PARTICIPANT,
    submitted_by_user_id: UUID | None = None,
    idempotency_key: str,
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
    submitted_at: datetime | None = None,
    correlation_id: str = "",
) -> RegistrationSubmission:
    """Create one immutable `RegistrationSubmission` (Schema §5.8), idempotently.

    Idempotent on `idempotency_key`: a retried call with the same key
    returns the existing row instead of creating a duplicate (Schema §2.1
    "Idempotent ingestion"). `sequence` is allocated under a row lock on
    the parent Registration so two concurrent submissions never receive
    the same sequence number.

    For `submission_kind=INITIAL` specifically, dedup is primarily by
    "does this Registration already have an INITIAL submission", not only
    by key (Prompt 6 P6-H-02) -- this is what makes two concurrent callers
    that supplied DIFFERENT idempotency keys (e.g. from the pre-fix
    session-cached-random-key race) still converge on the SAME result. A
    PostgreSQL partial unique constraint (`reg_submission_one_initial_uq`)
    backs this invariant at the database level as well.

    The foreign-key-collision check runs FIRST, immediately after the lock
    -- BEFORE the "does this Registration already have an INITIAL
    submission" recheck (Prompt 6 P6-H-01/P6-H-02 follow-up). Checking it
    afterward would let a foreign collision go undetected whenever the
    TARGET Registration already happens to have its own INITIAL submission
    (the function would return that instead, silently skipping the
    collision check) -- never returning or exposing the OTHER
    Registration's submission either way, but a genuine key mix-up must
    always raise, not be silently absorbed.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)

        # A concurrent request may have committed the same key while this
        # transaction waited for the parent-row lock. Re-check inside the
        # serialized section so an idempotent retry returns the winner rather
        # than leaking a unique-constraint IntegrityError.
        existing_for_key = RegistrationSubmission.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if (
            existing_for_key is not None
            and existing_for_key.registration_id != locked_registration.pk
        ):
            # Never return or expose another Registration's submission
            # merely because an external caller supplied the same key
            # (Prompt 6 P6-H-02) -- fail safely instead. Checked BEFORE the
            # existing-INITIAL recheck below, so this is never skipped just
            # because `locked_registration` already has its own INITIAL
            # submission from a different, legitimate key.
            raise IdempotencyKeyConflictError(
                "This idempotency key is already associated with a different Registration."
            )

        if submission_kind == RegistrationSubmissionKind.INITIAL:
            existing_initial = RegistrationSubmission.objects.filter(
                registration=locked_registration,
                submission_kind=RegistrationSubmissionKind.INITIAL,
            ).first()
            if existing_initial is not None:
                return existing_initial

        if existing_for_key is not None:
            return existing_for_key
        next_sequence = (
            RegistrationSubmission.objects.filter(registration=locked_registration).count() + 1
        )
        submission = RegistrationSubmission.objects.create(
            registration=locked_registration,
            sequence=next_sequence,
            submission_kind=submission_kind,
            snapshot_json=snapshot,
            snapshot_hash=_canonical_snapshot_hash(snapshot),
            submitted_by_type=submitted_by_type,
            submitted_by_user_id=submitted_by_user_id,
            submitted_at=submitted_at or timezone.now(),
            idempotency_key=idempotency_key,
        )
        if next_sequence == 1:
            locked_registration.public_status = RegistrationPublicStatus.SUBMITTED
            locked_registration.submitted_at = submission.submitted_at
            locked_registration.version += 1
            locked_registration.save(
                update_fields=["public_status", "submitted_at", "version", "updated_at"]
            )
        audit_recorder.record(
            AuditRecord(
                actor_type=submitted_by_type,
                actor_user_id=submitted_by_user_id,
                action_code="REGISTRATION_SUBMISSION_RECORDED",
                target_type="Registration",
                target_uuid=locked_registration.pk,
                event_edition_id=locked_registration.event_edition_id,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="registration.submission_recorded",
                aggregate_type="Registration",
                aggregate_id=str(locked_registration.pk),
                payload={"sequence": next_sequence, "submission_kind": submission_kind},
            )
        )
    return submission


# ---------------------------------------------------------------------------
# Universal registration wizard step services (Prompt 4, AF-REG-02..AF-REG-06).
#
# The public form never exposes a Participant Role, Badge Type or Access
# Profile choice (fixed product rule) -- nothing below accepts or
# stores one. Duplicate detection is advisory only: a cross-person match
# flags `internal_status = DUPLICATE_REVIEW` for an authorized reviewer and
# never blocks, rejects, or reveals the match to the applicant
# (AF-REG-05: "Duplicate detection is an aid, not an automatic merge
# decision"; UI/UX §6.6: "Public messages must not reveal whether another
# person exists in the system").
# ---------------------------------------------------------------------------

_STEP_ORDER = ["identity", "contact", "professional", "interests", "review", "notices"]


@transaction.atomic
def get_or_create_active_draft(*, person, event_edition: EventEdition) -> Registration:
    """Resume the person's current OPEN Draft for this event, or start a new one (AF-REG-01).

    "Same Person, same source context, existing Draft -> resume the Draft"
    (AF-REG-05). `source_context_key` is fixed to `"open"` for V1's single
    universal open-registration context per event.
    """
    # Serialize first-time creation per participant. Without this lock, two
    # simultaneous first requests can both observe no row and one leaks a
    # partial-unique-constraint IntegrityError instead of resuming the winner.
    from apps.people.models import Person

    locked_person = Person.objects.select_for_update().get(pk=person.pk)
    existing = (
        Registration.objects.filter(
            person=locked_person,
            event_edition=event_edition,
            source_kind=RegistrationSourceKind.OPEN,
            source_context_key="open",
            is_current_context=True,
        )
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return existing
    return create_open_draft_registration(
        event_edition=event_edition,
        source_context_key="open",
        preferred_language=locked_person.preferred_language,
        person_id=locked_person.id,
    ).registration


class RegistrationNotRestartable(ValueError):
    """Only a registration finally rejected on identity grounds offers a new
    registration from the same account (owner decision IDV-Q2)."""


def is_identity_rejected(registration: Registration) -> bool:
    """True for a registration closed by a final identity rejection (IDV-Q2):
    NOT_APPROVED, released (non-current), its identity case REJECTED and its
    current participation decision recorded by that rejection. A terminal
    state: such a registration is never reopened."""
    from apps.people.models import IdentityStatus, IdentityVerification
    from apps.reviews.models import RegistrationDecision
    from apps.reviews.services import IDENTITY_REJECTION_INTERNAL_REASON

    return (
        registration.public_status == RegistrationPublicStatus.NOT_APPROVED
        and not registration.is_current_context
        and IdentityVerification.objects.filter(
            registration_id=registration.pk, status=IdentityStatus.REJECTED
        ).exists()
        and RegistrationDecision.objects.filter(
            registration_id=registration.pk,
            is_current=True,
            internal_reason_code=IDENTITY_REJECTION_INTERNAL_REASON,
        ).exists()
    )


class RestartKind:
    """The next step a rejected registration offers (IDV-Q2, corrected by IDV-Q-C1)."""

    OPEN = "OPEN"  # a new public registration
    INVITATION = "INVITATION"  # a new registration through the same, still valid invitation
    PUBLIC_CLOSED = "PUBLIC_CLOSED"  # an open registration, while public registration is closed
    EVENT_CLOSED = "EVENT_CLOSED"  # the event accepts no new registration at all
    NEW_INVITATION_NEEDED = "NEW_INVITATION_NEEDED"  # the invitation can no longer be used
    CONTACT_TEAM = "CONTACT_TEAM"  # created by staff or an organization (on behalf, delegation)


@dataclass(frozen=True)
class RestartOption:
    kind: str
    link: Any = None

    @property
    def available(self) -> bool:
        return self.kind in (RestartKind.OPEN, RestartKind.INVITATION)


def restart_option_for(registration: Registration, *, now=None) -> RestartOption:
    """The accurate, same-account next step after a final identity rejection,
    by the registration's origin (IDV-Q-C1, finding 3). Read-only; the restart
    itself re-checks everything under lock.

    * Open registration: a new public registration while public registration
      is open.
    * Invitation: a new registration through the SAME invitation link while
      it could start one now (active, not expired, campaign valid and not full,
      event not closed). Otherwise a new invitation from the inviting
      organization is needed; the public channel is never offered as a way
      around the invitation.
    * On behalf or delegation: the organization or the registration team.
    """
    from apps.events.policies.registration_channels import (
        ChannelState,
        effective_channel_state,
        public_registration_is_open,
    )

    event = registration.event_edition
    if effective_channel_state(event, now=now) == ChannelState.CLOSED:
        return RestartOption(RestartKind.EVENT_CLOSED)
    if registration.source_kind == RegistrationSourceKind.OPEN:
        if public_registration_is_open(event, now=now):
            return RestartOption(RestartKind.OPEN)
        return RestartOption(RestartKind.PUBLIC_CLOSED)
    if registration.source_kind == RegistrationSourceKind.INVITATION:
        from apps.invitations.services import reusable_invitation_link

        link = reusable_invitation_link(registration, now=now)
        if link is not None:
            return RestartOption(RestartKind.INVITATION, link=link)
        return RestartOption(RestartKind.NEW_INVITATION_NEEDED)
    return RestartOption(RestartKind.CONTACT_TEAM)


def start_registration_from_invitation(*, link, person, correlation_id: str = "") -> Registration:
    """A registration through an invitation link, for a participant who is
    already signed in (IDV-Q-C1).

    Under the Person lock: the person's CURRENT registration from that
    invitation's campaign is returned when it exists (one current context per
    campaign; a double click converges on it); otherwise a new draft is created
    by `apps.invitations.services.create_invited_draft_registration`, which
    re-validates the link and its campaign under lock and records the
    invitation use. Raises `InvitationLinkUnavailable` for an unusable link.
    Capacity is enforced at the final submission, as for every invitation.

    A current registration of that campaign that the participant WITHDREW
    (owner correction, 2026-10-04) is not returned: opening an invitation of
    the same campaign is the way to register again, so its context is released
    (it stays WITHDRAWN with its history) and a new draft is created in the
    same transaction, or nothing changes when the link is unusable.
    """
    from apps.invitations.services import create_invited_draft_registration
    from apps.people.models import Person

    with transaction.atomic():
        locked_person = Person.objects.select_for_update().get(pk=person.pk)
        existing = (
            Registration.objects.filter(
                person=locked_person,
                source_kind=RegistrationSourceKind.INVITATION,
                source_context_key=f"invitation-campaign:{link.campaign_id}",
                is_current_context=True,
            )
            .order_by("-created_at")
            .first()
        )
        withdrawn = None
        if existing is not None:
            existing = Registration.objects.select_for_update().get(pk=existing.pk)
            if not is_participant_withdrawn(existing):
                return existing
            withdrawn = existing
            _release_withdrawn_context(withdrawn)
        draft = create_invited_draft_registration(
            link,
            preferred_language=locked_person.preferred_language,
            person_id=locked_person.pk,
            correlation_id=correlation_id,
        ).registration
        if withdrawn is not None:
            PersistentAuditRecorder().record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=locked_person.pk,
                    action_code=action_codes.REGISTRATION_RESTARTED_AFTER_WITHDRAWAL,
                    target_type="Registration",
                    target_uuid=withdrawn.pk,
                    event_edition_id=withdrawn.event_edition_id,
                    result="SUCCESS",
                    after_summary={
                        "new_registration": draft.public_reference,
                        "origin": RegistrationSourceKind.INVITATION,
                    },
                    correlation_id=correlation_id,
                )
            )
        return draft


def start_registration_after_identity_rejection(
    *, registration: Registration, person, correlation_id: str = ""
) -> Registration:
    """Register again from the same account after a final identity rejection
    (owner decision IDV-Q2; origin-aware since IDV-Q-C1).

    * An open registration gets the person's current open draft for the same
      event, created when none exists (`get_or_create_active_draft`), while
      the public channel is open (`RegistrationChannelClosed` otherwise).
    * An invitation registration gets a new draft through the SAME invitation,
      via `apps.invitations.services.create_invited_draft_registration`, which
      re-validates the link and its campaign under lock and records the
      invitation use; capacity is enforced again at the final submission.
      An unusable invitation raises `InvitationLinkUnavailable`: a new
      invitation from the inviting organization is needed.
    * Any other origin is refused (`RegistrationNotRestartable`).

    Everything runs under the Person lock, so a double click or two tabs
    converge on one draft. The rejected registration is never reopened or
    changed. Its rejected state is terminal, so it is read without a lock
    (lock order: the Person row first, then the link and the campaign).
    """
    from apps.people.models import Person

    if registration.person_id != getattr(person, "pk", None):
        raise RegistrationNotRestartable("not_owner")
    if not is_identity_rejected(registration):
        raise RegistrationNotRestartable("not_identity_rejected")
    option = restart_option_for(registration)
    if registration.source_kind not in (
        RegistrationSourceKind.OPEN,
        RegistrationSourceKind.INVITATION,
    ):
        raise RegistrationNotRestartable(option.kind)
    with transaction.atomic():
        locked_person = Person.objects.select_for_update().get(pk=person.pk)
        if registration.source_kind == RegistrationSourceKind.OPEN:
            source_kind, context_key = RegistrationSourceKind.OPEN, "open"
        else:
            source_kind = RegistrationSourceKind.INVITATION
            context_key = f"invitation-campaign:{registration.invitation_campaign_id}"
        existing = (
            Registration.objects.filter(
                person=locked_person,
                event_edition_id=registration.event_edition_id,
                source_kind=source_kind,
                source_context_key=context_key,
                is_current_context=True,
            )
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing
        if source_kind == RegistrationSourceKind.OPEN:
            draft = get_or_create_active_draft(
                person=locked_person, event_edition=registration.event_edition
            )
        else:
            from apps.invitations.services import InvitationLinkUnavailable

            if option.link is None:
                raise InvitationLinkUnavailable("This invitation link is not available.")
            draft = start_registration_from_invitation(
                link=option.link, person=locked_person, correlation_id=correlation_id
            )
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=locked_person.pk,
                action_code=action_codes.IDENTITY_REGISTRATION_RESTARTED,
                target_type="Registration",
                target_uuid=registration.pk,
                event_edition_id=registration.event_edition_id,
                result="SUCCESS",
                after_summary={
                    "new_registration": draft.public_reference,
                    "origin": source_kind,
                },
                correlation_id=correlation_id,
            )
        )
    return draft


def is_participant_withdrawn(registration: Registration) -> bool:
    """True for a registration the participant withdrew (owner correction,
    2026-10-04): WITHDRAWN with a withdrawal time and no operational
    cancellation. A registration the registration team cancelled is not one;
    its participant contacts the team instead."""
    return (
        registration.public_status == RegistrationPublicStatus.WITHDRAWN
        and registration.withdrawn_at is not None
        and registration.cancelled_at is None
    )


def _origin_context(registration: Registration) -> tuple[str, str]:
    """The source kind and context key a new registration of the same origin
    uses: the one open context, or the same invitation campaign."""
    if registration.source_kind == RegistrationSourceKind.OPEN:
        return RegistrationSourceKind.OPEN, "open"
    return (
        RegistrationSourceKind.INVITATION,
        f"invitation-campaign:{registration.invitation_campaign_id}",
    )


def current_registration_of_same_origin(registration: Registration):
    """The participant's CURRENT registration for the same event and origin
    context as `registration`, other than itself, or None. Read-only."""
    if registration.source_kind not in (
        RegistrationSourceKind.OPEN,
        RegistrationSourceKind.INVITATION,
    ):
        return None
    source_kind, context_key = _origin_context(registration)
    return (
        Registration.objects.filter(
            person_id=registration.person_id,
            event_edition_id=registration.event_edition_id,
            source_kind=source_kind,
            source_context_key=context_key,
            is_current_context=True,
        )
        .exclude(pk=registration.pk)
        .order_by("-created_at")
        .first()
    )


def _release_withdrawn_context(locked: Registration) -> None:
    """Mark a withdrawn registration's context non-current, which releases its
    deduplication key for the new registration. Only that flag and the version
    change: it stays WITHDRAWN with all its history. MUST run inside the
    caller's transaction with the row locked."""
    if locked.is_current_context:
        locked.is_current_context = False
        locked.version += 1
        locked.save(update_fields=["is_current_context", "version", "updated_at"])


def start_registration_after_withdrawal(
    *, registration: Registration, person, correlation_id: str = ""
) -> Registration:
    """Register again from the same account after the participant withdrew a
    registration (owner correction, 2026-10-04), with the origin-aware policy
    of a final identity rejection (`restart_option_for`).

    * An open registration gets a NEW public draft for the same event while
      the public channel is open (`RegistrationChannelClosed` otherwise).
    * An invitation registration gets a NEW draft through the same invitation,
      which `apps.invitations.services.create_invited_draft_registration`
      re-validates under lock (link active and not expired, campaign valid,
      event not closed) and records as an invitation use; capacity is
      enforced again at the final submission. An unusable invitation raises
      `InvitationLinkUnavailable`: a new invitation is needed, and the public
      channel is never offered instead.
    * Any other origin (on behalf, delegation) is refused
      (`RegistrationNotRestartable`): the organization or the registration team
      creates a new one through the authorized process.

    The withdrawn registration is never reopened: it stays WITHDRAWN with its
    audit, consent and acceptance history; only its context is released so the
    new draft can take the deduplication key (in the same transaction, so a
    refusal releases nothing). The new draft starts empty: no consent, no
    accommodation data, no document and no identity declaration is carried
    over, and every current requirement applies to its own submission.

    Lock order: the Person row, then the withdrawn Registration, then (inside
    the draft creation) the event, the invitation link and its campaign. A
    double click, two tabs or a retry wait on the Person lock and converge on
    the one new draft.
    """
    from apps.people.models import Person

    if registration.person_id != getattr(person, "pk", None):
        raise RegistrationNotRestartable("not_owner")
    if registration.source_kind not in (
        RegistrationSourceKind.OPEN,
        RegistrationSourceKind.INVITATION,
    ):
        raise RegistrationNotRestartable(RestartKind.CONTACT_TEAM)
    with transaction.atomic():
        locked_person = Person.objects.select_for_update().get(pk=person.pk)
        locked = Registration.objects.select_for_update().get(pk=registration.pk)
        if not is_participant_withdrawn(locked):
            # For example reopened or cancelled by staff meanwhile.
            raise RegistrationNotRestartable("not_withdrawn")
        existing = current_registration_of_same_origin(locked)
        if existing is not None:
            return existing  # a double click or another tab already registered again
        option = restart_option_for(locked)
        _release_withdrawn_context(locked)
        if locked.source_kind == RegistrationSourceKind.OPEN:
            draft = create_open_draft_registration(
                event_edition=locked.event_edition,
                source_context_key="open",
                preferred_language=locked_person.preferred_language,
                person_id=locked_person.pk,
                correlation_id=correlation_id,
            ).registration
        else:
            from apps.invitations.services import (
                InvitationLinkUnavailable,
                create_invited_draft_registration,
            )

            if option.link is None:
                raise InvitationLinkUnavailable("This invitation link is not available.")
            draft = create_invited_draft_registration(
                option.link,
                preferred_language=locked_person.preferred_language,
                person_id=locked_person.pk,
                correlation_id=correlation_id,
            ).registration
        PersistentAuditRecorder().record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=locked_person.pk,
                action_code=action_codes.REGISTRATION_RESTARTED_AFTER_WITHDRAWAL,
                target_type="Registration",
                target_uuid=locked.pk,
                event_edition_id=locked.event_edition_id,
                result="SUCCESS",
                after_summary={
                    "new_registration": draft.public_reference,
                    "origin": locked.source_kind,
                },
                correlation_id=correlation_id,
            )
        )
    return draft


def advance_current_step(registration: Registration, step: str) -> None:
    """Persist the wizard's forward progress so "Continue" always resumes correctly
    (Prompt 4 final closure pass §5). A no-op past a step the registration has
    already reached, so navigating backward to re-edit an earlier step never
    regresses `current_step`."""
    if step not in _STEP_ORDER:
        raise ValueError(f"Unknown wizard step: {step!r}")
    current_index = (
        _STEP_ORDER.index(registration.current_step)
        if registration.current_step in _STEP_ORDER
        else -1
    )
    if _STEP_ORDER.index(step) <= current_index:
        return
    # UX-C2 (C): one conditional UPDATE, so a stale request never moves the
    # step of a record that is no longer a DRAFT. A concurrent submission
    # holding the row lock makes it wait, and PostgreSQL then re-evaluates the
    # condition on the committed row.
    updated = Registration.objects.filter(
        pk=registration.pk, public_status=RegistrationPublicStatus.DRAFT
    ).update(current_step=step)
    if updated:
        registration.current_step = step


class RegistrationNotDraft(ValueError):
    """The registration is no longer a DRAFT, so a wizard step may not change it."""


def _require_draft(registration: Registration) -> None:
    if registration.public_status != RegistrationPublicStatus.DRAFT:
        raise RegistrationNotDraft(
            "Only a DRAFT registration may be edited through the wizard steps."
        )


def _lock_draft(registration: Registration) -> Registration:
    """Lock the Registration row and require its AUTHORITATIVE status to be DRAFT.

    MUST run inside the caller's transaction. The caller's instance may have
    been loaded before a concurrent submission committed; the locked row is
    the committed truth (UX-C1, UX-F05). Every writer of sensitive
    accommodation data takes this lock FIRST, as `submit_full_registration`
    does, so the lock order is always Registration, then AccommodationRequest.
    """
    locked = Registration.objects.select_for_update().get(pk=registration.pk)
    _require_draft(locked)
    return locked


def _require_selectable_country(code_id: str | None, *, what: str) -> None:
    """The confirmed selectable-country policy (M07): active and not excluded.

    The same queryset the forms offer, so a direct caller or a stale draft
    cannot store or submit a country the lists do not offer. It decides no
    recognition question beyond the confirmed IL/XK exclusion (C-01 OPEN).
    """
    from apps.core.models import selectable_countries

    if not code_id or not selectable_countries().filter(pk=code_id).exists():
        raise ValidationError(f"The {what} is not an available country.")


def _flag_duplicate_candidate(registration: Registration, has_conflict: bool) -> None:
    if has_conflict and registration.internal_status != RegistrationInternalStatus.DUPLICATE_REVIEW:
        registration.internal_status = RegistrationInternalStatus.DUPLICATE_REVIEW
        registration.save(update_fields=["internal_status", "updated_at"])


def _validate_past_date(value: date | None, *, field_label: str) -> None:
    if value is None or value >= timezone.now().date():
        raise ValidationError(f"{field_label} must be a date in the past.")


def _validate_future_date(value: date | None, *, field_label: str) -> None:
    if value is None or value <= timezone.now().date():
        raise ValidationError(f"{field_label} must be a date in the future.")


MAX_PARTICIPANT_AGE = 120


def age_problem(date_of_birth: date | None, event_edition: EventEdition) -> str | None:
    """UX-2 (M12, D-09): why `date_of_birth` is not eligible, or None.

    The reference date is the local calendar date of `starts_at` in the
    event's timezone; "today" is also taken in that timezone. Someone born on
    29 February reaches that birthday on 1 March in other years (tuple
    comparison). Never inferred from the interface language.
    """
    from zoneinfo import ZoneInfo

    if date_of_birth is None:
        return "missing"
    if event_edition.starts_at is None or not event_edition.timezone:
        return "no_event_date"  # fail closed, never "no check"
    zone = ZoneInfo(event_edition.timezone)
    today = timezone.now().astimezone(zone).date()
    if date_of_birth >= today:
        return "future"
    reference = event_edition.starts_at.astimezone(zone).date()
    age = (
        reference.year
        - date_of_birth.year
        - ((reference.month, reference.day) < (date_of_birth.month, date_of_birth.day))
    )
    if age < event_edition.minimum_participant_age:
        return "too_young"
    if age > MAX_PARTICIPANT_AGE:
        return "too_old"
    return None


def _validate_nin_format(nin_value: str) -> None:
    if len(nin_value) != 18 or not nin_value.isascii() or not nin_value.isdigit():
        raise ValidationError("The national identity number must be exactly 18 digits.")


@transaction.atomic
def save_identity_step(
    *,
    registration: Registration,
    given_names: str,
    family_name: str,
    date_of_birth,
    nationality_code_id: str,
    country_of_residence_id: str,
    identity_path: str,
    nin_value: str = "",
    passport_number: str = "",
    passport_country_code_id: str = "",
    passport_expires_at=None,
    passport_identity_page_file=None,
    exemption_document_kind: str = "",
    exemption_document_number: str = "",
    exemption_document_expires_at=None,
    exemption_document_file=None,
) -> None:
    """Save identity facts and the identity path (AF-REG-03/AF-REG-04).

    IDV-2 (amendment A-13, A13-14): no identity provider is called here. The
    NIN is only stored, as DECLARED; verification starts after the final
    submission (`apps.people.services.identity_verification`). IDV-3 (A13-01,
    A13-02): the NIN path is for Algerian nationals and the passport path for
    foreign nationals only; the passport identity page is accepted on every
    passport path. Re-declaring an unchanged NIN/passport reuses the existing
    `IdentityIdentifier` row rather than creating another one (Prompt 4 final
    closure pass §2).

    Owner decision IDV-Q3: `identity_path="NIN_EXEMPTION"` is the documentary
    route of an Algerian national without a usable NIN. It is accepted only
    while staff hold an ACTIVE exemption for THIS registration (checked under
    the lock, so a stale form after a revocation is refused), and takes an
    Algerian national identity card or passport, its number, expiry date and
    photo (`apps.people.services.nin_exemption.declare_exemption_document`).
    No NIN is stored for it. Saving any other path takes the draft off the
    documentary route.
    """
    from apps.core.text_rules import TextRuleError, normalize_latin_name, normalize_nin
    from apps.people.models import IdentifierStatus, IdentifierType
    from apps.people.selectors import identifiers_for_value, unchanged_identifier_for_person
    from apps.people.services import create_identity_identifier
    from apps.registrations.models import RegistrationProfile

    # UX-C2 (C): the Registration row is locked first and its committed status
    # must still be DRAFT, so a stale request never changes a submitted record.
    registration = _lock_draft(registration)
    _validate_past_date(date_of_birth, field_label="Date of birth")
    # UX-C1 (UX-F03): the country lists' policy holds for any caller.
    _require_selectable_country(nationality_code_id, what="nationality")
    _require_selectable_country(country_of_residence_id, what="country of residence")
    # UX-2 (M11, M12, M03): the same rules as the form, for any caller.
    try:
        given_names = normalize_latin_name(given_names)
        family_name = normalize_latin_name(family_name)
    except TextRuleError:
        raise ValidationError("Names must use Latin letters.") from None
    if age_problem(date_of_birth, registration.event_edition) is not None:
        raise ValidationError("The date of birth does not meet the event age rule.")
    if identity_path == "NIN":
        try:
            nin_value = normalize_nin(nin_value)
        except TextRuleError:
            raise ValidationError("A NIN has exactly 18 digits.") from None
    if identity_path == "PASSPORT":
        passport_number = "".join((passport_number or "").split()).upper()
    if identity_path == "NIN":
        if nationality_code_id != ALGERIA_COUNTRY_CODE:
            raise ValidationError(
                "The national identity number path is only available for Algerian nationals."
            )
        _validate_nin_format(nin_value)
        if passport_identity_page_file is not None:
            # Never requested on the NIN path (Prompt 5 correction pass §2) --
            # fail-closed defence in depth against a caller bypassing the form.
            raise ValidationError(
                "The passport identity page is only accepted on the passport path."
            )
    elif identity_path == "PASSPORT":
        if nationality_code_id == ALGERIA_COUNTRY_CODE:
            # A13-01 (SCOPE-01): no self-service passport route for Algerian
            # nationals; only the staff-assisted exception exists.
            raise ValidationError("Algerian nationals use the national identity number path.")
        _validate_future_date(passport_expires_at, field_label="Passport expiry date")
        _require_selectable_country(passport_country_code_id, what="passport issuing country")
    elif identity_path == "NIN_EXEMPTION":
        # IDV-Q3: Algerian nationals only, and only with a staff grant (the
        # grant itself is checked, locked, by `declare_exemption_document`).
        if nationality_code_id != ALGERIA_COUNTRY_CODE:
            raise ValidationError("The NIN exemption route is only for Algerian nationals.")
        if passport_identity_page_file is not None:
            raise ValidationError(
                "The passport identity page is only accepted on the passport path."
            )
    else:
        raise ValueError(f"Unknown identity_path: {identity_path!r}")

    full_name = f"{given_names} {family_name}".strip()
    RegistrationProfile.objects.update_or_create(
        registration=registration,
        defaults={
            "submitted_given_names": given_names,
            "submitted_family_name": family_name,
            "submitted_full_name": full_name,
            "date_of_birth": date_of_birth,
            "nationality_code_id": nationality_code_id,
            "country_of_residence_id": country_of_residence_id,
        },
    )

    person = registration.person
    from apps.people.services.nin_exemption import (
        declare_exemption_document,
        release_exemption_document,
    )

    if identity_path == "NIN_EXEMPTION":
        declare_exemption_document(
            registration=registration,
            person=person,
            document_kind=exemption_document_kind,
            document_number=exemption_document_number,
            document_expires_at=exemption_document_expires_at,
            document_file=exemption_document_file,
        )
        return
    release_exemption_document(registration)
    if identity_path == "NIN":
        existing_identifier = unchanged_identifier_for_person(
            person,
            identifier_type=IdentifierType.NIN,
            country_code_id=nationality_code_id,
            raw_value=nin_value,
        )
        if existing_identifier is not None:
            identifier = existing_identifier
        else:
            conflict = (
                identifiers_for_value(
                    identifier_type=IdentifierType.NIN,
                    country_code_id=nationality_code_id,
                    raw_value=nin_value,
                )
                .filter(status=IdentifierStatus.VERIFIED)
                .exclude(person_id=person.id)
                .exists()
            )
            identifier = create_identity_identifier(
                person=person,
                identifier_type=IdentifierType.NIN,
                country_code_id=nationality_code_id,
                raw_value=nin_value,
                status=IdentifierStatus.DECLARED,
            )
            _flag_duplicate_candidate(registration, conflict)
    elif identity_path == "PASSPORT":
        from apps.people.services.identity_verification import identifier_is_bound

        existing_identifier = unchanged_identifier_for_person(
            person,
            identifier_type=IdentifierType.PASSPORT,
            country_code_id=passport_country_code_id,
            raw_value=passport_number,
        )
        if (
            existing_identifier is not None
            and existing_identifier.expires_at != passport_expires_at
            and identifier_is_bound(existing_identifier)
        ):
            # IDV-Q-C1: another expiry for a passport a submitted case uses is
            # a new declaration for this draft; the checked one is untouched.
            existing_identifier = None
        if existing_identifier is not None:
            identifier = existing_identifier
        else:
            conflict = (
                identifiers_for_value(
                    identifier_type=IdentifierType.PASSPORT,
                    country_code_id=passport_country_code_id,
                    raw_value=passport_number,
                )
                .filter(status=IdentifierStatus.VERIFIED)
                .exclude(person_id=person.id)
                .exists()
            )
            identifier = create_identity_identifier(
                person=person,
                identifier_type=IdentifierType.PASSPORT,
                country_code_id=passport_country_code_id,
                raw_value=passport_number,
                status=IdentifierStatus.DECLARED,
            )
            _flag_duplicate_candidate(registration, conflict)
        if identifier.expires_at != passport_expires_at:
            identifier.expires_at = passport_expires_at
            identifier.save(update_fields=["expires_at", "updated_at"])
        if passport_identity_page_file is not None:
            from apps.documents.services import save_passport_identity_page

            save_passport_identity_page(
                registration=registration,
                person=person,
                uploaded_file=passport_identity_page_file,
            )


@transaction.atomic
def save_contact_step(
    *, registration: Registration, mobile_number: str, mobile_country_code_id: str
) -> None:
    """Save the optional/conditional mobile contact (Schema §4.5, TRD phone rule).

    Validates and normalizes with `phonenumbers` to E.164 (Prompt 4 final
    closure pass §3). Never assigns a self-declared, unverified contact to
    `verified_mobile_contact` -- it goes to `declared_mobile_contact`
    instead. Re-saving an unchanged number reuses the existing
    `ContactPoint` row.
    """
    from apps.people.models import ContactPointType
    from apps.people.selectors import contact_points_for_value, unchanged_contact_point_for_person
    from apps.people.services import create_contact_point
    from apps.registrations.models import RegistrationProfile

    # UX-C2 (C): the Registration row is locked first and its committed status
    # must still be DRAFT, so a stale request never changes a submitted record.
    registration = _lock_draft(registration)
    if not mobile_number:
        RegistrationProfile.objects.filter(registration=registration).update(
            declared_mobile_contact=None
        )
        return

    from apps.people.services import parse_mobile_number

    # UX-2 (S-10): the stored region is the region of the parsed number, so a
    # pasted international number is never filed under the selected country.
    e164_value, mobile_country_code_id = parse_mobile_number(mobile_number, mobile_country_code_id)
    # UX-C1 (UX-F03): a pasted number decides the region, and that region
    # must be one the calling-code list offers -- as the form requires.
    from apps.core.models import selectable_countries

    if not selectable_countries().filter(pk=mobile_country_code_id).exists():
        from apps.people.services import PhoneValidationError

        raise PhoneValidationError("This country is not available in the list.")
    person = registration.person
    conflict = (
        contact_points_for_value(contact_type=ContactPointType.MOBILE, raw_value=e164_value)
        .filter(is_verified=True)
        .exclude(person_id=person.id)
        .exists()
    )
    existing = unchanged_contact_point_for_person(
        person, contact_type=ContactPointType.MOBILE, raw_value=e164_value
    )
    if existing is not None:
        contact = existing
    else:
        contact = create_contact_point(
            person=person,
            contact_type=ContactPointType.MOBILE,
            raw_value=e164_value,
            country_code_id=mobile_country_code_id,
            is_verified=False,
            is_primary=True,
        )
        _flag_duplicate_candidate(registration, conflict)

    update_fields: dict[str, Any] = {"declared_mobile_contact": contact}
    if contact.is_verified:
        update_fields["verified_mobile_contact"] = contact
    RegistrationProfile.objects.filter(registration=registration).update(**update_fields)


#: Approved professional limits (D-06, D-08), shared by the form, the step
#: service and the submission guard (UX-C1, UX-F03).
ORGANIZATION_NAME_MAX_LENGTH = 200
JOB_TITLE_MAX_LENGTH = 120
DEPARTMENT_MAX_LENGTH = 120
BIOGRAPHY_MAX_LENGTH = 500


def _optional_latin_text(value: str, max_length: int) -> str:
    """L-TEXT for a provided value; an empty value stays empty."""
    from apps.core.text_rules import normalize_latin_text

    if not (value or "").strip():
        return ""
    return normalize_latin_text(value, max_length=max_length)


@transaction.atomic
def save_professional_step(
    *,
    registration: Registration,
    organization_name: str,
    organization_type: str = "",
    job_title: str = "",
    department: str = "",
    sector_code_id: str | None = None,
    country_code_id: str | None = None,
    organization_website: str = "",
    professional_profile_url: str = "",
    biography: str = "",
    profile_photo_file=None,
    operating_scope: str = "",
) -> None:
    """Save professional affiliation (V1 required fields) and, if provided, the
    profile photograph (Prompt 4 final closure pass §4). Matches (never
    overwrites) the master Organization.

    Every provided value meets the same rules as the form (UX-C1, UX-F03):
    Latin-script text and its limits, the approved organization types and
    operating scopes, an active sector and a selectable headquarters country.
    Requiredness stays with the submission guard, as before. The legacy
    `INSTITUTION` type is accepted only when this draft already holds it.
    """
    from apps.core.models import Sector
    from apps.core.text_rules import TextRuleError, normalize_optional_url
    from apps.documents.services import save_profile_photo
    from apps.organizations.models import (
        NEW_SELECTION_ORGANIZATION_TYPES,
        OperatingScope,
        OrganizationType,
        ProfessionalAffiliation,
    )
    from apps.organizations.services import match_or_create_organization

    # UX-C2 (C): the Registration row is locked first and its committed status
    # must still be DRAFT, so a stale request never changes a submitted record.
    registration = _lock_draft(registration)
    # UX-2 (S-11): optional links are canonicalized the same way for any caller.
    try:
        organization_website = normalize_optional_url(organization_website)
        professional_profile_url = normalize_optional_url(professional_profile_url)
    except TextRuleError:
        raise ValidationError("Enter a valid web address.") from None
    try:
        organization_name = _optional_latin_text(organization_name, ORGANIZATION_NAME_MAX_LENGTH)
        job_title = _optional_latin_text(job_title, JOB_TITLE_MAX_LENGTH)
        department = _optional_latin_text(department, DEPARTMENT_MAX_LENGTH)
    except TextRuleError:
        raise ValidationError("Use Latin letters within the field limit.") from None
    biography = normalize_long_text(biography)
    if len(biography) > BIOGRAPHY_MAX_LENGTH:
        raise ValidationError("The biography is too long.")
    if organization_type and organization_type not in NEW_SELECTION_ORGANIZATION_TYPES:
        held = (
            ProfessionalAffiliation.objects.filter(registration=registration)
            .values_list("organization_type", flat=True)
            .first()
        )
        if organization_type != held or organization_type not in OrganizationType.values:
            raise ValidationError("Select a valid organization type.")
    if operating_scope and operating_scope not in OperatingScope.values:
        raise ValidationError("Select a valid operating scope.")
    if sector_code_id and not Sector.objects.filter(pk=sector_code_id, is_active=True).exists():
        raise ValidationError("Select a valid sector.")
    if country_code_id:
        _require_selectable_country(country_code_id, what="headquarters country")
    organization = None
    if organization_name:
        organization = match_or_create_organization(
            organization_name, country_code_id=country_code_id
        )
    ProfessionalAffiliation.objects.update_or_create(
        registration=registration,
        defaults={
            "organization": organization,
            "submitted_organization_name": organization_name,
            "organization_type": organization_type,
            "job_title": job_title,
            "department": department,
            "sector_id": sector_code_id or None,
            "country_code_id": country_code_id or None,
            "organization_website": organization_website,
            "professional_profile_url": professional_profile_url,
            "biography": biography,
            "operating_scope": operating_scope,
        },
    )
    if profile_photo_file is not None:
        save_profile_photo(
            registration=registration, person=registration.person, uploaded_file=profile_photo_file
        )


@transaction.atomic
def save_interests_step(
    *,
    registration: Registration,
    interest_topic_ids: list,
    objectives_text: str,
) -> None:
    """Save interests and objectives. Never auto-assigns a Badge Type.

    The legacy free-text `accessibility_needs_text` column is no longer
    written (UX-3): accommodation needs use `save_accommodation_request`.
    Values saved before UX-3 are preserved, never rewritten or purged here.
    """
    from apps.registrations.models import RegistrationInterest, RegistrationProfile

    # UX-C2 (C): the Registration row is locked first and its committed status
    # must still be DRAFT, so a stale request never changes a submitted record.
    registration = _lock_draft(registration)
    # UXR-C1 (UXR-F06): every rule is checked before anything changes, so an
    # invalid request is refused as a whole, never saved as a subset.
    topics = _event_interest_topics(registration, interest_topic_ids)
    objectives_text = _objectives_or_error(objectives_text)
    RegistrationProfile.objects.filter(registration=registration).update(
        objectives_text=objectives_text
    )
    RegistrationInterest.objects.filter(registration=registration).delete()
    for topic in topics:
        RegistrationInterest.objects.create(registration=registration, interest_topic=topic)


#: The approved objectives limit (the form's existing 2000), counted after
#: `normalize_long_text`, shared by the form, the step service and the guard.
OBJECTIVES_MAX_LENGTH = 2000


def _event_interest_topics(registration: Registration, interest_topic_ids) -> list:
    """The requested topics, each existing, active and of THIS registration's
    event (UXR-F06), or `ValidationError` for the whole request."""
    from apps.registrations.models import InterestTopic

    requested = {str(topic_id) for topic_id in interest_topic_ids or []}
    if not requested:
        return []
    try:
        topics = list(
            InterestTopic.objects.filter(
                id__in=requested,
                is_active=True,
                event_edition_id=registration.event_edition_id,
            ).order_by("group_code", "code")
        )
    except ValidationError, ValueError:  # a malformed id
        raise ValidationError("Select valid interest topics.") from None
    if len(topics) != len(requested):
        raise ValidationError("Select valid interest topics.")
    return topics


def _objectives_or_error(objectives_text: str) -> str:
    text = normalize_long_text(objectives_text)
    if len(text) > OBJECTIVES_MAX_LENGTH:
        raise ValidationError("The objectives text is too long.")
    return text


@transaction.atomic
def save_interests_and_accommodation_step(
    *,
    registration: Registration,
    interest_topic_ids: list,
    objectives_text: str,
    accommodation_answer: str,
    accommodation_categories: list[str],
    accommodation_note: str,
) -> None:
    """The interests step POST as ONE transaction (UX-C2, C).

    The Registration is locked first, so a stale request either applies both
    parts to a DRAFT or neither: never new interests with an unchanged
    accommodation answer, and never either part on a submitted record.
    """
    locked = _lock_draft(registration)
    save_interests_step(
        registration=locked, interest_topic_ids=interest_topic_ids, objectives_text=objectives_text
    )
    save_accommodation_request(
        registration=locked,
        answer=accommodation_answer,
        categories=accommodation_categories,
        note=accommodation_note,
    )


# ---------------------------------------------------------------------------
# UX-3 (M21, D-10, S-18): voluntary accommodation request
# ---------------------------------------------------------------------------

ACCOMMODATION_NOTE_MAX_LENGTH = 300


class AccommodationValidationError(ValueError):
    """An accommodation answer, category or note outside the approved contract."""


def normalize_long_text(value: str) -> str:
    """CR LF and CR become LF, then NFC, then trim (S-12). Counting code points
    of this value gives the same number in the browser counter and here."""
    import unicodedata

    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text).strip()


@transaction.atomic
def save_accommodation_request(
    *, registration: Registration, answer: str, categories: list[str], note: str
) -> None:
    """Save, replace or clear the draft's accommodation request.

    The question is voluntary: an empty answer removes any saved request,
    and declining carries no consequence anywhere. Only a YES answer keeps
    categories and a note; NO and PREFER_NOT_TO_SAY keep the answer alone.
    Nothing here is audited with its content, logged or copied elsewhere.

    Serialized with submission and withdrawal (UX-C1, UX-F05): the
    Registration row is locked first and its committed status must still be
    DRAFT, so a request carrying a stale DRAFT instance can neither change a
    submitted request nor reset a withdrawal.
    """
    from apps.registrations.models import (
        AccommodationAnswer,
        AccommodationCategory,
        AccommodationRequest,
    )

    registration = _lock_draft(registration)
    if not answer:
        AccommodationRequest.objects.filter(registration=registration).delete()
        return
    if answer not in AccommodationAnswer.values:
        raise AccommodationValidationError("Unknown accommodation answer.")
    allowed = list(AccommodationCategory.values)
    chosen = [code for code in allowed if code in set(categories or [])]
    if len(chosen) != len(set(categories or [])):
        raise AccommodationValidationError("Unknown accommodation category.")
    note = normalize_long_text(note)
    if len(note) > ACCOMMODATION_NOTE_MAX_LENGTH:
        raise AccommodationValidationError("The note is too long.")
    if answer != AccommodationAnswer.YES:
        chosen, note = [], ""
    AccommodationRequest.objects.update_or_create(
        registration=registration,
        defaults={
            "answer": answer,
            "categories": chosen,
            "note_encrypted": note,
            "withdrawn_at": None,
        },
    )


def accommodation_request_for(registration: Registration):
    """The registration's own accommodation request, for the participant's
    own wizard and review pages only. Staff read through the scoped selector."""
    from apps.registrations.models import AccommodationRequest

    return AccommodationRequest.objects.filter(registration=registration).first()


@transaction.atomic
def withdraw_accommodation_consent(*, registration: Registration, person, correlation_id: str = ""):
    """Withdraw the explicit consent for accommodation data (D-11, UX-3).

    Records a WITHDRAWN consent event and stops all use of the data: the
    categories and note are cleared at once, unless the registration is
    under an active legal hold, in which case they are kept but hidden from
    every view and the hold's own process governs them. The answer itself is
    kept as evidence that a request once existed. Returns True when anything
    changed. Other processing of the registration is unaffected: it does not
    rest on this consent.
    """
    from apps.privacy.models import AcceptanceSource, ConsentAction, ConsentPurpose, ConsentRecord
    from apps.privacy.selectors import is_under_legal_hold
    from apps.registrations.models import AccommodationRequest

    # UX-C1 (UX-F05): the Registration row first, then the request row --
    # the lock order of `save_accommodation_request` and
    # `submit_full_registration`, so the three never interleave or deadlock.
    registration = Registration.objects.select_for_update().get(pk=registration.pk)
    request = (
        AccommodationRequest.objects.select_for_update()
        .filter(registration=registration, withdrawn_at__isnull=True)
        .first()
    )
    if request is None or registration.person_id != getattr(person, "pk", None):
        return False
    now = timezone.now()
    purpose = ConsentPurpose.objects.filter(code=SENSITIVE_ACCOMMODATION_PURPOSE_CODE).first()
    if purpose is not None:
        ConsentRecord.objects.create(
            person=person,
            purpose=purpose,
            action=ConsentAction.WITHDRAWN,
            source=AcceptanceSource.PUBLIC_WEB,
            occurred_at=now,
        )
    request.withdrawn_at = now
    fields = ["withdrawn_at", "updated_at"]
    if not is_under_legal_hold(registration):
        request.categories = []
        request.note_encrypted = ""
        fields += ["categories", "note_encrypted"]
    request.save(update_fields=fields)
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="PARTICIPANT",
            actor_person_id=person.pk,
            action_code=action_codes.ACCOMMODATION_CONSENT_WITHDRAWN,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            result="SUCCESS",
            correlation_id=correlation_id,
        )
    )
    return True


#: Consent purposes seeded by `privacy.0004` (UX-3, D-11).
DATA_PROCESSING_PURPOSE_CODE = "PERSONAL_DATA_PROCESSING"
SENSITIVE_ACCOMMODATION_PURPOSE_CODE = "SENSITIVE_ACCOMMODATION_DATA"


class SensitiveConsentRequired(Exception):
    """Accommodation data is present but its explicit consent was not given."""


class ProcessingConsentRequired(Exception):
    """The required explicit consent to personal-data processing (D-11) was
    not affirmatively given, or was given for a purpose other than the active
    `PERSONAL_DATA_PROCESSING` purpose."""


def _resolve_processing_purpose(candidate):
    """The active processing-consent purpose, resolved on the server (UX-F02).

    Missing or inactive: the notices step is unavailable, exactly like a
    missing legal version. A caller-supplied purpose must be that one row.
    """
    from apps.privacy.models import ConsentPurpose

    purpose = ConsentPurpose.objects.filter(
        code=DATA_PROCESSING_PURPOSE_CODE, is_active=True
    ).first()
    if purpose is None:
        raise IncompleteRegistrationError("notices")
    if candidate is not None and candidate.pk != purpose.pk:
        raise ProcessingConsentRequired("The consent does not name the processing purpose.")
    return purpose


# ---------------------------------------------------------------------------
# Submission completeness guard (Prompt 4 final closure pass §1 -- BLOCKER)
# ---------------------------------------------------------------------------


class IncompleteRegistrationError(Exception):
    """Raised when a Draft is missing a required field at submission time.

    Identifies only the EARLIEST incomplete wizard step, never the specific
    field or the value that failed -- the view shows one generic localized
    message and redirects there, without exposing anything sensitive about
    why (Prompt 4 final closure pass §1).
    """

    def __init__(self, step: str) -> None:
        self.step = step
        super().__init__(f"registration_incomplete:{step}")


def _require_valid_active_account(registration: Registration) -> None:
    from apps.people.models import ContactPointStatus, ContactPointType, ParticipantAccountStatus

    person = registration.person
    if person is None:
        raise IncompleteRegistrationError("account")
    account = getattr(person, "participant_account", None)
    if account is None or account.status != ParticipantAccountStatus.ACTIVE:
        raise IncompleteRegistrationError("account")
    has_verified_login_email = person.contact_points.filter(
        type=ContactPointType.EMAIL,
        status=ContactPointStatus.ACTIVE,
        is_verified=True,
        login_enabled=True,
    ).exists()
    if not has_verified_login_email:
        raise IncompleteRegistrationError("account")


def _fetch_profile(registration: Registration):
    """Query `RegistrationProfile` fresh rather than trusting `registration.profile`.

    The reverse OneToOne accessor can be silently cached onto `registration`
    as a side effect of an earlier `update_or_create()` in the SAME step
    (e.g. `save_identity_step`), and a later wizard step updates the row
    with a bulk `.update()` that never refreshes that cache -- a caller
    that reuses one `Registration` instance across several service calls
    would otherwise see a stale profile here. Always querying fresh makes
    the completeness guard correct regardless of caller instance-reuse
    pattern (Prompt 4 final closure pass §1).
    """
    from apps.registrations.models import RegistrationProfile

    return RegistrationProfile.objects.filter(registration=registration).first()


def _fetch_professional_affiliation(registration: Registration):
    """Same reasoning as `_fetch_profile`, for `ProfessionalAffiliation`."""
    from apps.organizations.models import ProfessionalAffiliation

    return ProfessionalAffiliation.objects.filter(registration=registration).first()


def _require_complete_identity(registration: Registration) -> None:
    from apps.people.models import IdentifierStatus, IdentifierType

    profile = _fetch_profile(registration)
    if profile is None:
        raise IncompleteRegistrationError("identity")
    if not profile.submitted_given_names.strip() or not profile.submitted_family_name.strip():
        raise IncompleteRegistrationError("identity")
    if profile.date_of_birth is None or profile.date_of_birth >= timezone.now().date():
        raise IncompleteRegistrationError("identity")
    if not profile.nationality_code_id or not profile.country_of_residence_id:
        raise IncompleteRegistrationError("identity")
    # UX-2: a draft saved before the rules changed cannot bypass them at
    # submission (names in Latin letters, event age rule). Submitted records
    # are never re-checked or rewritten.
    from apps.core.text_rules import TextRuleError, normalize_latin_name

    try:
        normalize_latin_name(profile.submitted_given_names)
        normalize_latin_name(profile.submitted_family_name)
    except TextRuleError:
        raise IncompleteRegistrationError("identity") from None
    if age_problem(profile.date_of_birth, registration.event_edition) is not None:
        raise IncompleteRegistrationError("identity")
    # UX-C1 (UX-F03): nor the country lists' policy.
    if not _is_selectable_country(profile.nationality_code_id) or not _is_selectable_country(
        profile.country_of_residence_id
    ):
        raise IncompleteRegistrationError("identity")

    person = registration.person
    if profile.nationality_code_id == ALGERIA_COUNTRY_CODE:
        # IDV-Q3: a draft on the staff-granted documentary route is complete
        # with its declared Algerian document and a reviewable image of it,
        # never with a NIN. The grant must still be ACTIVE (the caller holds
        # the Registration lock, which a revocation also takes).
        from apps.people.services.nin_exemption import (
            active_exemption,
            exemption_document_complete,
        )

        exemption = active_exemption(registration.pk)
        if exemption is not None and exemption.document_identifier_id is not None:
            if not exemption_document_complete(registration, exemption):
                raise IncompleteRegistrationError("identity")
            return
        has_nin = (
            person.identity_identifiers.filter(identifier_type=IdentifierType.NIN)
            .exclude(status=IdentifierStatus.REPLACED)
            .exists()
        )
        if not has_nin:
            raise IncompleteRegistrationError("identity")
    else:
        passport = (
            person.identity_identifiers.filter(identifier_type=IdentifierType.PASSPORT)
            .exclude(status=IdentifierStatus.REPLACED)
            .order_by("-created_at")
            .first()
        )
        if (
            passport is None
            or passport.expires_at is None
            or passport.expires_at <= timezone.now().date()
            or not _is_selectable_country(passport.country_code_id)
        ):
            raise IncompleteRegistrationError("identity")
        # IDV-3 (A13-02, DOC-01): every foreign participant's identity is
        # reviewed manually from the passport identity page, so a clean one is
        # required for every new submission. Submitted records are not forced.
        from apps.documents.services import active_passport_identity_page

        if active_passport_identity_page(registration) is None:
            raise IncompleteRegistrationError("identity")


def _is_selectable_country(code_id: str | None) -> bool:
    try:
        _require_selectable_country(code_id, what="country")
    except ValidationError:
        return False
    return True


def _mobile_region(contact) -> str:
    """The region of a stored mobile contact, derived from its stored value
    (UX-C2, B), or "" when the value is not an acceptable mobile number.

    The stored E.164 value goes through the authoritative parser with its
    approved validity and type rules (MOBILE or FIXED_LINE_OR_MOBILE). The
    stored `country_code_id` is only a hint for shared calling codes: it is
    kept only when the number is genuinely valid for that region, exactly as
    at entry. Missing or inconsistent metadata is never trusted and never
    rewritten here.
    """
    from apps.people.services import PhoneValidationError, parse_mobile_number

    value = contact.value_encrypted or ""
    if not value.startswith("+"):
        return ""  # a stored value is always E.164; anything else is inconsistent
    try:
        _e164, region = parse_mobile_number(value, contact.country_code_id or "")
    except PhoneValidationError:
        return ""
    return region


def _require_complete_contact(registration: Registration) -> None:
    profile = _fetch_profile(registration)
    if profile is None:
        raise IncompleteRegistrationError("contact")
    # UX-2 (M13, D-01): a mobile number is required for every new
    # registration, whatever the nationality and the channel. This supersedes
    # the earlier Algeria-only rule (amendment A-01).
    if profile.declared_mobile_contact_id is None and profile.verified_mobile_contact_id is None:
        raise IncompleteRegistrationError("contact")
    # UX-C1 (UX-F03): the number's region must be one the list offers, also
    # for a draft saved before that rule or by a direct caller.
    contact = profile.declared_mobile_contact or profile.verified_mobile_contact
    if not _is_selectable_country(_mobile_region(contact)):
        raise IncompleteRegistrationError("contact")


def _require_complete_professional(registration: Registration) -> None:
    from apps.core.models import Sector
    from apps.core.text_rules import TextRuleError, normalize_latin_text, normalize_optional_url
    from apps.documents.services import active_profile_photo
    from apps.organizations.models import OperatingScope, OrganizationType

    affiliation = _fetch_professional_affiliation(registration)
    if affiliation is None:
        raise IncompleteRegistrationError("professional")
    # UX-2 (M17-M19, D-07, D-08): website, profile link, department and
    # biography are optional; the operating scope is required.
    required_text_fields = (
        affiliation.submitted_organization_name,
        affiliation.organization_type,
        affiliation.job_title,
        affiliation.operating_scope,
    )
    if not all(field.strip() for field in required_text_fields):
        raise IncompleteRegistrationError("professional")
    if affiliation.sector_id is None or affiliation.country_code_id is None:
        raise IncompleteRegistrationError("professional")
    # UX-C1 (UX-F03): the approved rules, also for a draft saved before them
    # or by a direct caller. Submitted records are never re-checked. Any known
    # type is accepted, so a draft already holding the legacy INSTITUTION
    # type can still be submitted; new input cannot choose it.
    try:
        normalize_latin_text(
            affiliation.submitted_organization_name, max_length=ORGANIZATION_NAME_MAX_LENGTH
        )
        normalize_latin_text(affiliation.job_title, max_length=JOB_TITLE_MAX_LENGTH)
        _optional_latin_text(affiliation.department, DEPARTMENT_MAX_LENGTH)
        normalize_optional_url(affiliation.organization_website)
        normalize_optional_url(affiliation.professional_profile_url)
    except TextRuleError:
        raise IncompleteRegistrationError("professional") from None
    if (
        len(normalize_long_text(affiliation.biography)) > BIOGRAPHY_MAX_LENGTH
        or affiliation.organization_type not in OrganizationType.values
        or affiliation.operating_scope not in OperatingScope.values
        or not Sector.objects.filter(pk=affiliation.sector_id, is_active=True).exists()
        or not _is_selectable_country(affiliation.country_code_id)
    ):
        raise IncompleteRegistrationError("professional")
    if active_profile_photo(registration) is None:
        raise IncompleteRegistrationError("professional")


def _require_complete_interests(registration: Registration) -> None:
    interests = registration.interests.all()
    if not interests.exists():
        raise IncompleteRegistrationError("interests")
    # UXR-C1 (UXR-F06): a draft saved by a direct caller or before this rule
    # cannot submit another event's topic, a deactivated topic, or objectives
    # beyond the approved limit. Submitted records are never re-checked.
    if (
        interests.exclude(interest_topic__event_edition_id=registration.event_edition_id).exists()
        or interests.filter(interest_topic__is_active=False).exists()
    ):
        raise IncompleteRegistrationError("interests")
    profile = _fetch_profile(registration)
    if profile is None:
        raise IncompleteRegistrationError("interests")
    objectives = normalize_long_text(profile.objectives_text)
    if not objectives or len(objectives) > OBJECTIVES_MAX_LENGTH:
        raise IncompleteRegistrationError("interests")


def _require_effective_legal_versions(privacy_notice_version, terms_version) -> None:
    now = timezone.now()
    for version, expected_code in (
        (privacy_notice_version, "PRIVACY_NOTICE"),
        (terms_version, "TERMS"),
    ):
        if version is None or version.status != LegalDocumentVersionStatus.PUBLISHED:
            raise IncompleteRegistrationError("notices")
        if version.legal_document.code != expected_code:
            raise IncompleteRegistrationError("notices")
        if version.effective_from > now:
            raise IncompleteRegistrationError("notices")
        if version.effective_until is not None and version.effective_until <= now:
            raise IncompleteRegistrationError("notices")


def require_complete_registration(
    registration: Registration, *, privacy_notice_version, terms_version
) -> None:
    """Reject submission of an incomplete Draft, identifying the earliest incomplete step.

    Called from `submit_full_registration` BEFORE any acceptance record,
    consent record, submission snapshot, submission row, audit success
    event, or confirmation message is created (Prompt 4 final closure pass
    §1 -- BLOCKER). Exposed as its own function so it also protects a
    caller that bypasses the wizard views entirely and calls the service
    layer directly.
    """
    _require_valid_active_account(registration)
    _require_complete_identity(registration)
    _require_complete_contact(registration)
    _require_complete_professional(registration)
    _require_complete_interests(registration)
    _require_effective_legal_versions(privacy_notice_version, terms_version)


def build_registration_snapshot(
    registration: Registration,
    *,
    privacy_notice_version=None,
    privacy_action: str | None = None,
    terms_version=None,
    terms_action: str | None = None,
    sensitive_support_consent_recorded: bool = False,
) -> dict[str, Any]:
    """Build the canonical immutable submission snapshot (Schema §5.8).

    Contains masked identity/mobile evidence only -- never a clear NIN,
    passport, or contact value, and never document bytes, storage
    credentials, or a private storage URL (Prompt 4 final closure pass §7).
    """
    from apps.documents.services import active_passport_identity_page, active_profile_photo
    from apps.people.models import IdentifierStatus

    profile = _fetch_profile(registration)
    affiliation = _fetch_professional_affiliation(registration)
    interests = list(
        registration.interests.select_related("interest_topic").values_list(
            "interest_topic__code", flat=True
        )
    )

    person = registration.person
    identity_evidence = []
    if person is not None:
        for identifier in person.identity_identifiers.exclude(
            status=IdentifierStatus.REPLACED
        ).order_by("identifier_type", "-created_at"):
            identity_evidence.append(
                {
                    "identifier_type": identifier.identifier_type,
                    "country_code": identifier.country_code_id,
                    "masked_value": identifier.masked_value,
                    "status": identifier.status,
                }
            )

    mobile_contact = None
    if profile is not None:
        mobile_contact = profile.declared_mobile_contact or profile.verified_mobile_contact
    mobile_evidence = (
        {"masked_value": mobile_contact.masked_value, "verified": mobile_contact.is_verified}
        if mobile_contact is not None
        else None
    )

    photo_document = active_profile_photo(registration)
    photo_evidence = (
        {
            "document_id": str(photo_document.pk),
            "scan_status": photo_document.stored_object.malware_scan_status,
        }
        if photo_document is not None
        else None
    )

    passport_page_document = active_passport_identity_page(registration)
    passport_page_evidence = (
        {
            "document_id": str(passport_page_document.pk),
            "scan_status": passport_page_document.stored_object.malware_scan_status,
        }
        if passport_page_document is not None
        else None
    )

    def _legal_snapshot(version, action):
        if version is None:
            return None
        return {
            "version_id": str(version.pk),
            "version_label": version.version_label,
            "language": version.language,
            "action": action,
        }

    return {
        "public_reference": registration.public_reference,
        "event_edition": registration.event_edition.code,
        "identity": {
            "given_names": profile.submitted_given_names if profile else "",
            "family_name": profile.submitted_family_name if profile else "",
            "date_of_birth": profile.date_of_birth.isoformat()
            if profile and profile.date_of_birth
            else None,
            "nationality_code": profile.nationality_code_id if profile else None,
            "country_of_residence_code": profile.country_of_residence_id if profile else None,
            "evidence": identity_evidence,
        },
        "mobile": mobile_evidence,
        "professional": {
            "organization_name": affiliation.submitted_organization_name if affiliation else "",
            "organization_type": affiliation.organization_type if affiliation else "",
            "job_title": affiliation.job_title if affiliation else "",
            "department": affiliation.department if affiliation else "",
            "sector_code": affiliation.sector_id if affiliation else None,
            "country_code": affiliation.country_code_id if affiliation else None,
            "organization_website": affiliation.organization_website if affiliation else "",
            "professional_profile_url": affiliation.professional_profile_url if affiliation else "",
            "biography": affiliation.biography if affiliation else "",
        },
        "profile_photo": photo_evidence,
        "passport_identity_page": passport_page_evidence,
        "interests": interests,
        "objectives_text": profile.objectives_text if profile else "",
        # UX-3 (S-18): the answer only. Categories and the note stay in the
        # restricted AccommodationRequest row and are never copied here; the
        # legacy free text is no longer copied into new snapshots.
        "accommodation": _accommodation_snapshot(
            registration, consent_recorded=sensitive_support_consent_recorded
        ),
        "preferred_language": registration.preferred_language,
        "legal_acceptance": {
            "privacy_notice": _legal_snapshot(privacy_notice_version, privacy_action),
            "terms": _legal_snapshot(terms_version, terms_action),
        },
    }


#: Snapshot key of the registration-scoped sensitive-support consent fact (UX-C2, G).
SENSITIVE_SUPPORT_CONSENT_KEY = "sensitive_support_consent_recorded"


def _accommodation_snapshot(registration: Registration, *, consent_recorded: bool) -> dict:
    """Two distinct facts (UX-C2, G): whether details were provided, and
    whether THIS submission recorded the explicit sensitive-support consent.
    The first is never read as the second for new submissions."""
    request = accommodation_request_for(registration)
    if request is None:
        return {
            "answer": None,
            "sensitive_data_provided": False,
            SENSITIVE_SUPPORT_CONSENT_KEY: False,
        }
    return {
        "answer": request.answer,
        "sensitive_data_provided": request.has_sensitive_data,
        SENSITIVE_SUPPORT_CONSENT_KEY: bool(consent_recorded),
    }


@transaction.atomic
def submit_full_registration(
    *,
    registration: Registration,
    privacy_notice_version,
    terms_version,
    marketing_consent_purpose=None,
    marketing_consent_granted: bool = False,
    data_processing_consent_granted: bool,
    data_processing_consent_purpose=None,
    sensitive_data_consent_granted: bool = False,
    session_reference: str,
    idempotency_key: str,
) -> RegistrationSubmission:
    """Record required legal acceptances, optional consent, and the submission (AF-REG-06).

    The explicit consent to personal-data processing (D-11) is required for
    every new submission (UX-C1, UX-F02): `data_processing_consent_granted`
    must be exactly True and the purpose is resolved here, never trusted
    from the caller; a supplied purpose must be the active one. Missing or
    wrong consent fails before any evidence is written. An already-completed
    submission is still returned unchanged to a retry.

    Repeated submissions -- a double-click, a client retry, or a fresh
    request racing a concurrent one, WHETHER OR NOT they supplied the same
    `idempotency_key` -- return the same result rather than creating a
    second acceptance trail, consent record, snapshot, submission, audit
    event, or outbox event (AF-REG-06: "Repeated clicks or retries MUST
    return the same successful result"; Prompt 6 P6-H-02 correction).
    Rejects an incomplete Draft BEFORE creating any such evidence (Prompt 4
    final closure pass §1 -- BLOCKER).

    The Registration row is locked FIRST, before the draft-state decision,
    the acceptance/consent checks, the snapshot, or the submission itself
    -- a concurrent call for the SAME Registration (even one that computed
    a different idempotency key, which is exactly the failure mode this
    correction closes) is fully serialized behind this lock, and the
    "does an INITIAL submission already exist for this Registration"
    recheck immediately after acquiring it is what makes the two calls
    converge on one result regardless of their keys.
    """
    from apps.privacy.models import (
        AcceptanceAction,
        AcceptanceRecord,
        AcceptanceSource,
        ConsentAction,
        ConsentRecord,
    )
    from apps.registrations.models import AccommodationAnswer

    locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)

    existing_initial = RegistrationSubmission.objects.filter(
        registration=locked_registration, submission_kind=RegistrationSubmissionKind.INITIAL
    ).first()
    if existing_initial is not None:
        return existing_initial

    _require_draft(locked_registration)
    # UX-4 (D-12): a closure that committed first wins, including against a
    # page that was opened before the closure. Checked before any evidence
    # is written; the draft itself is left untouched.
    require_channel_for_submission(locked_registration)
    require_complete_registration(
        locked_registration,
        privacy_notice_version=privacy_notice_version,
        terms_version=terms_version,
    )
    # UX-C1 (UX-F02): both consent decisions are made before any evidence is
    # written, not merely before the transaction commits.
    processing_purpose = _resolve_processing_purpose(data_processing_consent_purpose)
    if data_processing_consent_granted is not True:
        raise ProcessingConsentRequired("The explicit processing consent was not given.")
    # UX-3 (D-11): accommodation categories or a note need their own explicit
    # consent; nothing is recorded if it is missing. The draft keeps the data
    # so the participant can consent or remove it. The Registration lock above
    # also serializes every accommodation writer (UX-F05), so this read is
    # the committed state the snapshot will record.
    accommodation = accommodation_request_for(locked_registration)
    needs_sensitive_consent = accommodation is not None and accommodation.has_sensitive_data
    # UX-C2 (A): only the boolean True is a consent. False, None, strings,
    # integers (including 1) and anything else are refused when the consent is
    # required, and a non-boolean is refused in every case, before any effect.
    if type(sensitive_data_consent_granted) is not bool:
        raise SensitiveConsentRequired("The accommodation consent must be a boolean decision.")
    if needs_sensitive_consent and sensitive_data_consent_granted is not True:
        raise SensitiveConsentRequired("Accommodation data needs explicit consent.")
    # UX-C2 (G): a YES answer without details may carry a VOLUNTARY consent,
    # recorded like the required one. Any other answer records none.
    records_sensitive_consent = needs_sensitive_consent or (
        accommodation is not None
        and accommodation.withdrawn_at is None
        and accommodation.answer == AccommodationAnswer.YES
        and sensitive_data_consent_granted is True
    )
    sensitive_purpose = None
    if records_sensitive_consent:
        from apps.privacy.models import ConsentPurpose

        sensitive_purpose = ConsentPurpose.objects.filter(
            code=SENSITIVE_ACCOMMODATION_PURPOSE_CODE, is_active=True
        ).first()
        if sensitive_purpose is None:
            raise IncompleteRegistrationError("notices")
    person = locked_registration.person

    # Each acceptance record stores the LANGUAGE OF THE VERSION ACTUALLY
    # ACCEPTED, never the participant's ambient preferred language -- a
    # silent English fallback must never be recorded as if it were French
    # or Arabic (Prompt 4 final closure pass §7).
    already_accepted_privacy = AcceptanceRecord.objects.filter(
        registration=locked_registration, legal_document_version=privacy_notice_version
    ).exists()
    if not already_accepted_privacy:
        AcceptanceRecord.objects.create(
            person=person,
            registration=locked_registration,
            legal_document_version=privacy_notice_version,
            action=AcceptanceAction.ACKNOWLEDGED,
            accepted_at=timezone.now(),
            language=privacy_notice_version.language,
            source=AcceptanceSource.PUBLIC_WEB,
            session_reference=session_reference,
        )
    already_accepted_terms = AcceptanceRecord.objects.filter(
        registration=locked_registration, legal_document_version=terms_version
    ).exists()
    if not already_accepted_terms:
        AcceptanceRecord.objects.create(
            person=person,
            registration=locked_registration,
            legal_document_version=terms_version,
            action=AcceptanceAction.ACCEPTED,
            accepted_at=timezone.now(),
            language=terms_version.language,
            source=AcceptanceSource.PUBLIC_WEB,
            session_reference=session_reference,
        )
    ConsentRecord.objects.create(
        person=person,
        purpose=processing_purpose,
        action=ConsentAction.GRANTED,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
        legal_document_version=privacy_notice_version,
    )
    if records_sensitive_consent:
        ConsentRecord.objects.create(
            person=person,
            purpose=sensitive_purpose,
            action=ConsentAction.GRANTED,
            source=AcceptanceSource.PUBLIC_WEB,
            occurred_at=timezone.now(),
            legal_document_version=privacy_notice_version,
        )
    if marketing_consent_purpose is not None:
        ConsentRecord.objects.create(
            person=person,
            purpose=marketing_consent_purpose,
            action=ConsentAction.GRANTED if marketing_consent_granted else ConsentAction.WITHDRAWN,
            source=AcceptanceSource.PUBLIC_WEB,
            occurred_at=timezone.now(),
            legal_document_version=privacy_notice_version,
        )

    snapshot = build_registration_snapshot(
        locked_registration,
        privacy_notice_version=privacy_notice_version,
        privacy_action=AcceptanceAction.ACKNOWLEDGED,
        terms_version=terms_version,
        terms_action=AcceptanceAction.ACCEPTED,
        sensitive_support_consent_recorded=records_sensitive_consent,
    )

    # Invitation-campaign capacity is enforced HERE, at final submission,
    # never merely when the link was opened (Phase 2 Prompt 2). Only
    # reached once per Registration: the `existing_initial` short-circuit
    # above already returns before this point on every retry, so a
    # double-click or client retry never re-evaluates capacity twice.
    # Local import avoids a module-level circular import (`invitations`
    # imports THIS module for its own draft-creation services); this
    # branch is a pure no-op for every Phase 1/non-invitation Registration.
    if locked_registration.invitation_campaign_id is not None:
        from apps.invitations.services import reserve_and_record_campaign_submission

        reserve_and_record_campaign_submission(
            campaign_id=locked_registration.invitation_campaign_id,
            registration=locked_registration,
        )

    submission = record_registration_submission(
        registration=locked_registration,
        snapshot=snapshot,
        idempotency_key=idempotency_key,
    )
    # IDV-2 (A13-14): the identity verification and its durable job commit with
    # the submission, or not at all. No provider is called here.
    from apps.people.services.identity_verification import start_identity_verification

    start_identity_verification(registration=locked_registration)
    return submission
