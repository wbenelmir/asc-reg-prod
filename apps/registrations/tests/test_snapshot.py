"""Immutable submission snapshot and legal-version selection tests
(Prompt 4 final closure pass §7)."""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus
from apps.privacy.selectors import (
    effective_published_version,
    effective_published_version_with_fallback,
)
from apps.registrations.services import get_or_create_active_draft, submit_full_registration

from .factories import DEFAULT_NIN, walk_draft_through_every_step

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="SNAPTEST",
        name="Snapshot Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def draft(event: EventEdition):
    person = resolve_or_create_participant_for_email("snapshot@example.com")
    return get_or_create_active_draft(person=person, event_edition=event)


@pytest.fixture
def french_privacy_and_english_terms():
    """Only a FRENCH Privacy Notice and an ENGLISH Terms version are published --
    forces the fallback path for one document but not the other."""
    privacy_doc, _ = LegalDocument.objects.get_or_create(
        code="PRIVACY_NOTICE", defaults={"document_type": "PRIVACY_NOTICE"}
    )
    terms_doc, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": "TERMS"}
    )
    privacy_fr = LegalDocumentVersion.objects.create(
        legal_document=privacy_doc,
        language="fr",
        version_label="snap-fr",
        content="Texte de confidentialite",
        content_hash="c" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_en = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="snap-en",
        content="Terms text",
        content_hash="d" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    return privacy_fr, terms_en


def test_snapshot_never_contains_the_clear_nin_value(draft) -> None:
    walk_draft_through_every_step(draft)
    from apps.registrations.services import build_registration_snapshot

    snapshot = build_registration_snapshot(draft)
    serialized = str(snapshot)
    assert DEFAULT_NIN not in serialized
    assert snapshot["identity"]["evidence"][0]["masked_value"].startswith("***")


def test_snapshot_never_contains_the_clear_mobile_number(draft) -> None:
    walk_draft_through_every_step(draft)
    from apps.registrations.services import build_registration_snapshot

    snapshot = build_registration_snapshot(draft)
    assert "+213551234567" not in str(snapshot)
    assert snapshot["mobile"]["masked_value"].startswith("***")


def test_snapshot_never_contains_a_storage_key_or_private_url(draft) -> None:
    walk_draft_through_every_step(draft)
    from apps.documents.services import active_profile_photo
    from apps.registrations.services import build_registration_snapshot

    snapshot = build_registration_snapshot(draft)
    photo = active_profile_photo(draft)
    assert photo.stored_object.storage_key not in str(snapshot)
    assert snapshot["profile_photo"]["document_id"] == str(photo.pk)
    assert snapshot["profile_photo"]["scan_status"] == "CLEAN"


def test_snapshot_contains_complete_professional_fields(draft) -> None:
    walk_draft_through_every_step(draft)
    from apps.registrations.services import build_registration_snapshot

    snapshot = build_registration_snapshot(draft)
    professional = snapshot["professional"]
    assert professional["organization_type"] == "COMPANY"
    assert professional["organization_website"]
    assert professional["professional_profile_url"]
    assert professional["biography"]


def test_english_fallback_is_recorded_as_english_not_the_participant_preference(
    draft, french_privacy_and_english_terms
) -> None:
    """The participant's registration is in French, but only an English Terms
    version is published -- the recorded acceptance language must be "en", never
    silently "fr" (Prompt 4 final closure pass §7)."""
    walk_draft_through_every_step(draft)
    draft.preferred_language = "fr"
    draft.save(update_fields=["preferred_language"])
    privacy_version, terms_version = french_privacy_and_english_terms
    assert privacy_version.language == "fr"
    assert terms_version.language == "en"  # the only published Terms version

    submission = submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess-1",
        idempotency_key=str(uuid.uuid4()),
    )
    from apps.privacy.models import AcceptanceRecord

    terms_acceptance = AcceptanceRecord.objects.get(
        registration=draft, legal_document_version=terms_version
    )
    assert terms_acceptance.language == "en"
    privacy_acceptance = AcceptanceRecord.objects.get(
        registration=draft, legal_document_version=privacy_version
    )
    assert privacy_acceptance.language == "fr"
    assert submission.snapshot_json["legal_acceptance"]["terms"]["language"] == "en"
    assert submission.snapshot_json["legal_acceptance"]["privacy_notice"]["language"] == "fr"


def test_effective_published_version_ignores_a_not_yet_effective_version() -> None:
    document = LegalDocument.objects.create(code="FUTURETEST", document_type="OTHER")
    LegalDocumentVersion.objects.create(
        legal_document=document,
        language="en",
        version_label="v1",
        content="not yet effective",
        content_hash="e" * 64,
        effective_from=timezone.now() + timezone.timedelta(days=1),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    assert effective_published_version("FUTURETEST", "en") is None


def test_effective_published_version_ignores_an_expired_version() -> None:
    document = LegalDocument.objects.create(code="EXPIREDTEST", document_type="OTHER")
    LegalDocumentVersion.objects.create(
        legal_document=document,
        language="en",
        version_label="v1",
        content="expired",
        content_hash="f" * 64,
        effective_from=timezone.now() - timezone.timedelta(days=30),
        effective_until=timezone.now() - timezone.timedelta(days=1),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    assert effective_published_version("EXPIREDTEST", "en") is None


def test_effective_published_version_ignores_a_draft_version() -> None:
    document = LegalDocument.objects.create(code="DRAFTTEST", document_type="OTHER")
    LegalDocumentVersion.objects.create(
        legal_document=document,
        language="en",
        version_label="v1",
        content="still a draft",
        content_hash="1" * 64,
        effective_from=timezone.now() - timezone.timedelta(days=1),
        status=LegalDocumentVersionStatus.DRAFT,
    )
    assert effective_published_version("DRAFTTEST", "en") is None


def test_fallback_result_carries_its_own_actual_language_not_the_request() -> None:
    document = LegalDocument.objects.create(code="FALLBACKTEST", document_type="OTHER")
    english = LegalDocumentVersion.objects.create(
        legal_document=document,
        language="en",
        version_label="v1",
        content="english only",
        content_hash="2" * 64,
        effective_from=timezone.now() - timezone.timedelta(days=1),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    resolved = effective_published_version_with_fallback("FALLBACKTEST", "ar")
    assert resolved.pk == english.pk
    assert resolved.language == "en"  # caller must read .language, not assume "ar"
