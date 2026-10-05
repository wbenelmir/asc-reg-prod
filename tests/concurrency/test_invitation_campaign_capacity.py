"""Real multi-connection PostgreSQL concurrency test for invitation-campaign
capacity (Phase 2 Prompt 2).

Reproduces the exact scenario the requirement names directly: a campaign
with `capacity=1`, two DIFFERENT registrations submitting concurrently.
Exactly one must consume the final place; the other must be rejected with
`CampaignCapacityExceededError`, never a partial write, a deadlock, or a
double-counted place. A subsequent retry of the WINNING submission must not
consume a second place.

Marked `concurrency`, matching `pyproject.toml`'s definition: "real
multi-connection PostgreSQL concurrency tests (not Redis integration)".
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.invitations.models import InvitationCampaignStatus, InvitationUse, InvitationUseKind
from apps.invitations.services import (
    CampaignCapacityExceededError,
    change_campaign_status,
    create_campaign,
    issue_initial_link,
)
from apps.invitations.services import (
    create_invited_draft_registration as create_invited_draft_with_evidence,
)
from apps.organizations.models import Organization, OrganizationType
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.services import submit_full_registration
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def test_concurrent_submissions_against_capacity_one_leave_exactly_one_winner() -> None:
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    event = EventEdition.objects.create(
        code="CAPCONC",
        name="Capacity Concurrency Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )
    organization = Organization.objects.create(
        official_name="Concurrency Org",
        normalized_name="concurrency org",
        organization_type=OrganizationType.OTHER,
    )
    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Capacity Concurrency Campaign",
            public_reference="CAMP-CAPCONC-0001",
            capacity=1,
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    link, _raw_token = issue_initial_link(campaign)

    privacy_doc, _ = LegalDocument.objects.get_or_create(
        code="PRIVACY_NOTICE", defaults={"document_type": "PRIVACY_NOTICE"}
    )
    terms_doc, _ = LegalDocument.objects.get_or_create(
        code="TERMS", defaults={"document_type": "TERMS"}
    )
    privacy_version = LegalDocumentVersion.objects.create(
        legal_document=privacy_doc,
        language="en",
        version_label="capconc-v1",
        content="Privacy text",
        content_hash="a" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    terms_version = LegalDocumentVersion.objects.create(
        legal_document=terms_doc,
        language="en",
        version_label="capconc-v1",
        content="Terms text",
        content_hash="b" * 64,
        effective_from=timezone.now(),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )

    registrations = []
    for index in range(2):
        person = resolve_or_create_participant_for_email(f"cap-conc-{index}@example.com")
        result = create_invited_draft_with_evidence(
            link, preferred_language="en", person_id=person.pk
        )
        walk_draft_through_every_step(result.registration)
        registrations.append(result.registration)

    def _make_submit(registration):
        def _submit():
            return submit_full_registration(
                registration=registration,
                privacy_notice_version=privacy_version,
                terms_version=terms_version,
                data_processing_consent_granted=True,
                session_reference="capacity-concurrency-test",
                idempotency_key=str(uuid.uuid4()),
            )

        return _submit

    results, errors = _run_callables_in_threads([_make_submit(r) for r in registrations])

    successes = [r for r in results if r is not None]
    capacity_errors = [e for e in errors if isinstance(e, CampaignCapacityExceededError)]
    other_errors = [e for e in errors if not isinstance(e, CampaignCapacityExceededError)]

    assert not other_errors, f"unexpected error(s): {other_errors}"
    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert len(capacity_errors) == 1, f"expected exactly one capacity rejection, got {errors}"

    assert (
        InvitationUse.objects.filter(
            campaign=campaign, use_kind=InvitationUseKind.SUBMITTED
        ).count()
        == 1
    )

    # A retry of the WINNING submission must not consume a second place.
    for registration in registrations:
        registration.refresh_from_db()
    winner = next(r for r in registrations if r.public_status == "SUBMITTED")
    submit_full_registration(
        registration=winner,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="capacity-concurrency-test-retry",
        idempotency_key=str(uuid.uuid4()),
    )
    assert (
        InvitationUse.objects.filter(
            campaign=campaign, use_kind=InvitationUseKind.SUBMITTED
        ).count()
        == 1
    )
