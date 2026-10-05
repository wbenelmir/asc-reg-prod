"""Legal document, acceptance and consent separation tests (Schema §14)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.events.models import EventEdition
from apps.people.models import Person
from apps.privacy.models import (
    AcceptanceAction,
    AcceptanceRecord,
    AcceptanceSource,
    ConsentAction,
    ConsentPurpose,
    ConsentRecord,
    LegalDocument,
    LegalDocumentType,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def terms_version() -> LegalDocumentVersion:
    # privacy.0002 already seeds a LegalDocument with code="TERMS" -- reuse
    # it rather than colliding with the unique `code` constraint.
    document, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": LegalDocumentType.TERMS}
    )
    return LegalDocumentVersion.objects.create(
        legal_document=document,
        language="en",
        version_label="v1-test",
        content="Terms text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )


@pytest.fixture
def registration() -> Registration:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="PRVTEST", name="ASC 2026", timezone="UTC", starts_at=now, ends_at=now
    )
    return Registration.objects.create(
        public_reference="ASC2026-R-000001",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="ctx",
    )


def test_acceptance_and_consent_are_separate_tables(
    terms_version: LegalDocumentVersion, registration: Registration
) -> None:
    person = Person.objects.create(display_name="Consent Test")
    AcceptanceRecord.objects.create(
        person=person,
        registration=registration,
        legal_document_version=terms_version,
        action=AcceptanceAction.ACCEPTED,
        accepted_at=timezone.now(),
        language="en",
        source=AcceptanceSource.PUBLIC_WEB,
    )
    purpose, _ = ConsentPurpose.objects.get_or_create(
        code="MARKETING", defaults={"name": "Marketing communications"}
    )
    ConsentRecord.objects.create(
        person=person,
        purpose=purpose,
        action=ConsentAction.GRANTED,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
    )
    assert AcceptanceRecord.objects.filter(person=person).count() == 1
    assert ConsentRecord.objects.filter(person=person).count() == 1


def test_withdrawing_consent_never_rewrites_historical_acceptance(
    terms_version: LegalDocumentVersion, registration: Registration
) -> None:
    person = Person.objects.create(display_name="Withdraw Test")
    acceptance = AcceptanceRecord.objects.create(
        person=person,
        registration=registration,
        legal_document_version=terms_version,
        action=AcceptanceAction.ACCEPTED,
        accepted_at=timezone.now(),
        language="en",
        source=AcceptanceSource.PUBLIC_WEB,
    )
    purpose = ConsentPurpose.objects.create(code="NEWSLETTER", name="Newsletter")
    ConsentRecord.objects.create(
        person=person,
        purpose=purpose,
        action=ConsentAction.GRANTED,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
    )
    ConsentRecord.objects.create(
        person=person,
        purpose=purpose,
        action=ConsentAction.WITHDRAWN,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
    )
    acceptance.refresh_from_db()
    assert acceptance.action == AcceptanceAction.ACCEPTED
    # Consent history is append-only: both GRANTED and WITHDRAWN rows remain.
    assert ConsentRecord.objects.filter(person=person, purpose=purpose).count() == 2


def test_current_consent_is_derived_from_latest_record(
    terms_version: LegalDocumentVersion,
) -> None:
    person = Person.objects.create(display_name="Latest Consent Test")
    purpose = ConsentPurpose.objects.create(code="RESEARCH", name="Research")
    ConsentRecord.objects.create(
        person=person,
        purpose=purpose,
        action=ConsentAction.GRANTED,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
    )
    ConsentRecord.objects.create(
        person=person,
        purpose=purpose,
        action=ConsentAction.WITHDRAWN,
        source=AcceptanceSource.PUBLIC_WEB,
        occurred_at=timezone.now(),
    )
    latest = (
        ConsentRecord.objects.filter(person=person, purpose=purpose)
        .order_by("-occurred_at")
        .first()
    )
    assert latest.action == ConsentAction.WITHDRAWN
