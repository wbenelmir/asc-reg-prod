"""Staff identity review commands and the participant's correction resubmission (IDV-3).

Every command:

* checks its own dedicated permission against the exact scope of the case
  (`apps.people.policies`), in the service, before any write;
* locks the person (IDV-C1), the Registration row, then the
  IdentityVerification row, and compares the caller's `expected_version`, so a
  second reviewer, a double submit, a worker update or a correction of the
  same person's identifier in another registration is a visible conflict
  (`StaleIdentityVersion`), never a silent overwrite;
* refuses a closed registration (withdrawn, cancelled, not approved) under
  that lock, and never reopens one (R-IDV-06);
* rechecks cross-person duplicates under the identifier advisory lock where an
  identifier would become verified or change (IDV-07);
* writes one append-only `IdentityDecision`, one audit event without civil
  data, and never changes participation status beyond the documented mapping
  (return for correction <-> Additional Information Required; a final
  rejection -> a NOT_APPROVED participation decision, owner decision IDV-Q2;
  a verification -> an eligible registration enters participation review,
  SUBMITTED -> UNDER_REVIEW with its STANDARD case, `apps.reviews.intake`).

Identity confirmation never approves participation (IDV-10); approval checks
the identity itself (owner decision IDV-Q1, `apps.people.selectors.clearance`).

Owner decision IDV-Q3: the NIN exemption route (`IdentityRoute.NIN_EXEMPTION`)
is verified only from documents (`verify_identity_manually`, source
`MANUAL_NIN_EXEMPTION`); it has no NIN to correct or recheck, so no ministry
check exists for it, and the Algerian staff-assisted exception (a NIN-route
tool) does not apply to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit import action_codes
from apps.people.models import (
    IdentifierStatus,
    IdentifierType,
    IdentityDecision,
    IdentityDecisionAction,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityRoute,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationAttempt,
    IdentityVerificationMethod,
    IdentityVerificationResult,
    VerificationSource,
)
from apps.people.policies import require_identity_permission
from apps.people.services.identity_verification import (
    ACTIVE_PUBLIC_STATUSES,
    ALGERIA_COUNTRY_CODE,
    VERIFIABLE_IDENTIFIER_STATUSES,
    IdentityVerificationError,
    _audit,
    create_revision,
    discard_open_jobs,
    find_cross_person_conflicts,
    lock_cases_sharing_identifier,
    lock_identifier_values,
    lock_person_identity,
    rebind_cases_after_replacement,
    schedule_job,
    supersede_same_person_declarations,
    verify_identifier_or_conflict,
)

#: The shortest explanation accepted where an action requires one.
MIN_EXPLANATION_LENGTH = 10
MAX_NOTE_LENGTH = 1000


class StaleIdentityVersion(IdentityVerificationError):
    """The case changed since the caller loaded it (another reviewer, the
    worker, or the participant). Refresh and decide again."""


class IdentityStateError(IdentityVerificationError):
    """The action is not allowed in the case's current state."""


class DuplicateIdentityConflict(IdentityVerificationError):
    """Another person holds this identifier (verified or pending). Staff cannot
    override it (IDV-06); the conflict must be resolved first."""


class EvidenceUnavailable(IdentityVerificationError):
    """No reviewable evidence of the required kind (missing, pending a scan,
    rejected by the scan, replaced, or of another registration)."""


class InvalidIdentityInput(IdentityVerificationError):
    """A preset reason, an item, an explanation or a value is not acceptable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# Preset reasons and correction items (brief, English; the UI shows them)
# ---------------------------------------------------------------------------

MANUAL_VERIFY_REASONS = {
    "DOCUMENT_MATCHES_SUBMISSION": _("The document matches the submitted identity"),
    "MINISTRY_RECORD_UNAVAILABLE": _(
        "No usable ministry result; the document confirms the identity"
    ),
    "NAME_VARIANT_CONFIRMED": _("A name variant is confirmed on the document"),
    "BIRTH_DATE_CONFIRMED": _("The birth date is confirmed on the document"),
}
EXCEPTION_REASONS = {
    "NO_USABLE_MINISTRY_RECORD": _(
        "No usable ministry record; an official Algerian document confirms the identity"
    ),
    "OTHER_DOCUMENTED": _("Other documented reason (explain below)"),
}
NIN_CORRECTION_REASONS = {
    "TYPING_ERROR_CONFIRMED": _("A typing error, confirmed on the identity document"),
    "DIGITS_TRANSPOSED_CONFIRMED": _("Transposed digits, confirmed on the identity document"),
}
RECHECK_REASONS = {
    "RECHECK_AFTER_TECHNICAL_FAILURE": _("Recheck after a technical failure"),
    "RECHECK_AFTER_CONFIGURATION_FIX": _("Recheck after the service configuration was fixed"),
    "RECHECK_AFTER_LINKED_CORRECTION": _(
        "Recheck after the identity number was corrected in another registration"
    ),
}
REJECT_REASONS = {
    "DOCUMENT_DOES_NOT_MATCH": _("The document does not match the submitted identity"),
    "DOCUMENT_NOT_GENUINE_SUSPECTED": _("The document is suspected not to be genuine"),
    "IDENTITY_OF_ANOTHER_PERSON": _("The identity belongs to another person"),
    "NO_VALID_EVIDENCE": _("No valid evidence was provided"),
    "OTHER": _("Other reason (explain below)"),
}

#: Participant-facing correction items, per route. Each label is safe to show
#: the participant: it names no provider result and no other person.
RETURN_ITEMS = {
    IdentityRoute.NIN: {
        "NIN_NUMBER": _("Check your national identity number"),
        "NAME_SPELLING": _("Check the spelling of your names, exactly as on your identity card"),
        "BIRTH_DATE": _("Check your date of birth"),
        "NATIONAL_ID_CARD": _("Upload a clear photo of your national identity card"),
        "ALGERIAN_PASSPORT_PAGE": _("Upload a clear photo of your passport identity page"),
    },
    IdentityRoute.PASSPORT: {
        "NAME_SPELLING": _("Check the spelling of your names, exactly as in your passport"),
        "BIRTH_DATE": _("Check your date of birth"),
        "PASSPORT_DETAILS": _("Check your passport number, issuing country and expiry date"),
        "PASSPORT_PAGE": _("Upload a clear photo of your passport identity page"),
    },
    # IDV-Q3: the documentary route (no NIN, no ministry check).
    IdentityRoute.NIN_EXEMPTION: {
        "NAME_SPELLING": _(
            "Check the spelling of your names, exactly as on your identity document"
        ),
        "BIRTH_DATE": _("Check your date of birth"),
        "DOCUMENT_DETAILS": _(
            "Check the number and expiry date of the identity document you declared"
        ),
        "DOCUMENT_IMAGE": _("Upload a clear photo of the identity document you declared"),
    },
}
#: Items that require a new or existing reviewable document of this type.
#: `DOCUMENT_IMAGE` (IDV-Q3) wants the type of the declared document.
EVIDENCE_ITEMS = {
    "NATIONAL_ID_CARD": "NATIONAL_ID_CARD",
    "ALGERIAN_PASSPORT_PAGE": "PASSPORT_IDENTITY_PAGE",
    "PASSPORT_PAGE": "PASSPORT_IDENTITY_PAGE",
}


def manual_evidence_types(verification) -> set[str]:
    """The evidence a normal manual verification of this case reviews: the
    national identity card on the NIN route, the passport identity page on
    the passport route, and on the NIN exemption route (IDV-Q3) the image of
    the document the participant declared."""
    if verification.route == IdentityRoute.NIN:
        return {"NATIONAL_ID_CARD"}
    if verification.route == IdentityRoute.NIN_EXEMPTION:
        from apps.people.services.nin_exemption import evidence_type_for

        identifier = verification.current_revision.identifier
        wanted = evidence_type_for(identifier)
        return {wanted} if wanted else set()
    return {"PASSPORT_IDENTITY_PAGE"}


def _clean_note(note: str) -> str:
    from apps.registrations.services import normalize_long_text

    text = normalize_long_text(note or "")
    if len(text) > MAX_NOTE_LENGTH:
        raise InvalidIdentityInput("note_too_long")
    return text


def _require_reason(reason_code: str, catalogue: dict) -> None:
    if reason_code not in catalogue:
        raise InvalidIdentityInput("reason")


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _load(verification_id) -> IdentityVerification:
    try:
        return IdentityVerification.objects.select_related("current_revision").get(
            pk=verification_id
        )
    except IdentityVerification.DoesNotExist:
        raise IdentityStateError("missing") from None


def _lock_case(verification_id, expected_version: int):
    """Lock the person, the Registration and the case, in the lock order of
    `apps.people.services.identity_verification`, and compare the version."""
    from apps.registrations.models import Registration

    registration_id, person_id = (
        IdentityVerification.objects.filter(pk=verification_id)
        .values_list("registration_id", "person_id")
        .get()
    )
    lock_person_identity(person_id)
    registration = Registration.objects.select_for_update().get(pk=registration_id)
    verification = (
        IdentityVerification.objects.select_for_update(of=("self",))
        .select_related("current_revision__identifier")
        .get(pk=verification_id)
    )
    if verification.version != expected_version:
        raise StaleIdentityVersion("version")
    return registration, verification


def _require_active(registration) -> None:
    if registration.public_status not in ACTIVE_PUBLIC_STATUSES:
        raise IdentityStateError("registration_not_active")


def _transition(verification, *, status, reason_code=None, source="", actor=None, now=None):
    now = now or timezone.now()
    from_status = verification.status
    verification.status = status
    if reason_code is not None:
        verification.reason_code = reason_code
    verification.verification_source = source
    verification.status_changed_at = now
    if status in IdentityStatus.verified_statuses() or status == IdentityStatus.REJECTED:
        verification.decided_at = now
        verification.decided_by = actor
    verification.version += 1
    verification.save()
    return from_status


def _decision(
    verification,
    *,
    action,
    from_status,
    actor_user=None,
    actor_person=None,
    reason_code="",
    note="",
    items=(),
    evidence=None,
    **extra_fields,
) -> IdentityDecision:  # noqa: E501
    return IdentityDecision.objects.create(
        verification=verification,
        revision=verification.current_revision,
        action=action,
        from_status=from_status,
        to_status=verification.status,
        reason_code=reason_code,
        note_encrypted=note,
        requested_items=list(items),
        evidence_document=evidence,
        actor_user=actor_user,
        actor_person=actor_person,
        **extra_fields,
    )


def _reviewable_document(registration, document_id, allowed_types):
    from apps.documents.models import Document
    from apps.documents.services import is_reviewable_evidence

    if not document_id:
        raise EvidenceUnavailable("missing")
    document = (
        Document.objects.select_related("stored_object")
        .filter(pk=document_id, registration=registration)
        .first()
    )
    if document is None or document.document_type not in allowed_types:
        raise EvidenceUnavailable("wrong_document")
    if not is_reviewable_evidence(document):
        raise EvidenceUnavailable("not_reviewable")
    return document


def _set_registration_status(registration, *, public_status, internal_status) -> None:
    fields = []
    if registration.public_status != public_status:
        registration.public_status = public_status
        fields.append("public_status")
    if registration.internal_status != internal_status:
        registration.internal_status = internal_status
        fields.append("internal_status")
    if fields:
        registration.version += 1
        registration.save(update_fields=[*fields, "version", "updated_at"])


def _notify(
    registration,
    *,
    purpose_code: str,
    idempotency_key: str,
    language: str = "",
    extra_context: dict | None = None,
) -> None:
    """Best effort, safe, existing templates only: the message names the
    registration reference and the event, never a reason or a result.

    `language` overrides the registration's preferred language (UPD-03: the
    correction request is written in the participant's submission language);
    `extra_context` adds values the template version explicitly allows."""
    from apps.communications.services import queue_communication
    from apps.people.models import ContactPointStatus, ContactPointType

    if registration.person_id is None:
        return
    contact = registration.person.contact_points.filter(
        type=ContactPointType.EMAIL,
        login_enabled=True,
        is_verified=True,
        status=ContactPointStatus.ACTIVE,
    ).first()
    if contact is None:
        return
    language = language or registration.preferred_language or "en"
    queue_communication(
        purpose_code=purpose_code,
        event_edition=registration.event_edition,
        person=registration.person,
        registration=registration,
        language=language,
        destination=contact.value_encrypted,
        context={
            "public_reference": registration.public_reference,
            "event_name": registration.event_edition.display_name(language),
            **(extra_context or {}),
        },
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# UPD-03: the note to the participant on a correction request
# ---------------------------------------------------------------------------

#: The longest note to the participant (code points, after normalization).
MAX_PARTICIPANT_NOTE_LENGTH = 2000


def clean_participant_note(note: str) -> str:
    """Normalize a note written for the participant: line breaks kept (CR LF
    and CR become LF), NFC, trimmed. Control characters other than line
    breaks and tabs are refused, never silently removed."""
    from apps.registrations.services import normalize_long_text

    text = normalize_long_text(note or "")
    if len(text) > MAX_PARTICIPANT_NOTE_LENGTH:
        raise InvalidIdentityInput("participant_note_too_long")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise InvalidIdentityInput("participant_note_invalid")
    return text


def correction_notification_language(registration, requested: str = "") -> tuple[str, str]:
    """`(language, basis)` for a correction request (UPD-03).

    The registration's submission language when it is known (recorded, or
    derived from the accepted notice); otherwise the language staff chose
    explicitly (`STAFF_SELECTED`). Without a choice, the registration's
    preferred language is used and recorded as such (`PREFERRED_FALLBACK`),
    never a language inferred from nationality or the staff interface."""
    from apps.registrations import language as registration_language

    submission = registration_language.submission_language_for(registration)
    if submission.known:
        return submission.code, submission.basis
    chosen = registration_language.clean_language(requested)
    if chosen:
        return chosen, registration_language.STAFF_SELECTED
    return registration_language.explicit_fallback_language(registration), "PREFERRED_FALLBACK"


def participant_note_section(note: str, language: str) -> str:
    """The note block of the correction e-mail, in `language`, or "" when
    there is no note. Plain text: the e-mail is sent as text, so the note's
    line breaks are kept as they are."""
    from django.utils import translation

    if not note:
        return ""
    with translation.override(language):
        heading = str(_("Message from the registration team:"))
    return f"\n\n{heading}\n{note}\n"


# ---------------------------------------------------------------------------
# Staff commands
# ---------------------------------------------------------------------------


def verify_identity_manually(
    verification_id,
    *,
    actor,
    expected_version: int,
    evidence_document_id,
    reason_code: str,
    note: str = "",
    exception: bool = False,
    correlation_id: str = "",
) -> IdentityVerification:
    """Confirm an identity from reviewed evidence (IDV-06), or, with
    `exception=True`, by the Algerian staff-assisted exception (A13-01).

    Normal manual verification uses the national identity card on the NIN
    route, the passport identity page on the passport route, and the image of
    the declared Algerian document on the NIN exemption route (IDV-Q3). The exception
    applies to the NIN route only, accepts either Algerian document, needs
    `people.apply_identity_exception` and a written explanation. Neither ever
    overrides a cross-person duplicate, and both stay distinguishable from API
    verification (`verification_source`).
    """
    verification = _load(verification_id)
    codename = "apply_identity_exception" if exception else "verify_identity_manually"
    require_identity_permission(actor, codename, verification)
    _require_reason(reason_code, EXCEPTION_REASONS if exception else MANUAL_VERIFY_REASONS)
    note = _clean_note(note)
    if exception and len(note) < MIN_EXPLANATION_LENGTH:
        raise InvalidIdentityInput("explanation_required")

    with transaction.atomic():
        registration, locked = _lock_case(verification_id, expected_version)
        _require_active(registration)
        if locked.status != IdentityStatus.MANUAL_REVIEW:
            raise IdentityStateError("not_in_manual_review")
        if exception and locked.route != IdentityRoute.NIN:
            raise IdentityStateError("exception_is_for_the_nin_route")
        allowed = manual_evidence_types(locked)
        if exception:
            allowed = {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
            source = VerificationSource.STAFF_EXCEPTION
        elif locked.route == IdentityRoute.NIN:
            source = VerificationSource.MANUAL_NATIONAL_ID_CARD
        elif locked.route == IdentityRoute.NIN_EXEMPTION:
            # IDV-Q3: from the declared Algerian document only.
            source = VerificationSource.MANUAL_NIN_EXEMPTION
        else:
            source = VerificationSource.MANUAL_PASSPORT
        document = _reviewable_document(registration, evidence_document_id, allowed)
        identifier = locked.current_revision.identifier
        from apps.registrations.models import RegistrationProfile

        # IDV-Q-C1: an older verified declaration of the same document by the
        # same person (another expiry) is retired and its cases rebound,
        # explicitly, before this one is verified.
        supersede_same_person_declarations(
            locked, identifier, actor_user=actor, correlation_id=correlation_id
        )
        outcome = verify_identifier_or_conflict(
            locked,
            identifier,
            revision=locked.current_revision,
            profile=RegistrationProfile.objects.get(registration=registration),
        )
        if outcome == IdentityReasonCode.IDENTITY_DATA_CHANGED:
            # The identifier or the identity changed without this case being
            # rebound: never confirm what is no longer current (R-IDV-01).
            raise IdentityStateError("identity_changed")
        if outcome:
            raise DuplicateIdentityConflict("duplicate")
        now = timezone.now()
        from_status = _transition(
            locked, status=IdentityStatus.MANUALLY_VERIFIED, reason_code="", source=source,
            actor=actor, now=now,
        )  # fmt: skip
        discard_open_jobs(locked)
        IdentityVerificationAttempt.objects.create(
            identity_identifier=identifier,
            registration=registration,
            method=(
                IdentityVerificationMethod.SUPERVISED_CORRECTION
                if exception
                else IdentityVerificationMethod.MANUAL_DOCUMENT
            ),
            provider_code="STAFF",
            result=IdentityVerificationResult.MATCH,
            attempted_at=now,
            performed_by_user=actor,
            revision=locked.current_revision,
            outcome="MANUAL",
            evidence_document=document,
            applied=True,
        )
        type(document).objects.filter(pk=document.pk).update(
            verified_at=now, verified_by_user=actor
        )
        _decision(
            locked,
            action=(
                IdentityDecisionAction.EXCEPTION_VERIFY
                if exception
                else IdentityDecisionAction.MANUAL_VERIFY
            ),
            from_status=from_status,
            actor_user=actor,
            reason_code=reason_code,
            note=note,
            evidence=document,
        )
        _audit(
            action_codes.IDENTITY_VERIFIED_BY_EXCEPTION
            if exception
            else action_codes.IDENTITY_VERIFIED_MANUALLY,
            verification=locked,
            actor_user=actor,
            reason_code=reason_code,
            after={"status": locked.status, "source": source},
            correlation_id=correlation_id,
        )
        # Every manual route (document, passport, NIN exemption, staff
        # exception) then waits for its participation decision, in this
        # transaction. Identity confirmation still never approves (IDV-10).
        from apps.reviews.intake import EntryTrigger, enqueue_for_participation_review

        enqueue_for_participation_review(
            registration.pk,
            trigger=EntryTrigger.IDENTITY_MANUALLY_VERIFIED,
            actor=actor,
            correlation_id=correlation_id,
        )
    return locked


def return_identity_for_correction(
    verification_id,
    *,
    actor,
    expected_version: int,
    items: list[str],
    note: str = "",
    participant_note: str = "",
    notification_language: str = "",
    correlation_id: str = "",
) -> IdentityVerification:
    """Ask the participant to correct and resubmit on the SAME account and
    registration (IDV-09). The participant sees Additional Information
    Required and the preset items, never the reason or a provider result.

    UPD-03: `participant_note` is an optional note written FOR the
    participant, in the participant's submission language (no automatic
    translation). It belongs to this return (its own decision row), is shown
    in the participant's workspace and correction page, and is included in
    the correction e-mail, sent once per return in that language
    (`correction_notification_language`; `notification_language` is the
    staff choice used only when the submission language is unknown). `note`
    stays the internal note, never shown to the participant."""
    from apps.registrations.models import RegistrationInternalStatus, RegistrationPublicStatus
    from apps.reviews.models import InformationRequest, InformationRequestStatus

    verification = _load(verification_id)
    require_identity_permission(actor, "return_identity_for_correction", verification)
    catalogue = RETURN_ITEMS[verification.route]
    items = list(dict.fromkeys(items or []))
    if not items or any(item not in catalogue for item in items):
        raise InvalidIdentityInput("items")
    note = _clean_note(note)
    participant_note = clean_participant_note(participant_note)

    with transaction.atomic():
        registration, locked = _lock_case(verification_id, expected_version)
        if registration.public_status not in (
            RegistrationPublicStatus.SUBMITTED,
            RegistrationPublicStatus.UNDER_REVIEW,
        ):
            raise IdentityStateError("registration_not_reviewable")
        if locked.status != IdentityStatus.MANUAL_REVIEW:
            raise IdentityStateError("not_in_manual_review")
        if InformationRequest.objects.filter(
            registration=registration, status__in=InformationRequestStatus.active_statuses()
        ).exists():
            # One participant request at a time: the generic request flow owns
            # the same public status.
            raise IdentityStateError("information_request_active")
        from_status = _transition(locked, status=IdentityStatus.RETURNED_FOR_CORRECTION)
        discard_open_jobs(locked)
        _set_registration_status(
            registration,
            public_status=RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
            internal_status=RegistrationInternalStatus.AWAITING_APPLICANT,
        )
        language, language_basis = correction_notification_language(
            registration, notification_language
        )
        decision = _decision(
            locked,
            action=IdentityDecisionAction.RETURN_FOR_CORRECTION,
            from_status=from_status,
            actor_user=actor,
            note=note,
            items=items,
            participant_note_encrypted=participant_note,
            notification_language=language,
            notification_language_basis=language_basis,
        )
        _audit(
            action_codes.IDENTITY_RETURNED_FOR_CORRECTION,
            verification=locked,
            actor_user=actor,
            after={
                "status": locked.status,
                "items": items,
                # Whether a note was written, and the language used; never the note.
                "participant_note": bool(participant_note),
                "notification_language": language,
                "notification_language_basis": language_basis,
            },
            correlation_id=correlation_id,
        )
        from apps.communications.purposes import CommunicationPurpose

        # One message per return: the key is the decision, so a retried
        # delivery or a repeated call never sends a second one.
        _notify(
            registration,
            purpose_code=CommunicationPurpose.IDENTITY_CORRECTION_REQUEST,
            idempotency_key=f"identity-correction-request:{decision.pk}",
            language=language,
            extra_context={
                "participant_note_section": participant_note_section(participant_note, language)
            },
        )
    return locked


def correct_nin_and_recheck(
    verification_id,
    *,
    actor,
    expected_version: int,
    new_nin: str,
    reason_code: str,
    note: str = "",
    evidence_document_id=None,
    confirmed: bool = False,
    correlation_id: str = "",
) -> IdentityVerification:
    """Correct the NIN from evidence and recheck it with the ministry, or
    recheck the unchanged NIN after a technical failure (IDV-05).

    A changed NIN needs reviewable evidence, a correction reason and a
    deliberate confirmation; the duplicate check runs before the change is
    accepted. The old identifier is REPLACED, a new identity revision and a
    new job are created, and the case stays PENDING until that job's own
    result arrives -- an older result can never verify the new NIN.
    """
    from apps.core.text_rules import TextRuleError, normalize_nin
    from apps.people.services import create_identity_identifier

    verification = _load(verification_id)
    require_identity_permission(actor, "correct_identity_nin", verification)
    try:
        nin = normalize_nin(new_nin)
    except TextRuleError:
        raise InvalidIdentityInput("nin_format") from None
    note = _clean_note(note)

    with transaction.atomic():
        registration, locked = _lock_case(verification_id, expected_version)
        _require_active(registration)
        if locked.route != IdentityRoute.NIN:
            raise IdentityStateError("not_nin_route")
        if locked.status != IdentityStatus.MANUAL_REVIEW:
            raise IdentityStateError("not_in_manual_review")
        current = locked.current_revision.identifier
        changed = nin != current.value_encrypted
        document = None
        if changed:
            _require_reason(reason_code, NIN_CORRECTION_REASONS)
            if confirmed is not True:
                raise InvalidIdentityInput("confirmation_required")
            document = _reviewable_document(
                registration, evidence_document_id, {"NATIONAL_ID_CARD", "PASSPORT_IDENTITY_PAGE"}
            )
            # The person's other cases using this identifier, locked before
            # any identifier lock (lock order); rebound below (R-IDV-01).
            siblings = lock_cases_sharing_identifier(current.pk, exclude_verification_id=locked.pk)
            lock_identifier_values(
                IdentifierType.NIN, ALGERIA_COUNTRY_CODE, [current.value_encrypted, nin]
            )
            if find_cross_person_conflicts(
                identifier_type=IdentifierType.NIN,
                country_code_id=ALGERIA_COUNTRY_CODE,
                raw_value=nin,
                person_id=locked.person_id,
            ).exists:
                raise DuplicateIdentityConflict("duplicate")
            current.status = IdentifierStatus.REPLACED
            current.save(update_fields=["status", "updated_at"])
            identifier = create_identity_identifier(
                person=locked.person,
                identifier_type=IdentifierType.NIN,
                country_code_id=ALGERIA_COUNTRY_CODE,
                raw_value=nin,
                status=IdentifierStatus.DECLARED,
            )
            rebind_cases_after_replacement(
                siblings,
                replacement=identifier,
                changed_field="nin",
                origin=locked,
                actor_user=actor,
                correlation_id=correlation_id,
            )
            source = IdentityRevisionSource.STAFF_NIN_CORRECTION
            action = IdentityDecisionAction.NIN_CORRECTION
            audit_code = action_codes.IDENTITY_NIN_CORRECTED
        else:
            _require_reason(reason_code, RECHECK_REASONS)
            if current.status not in VERIFIABLE_IDENTIFIER_STATUSES:
                raise IdentityStateError("identity_changed")
            identifier = current
            source = IdentityRevisionSource.STAFF_RECHECK
            action = IdentityDecisionAction.RECHECK
            audit_code = action_codes.IDENTITY_RECHECK_REQUESTED
        from apps.registrations.models import RegistrationProfile

        profile = RegistrationProfile.objects.get(registration=registration)
        revision = create_revision(
            verification=locked,
            identifier=identifier,
            profile=profile,
            source=source,
            changed_fields=["nin"] if changed else [],
            created_by_user=actor,
        )
        discard_open_jobs(locked)
        locked.current_revision = revision
        from_status = _transition(locked, status=IdentityStatus.PENDING, reason_code="")
        schedule_job(verification=locked, revision=revision)
        _decision(
            locked,
            action=action,
            from_status=from_status,
            actor_user=actor,
            reason_code=reason_code,
            note=note,
            evidence=document,
        )
        _audit(
            audit_code,
            verification=locked,
            actor_user=actor,
            reason_code=reason_code,
            after={"status": locked.status, "revision": revision.number, "nin_changed": changed},
            correlation_id=correlation_id,
        )
    return locked


# ---------------------------------------------------------------------------
# UPD-02: save corrections and verify identity, in one reviewer action
# ---------------------------------------------------------------------------

#: Preset reasons of a correction-and-verification.
CORRECTION_VERIFY_REASONS = {
    "DATA_CORRECTED_FROM_DOCUMENT": _(
        "The submitted data was corrected from the identity document, which confirms the identity"
    ),
    "TYPING_ERROR_CORRECTED": _("A typing error was corrected; the document confirms the identity"),
    "NAME_VARIANT_CONFIRMED": _("A name variant is confirmed on the document"),
}

#: The fields a reviewer may correct, in display order.
CORRECTABLE_FIELDS = ("given_names", "family_name", "date_of_birth", "nin")


@dataclass(frozen=True)
class MinistryRecheck:
    """One lookup of a corrected NIN, made before the correction is saved and
    outside any database transaction. `outcome` is None when no lookup was
    possible (`skipped` says why: no official service, or no free slot)."""

    outcome: object = None
    provider: object = None
    duration_ms: int = 0
    skipped: str = ""


def ministry_recheck_possible(provider=None) -> bool:
    """Whether a corrected NIN can be checked with the Ministry service here:
    the official adapter, configured (or a development simulation where the
    settings allow one, as for the worker)."""
    from django.conf import settings

    from apps.people.nin_provider import get_nin_provider

    try:
        provider = provider or get_nin_provider()
    except Exception:  # noqa: BLE001 - an unloadable backend means no recheck
        return False
    official = bool(getattr(provider, "IS_OFFICIAL", False))
    if not official and not getattr(settings, "IDENTITY_ALLOW_SIMULATED_PROVIDER", False):
        return False
    try:
        return not provider.configuration_problems()
    except Exception:  # noqa: BLE001
        return False


def _recheck_corrected_nin(nin: str, provider=None) -> MinistryRecheck:
    """Look the corrected NIN up through the existing adapter, inside a
    provider concurrency slot, never inside a transaction (A13-14)."""
    import time

    from apps.people.nin_provider import get_nin_provider
    from apps.people.services.identity_verification import (
        _require_no_open_transaction,
        _safe_lookup,
        provider_slot,
    )

    provider = provider or get_nin_provider()
    if not ministry_recheck_possible(provider):
        return MinistryRecheck(skipped="not_configured")
    _require_no_open_transaction()
    with provider_slot() as acquired:
        if not acquired:
            return MinistryRecheck(provider=provider, skipped="busy")
        started = time.monotonic()
        outcome = _safe_lookup(provider, nin)
        duration_ms = int((time.monotonic() - started) * 1000)
    return MinistryRecheck(outcome=outcome, provider=provider, duration_ms=duration_ms)


def _changes_payload(before: dict, after: dict, changed: list[str]) -> str:
    """Encrypted-at-rest JSON of the previous and corrected values of the
    changed fields only (protected; never logged, never in an audit summary)."""
    import json

    return json.dumps(
        {field: {"before": before[field], "after": after[field]} for field in changed},
        ensure_ascii=False,
        sort_keys=True,
    )


def correct_and_verify_identity(
    verification_id,
    *,
    actor,
    expected_version: int,
    given_names: str,
    family_name: str,
    date_of_birth,
    nin: str = "",
    evidence_document_id,
    reason_code: str,
    note: str = "",
    provider=None,
    correlation_id: str = "",
) -> IdentityVerification:
    """Save validated corrections and verify the identity in one action
    (UPD-02), without returning the request to the participant and without a
    new upload: the reviewer compares the submitted values, the retained
    Ministry facts and the existing evidence, corrects the first name, last
    name, date of birth and (NIN route) the NIN, and confirms.

    * Permissions: `people.verify_identity_manually` in the case's scope, plus
      `people.correct_identity_nin` when the NIN changes. Validation (Latin
      names, the event's age rule, the 18-digit NIN, an allowed reason, a
      reviewable document of the route's kind) happens before any write; a
      refusal leaves no partial edit and no approval.
    * A changed NIN of an Algerian participant is looked up again through the
      existing adapter FIRST, outside any transaction (no lock is held during
      the call). A verifying answer for the corrected values makes the case
      API_VERIFIED (truthful provenance). Not found, unavailable, a conflicting
      record or no service: the reviewer's manual resolution from the
      evidence applies and the case is MANUALLY_VERIFIED. The lookup attempt
      is retained either way; a conflicting record is never called API
      verified. Foreign participants and the NIN exemption route are never
      looked up.
    * The Ministry date never replaces the reviewer's date (with `presume`
      true it is not even compared); no official value is written over a
      reviewer's value.
    * Everything is written in ONE transaction under the identity lock order,
      after the version check: a case changed meanwhile (another reviewer,
      the participant, the worker, a withdrawal) is a conflict and nothing is
      saved. Pending checks are discarded and the new revision makes any
      late result stale, so an older API result can never overwrite this
      decision.
    * The verified, active, eligible registration enters participation
      review exactly once (`enqueue_for_participation_review`, idempotent).
      Identity verification never approves participation.
    """
    from apps.core.text_rules import TextRuleError, normalize_latin_name, normalize_nin
    from apps.people.identity_contract import LookupKind, evaluate_identity_match
    from apps.people.services import create_identity_identifier
    from apps.people.services.identity_verification import (
        _REASON_FOR_KIND,
        _RESULT_FOR_REASON,
    )
    from apps.registrations.models import Registration, RegistrationProfile
    from apps.registrations.services import age_problem

    verification = _load(verification_id)
    require_identity_permission(actor, "verify_identity_manually", verification)
    _require_reason(reason_code, CORRECTION_VERIFY_REASONS)
    note = _clean_note(note)
    try:
        given_names = normalize_latin_name(given_names)
        family_name = normalize_latin_name(family_name)
    except TextRuleError:
        raise InvalidIdentityInput("names") from None
    registration = Registration.objects.select_related("event_edition").get(
        pk=verification.registration_id
    )
    if date_of_birth is None or age_problem(date_of_birth, registration.event_edition):
        raise InvalidIdentityInput("birth_date")
    route = verification.route
    current = verification.current_revision.identifier
    new_nin = ""
    if route == IdentityRoute.NIN:
        try:
            new_nin = normalize_nin(nin)
        except TextRuleError:
            raise InvalidIdentityInput("nin_format") from None
    elif (nin or "").strip():
        raise InvalidIdentityInput("nin_not_applicable")
    nin_changed = bool(new_nin) and new_nin != current.value_encrypted
    if nin_changed:
        require_identity_permission(actor, "correct_identity_nin", verification)

    # Read-only pre-checks, so the Ministry is never asked about a case that
    # cannot be decided. All of them are repeated under the locks below.
    if registration.public_status not in ACTIVE_PUBLIC_STATUSES:
        raise IdentityStateError("registration_not_active")
    if verification.status != IdentityStatus.MANUAL_REVIEW:
        raise IdentityStateError("not_in_manual_review")
    if verification.version != expected_version:
        raise StaleIdentityVersion("version")
    _reviewable_document(registration, evidence_document_id, manual_evidence_types(verification))
    if (
        nin_changed
        and find_cross_person_conflicts(
            identifier_type=IdentifierType.NIN,
            country_code_id=ALGERIA_COUNTRY_CODE,
            raw_value=new_nin,
            person_id=verification.person_id,
        ).exists
    ):
        raise DuplicateIdentityConflict("duplicate")

    recheck = MinistryRecheck(skipped="not_applicable")
    if nin_changed and route == IdentityRoute.NIN:
        recheck = _recheck_corrected_nin(new_nin, provider)

    with transaction.atomic():
        registration, locked = _lock_case(verification_id, expected_version)
        _require_active(registration)
        if locked.status != IdentityStatus.MANUAL_REVIEW:
            raise IdentityStateError("not_in_manual_review")
        document = _reviewable_document(
            registration, evidence_document_id, manual_evidence_types(locked)
        )
        profile = RegistrationProfile.objects.get(registration=registration)
        current = locked.current_revision.identifier
        before = {
            "given_names": profile.submitted_given_names,
            "family_name": profile.submitted_family_name,
            "date_of_birth": profile.date_of_birth.isoformat() if profile.date_of_birth else "",
            "nin": current.value_encrypted if route == IdentityRoute.NIN else "",
        }
        after = {
            "given_names": given_names,
            "family_name": family_name,
            "date_of_birth": date_of_birth.isoformat(),
            "nin": new_nin if route == IdentityRoute.NIN else "",
        }
        changed = [field for field in CORRECTABLE_FIELDS if before[field] != after[field]]
        identifier = current
        if "nin" in changed:
            siblings = lock_cases_sharing_identifier(current.pk, exclude_verification_id=locked.pk)
            lock_identifier_values(
                IdentifierType.NIN, ALGERIA_COUNTRY_CODE, [current.value_encrypted, new_nin]
            )
            if find_cross_person_conflicts(
                identifier_type=IdentifierType.NIN,
                country_code_id=ALGERIA_COUNTRY_CODE,
                raw_value=new_nin,
                person_id=locked.person_id,
            ).exists:
                raise DuplicateIdentityConflict("duplicate")
            current.status = IdentifierStatus.REPLACED
            current.save(update_fields=["status", "updated_at"])
            identifier = create_identity_identifier(
                person=locked.person,
                identifier_type=IdentifierType.NIN,
                country_code_id=ALGERIA_COUNTRY_CODE,
                raw_value=new_nin,
                status=IdentifierStatus.DECLARED,
            )
            rebind_cases_after_replacement(
                siblings,
                replacement=identifier,
                changed_field="nin",
                origin=locked,
                actor_user=actor,
                correlation_id=correlation_id,
            )
        elif current.status not in VERIFIABLE_IDENTIFIER_STATUSES:
            raise IdentityStateError("identity_changed")
        profile_fields = [field for field in changed if field != "nin"]
        if profile_fields:
            profile.submitted_given_names = given_names
            profile.submitted_family_name = family_name
            profile.submitted_full_name = f"{given_names} {family_name}".strip()
            profile.date_of_birth = date_of_birth
            profile.save(
                update_fields=[
                    "submitted_given_names",
                    "submitted_family_name",
                    "submitted_full_name",
                    "date_of_birth",
                    "updated_at",
                ]
            )
        revision = locked.current_revision
        if changed:
            revision = create_revision(
                verification=locked,
                identifier=identifier,
                profile=profile,
                source=IdentityRevisionSource.STAFF_IDENTITY_CORRECTION,
                changed_fields=changed,
                created_by_user=actor,
            )
            locked.current_revision = revision
        discard_open_jobs(locked)

        # Provenance: the API path only for a verifying answer about exactly
        # these corrected values; otherwise the reviewer's manual resolution.
        outcome = recheck.outcome
        evaluation = None
        attempt_reason = ""
        api_source = ""
        if outcome is not None:
            if outcome.kind == LookupKind.FOUND and outcome.facts is not None:
                evaluation = evaluate_identity_match(
                    requested_nin=new_nin,
                    submitted_given_names=given_names,
                    submitted_family_name=family_name,
                    submitted_birth_date=date_of_birth,
                    facts=outcome.facts,
                )
                attempt_reason = evaluation.reason_code
                if evaluation.verified:
                    api_source = (
                        VerificationSource.MINISTRY_API
                        if getattr(recheck.provider, "IS_OFFICIAL", False)
                        else VerificationSource.SIMULATED_API
                    )
            elif outcome.kind == LookupKind.FOUND:
                attempt_reason = IdentityReasonCode.INVALID_RESPONSE
            else:
                attempt_reason = _REASON_FOR_KIND.get(
                    outcome.kind, IdentityReasonCode.INVALID_RESPONSE
                )

        supersede_same_person_declarations(
            locked, identifier, actor_user=actor, correlation_id=correlation_id
        )
        final = verify_identifier_or_conflict(
            locked, identifier, revision=revision, profile=profile
        )
        if final == IdentityReasonCode.IDENTITY_DATA_CHANGED:
            raise IdentityStateError("identity_changed")
        if final:
            raise DuplicateIdentityConflict("duplicate")

        now = timezone.now()
        if api_source:
            status, source = IdentityStatus.API_VERIFIED, api_source
        elif route == IdentityRoute.NIN:
            status, source = (
                IdentityStatus.MANUALLY_VERIFIED,
                (VerificationSource.MANUAL_NATIONAL_ID_CARD),
            )
        elif route == IdentityRoute.NIN_EXEMPTION:
            status, source = (
                IdentityStatus.MANUALLY_VERIFIED,
                (VerificationSource.MANUAL_NIN_EXEMPTION),
            )
        else:
            status, source = IdentityStatus.MANUALLY_VERIFIED, VerificationSource.MANUAL_PASSPORT
        from_status = _transition(
            locked, status=status, reason_code="", source=source, actor=actor, now=now
        )
        if outcome is not None:
            # The lookup attempt is retained either way, on the revision it
            # checked. Official facts are kept only for the requested NIN.
            retain = evaluation is not None and bool(evaluation.retained_family_name_latin)
            IdentityVerificationAttempt.objects.create(
                identity_identifier=identifier,
                registration=registration,
                method=IdentityVerificationMethod.EXTERNAL_SERVICE,
                provider_code=getattr(recheck.provider, "PROVIDER_CODE", "UNKNOWN")[:64],
                request_reference=(outcome.request_reference or "")[:64],
                result=_RESULT_FOR_REASON.get(
                    attempt_reason, IdentityVerificationResult.INCONCLUSIVE
                ),
                attempted_at=now,
                performed_by_user=actor,
                revision=revision,
                attempt_number=1,
                outcome=outcome.kind,
                reason_code=attempt_reason,
                detail_code=(outcome.detail or "")[:40],
                is_official_provider=bool(getattr(recheck.provider, "IS_OFFICIAL", False)),
                applied=True,
                http_status=outcome.http_status if isinstance(outcome.http_status, int) else None,
                duration_ms=recheck.duration_ms,
                comparison=dict(evaluation.comparison) if evaluation is not None else {},
                presume_flag=evaluation.presume if evaluation is not None else "",
                policy_basis=evaluation.policy_basis if evaluation is not None else "",
                official_family_name_latin=(
                    evaluation.retained_family_name_latin if retain else ""
                ),
                official_given_names_latin=(
                    evaluation.retained_given_names_latin if retain else ""
                ),
                official_birth_date=(
                    evaluation.retained_birth_date.isoformat()
                    if retain and evaluation.retained_birth_date
                    else ""
                ),
                official_birth_date_text=evaluation.retained_birth_date_text if retain else "",
            )
        if not api_source:
            IdentityVerificationAttempt.objects.create(
                identity_identifier=identifier,
                registration=registration,
                method=IdentityVerificationMethod.MANUAL_DOCUMENT,
                provider_code="STAFF",
                result=IdentityVerificationResult.MATCH,
                attempted_at=now,
                performed_by_user=actor,
                revision=revision,
                outcome="MANUAL",
                evidence_document=document,
                applied=True,
            )
        type(document).objects.filter(pk=document.pk).update(
            verified_at=now, verified_by_user=actor
        )
        _decision(
            locked,
            action=IdentityDecisionAction.CORRECT_AND_VERIFY,
            from_status=from_status,
            actor_user=actor,
            reason_code=reason_code,
            note=note,
            items=changed,
            evidence=document,
            changes_encrypted=_changes_payload(before, after, changed) if changed else "",
        )
        recheck_label = outcome.kind if outcome is not None else recheck.skipped
        _audit(
            action_codes.IDENTITY_CORRECTED_AND_VERIFIED,
            verification=locked,
            actor_user=actor,
            reason_code=reason_code,
            after={
                "status": locked.status,
                "source": source,
                # Field names only; the values stay in the encrypted decision.
                "changed": changed,
                "revision": revision.number,
                "ministry_recheck": recheck_label,
                "ministry_reason": attempt_reason,
            },
            correlation_id=correlation_id,
        )
        from apps.reviews.intake import EntryTrigger, enqueue_for_participation_review

        enqueue_for_participation_review(
            registration.pk,
            trigger=(
                EntryTrigger.IDENTITY_API_VERIFIED
                if api_source
                else EntryTrigger.IDENTITY_MANUALLY_VERIFIED
            ),
            actor=actor,
            correlation_id=correlation_id,
        )
    return locked


def reject_identity(
    verification_id,
    *,
    actor,
    expected_version: int,
    reason_code: str,
    note: str,
    confirmed: bool,
    correlation_id: str = "",
) -> IdentityVerification:
    """The final identity rejection: a separate, reasoned, deliberately
    confirmed action (IDV-09).

    Owner decision IDV-Q2 (2026-10-02): it has a participant-visible outcome.
    In the same transaction the registration becomes NOT_APPROVED through a
    participation decision of its own (`record_identity_rejection_outcome`,
    which supersedes an earlier approval too), the context is released so the
    participant can register again from the same account, and one idempotent
    notification is queued. The two histories stay separate: this
    `IdentityDecision` holds the identity reason and note, the
    `RegistrationDecision` only the coded outcome. Unlike a return for
    correction (remediable on the same registration), this is final: the
    registration cannot be reopened.
    """
    from apps.reviews.services import record_identity_rejection_outcome

    verification = _load(verification_id)
    require_identity_permission(actor, "reject_identity", verification)
    _require_reason(reason_code, REJECT_REASONS)
    note = _clean_note(note)
    if len(note) < MIN_EXPLANATION_LENGTH:
        raise InvalidIdentityInput("explanation_required")
    if confirmed is not True:
        raise InvalidIdentityInput("confirmation_required")

    with transaction.atomic():
        registration, locked = _lock_case(verification_id, expected_version)
        _require_active(registration)  # R-IDV-06: never on a closed registration
        if locked.status not in (
            IdentityStatus.MANUAL_REVIEW,
            IdentityStatus.RETURNED_FOR_CORRECTION,
        ):
            raise IdentityStateError("not_rejectable")
        from_status = _transition(locked, status=IdentityStatus.REJECTED, actor=actor)
        discard_open_jobs(locked)
        decision = _decision(
            locked,
            action=IdentityDecisionAction.REJECT,
            from_status=from_status,
            actor_user=actor,
            reason_code=reason_code,
            note=note,
        )
        _audit(
            action_codes.IDENTITY_REJECTED,
            verification=locked,
            actor_user=actor,
            reason_code=reason_code,
            after={"status": locked.status, "registration_outcome": "NOT_APPROVED"},
            correlation_id=correlation_id,
        )
        # IDV-Q2: the participation outcome, in this transaction (the
        # Registration row is already locked; lock order unchanged). A
        # correction request still open is withdrawn with it.
        record_identity_rejection_outcome(
            registration=registration,
            decided_by=actor,
            identity_decision_id=decision.pk,
            correlation_id=correlation_id,
        )
    return locked


# ---------------------------------------------------------------------------
# Participant resubmission (same account, same registration)
# ---------------------------------------------------------------------------


def _resubmit_exemption_document(
    locked, *, current, person, document_number, document_expires_at, changed, correlation_id
):
    """The declared document of a NIN exemption case, corrected (IDV-Q3).

    Same rules as the identity step: an Algerian document of the declared
    kind, a printed number, an expiry date in the future (required for a
    passport). A changed number replaces the identifier for the person and
    rebinds the person's other cases that use it (R-IDV-01), exactly like a
    passport correction. Returns the identifier to bind."""
    from django.core.exceptions import ValidationError

    from apps.people.services import create_identity_identifier
    from apps.people.services.nin_exemption import normalize_document_number

    try:
        number = normalize_document_number(document_number)
    except ValidationError:
        raise InvalidIdentityInput("document_details") from None
    today = timezone.now().date()
    if current.identifier_type == IdentifierType.PASSPORT and document_expires_at is None:
        raise InvalidIdentityInput("document_expiry")
    if document_expires_at is not None and document_expires_at <= today:
        raise InvalidIdentityInput("document_expiry")
    identifier = current
    number_changed = number != current.value_encrypted
    if number_changed or current.expires_at != document_expires_at:
        # IDV-Q-C1: the case's identifier is bound, so a new expiry, like a new
        # number, replaces it; it is never changed in place.
        field = "document_number" if number_changed else "document_expiry"
        changed.append(field)
        siblings = lock_cases_sharing_identifier(current.pk, exclude_verification_id=locked.pk)
        lock_identifier_values(current.identifier_type, current.country_code_id, [number])
        current.status = IdentifierStatus.REPLACED
        current.save(update_fields=["status", "updated_at"])
        identifier = create_identity_identifier(
            person=person,
            identifier_type=current.identifier_type,
            country_code_id=current.country_code_id,
            raw_value=number,
        )
        identifier.expires_at = document_expires_at
        identifier.save(update_fields=["expires_at", "updated_at"])
        rebind_cases_after_replacement(
            siblings,
            replacement=identifier,
            changed_field=field,
            origin=locked,
            actor_person=person,
            correlation_id=correlation_id,
        )
    return identifier


def _save_exemption_image(registration, person, identifier, uploaded_file) -> None:
    """The photo of the declared document, through the existing private,
    scanned document path (IDV-Q3)."""
    from apps.documents.services import save_national_id_card, save_passport_identity_page

    if identifier.identifier_type == IdentifierType.NATIONAL_ID_CARD:
        save_national_id_card(registration=registration, person=person, uploaded_file=uploaded_file)
    else:
        save_passport_identity_page(
            registration=registration,
            person=person,
            uploaded_file=uploaded_file,
            allow_algerian_exception=True,
        )


@dataclass(frozen=True)
class CorrectionRequest:
    """The open correction request a participant answers. `participant_note`
    is the team's note written for this request (UPD-03), "" when none."""

    verification: IdentityVerification
    items: tuple[str, ...]
    labels: tuple[str, ...]
    participant_note: str = ""


def open_correction_request(registration) -> CorrectionRequest | None:
    """The correction the participant may answer now, or None: the identity is
    returned for correction AND the registration (as stored, not as held in
    memory) still awaits the participant (R-IDV-06)."""
    from apps.registrations.models import RegistrationPublicStatus

    verification = IdentityVerification.objects.filter(
        registration_id=registration.pk,
        registration__public_status=RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
        status=IdentityStatus.RETURNED_FOR_CORRECTION,
    ).first()
    if verification is None:
        return None
    decision = (
        verification.decisions.filter(action=IdentityDecisionAction.RETURN_FOR_CORRECTION)
        .order_by("-created_at")
        .first()
    )
    items = tuple(decision.requested_items) if decision is not None else ()
    catalogue = RETURN_ITEMS[verification.route]
    return CorrectionRequest(
        verification=verification,
        items=items,
        labels=tuple(str(catalogue[item]) for item in items if item in catalogue),
        participant_note=decision.participant_note_encrypted if decision is not None else "",
    )


def resubmit_identity_correction(
    registration,
    *,
    person,
    expected_version: int,
    given_names: str,
    family_name: str,
    date_of_birth,
    nin_value: str = "",
    passport_number: str = "",
    passport_country_code_id: str = "",
    passport_expires_at=None,
    national_id_card_file=None,
    passport_page_file=None,
    document_number: str = "",
    document_expires_at=None,
    document_file=None,
    correlation_id: str = "",
) -> IdentityVerification:
    """The participant corrects the returned identity and resubmits it.

    Same account, same Registration: a new identity revision, the previous
    ones kept. A changed NIN, name or birth date on the NIN route starts a new
    ministry check; anything else (only evidence supplied, or the passport
    route) goes back to manual review. On the NIN exemption route (IDV-Q3)
    the declared document's number, expiry date and image are corrected
    instead, and the case always goes back to manual documentary review. The
    participant is never told about a duplicate or a provider result. Raises
    `InvalidIdentityInput` for a value the wizard rules refuse,
    `EvidenceUnavailable` when a requested document is still missing.
    """
    from apps.core.text_rules import TextRuleError, normalize_latin_name, normalize_nin
    from apps.documents.services import (
        active_identity_evidence,
        is_reviewable_evidence,
        save_national_id_card,
        save_passport_identity_page,
    )
    from apps.people.services import create_identity_identifier
    from apps.registrations.models import (
        RegistrationInternalStatus,
        RegistrationProfile,
        RegistrationPublicStatus,
        RegistrationSubmissionKind,
    )
    from apps.registrations.services import (
        _require_selectable_country,
        age_problem,
        build_registration_snapshot,
        record_registration_submission,
    )

    if registration.person_id != getattr(person, "pk", None):
        raise IdentityStateError("not_owner")
    try:
        given_names = normalize_latin_name(given_names)
        family_name = normalize_latin_name(family_name)
    except TextRuleError:
        raise InvalidIdentityInput("names") from None
    if date_of_birth is None or age_problem(date_of_birth, registration.event_edition) is not None:
        raise InvalidIdentityInput("birth_date")

    verification_id = (
        IdentityVerification.objects.filter(registration=registration)
        .values_list("pk", flat=True)
        .first()
    )
    if verification_id is None:
        raise IdentityStateError("no_identity_case")

    with transaction.atomic():
        locked_registration, locked = _lock_case(verification_id, expected_version)
        if (
            locked_registration.public_status
            != RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED
        ):
            # R-IDV-06: a withdrawn, cancelled or otherwise closed
            # registration is never resubmitted (nor reopened) here.
            raise IdentityStateError("registration_not_awaiting_correction")
        if locked.status != IdentityStatus.RETURNED_FOR_CORRECTION:
            raise IdentityStateError("not_returned")
        request = open_correction_request(locked_registration)
        items = set(request.items if request else ())
        profile = RegistrationProfile.objects.get(registration=locked_registration)
        current = locked.current_revision.identifier
        changed: list[str] = []
        if given_names != profile.submitted_given_names:
            changed.append("given_names")
        if family_name != profile.submitted_family_name:
            changed.append("family_name")
        if date_of_birth != profile.date_of_birth:
            changed.append("date_of_birth")
        identifier = current

        if locked.route == IdentityRoute.NIN:
            try:
                nin = normalize_nin(nin_value)
            except TextRuleError:
                raise InvalidIdentityInput("nin_format") from None
            if nin != current.value_encrypted:
                changed.append("nin")
                siblings = lock_cases_sharing_identifier(
                    current.pk, exclude_verification_id=locked.pk
                )
                lock_identifier_values(
                    IdentifierType.NIN, ALGERIA_COUNTRY_CODE, [current.value_encrypted, nin]
                )
                current.status = IdentifierStatus.REPLACED
                current.save(update_fields=["status", "updated_at"])
                identifier = create_identity_identifier(
                    person=person,
                    identifier_type=IdentifierType.NIN,
                    country_code_id=ALGERIA_COUNTRY_CODE,
                    raw_value=nin,
                )
                rebind_cases_after_replacement(
                    siblings,
                    replacement=identifier,
                    changed_field="nin",
                    origin=locked,
                    actor_person=person,
                    correlation_id=correlation_id,
                )
        elif locked.route == IdentityRoute.NIN_EXEMPTION:
            identifier = _resubmit_exemption_document(
                locked,
                current=current,
                person=person,
                document_number=document_number,
                document_expires_at=document_expires_at,
                changed=changed,
                correlation_id=correlation_id,
            )
        else:
            number = "".join((passport_number or "").split()).upper()
            if not number or passport_expires_at is None:
                raise InvalidIdentityInput("passport_details")
            if passport_expires_at <= timezone.now().date():
                raise InvalidIdentityInput("passport_expiry")
            try:
                _require_selectable_country(
                    passport_country_code_id, what="passport issuing country"
                )
            except Exception:  # noqa: BLE001 - a refused country is an input error
                raise InvalidIdentityInput("passport_country") from None
            number_changed = (
                number != current.value_encrypted
                or passport_country_code_id != current.country_code_id
            )
            if number_changed or current.expires_at != passport_expires_at:
                # IDV-Q-C1: the case's identifier is bound (shared with the
                # person's other cases), so a new expiry, like a new number,
                # replaces it and rebinds those cases explicitly; it is never
                # changed in place.
                field = "passport_number" if number_changed else "passport_expiry"
                changed.append(field)
                siblings = lock_cases_sharing_identifier(
                    current.pk, exclude_verification_id=locked.pk
                )
                lock_identifier_values(
                    IdentifierType.PASSPORT,
                    passport_country_code_id,
                    [number],
                )
                current.status = IdentifierStatus.REPLACED
                current.save(update_fields=["status", "updated_at"])
                identifier = create_identity_identifier(
                    person=person,
                    identifier_type=IdentifierType.PASSPORT,
                    country_code_id=passport_country_code_id,
                    raw_value=number,
                )
                identifier.expires_at = passport_expires_at
                identifier.save(update_fields=["expires_at", "updated_at"])
                rebind_cases_after_replacement(
                    siblings,
                    replacement=identifier,
                    changed_field=field,
                    origin=locked,
                    actor_person=person,
                    correlation_id=correlation_id,
                )

        if changed:
            profile.submitted_given_names = given_names
            profile.submitted_family_name = family_name
            profile.submitted_full_name = f"{given_names} {family_name}".strip()
            profile.date_of_birth = date_of_birth
            profile.save(
                update_fields=[
                    "submitted_given_names",
                    "submitted_family_name",
                    "submitted_full_name",
                    "date_of_birth",
                    "updated_at",
                ]
            )

        if national_id_card_file is not None:
            if locked.route != IdentityRoute.NIN:
                raise InvalidIdentityInput("evidence_type")
            save_national_id_card(
                registration=locked_registration, person=person, uploaded_file=national_id_card_file
            )
            changed.append("national_id_card")
        if document_file is not None:
            if locked.route != IdentityRoute.NIN_EXEMPTION:
                raise InvalidIdentityInput("evidence_type")
            _save_exemption_image(locked_registration, person, identifier, document_file)
            changed.append("identity_document")
        if passport_page_file is not None:
            if locked.route == IdentityRoute.NIN_EXEMPTION:
                raise InvalidIdentityInput("evidence_type")
            exception_page = locked.route == IdentityRoute.NIN
            if exception_page and "ALGERIAN_PASSPORT_PAGE" not in items:
                raise InvalidIdentityInput("evidence_type")
            save_passport_identity_page(
                registration=locked_registration,
                person=person,
                uploaded_file=passport_page_file,
                allow_algerian_exception=exception_page,
            )
            changed.append("passport_identity_page")

        evidence = active_identity_evidence(locked_registration)
        for item in items:
            wanted = EVIDENCE_ITEMS.get(item)
            if item == "DOCUMENT_IMAGE":
                from apps.people.services.nin_exemption import evidence_type_for

                wanted = evidence_type_for(identifier)
            if wanted and not any(
                doc.document_type == wanted and is_reviewable_evidence(doc) for doc in evidence
            ):
                raise EvidenceUnavailable(item)

        revision = create_revision(
            verification=locked,
            identifier=identifier,
            profile=profile,
            source=IdentityRevisionSource.PARTICIPANT_RESUBMISSION,
            changed_fields=changed,
            created_by_person=person,
        )
        locked.current_revision = revision
        lock_identifier_values(
            identifier.identifier_type, identifier.country_code_id, [identifier.value_encrypted]
        )
        conflict = find_cross_person_conflicts(
            identifier_type=identifier.identifier_type,
            country_code_id=identifier.country_code_id,
            raw_value=identifier.value_encrypted,
            person_id=person.pk,
        ).exists
        identity_changed = bool(
            {"nin", "given_names", "family_name", "date_of_birth"} & set(changed)
        )
        if conflict:
            new_status, reason = (
                IdentityStatus.MANUAL_REVIEW,
                IdentityReasonCode.DUPLICATE_IDENTIFIER,
            )
        elif locked.route == IdentityRoute.NIN and identity_changed:
            new_status, reason = IdentityStatus.PENDING, ""
        else:
            new_status, reason = (
                IdentityStatus.MANUAL_REVIEW,
                IdentityReasonCode.PARTICIPANT_RESUBMITTED,
            )
        from_status = _transition(locked, status=new_status, reason_code=reason)
        if new_status == IdentityStatus.PENDING:
            schedule_job(verification=locked, revision=revision)
        _set_registration_status(
            locked_registration,
            public_status=RegistrationPublicStatus.SUBMITTED,
            internal_status=RegistrationInternalStatus.VERIFICATION_PENDING,
        )
        record_registration_submission(
            registration=locked_registration,
            snapshot=build_registration_snapshot(locked_registration),
            submission_kind=RegistrationSubmissionKind.ADDITIONAL_INFORMATION_RESPONSE,
            idempotency_key=f"identity-correction:{locked.pk}:{revision.number}",
            correlation_id=correlation_id,
        )
        _decision(
            locked,
            action=IdentityDecisionAction.PARTICIPANT_RESUBMISSION,
            from_status=from_status,
            actor_person=person,
            items=sorted(items),
        )
        _audit(
            action_codes.IDENTITY_PARTICIPANT_RESUBMITTED,
            verification=locked,
            actor_person=person,
            after={"status": locked.status, "revision": revision.number, "changed": changed},
            correlation_id=correlation_id,
        )
        from apps.communications.purposes import CommunicationPurpose

        _notify(
            locked_registration,
            purpose_code=CommunicationPurpose.INFORMATION_RESPONSE_CONFIRMATION,
            idempotency_key=f"identity-correction-received:{locked.pk}:{revision.number}",
        )
    return locked
