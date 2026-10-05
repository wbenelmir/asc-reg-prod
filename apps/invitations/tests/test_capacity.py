"""Campaign capacity enforcement and idempotent-retry tests (Phase 2 Prompt 2
requirement 10). Real multi-connection concurrency lives separately in
tests/concurrency/test_invitation_campaign_capacity.py."""

from __future__ import annotations

import uuid

import pytest

from apps.invitations.models import InvitationCampaignStatus, InvitationUse, InvitationUseKind
from apps.invitations.services import (
    CampaignCapacityExceededError,
    change_campaign_status,
    create_campaign,
    create_invited_draft_registration,
    issue_initial_link,
)
from apps.registrations.services import submit_full_registration

from ...registrations.tests.factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db


@pytest.fixture
def capacity_one_campaign(event, organization):
    campaign = create_campaign(
        event_edition=event,
        organization=organization,
        name="Capacity One",
        public_reference="CAMP-CAP1-0001",
        capacity=1,
    )
    return change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)


def _submit(registration, legal_versions, key=None):
    walk_draft_through_every_step(registration)
    privacy_version, terms_version = legal_versions
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess",
        idempotency_key=key or str(uuid.uuid4()),
    )


def test_first_submission_consumes_the_only_place(
    capacity_one_campaign, event, legal_versions
) -> None:
    from .conftest import make_person

    link, _token = issue_initial_link(capacity_one_campaign)
    person = make_person("cap-one@example.com")
    result = create_invited_draft_registration(link, preferred_language="en", person_id=person.pk)
    _submit(result.registration, legal_versions)
    assert (
        InvitationUse.objects.filter(
            campaign=capacity_one_campaign, use_kind=InvitationUseKind.SUBMITTED
        ).count()
        == 1
    )


def test_second_registration_is_rejected_once_capacity_is_exhausted(
    capacity_one_campaign, event, legal_versions
) -> None:
    from .conftest import make_person

    link, _token = issue_initial_link(capacity_one_campaign)
    person_a = make_person("cap-a@example.com")
    person_b = make_person("cap-b@example.com")

    result_a = create_invited_draft_registration(
        link, preferred_language="en", person_id=person_a.pk
    )
    _submit(result_a.registration, legal_versions)

    result_b = create_invited_draft_registration(
        link, preferred_language="en", person_id=person_b.pk
    )
    walk_draft_through_every_step(result_b.registration)
    privacy_version, terms_version = legal_versions
    with pytest.raises(CampaignCapacityExceededError):
        submit_full_registration(
            registration=result_b.registration,
            privacy_notice_version=privacy_version,
            terms_version=terms_version,
            data_processing_consent_granted=True,
            session_reference="sess",
            idempotency_key=str(uuid.uuid4()),
        )


def test_retrying_the_same_submission_does_not_consume_a_second_place(
    capacity_one_campaign, event, legal_versions
) -> None:
    from .conftest import make_person

    link, _token = issue_initial_link(capacity_one_campaign)
    person = make_person("cap-retry@example.com")
    result = create_invited_draft_registration(link, preferred_language="en", person_id=person.pk)
    walk_draft_through_every_step(result.registration)
    privacy_version, terms_version = legal_versions
    key = str(uuid.uuid4())
    first = submit_full_registration(
        registration=result.registration,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess",
        idempotency_key=key,
    )
    second = submit_full_registration(
        registration=result.registration,
        privacy_notice_version=privacy_version,
        terms_version=terms_version,
        data_processing_consent_granted=True,
        session_reference="sess",
        idempotency_key=key,
    )
    assert first.pk == second.pk
    assert (
        InvitationUse.objects.filter(
            campaign=capacity_one_campaign, use_kind=InvitationUseKind.SUBMITTED
        ).count()
        == 1
    )


def test_unlimited_capacity_never_rejects(event, organization, legal_versions) -> None:
    from .conftest import make_person

    campaign = change_campaign_status(
        create_campaign(
            event_edition=event,
            organization=organization,
            name="Unlimited",
            public_reference="CAMP-UNLIM-0001",
            capacity=None,
        ),
        InvitationCampaignStatus.ACTIVE,
    )
    link, _token = issue_initial_link(campaign)
    for index in range(3):
        person = make_person(f"unlimited-{index}@example.com")
        result = create_invited_draft_registration(
            link, preferred_language="en", person_id=person.pk
        )
        _submit(result.registration, legal_versions)
    assert (
        InvitationUse.objects.filter(
            campaign=campaign, use_kind=InvitationUseKind.SUBMITTED
        ).count()
        == 3
    )
