from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accounts.tests.sign_in import staff_sign_in
from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.privacy.models import (
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)


@pytest.fixture(autouse=True)
def _isolated_private_storage_root(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture(autouse=True)
def _seed_countries():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="INVTEST",
        name="Invitation Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def other_event() -> EventEdition:
    return EventEdition.objects.create(
        code="INVTEST2",
        name="Invitation Test 2",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Acme Ministry",
        normalized_name="acme ministry",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def other_organization() -> Organization:
    return Organization.objects.create(
        official_name="Beta Institute",
        normalized_name="beta institute",
        organization_type=OrganizationType.INSTITUTION,
    )


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
        version_label="inv-v1",
        content="Privacy text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_version = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="inv-v1",
        content="Terms text",
        content_hash="b" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    return privacy_version, terms_version


def make_person(email: str):
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email(email)


TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105


def make_operational_user_with_membership(
    *, email: str, group_name: str, event_edition=None, organization=None
):
    from django.contrib.auth.models import Group

    from apps.accounts.models import (
        OperationalUser,
        OperationalUserStatus,
        ScopedGroupMembership,
    )

    user = OperationalUser.objects.create_user(
        email=email, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    group = Group.objects.get(name=group_name)
    ScopedGroupMembership.objects.create(
        user=user,
        group=group,
        event_edition=event_edition,
        organization=organization,
        granted_by=user,
    )
    return user


def sign_in_operational(client, email: str) -> None:
    """Establish a REAL operational session through the sign-in view -- never
    `client.force_login()`, which never sets the session-expiry timestamps
    `OperationalSessionExpiryMiddleware` requires on every subsequent
    request (mirrors `apps.registrations.tests.test_views._sign_in`)."""

    staff_sign_in(client, email, TEST_OPERATIONAL_PASSWORD)
