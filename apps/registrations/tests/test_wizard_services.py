"""Universal registration wizard step-service tests (AF-REG-02..AF-REG-06)."""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.people.models import IdentifierStatus, IdentifierType, Person
from apps.privacy.models import (
    AcceptanceRecord,
    ConsentPurpose,
    ConsentRecord,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import (
    InterestTopic,
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
)
from apps.registrations.services import (
    get_or_create_active_draft,
    save_contact_step,
    save_identity_step,
    save_interests_step,
    save_professional_step,
    submit_full_registration,
)

from .factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_countries():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    from django.utils import timezone

    return EventEdition.objects.create(
        code="WIZTEST",
        name="Wizard Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def person() -> Person:
    """A Person with a verified, login-enabled email and an ACTIVE ParticipantAccount --
    the state `resolve_or_create_participant_for_email` always establishes before a Draft
    can exist in production, and a precondition `submit_full_registration` now enforces
    directly (Prompt 4 final closure pass §1)."""
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email("wizard-person@example.com")


@pytest.fixture
def draft(person: Person, event: EventEdition) -> Registration:
    return get_or_create_active_draft(person=person, event_edition=event)


def test_get_or_create_active_draft_resumes_existing_draft(
    person: Person, event: EventEdition
) -> None:
    first = get_or_create_active_draft(person=person, event_edition=event)
    second = get_or_create_active_draft(person=person, event_edition=event)
    assert first.pk == second.pk


def test_save_identity_step_only_declares_the_nin(draft: Registration) -> None:
    """IDV-2 (A13-14): the draft step never verifies; it declares the NIN, and
    verification starts after the final submission (the former stub MATCH in
    the draft step is superseded)."""
    save_identity_step(
        registration=draft,
        given_names="Amine",
        family_name="Benali",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value="123456789012345678",
    )
    identifier = draft.person.identity_identifiers.get(identifier_type=IdentifierType.NIN)
    assert identifier.status == IdentifierStatus.DECLARED
    assert not identifier.verification_attempts.exists()
    draft.refresh_from_db()
    assert draft.public_status == RegistrationPublicStatus.DRAFT
    assert draft.internal_status == RegistrationInternalStatus.PENDING_ASSIGNMENT


def test_cross_person_verified_nin_flags_duplicate_review_not_a_hard_block(
    event: EventEdition,
) -> None:
    from apps.people.services import create_identity_identifier

    person_a = Person.objects.create(display_name="Person A")
    person_b = Person.objects.create(display_name="Person B")
    draft_b = get_or_create_active_draft(person=person_b, event_edition=event)

    nin_value = "111111111111111112"
    # Person A already holds this NIN verified (synthetic, set directly).
    create_identity_identifier(
        person=person_a,
        identifier_type=IdentifierType.NIN,
        country_code_id="DZ",
        raw_value=nin_value,
        status=IdentifierStatus.VERIFIED,
    )
    # Second person declaring the SAME NIN never raises -- it is flagged for
    # review, never silently merged and never a hard failure for the applicant.
    save_identity_step(
        registration=draft_b,
        given_names="B",
        family_name="B",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value=nin_value,
    )
    draft_b.refresh_from_db()
    assert draft_b.internal_status == RegistrationInternalStatus.DUPLICATE_REVIEW
    # The second person's identifier must NOT have been marked verified,
    # since that would violate the cross-person verified-uniqueness invariant.
    identifier_b = person_b.identity_identifiers.get(identifier_type=IdentifierType.NIN)
    assert identifier_b.status != IdentifierStatus.VERIFIED


def test_save_contact_step_flags_duplicate_on_cross_person_verified_mobile(
    event: EventEdition,
) -> None:
    from apps.people.services import create_contact_point

    person_a = Person.objects.create(display_name="Mobile Owner")
    create_contact_point(
        person=person_a,
        contact_type="MOBILE",
        raw_value="+213555000111",
        country_code_id="DZ",
        is_verified=True,
    )
    person_b = Person.objects.create(display_name="Mobile Claimant")
    draft_b = get_or_create_active_draft(person=person_b, event_edition=event)

    save_contact_step(
        registration=draft_b, mobile_number="+213555000111", mobile_country_code_id="DZ"
    )
    draft_b.refresh_from_db()
    assert draft_b.internal_status == RegistrationInternalStatus.DUPLICATE_REVIEW


def test_save_professional_step_matches_existing_organization_by_normalized_name(
    draft: Registration,
) -> None:
    save_professional_step(
        registration=draft,
        organization_name="Acme Corp",
        job_title="Engineer",
        department="R&D",
        sector_code_id="TECH",
        country_code_id="DZ",
    )
    save_professional_step(
        registration=get_or_create_active_draft(
            person=Person.objects.create(display_name="Other"), event_edition=draft.event_edition
        ),
        organization_name="  acme corp  ",
        job_title="Manager",
        department="",
        sector_code_id=None,
        country_code_id=None,
    )
    from apps.organizations.models import Organization

    assert Organization.objects.filter(normalized_name="acme corp").count() == 1


def test_save_interests_step_never_assigns_a_badge_type(draft: Registration) -> None:
    topic = InterestTopic.objects.create(
        event_edition=draft.event_edition, code="FUNDING", label="Funding"
    )
    save_interests_step(
        registration=draft,
        interest_topic_ids=[topic.pk],
        objectives_text="Learn more",
    )
    draft.refresh_from_db()
    assert draft.interests.count() == 1
    assert not hasattr(draft, "badge_type")


@pytest.fixture
def legal_versions():
    # privacy.0002 already seeds LegalDocument rows with these codes -- reuse
    # them rather than colliding on the unique `code` constraint.
    privacy_doc, _ = LegalDocument.objects.get_or_create(
        code="PRIVACY_NOTICE", defaults={"document_type": "PRIVACY_NOTICE"}
    )
    terms_doc, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": "TERMS"}
    )

    privacy_version = LegalDocumentVersion.objects.create(
        legal_document=privacy_doc,
        language="en",
        version_label="v1",
        content="Privacy text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_version = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="v1",
        content="Terms text",
        content_hash="b" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    return privacy_version, terms_version


def test_submit_full_registration_records_both_acceptance_records_separately(
    draft: Registration, legal_versions
) -> None:
    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    submission = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=privacy_version
        ).count()
        == 1
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=terms_version
        ).count()
        == 1
    )
    draft.refresh_from_db()
    assert draft.public_status == RegistrationPublicStatus.SUBMITTED
    assert submission.registration_id == draft.pk


def test_submit_full_registration_is_idempotent_on_retry(
    draft: Registration, legal_versions
) -> None:
    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    idempotency_key = str(uuid.uuid4())
    first = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=idempotency_key,
    )
    draft.refresh_from_db()  # SUBMITTED now, idempotency short-circuits before the guard
    second = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=idempotency_key,
    )
    assert first.pk == second.pk
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=privacy_version
        ).count()
        == 1
    )


def test_submit_full_registration_optional_consent_is_separate_from_required_acceptance(
    draft: Registration, legal_versions
) -> None:
    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    purpose, _ = ConsentPurpose.objects.get_or_create(
        code="MARKETING", defaults={"name": "Marketing"}
    )
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        marketing_consent_purpose=purpose,
        marketing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    assert (
        ConsentRecord.objects.filter(person=draft.person, purpose=purpose, action="GRANTED").count()
        == 1
    )
    # Withdrawing later never rewrites the accepted Terms/Privacy history.
    ConsentRecord.objects.create(
        person=draft.person,
        purpose=purpose,
        action="WITHDRAWN",
        source="PUBLIC_WEB",
        occurred_at=timezone.now(),
    )
    assert AcceptanceRecord.objects.filter(registration=draft).count() == 2


# ---------------------------------------------------------------------------
# Prompt 6 P6-H-02: two concurrent initial-submission calls for the SAME
# Registration must converge on one result even when they supplied
# DIFFERENT idempotency keys -- exactly what the pre-fix session-cached-
# random-key race could produce for two racing requests.
# ---------------------------------------------------------------------------


def test_submit_full_registration_converges_on_one_result_despite_different_idempotency_keys(
    draft: Registration, legal_versions
) -> None:
    from apps.audit.models import AuditEvent
    from apps.core.models import OutboxEvent
    from apps.registrations.models import RegistrationSubmission, RegistrationSubmissionKind

    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    purpose, _ = ConsentPurpose.objects.get_or_create(
        code="MARKETING", defaults={"name": "Marketing"}
    )

    first = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        marketing_consent_purpose=purpose,
        marketing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    draft.refresh_from_db()
    second = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        marketing_consent_purpose=purpose,
        marketing_consent_granted=True,
        session_reference="sess-2",
        idempotency_key=str(uuid.uuid4()),  # a DIFFERENT key from the first call
    )

    assert first.pk == second.pk
    assert (
        RegistrationSubmission.objects.filter(
            registration=draft, submission_kind=RegistrationSubmissionKind.INITIAL
        ).count()
        == 1
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=privacy_version
        ).count()
        == 1
    )
    assert (
        AcceptanceRecord.objects.filter(
            registration=draft, legal_document_version=terms_version
        ).count()
        == 1
    )
    assert ConsentRecord.objects.filter(person=draft.person, purpose=purpose).count() == 1
    assert (
        AuditEvent.objects.filter(
            action_code="REGISTRATION_SUBMISSION_RECORDED", target_uuid=draft.pk, result="SUCCESS"
        ).count()
        == 1
    )
    assert (
        OutboxEvent.objects.filter(
            event_type="registration.submission_recorded", aggregate_id=str(draft.pk)
        ).count()
        == 1
    )


def test_idempotency_key_collision_with_another_registration_fails_safely(
    draft: Registration, event: EventEdition, legal_versions
) -> None:
    """A key already bound to a DIFFERENT Registration must never be treated
    as an idempotent retry -- it must never return or expose that other
    Registration's submission (Prompt 6 P6-H-02)."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import RegistrationSubmission
    from apps.registrations.services import (
        IdempotencyKeyConflictError,
        get_or_create_active_draft,
        record_registration_submission,
    )

    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    shared_key = str(uuid.uuid4())
    first = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=shared_key,
    )

    other_person = resolve_or_create_participant_for_email("other-registration@example.com")
    other_draft = get_or_create_active_draft(person=other_person, event_edition=event)
    # `record_registration_submission` is exercised directly here -- it has
    # no completeness precondition of its own (that guard lives in
    # `submit_full_registration`), so `other_draft` need not be walked
    # through every step just to prove the key-collision rejection.

    with pytest.raises(IdempotencyKeyConflictError):
        record_registration_submission(
            registration=other_draft,
            snapshot={"public_reference": other_draft.public_reference},
            idempotency_key=shared_key,  # already bound to `draft`, not `other_draft`
        )
    # The foreign key collision never created (or exposed) a submission for
    # `other_draft`.
    assert not RegistrationSubmission.objects.filter(registration=other_draft).exists()
    assert first.registration_id == draft.pk


def test_non_initial_submission_kinds_remain_possible_after_the_initial_one(
    draft: Registration, legal_versions
) -> None:
    """The "at most one INITIAL" rule applies specifically to `INITIAL` --
    later, explicitly authorized submission kinds must remain possible
    (Prompt 6 P6-H-02)."""
    from apps.registrations.models import RegistrationSubmissionKind
    from apps.registrations.services import record_registration_submission

    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    draft.refresh_from_db()

    later = record_registration_submission(
        registration=draft,
        snapshot={"public_reference": draft.public_reference},
        submission_kind=RegistrationSubmissionKind.ADDITIONAL_INFORMATION_RESPONSE,
        idempotency_key=str(uuid.uuid4()),
    )
    assert later.submission_kind == RegistrationSubmissionKind.ADDITIONAL_INFORMATION_RESPONSE
    assert later.sequence == 2


def test_foreign_idempotency_key_collision_raises_even_when_target_already_has_an_initial(
    draft: Registration, legal_versions
) -> None:
    """The foreign-key-collision check must run BEFORE the existing-INITIAL
    recheck (Prompt 6 P6-H-01/P6-H-02 follow-up) -- otherwise a genuine key
    mix-up would be silently absorbed whenever the TARGET Registration
    already happens to have its own INITIAL submission, since the function
    would return that instead of ever detecting the collision."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import RegistrationSubmission, RegistrationSubmissionKind
    from apps.registrations.services import (
        IdempotencyKeyConflictError,
        get_or_create_active_draft,
        record_registration_submission,
    )

    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    own_submission = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-own",
        idempotency_key=str(uuid.uuid4()),
    )

    # A separate EventEdition for `other_draft`: `walk_draft_through_every_step`
    # unconditionally creates a new InterestTopic, which would collide with
    # `draft`'s own topic under the SAME event.
    other_event = EventEdition.objects.create(
        code="WIZTEST2",
        name="Wizard Test 2",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    other_person = resolve_or_create_participant_for_email("other-collision@example.com")
    other_draft = get_or_create_active_draft(person=other_person, event_edition=other_event)
    walk_draft_through_every_step(other_draft)
    other_key = str(uuid.uuid4())
    other_submission = submit_full_registration(
        registration=other_draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-other",
        idempotency_key=other_key,
    )

    draft.refresh_from_db()
    with pytest.raises(IdempotencyKeyConflictError):
        record_registration_submission(
            registration=draft,  # already has its OWN INITIAL submission
            snapshot={"public_reference": draft.public_reference},
            idempotency_key=other_key,  # belongs to `other_draft`, not `draft`
        )

    # Neither Registration's submission was disturbed, replaced, or exposed.
    assert (
        RegistrationSubmission.objects.get(
            registration=draft, submission_kind=RegistrationSubmissionKind.INITIAL
        ).pk
        == own_submission.pk
    )
    assert (
        RegistrationSubmission.objects.get(
            registration=other_draft, submission_kind=RegistrationSubmissionKind.INITIAL
        ).pk
        == other_submission.pk
    )


def test_unused_different_key_for_the_same_registration_returns_its_existing_initial(
    draft: Registration, legal_versions
) -> None:
    from apps.registrations.models import RegistrationSubmission, RegistrationSubmissionKind
    from apps.registrations.services import record_registration_submission

    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    first = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    draft.refresh_from_db()

    result = record_registration_submission(
        registration=draft,
        snapshot={"public_reference": draft.public_reference},
        idempotency_key=str(uuid.uuid4()),  # never used before, for the SAME registration
    )
    assert result.pk == first.pk
    assert (
        RegistrationSubmission.objects.filter(
            registration=draft, submission_kind=RegistrationSubmissionKind.INITIAL
        ).count()
        == 1
    )


def test_no_person_event_uniqueness_multiple_registration_contexts_allowed(
    person: Person, event: EventEdition
) -> None:
    from apps.registrations.services import create_open_draft_registration

    first = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx-a",
        preferred_language="en",
        person_id=person.id,
    )
    second = create_open_draft_registration(
        event_edition=event,
        source_context_key="ctx-b",
        preferred_language="en",
        person_id=person.id,
    )
    assert Registration.objects.filter(person=person, event_edition=event).count() == 2
    assert first.registration.pk != second.registration.pk
