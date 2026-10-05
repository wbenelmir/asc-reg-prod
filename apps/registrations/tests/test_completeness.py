"""Submission completeness guard tests (Prompt 4 final closure pass §1 -- BLOCKER).

`submit_full_registration` must reject an incomplete Draft even when a
caller bypasses the wizard views entirely -- these tests call the service
layer directly, deliberately skipping steps, to prove the guard runs
regardless of how the caller reached the Draft.
"""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.people.models import ParticipantAccountStatus
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    AcceptanceRecord,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import RegistrationSubmission
from apps.registrations.services import (
    IncompleteRegistrationError,
    get_or_create_active_draft,
    submit_full_registration,
)

from .factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="COMPTEST",
        name="Completeness Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def person():
    return resolve_or_create_participant_for_email("completeness@example.com")


@pytest.fixture
def draft(person, event):
    return get_or_create_active_draft(person=person, event_edition=event)


@pytest.fixture
def legal_versions():
    privacy_doc, _ = LegalDocument.objects.get_or_create(
        code="PRIVACY_NOTICE", defaults={"document_type": "PRIVACY_NOTICE"}
    )
    terms_doc, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": "TERMS"}
    )
    privacy_version = LegalDocumentVersion.objects.create(
        legal_document=privacy_doc,
        language="en",
        version_label="completeness-v1",
        content="Privacy text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_version = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="completeness-v1",
        content="Terms text",
        content_hash="b" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    return privacy_version, terms_version


def _submit(draft, legal_versions):
    privacy_version, terms_version = legal_versions
    return submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )


def _assert_nothing_was_created(draft) -> None:
    """No acceptance record, submission row, or public-status change may exist
    when completeness validation fails (Prompt 4 final closure pass §1)."""
    draft.refresh_from_db()
    assert draft.public_status == "DRAFT"
    assert RegistrationSubmission.objects.filter(registration=draft).count() == 0
    assert AcceptanceRecord.objects.filter(registration=draft).count() == 0


def test_positive_complete_draft_submits_successfully(draft, legal_versions) -> None:
    walk_draft_through_every_step(draft)
    submission = _submit(draft, legal_versions)
    assert submission is not None
    draft.refresh_from_db()
    assert draft.public_status == "SUBMITTED"


def test_negative_missing_identity_step_is_rejected(draft, legal_versions) -> None:
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "identity"
    _assert_nothing_was_created(draft)


def test_negative_missing_contact_step_is_rejected_for_algerian_nationality(
    draft, legal_versions
) -> None:
    import datetime

    from apps.registrations.services import save_identity_step

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
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "contact"
    _assert_nothing_was_created(draft)


def test_negative_missing_professional_step_is_rejected(draft, legal_versions) -> None:
    import datetime

    from apps.registrations.services import save_contact_step, save_identity_step

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
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "professional"
    _assert_nothing_was_created(draft)


def test_negative_missing_profile_photo_is_rejected(draft, legal_versions) -> None:
    import datetime

    from apps.organizations.models import ProfessionalAffiliation
    from apps.organizations.services import match_or_create_organization
    from apps.registrations.services import save_contact_step, save_identity_step

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
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    # Every professional field filled in EXCEPT the photo -- completeness must
    # still fail at "professional", never silently accept a missing photo.
    ProfessionalAffiliation.objects.create(
        registration=draft,
        organization=match_or_create_organization("Acme Corp", country_code_id="DZ"),
        submitted_organization_name="Acme Corp",
        organization_type="COMPANY",
        job_title="Engineer",
        department="R&D",
        sector_id="TECH",
        country_code_id="DZ",
        organization_website="https://acme.example",
        professional_profile_url="https://linkedin.example/in/amine",
        biography="A bio.",
    )
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "professional"
    _assert_nothing_was_created(draft)


def test_negative_missing_interests_step_is_rejected(draft, legal_versions) -> None:
    import datetime

    from apps.documents.tests.factories import make_test_photo
    from apps.registrations.services import (
        save_contact_step,
        save_identity_step,
        save_professional_step,
    )

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
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    save_professional_step(
        registration=draft,
        organization_name="Acme Corp",
        organization_type="COMPANY",
        job_title="Engineer",
        department="R&D",
        sector_code_id="TECH",
        country_code_id="DZ",
        organization_website="https://acme.example",
        professional_profile_url="https://linkedin.example/in/amine",
        biography="A bio.",
        profile_photo_file=make_test_photo(),
        operating_scope="NATIONAL",
    )
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "interests"
    _assert_nothing_was_created(draft)


def test_negative_missing_or_unpublished_legal_version_is_rejected(draft, legal_versions) -> None:
    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    terms_version.status = LegalDocumentVersionStatus.DRAFT
    terms_version.save(update_fields=["status"])
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        submit_full_registration(
            registration=draft,
            privacy_notice_version=privacy_version,
            terms_version=terms_version,
            data_processing_consent_granted=True,
            session_reference="sess-1",
            idempotency_key=str(uuid.uuid4()),
        )
    assert excinfo.value.step == "notices"
    _assert_nothing_was_created(draft)


def test_negative_swapped_legal_document_types_are_rejected(draft, legal_versions) -> None:
    """The service boundary must distinguish Terms from the Privacy Notice."""
    walk_draft_through_every_step(draft)
    privacy_version, terms_version = legal_versions
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        submit_full_registration(
            registration=draft,
            privacy_notice_version=terms_version,
            terms_version=privacy_version,
            data_processing_consent_granted=True,
            session_reference="sess-1",
            idempotency_key=str(uuid.uuid4()),
        )
    assert excinfo.value.step == "notices"
    _assert_nothing_was_created(draft)


def test_negative_inactive_participant_account_is_rejected(draft, legal_versions, person) -> None:
    walk_draft_through_every_step(draft)
    person.participant_account.status = ParticipantAccountStatus.DISABLED
    person.participant_account.save(update_fields=["status"])
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft, legal_versions)
    assert excinfo.value.step == "account"
    _assert_nothing_was_created(draft)
