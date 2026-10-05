"""On-behalf draft creation and claim tests (Phase 2 Prompt 2 requirements
12-17; AF-ORG-03/04)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.invitations.models import OnBehalfClaim, OnBehalfClaimStatus
from apps.invitations.services import (
    ClaimUnavailable,
    claim_on_behalf_registration,
    create_on_behalf_draft,
)
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import AcceptanceRecord
from apps.registrations.models import Registration, RegistrationSourceKind
from apps.registrations.services import DeduplicationConflictError

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def creator(event, organization):
    return make_operational_user_with_membership(
        email="on-behalf-creator@example.com",
        group_name="On-Behalf Registrar",
        event_edition=event,
        organization=organization,
    )


def test_create_on_behalf_draft_is_unclaimed(event, organization, creator) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="intended@example.com",
    )
    assert registration.person_id is None
    assert registration.source_kind == RegistrationSourceKind.ON_BEHALF
    assert registration.created_on_behalf_by_id == creator.pk
    assert len(raw_token) > 30

    claim = OnBehalfClaim.objects.get(registration=registration)
    assert claim.status == OnBehalfClaimStatus.PENDING
    # Token hash is persisted; the raw token itself never is (ADR-0016).
    assert claim.claim_token_hash != raw_token


def test_creator_never_creates_a_legal_acceptance_record(event, organization, creator) -> None:
    registration, _token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="no-acceptance@example.com",
    )
    assert not AcceptanceRecord.objects.filter(registration=registration).exists()


def test_on_behalf_draft_is_never_marked_submitted(event, organization, creator) -> None:
    registration, _token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="not-submitted@example.com",
    )
    assert registration.public_status == "DRAFT"


def test_claim_succeeds_with_matching_verified_email(event, organization, creator) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="claimant@example.com",
    )
    person = resolve_or_create_participant_for_email("claimant@example.com")
    claimed = claim_on_behalf_registration(
        raw_claim_token=raw_token, verified_email="claimant@example.com", person=person
    )
    assert claimed.pk == registration.pk
    claimed.refresh_from_db()
    assert claimed.person_id == person.pk
    assert claimed.claimed_at is not None

    claim = OnBehalfClaim.objects.get(registration=registration)
    assert claim.status == OnBehalfClaimStatus.CLAIMED


def test_claim_fails_with_a_mismatched_email(event, organization, creator) -> None:
    _registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="intended-only@example.com",
    )
    wrong_person = resolve_or_create_participant_for_email("someone-else@example.com")
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token=raw_token,
            verified_email="someone-else@example.com",
            person=wrong_person,
        )


def test_claim_fails_for_an_unknown_token(event, organization, creator) -> None:
    person = resolve_or_create_participant_for_email("unknown-token@example.com")
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token="__never_issued_token__",  # noqa: S106 - not a password, a test token value
            verified_email="unknown-token@example.com",
            person=person,
        )


def test_claim_fails_once_expired(event, organization, creator) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="expired@example.com",
    )
    claim = OnBehalfClaim.objects.get(registration=registration)
    claim.expires_at = timezone.now() - timedelta(seconds=1)
    claim.save(update_fields=["expires_at"])

    person = resolve_or_create_participant_for_email("expired@example.com")
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token=raw_token, verified_email="expired@example.com", person=person
        )


def test_claim_is_single_use(event, organization, creator) -> None:
    _registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="single-use@example.com",
    )
    person = resolve_or_create_participant_for_email("single-use@example.com")
    claim_on_behalf_registration(
        raw_claim_token=raw_token, verified_email="single-use@example.com", person=person
    )
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token=raw_token, verified_email="single-use@example.com", person=person
        )


def test_claim_never_exposes_whether_the_token_exists_for_another_email(
    event, organization, creator
) -> None:
    """Both a genuinely-unknown token and a genuine-token-wrong-email case
    must raise the exact same exception type with no distinguishing detail."""
    _registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="oracle-check@example.com",
    )
    wrong_person = resolve_or_create_participant_for_email("wrong-oracle@example.com")

    with pytest.raises(ClaimUnavailable) as wrong_email_exc:
        claim_on_behalf_registration(
            raw_claim_token=raw_token,
            verified_email="wrong-oracle@example.com",
            person=wrong_person,
        )
    with pytest.raises(ClaimUnavailable) as unknown_token_exc:
        claim_on_behalf_registration(
            raw_claim_token="__totally_unknown_token__",  # noqa: S106 - not a password, a test token value
            verified_email="wrong-oracle@example.com",
            person=wrong_person,
        )
    assert str(wrong_email_exc.value) == str(unknown_token_exc.value)


def test_claim_collision_raises_a_controlled_conflict_not_a_500(
    event, organization, creator
) -> None:
    """A Person who already has ANOTHER current context whose dedup key
    happens to collide once claimed must get a safe, handleable exception."""
    from apps.registrations.services import compute_deduplication_key

    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="collision@example.com",
    )
    person = resolve_or_create_participant_for_email("collision@example.com")

    # Force a collision: create another CURRENT Registration for this same
    # Person whose dedup key equals what the claim would compute.
    colliding_key = compute_deduplication_key(
        event_edition_id=event.pk,
        person_id=person.pk,
        source_context_key=registration.source_context_key,
    )
    Registration.objects.create(
        public_reference="COLLIDE-0001",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key="pre-existing",
        deduplication_key=colliding_key,
    )

    with pytest.raises(DeduplicationConflictError):
        claim_on_behalf_registration(
            raw_claim_token=raw_token, verified_email="collision@example.com", person=person
        )
    # The original draft must remain unclaimed, not half-mutated.
    registration.refresh_from_db()
    assert registration.person_id is None
