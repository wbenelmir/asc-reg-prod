"""Shared fixtures for the identity verification tests (IDV-1 to IDV-4).

Synthetic data only: every NIN, name, date, address and document image is
invented. NINs starting with 99 are answered by the development simulation
(`apps/people/fixtures/identity_simulation.json`); any other NIN is
NOT_FOUND there.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.test import Client
from django.utils import timezone

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105

SIM_MATCH_NIN = "990000000000000010"  # BENTEST / AMINA / 07/03/1990, presume False
SIM_PRESUMED_NIN = "990000000000000028"  # OUZTEST / KARIM, presume True
SIM_NAME_MISMATCH_NIN = "990000000000000036"  # BENTESTI / SAMIR / 15/06/1992
SIM_LEADING_ZERO_NIN = "009900000000000044"  # ZEROTEST / LINA / 20/11/1988
SIM_OTHER_NIN_RETURNED = "990000000000000052"  # answers for ...060
SIM_ISO_DATE_NIN = "990000000000000079"  # DATETEST / YACINE, d_nais "1990-03-07"
SIM_NO_PRESUME_NIN = "990000000000000087"  # FLAGTEST / SARA, no presume key
UNKNOWN_NIN = "123456789012345678"  # NOT_FOUND in the simulation


@pytest.fixture(autouse=True)
def _isolated_private_storage_root(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture(autouse=True)
def _forget_cached_tokens():
    from apps.people.nin_provider import clear_token_cache

    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def idv_reference_data(db):
    from apps.core.models import Country, Sector

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


def make_event(code: str = "IDVTEST"):
    from apps.events.models import EventEdition

    return EventEdition.objects.create(
        code=code,
        name=f"Identity test {code}",
        timezone="UTC",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        ends_at=timezone.now() + datetime.timedelta(days=32),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def idv_event(idv_reference_data):
    return make_event()


@pytest.fixture
def legal_versions(db):
    from apps.privacy.models import (
        LegalDocument,
        LegalDocumentVersion,
        LegalDocumentVersionStatus,
    )

    versions = []
    for code, label in (("PRIVACY_NOTICE", "p"), ("TERMS", "t")):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        version = (
            LegalDocumentVersion.objects.filter(
                legal_document=document, status=LegalDocumentVersionStatus.PUBLISHED
            )
            .order_by("-effective_from")
            .first()
        )
        if version is None:
            version = LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-{label}",
                content="Synthetic text",
                content_hash=label * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        versions.append(version)
    return tuple(versions)


def submit_case(
    event,
    legal_versions,
    *,
    email: str | None = None,
    nationality: str = "DZ",
    nin: str = SIM_MATCH_NIN,
    given: str = "Amina",
    family: str = "Bentest",
    birth: datetime.date = datetime.date(1990, 3, 7),
    passport_number: str = "X1234567",
    passport_country: str = "FR",
    person=None,
):
    """Walk a draft through every step with the given identity and submit it."""
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import Registration
    from apps.registrations.services import (
        get_or_create_active_draft,
        save_identity_step,
        submit_full_registration,
    )
    from apps.registrations.tests.factories import walk_draft_through_every_step

    if person is None:
        person = resolve_or_create_participant_for_email(
            email or f"idv-{uuid.uuid4().hex[:10]}@example.test"
        )
    draft = get_or_create_active_draft(person=person, event_edition=event)
    path = "NIN" if nationality == "DZ" else "PASSPORT"
    walk_draft_through_every_step(
        draft,
        nationality_code_id=nationality,
        country_of_residence_id=nationality,
        identity_path=path,
        nin_value=nin,
        passport_number=passport_number,
        passport_country_code_id=passport_country,
    )
    save_identity_step(
        registration=draft,
        given_names=given,
        family_name=family,
        date_of_birth=birth,
        nationality_code_id=nationality,
        country_of_residence_id=nationality,
        identity_path=path,
        nin_value=nin if path == "NIN" else "",
        passport_number=passport_number if path == "PASSPORT" else "",
        passport_country_code_id=passport_country if path == "PASSPORT" else "",
        passport_expires_at=(
            datetime.date.today() + datetime.timedelta(days=400) if path == "PASSPORT" else None
        ),
    )
    privacy, terms = legal_versions
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="idv-session",
        idempotency_key=f"idv-{draft.pk}",
    )
    return Registration.objects.get(pk=draft.pk)


def case_for(registration):
    from apps.people.models import IdentityVerification

    return IdentityVerification.objects.get(registration=registration)


def process_all(provider=None) -> dict:
    from apps.people.services.identity_verification import run_due_identity_jobs

    return run_due_identity_jobs(limit=100, provider=provider)


def make_staff(email: str, group_name: str, *, event=None, organization=None):
    from django.contrib.auth.models import Group

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    user = OperationalUser.objects.create_user(
        email=email, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        granted_by=user,
    )
    return user


def staff_client(user) -> Client:
    from apps.accounts import session_expiry

    client = Client()
    client.force_login(user)
    session = client.session
    now = timezone.now().isoformat()
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return client


def participant_client(person) -> Client:
    from apps.accounts import participant_auth, session_expiry

    client = Client()
    session = client.session
    now = timezone.now().isoformat()
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return client


def upload_national_id_card(registration):
    from apps.documents.services import save_national_id_card
    from apps.documents.tests.factories import make_test_photo

    return save_national_id_card(
        registration=registration, person=registration.person, uploaded_file=make_test_photo()
    )
