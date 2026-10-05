"""The NIN exemption route (owner decision IDV-Q3, 2026-10-02; ADR-0026 decision 21).

An Algerian national who cannot supply a usable NIN cannot pass the NIN path
of the registration wizard. This module is the narrow, staff-authorized way
through, for ONE draft registration at a time:

1. Staff with `people.grant_nin_exemption` in the registration's exact scope
   grant an exemption for that draft, with a preset reason, a written
   explanation and a deliberate confirmation (`grant_nin_exemption`). It is
   audited, and it can be revoked while the registration is still a draft.
2. The participant's identity step then offers the documentary route: an
   Algerian national identity card or an Algerian passport, its number (the
   document's own number, never a NIN), the expiry date where the document
   has one, and a photo of it (`declare_exemption_document`). Nothing else
   changes in the wizard.
3. The final submission routes the identity to manual documentary review
   (`IdentityRoute.NIN_EXEMPTION`, reason `NIN_EXEMPTION_REVIEW`) and marks
   the exemption USED (`consume_for_submission`). No NIN is created or
   invented, no ministry job is scheduled, and the case is clearly labelled
   in the queue.
4. A reviewer verifies from the declared document only
   (`verify_identity_manually`, source `MANUAL_NIN_EXEMPTION`); the usual
   cross-person duplicate check applies to the document number.

It is not a passport bypass: nobody without a grant sees the route, a grant
binds one registration, the route accepts only Algerian documents of an
Algerian national, and the foreign-national passport route is unchanged.

Lock order: the Registration row first, then the exemption row (grant,
revoke, the identity step and the submission all follow it).
"""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.people.models import (
    IdentifierStatus,
    IdentifierType,
    NinExemption,
    NinExemptionReason,
    NinExemptionStatus,
)
from apps.people.policies import require_nin_exemption_permission
from apps.people.services.identity_review import (
    MAX_NOTE_LENGTH,
    MIN_EXPLANATION_LENGTH,
    IdentityStateError,
    InvalidIdentityInput,
    StaleIdentityVersion,
)

ALGERIA_COUNTRY_CODE = "DZ"

#: The documents the route accepts, and the identifier type each declares.
DOCUMENT_KINDS = {
    "NATIONAL_ID_CARD": IdentifierType.NATIONAL_ID_CARD,
    "PASSPORT": IdentifierType.PASSPORT,
}
DOCUMENT_KIND_LABELS = {
    "NATIONAL_ID_CARD": _("Algerian national identity card"),
    "PASSPORT": _("Algerian passport"),
}
#: The evidence document type that shows each declared identifier type.
EVIDENCE_TYPE_FOR_IDENTIFIER = {
    IdentifierType.NATIONAL_ID_CARD: "NATIONAL_ID_CARD",
    IdentifierType.PASSPORT: "PASSPORT_IDENTITY_PAGE",
}
MAX_DOCUMENT_NUMBER_LENGTH = 30


def _audit(action_code, *, exemption, actor_user=None, actor_person=None, after=None,
           reason_code="", correlation_id=""):  # fmt: skip
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
            target_type="NinExemption",
            target_uuid=exemption.pk,
            event_edition_id=exemption.event_edition_id,
            result="SUCCESS",
            reason_code=reason_code or None,
            after_summary=after,
            correlation_id=correlation_id,
        )
    )


def _clean_text(text: str, *, minimum: int) -> str:
    from apps.registrations.services import normalize_long_text

    value = normalize_long_text(text or "")
    if len(value) > MAX_NOTE_LENGTH:
        raise InvalidIdentityInput("note_too_long")
    if len(value) < minimum:
        raise InvalidIdentityInput("explanation_required")
    return value


def normalize_document_number(value: str) -> str:
    """Spaces and hyphens removed, upper case; Latin letters and digits only."""
    number = "".join((value or "").replace("-", " ").split()).upper()
    if (
        not number
        or len(number) > MAX_DOCUMENT_NUMBER_LENGTH
        or not number.isascii()
        or not number.isalnum()
    ):
        raise ValidationError("Enter the document number as printed on the document.")
    return number


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def active_exemption(registration_id, *, lock: bool = False) -> NinExemption | None:
    """The registration's ACTIVE exemption, or None. With `lock=True` the
    row is locked (the caller already holds the Registration row)."""
    queryset = NinExemption.objects.filter(
        registration_id=registration_id, status=NinExemptionStatus.ACTIVE
    )
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.select_related("document_identifier").first()


def latest_exemption(registration_id) -> NinExemption | None:
    return (
        NinExemption.objects.filter(registration_id=registration_id)
        .select_related("granted_by", "revoked_by", "document_identifier")
        .order_by("-created_at")
        .first()
    )


def exemption_for_case(verification) -> NinExemption | None:
    """The USED exemption behind a documentary-route case (review screen)."""
    return (
        NinExemption.objects.filter(
            registration_id=verification.registration_id, status=NinExemptionStatus.USED
        )
        .select_related("granted_by")
        .first()
    )


def evidence_type_for(identifier) -> str:
    return EVIDENCE_TYPE_FOR_IDENTIFIER.get(identifier.identifier_type, "")


def exemption_document_complete(registration, exemption, *, today=None) -> bool:
    """True when the declared document can be submitted: an Algerian
    document of an accepted kind, still current, not expired, belonging to
    the registration's person, with a reviewable image of that kind on file
    for this registration."""
    from apps.documents.services import active_identity_evidence, is_reviewable_evidence

    identifier = exemption.document_identifier
    today = today or timezone.now().date()
    if identifier is None or identifier.person_id != registration.person_id:
        return False
    if identifier.identifier_type not in EVIDENCE_TYPE_FOR_IDENTIFIER:
        return False
    if identifier.country_code_id != ALGERIA_COUNTRY_CODE:
        return False
    if identifier.status in (
        IdentifierStatus.REPLACED,
        IdentifierStatus.REVOKED,
        IdentifierStatus.EXPIRED,
    ):
        return False
    if identifier.identifier_type == IdentifierType.PASSPORT and identifier.expires_at is None:
        return False
    if identifier.expires_at is not None and identifier.expires_at <= today:
        return False
    wanted = evidence_type_for(identifier)
    return any(
        document.document_type == wanted and is_reviewable_evidence(document)
        for document in active_identity_evidence(registration)
    )


# ---------------------------------------------------------------------------
# Staff commands
# ---------------------------------------------------------------------------


def grant_nin_exemption(
    registration_id,
    *,
    actor,
    reason_code: str,
    explanation: str,
    confirmed: bool,
    correlation_id: str = "",
) -> NinExemption:
    """Grant the documentary route to ONE draft registration.

    Refused unless the actor holds `people.grant_nin_exemption` in the
    registration's exact scope, the reason is a preset one, the explanation is
    written and the action is deliberately confirmed. Only a DRAFT may receive
    one, only one live exemption may exist, and a registration whose saved
    nationality is not Algerian is refused (none saved yet is allowed: the
    participant may be blocked on the first step).
    """
    from apps.registrations.models import Registration, RegistrationPublicStatus

    registration = Registration.objects.filter(pk=registration_id).first()
    if registration is None:
        raise IdentityStateError("missing")
    require_nin_exemption_permission(actor, registration)
    if reason_code not in NinExemptionReason.values:
        raise InvalidIdentityInput("reason")
    explanation = _clean_text(explanation, minimum=MIN_EXPLANATION_LENGTH)
    if confirmed is not True:
        raise InvalidIdentityInput("confirmation_required")

    with transaction.atomic():
        locked = Registration.objects.select_for_update().get(pk=registration_id)
        if locked.public_status != RegistrationPublicStatus.DRAFT or locked.person_id is None:
            raise IdentityStateError("not_a_draft")
        profile = getattr(locked, "profile", None)
        if profile is not None and profile.nationality_code_id not in ("", None, "DZ"):
            raise IdentityStateError("not_algerian")
        if NinExemption.objects.filter(
            registration=locked,
            status__in=[NinExemptionStatus.ACTIVE, NinExemptionStatus.USED],
        ).exists():
            raise IdentityStateError("exemption_exists")
        exemption = NinExemption.objects.create(
            registration=locked,
            person_id=locked.person_id,
            event_edition_id=locked.event_edition_id,
            organization_id=locked.source_organization_id,
            reason_code=reason_code,
            explanation_encrypted=explanation,
            granted_by=actor,
            granted_at=timezone.now(),
        )
        _audit(
            action_codes.IDENTITY_NIN_EXEMPTION_GRANTED,
            exemption=exemption,
            actor_user=actor,
            reason_code=reason_code,
            after={"registration": locked.public_reference, "status": exemption.status},
            correlation_id=correlation_id,
        )
    return exemption


def revoke_nin_exemption(
    exemption_id,
    *,
    actor,
    expected_version: int,
    note: str,
    correlation_id: str = "",
) -> NinExemption:
    """Withdraw an ACTIVE exemption while the registration is still a draft.

    A USED exemption is history and cannot be revoked (the submitted case is
    decided through identity review). A declared document stays on record as
    history; the draft can no longer be submitted on the documentary route.
    """
    from apps.registrations.models import Registration, RegistrationPublicStatus

    exemption = NinExemption.objects.filter(pk=exemption_id).first()
    if exemption is None:
        raise IdentityStateError("missing")
    registration = Registration.objects.get(pk=exemption.registration_id)
    require_nin_exemption_permission(actor, registration)
    note = _clean_text(note, minimum=MIN_EXPLANATION_LENGTH)
    with transaction.atomic():
        locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)
        locked = NinExemption.objects.select_for_update().get(pk=exemption_id)
        if locked.version != expected_version:
            raise StaleIdentityVersion("version")
        if (
            locked.status != NinExemptionStatus.ACTIVE
            or locked_registration.public_status != RegistrationPublicStatus.DRAFT
        ):
            raise IdentityStateError("not_revocable")
        _revoke(locked, actor=actor, note=note, correlation_id=correlation_id)
    return locked


def _revoke(exemption, *, actor=None, note: str, correlation_id: str = "") -> None:
    exemption.status = NinExemptionStatus.REVOKED
    exemption.revoked_by = actor
    exemption.revoked_at = timezone.now()
    exemption.revocation_note_encrypted = note
    exemption.version += 1
    exemption.save()
    _audit(
        action_codes.IDENTITY_NIN_EXEMPTION_REVOKED,
        exemption=exemption,
        actor_user=actor,
        reason_code="" if actor is not None else "unused_at_submission",
        after={"status": exemption.status},
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Participant identity step and submission
# ---------------------------------------------------------------------------


def declare_exemption_document(
    *,
    registration,
    person,
    document_kind: str,
    document_number: str,
    document_expires_at: datetime.date | None,
    document_file=None,
):
    """Save the documentary route of the identity step (IDV-Q3).

    MUST run inside the identity step's transaction, after the Registration
    row was locked as a DRAFT and the profile (Algerian nationality) saved.
    Refused without an ACTIVE exemption -- a stale form after a revocation is
    refused here, whatever the page showed. Creates or reuses the person's
    identifier of the document (DECLARED), stores its photo through the
    existing private, scanned document path, and binds it to the exemption.
    Returns the identifier.
    """
    from apps.documents.services import (
        active_identity_evidence,
        is_reviewable_evidence,
        save_national_id_card,
        save_passport_identity_page,
    )
    from apps.people.selectors import identifiers_for_value, unchanged_identifier_for_person
    from apps.people.services import create_identity_identifier

    exemption = active_exemption(registration.pk, lock=True)
    if exemption is None:
        raise ValidationError("No NIN exemption is active for this registration.")
    identifier_type = DOCUMENT_KINDS.get(document_kind)
    if identifier_type is None:
        raise ValidationError("Choose the identity document.")
    number = normalize_document_number(document_number)
    today = timezone.now().date()
    if identifier_type == IdentifierType.PASSPORT and document_expires_at is None:
        raise ValidationError("Enter the passport expiry date.")
    if document_expires_at is not None and document_expires_at <= today:
        raise ValidationError("The identity document has expired.")

    from apps.people.services.identity_verification import identifier_is_bound

    identifier = unchanged_identifier_for_person(
        person,
        identifier_type=identifier_type,
        country_code_id=ALGERIA_COUNTRY_CODE,
        raw_value=number,
    )
    if (
        identifier is not None
        and identifier.expires_at != document_expires_at
        and identifier_is_bound(identifier)
    ):
        # IDV-Q-C1: another expiry (or none) for a document a submitted case
        # uses is a new declaration for this draft; the checked one is untouched.
        identifier = None
    if identifier is None:
        conflict = (
            identifiers_for_value(
                identifier_type=identifier_type,
                country_code_id=ALGERIA_COUNTRY_CODE,
                raw_value=number,
            )
            .filter(status=IdentifierStatus.VERIFIED)
            .exclude(person_id=person.pk)
            .exists()
        )
        identifier = create_identity_identifier(
            person=person,
            identifier_type=identifier_type,
            country_code_id=ALGERIA_COUNTRY_CODE,
            raw_value=number,
            status=IdentifierStatus.DECLARED,
        )
        from apps.registrations.services import _flag_duplicate_candidate

        _flag_duplicate_candidate(registration, conflict)
    if identifier.expires_at != document_expires_at:
        identifier.expires_at = document_expires_at
        identifier.save(update_fields=["expires_at", "updated_at"])

    wanted = EVIDENCE_TYPE_FOR_IDENTIFIER[identifier_type]
    if document_file is not None:
        if identifier_type == IdentifierType.NATIONAL_ID_CARD:
            save_national_id_card(
                registration=registration, person=person, uploaded_file=document_file
            )
        else:
            save_passport_identity_page(
                registration=registration,
                person=person,
                uploaded_file=document_file,
                allow_algerian_exception=True,
            )
    elif not any(
        document.document_type == wanted and is_reviewable_evidence(document)
        for document in active_identity_evidence(registration)
    ):
        raise ValidationError("Upload a photo of the identity document.")

    changed = exemption.document_identifier_id != identifier.pk
    exemption.document_identifier = identifier
    exemption.version += 1
    exemption.save(update_fields=["document_identifier", "version", "updated_at"])
    if changed:
        _audit(
            action_codes.IDENTITY_NIN_EXEMPTION_DOCUMENT_DECLARED,
            exemption=exemption,
            actor_person=person,
            after={"document_kind": document_kind},
        )
    return identifier


def release_exemption_document(registration) -> None:
    """The participant saved the NIN or passport path instead: the draft is
    no longer on the documentary route. The exemption stays ACTIVE (the
    participant may come back to it); the identifier stays as history.
    MUST run inside the identity step's transaction."""
    exemption = active_exemption(registration.pk, lock=True)
    if exemption is not None and exemption.document_identifier_id is not None:
        exemption.document_identifier = None
        exemption.version += 1
        exemption.save(update_fields=["document_identifier", "version", "updated_at"])


def consume_for_submission(registration, *, correlation_id: str = "") -> NinExemption | None:
    """At final submission (inside its transaction, Registration locked):
    return the exemption whose declared document this submission uses, marked
    USED; or None for the ordinary routes. An ACTIVE exemption the
    participant did not use is closed (REVOKED by the system, audited), so a
    submitted registration never carries a live grant."""
    exemption = active_exemption(registration.pk, lock=True)
    if exemption is None:
        return None
    if exemption.document_identifier_id is None:
        _revoke(
            exemption,
            note="Not used: the registration was submitted on another route.",
            correlation_id=correlation_id,
        )
        return None
    exemption.status = NinExemptionStatus.USED
    exemption.used_at = timezone.now()
    exemption.version += 1
    exemption.save()
    _audit(
        action_codes.IDENTITY_NIN_EXEMPTION_USED,
        exemption=exemption,
        actor_person=registration.person,
        after={"status": exemption.status},
        correlation_id=correlation_id,
    )
    return exemption
