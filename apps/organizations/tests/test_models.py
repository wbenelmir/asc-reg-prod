"""Organization/alias/affiliation invariant tests (Schema §6.2, §5.6)."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationAlias, ProfessionalAffiliation
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


def test_organization_cannot_self_merge() -> None:
    org = Organization.objects.create(official_name="Acme", organization_type="COMPANY")
    org.merged_into_id = org.id
    with pytest.raises(IntegrityError), transaction.atomic():
        org.save(update_fields=["merged_into"])


def test_alias_is_linked_to_organization() -> None:
    org = Organization.objects.create(official_name="Acme", organization_type="COMPANY")
    alias = OrganizationAlias.objects.create(
        organization=org, alias_name="Acme Corp", language="en"
    )
    assert alias in org.aliases.all()


def test_professional_affiliation_snapshot_survives_organization_rename() -> None:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="AFFTEST", name="Affiliation Test", timezone="UTC", starts_at=now, ends_at=now
    )
    registration = Registration.objects.create(
        public_reference="AFFTEST-R-000001",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="ctx",
    )
    org = Organization.objects.create(official_name="Original Name", organization_type="COMPANY")
    affiliation = ProfessionalAffiliation.objects.create(
        registration=registration,
        organization=org,
        submitted_organization_name="Original Name Ltd.",
        job_title="Engineer",
    )
    org.official_name = "Renamed Inc."
    org.save(update_fields=["official_name"])
    affiliation.refresh_from_db()
    assert affiliation.submitted_organization_name == "Original Name Ltd."


def test_one_affiliation_per_registration() -> None:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="AFFONE", name="Affiliation One", timezone="UTC", starts_at=now, ends_at=now
    )
    registration = Registration.objects.create(
        public_reference="AFFONE-R-000001",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="ctx",
    )
    ProfessionalAffiliation.objects.create(
        registration=registration, submitted_organization_name="Org A"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        ProfessionalAffiliation.objects.create(
            registration=registration, submitted_organization_name="Org B"
        )
