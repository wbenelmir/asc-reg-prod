"""The participant claim journey survives OTP authentication (Phase 2
Prompt 2 V2 correction pass requirement 6).

An unauthenticated visitor who opens `/claim/<token>/` must be redirected
through OTP WITHOUT losing the claim: only the token's non-secret HASH is
stashed in the session, `otp_verify` resumes the claim automatically after
a successful authentication, and every failure category (wrong verified
email, expired, reused, deleted/revoked) renders the exact same generic
`claim_unavailable` response.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core.testing import otp_request_data
from apps.invitations.models import OnBehalfClaim, OnBehalfClaimStatus
from apps.invitations.services import create_on_behalf_draft, hash_claim_token
from apps.invitations.views import PENDING_CLAIM_SESSION_KEY

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def creator(event, organization):
    return make_operational_user_with_membership(
        email="claim-survival-creator@example.com",
        group_name="On-Behalf Registrar",
        event_edition=event,
        organization=organization,
    )


def _verify_otp(client: Client, django_capture_on_commit_callbacks, email: str):
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        return client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )


def test_unauthenticated_claim_link_survives_otp_and_succeeds(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="claim-survives-otp@example.com",
    )
    client = Client()
    entry_response = client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))
    assert entry_response.status_code == 302
    assert entry_response.url == reverse("accounts:otp-request")

    # The session holds only the non-secret HASH -- never the raw token.
    stashed = client.session[PENDING_CLAIM_SESSION_KEY]
    assert stashed == hash_claim_token(raw_token)
    assert raw_token not in stashed

    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "claim-survives-otp@example.com"
    )
    assert verify_response.status_code == 302
    assert verify_response.url == reverse("invitations:on-behalf-claim-resume")

    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 302
    assert resume_response.url == reverse("registrations:step-identity")

    registration.refresh_from_db()
    assert registration.person_id is not None
    assert PENDING_CLAIM_SESSION_KEY not in client.session


def test_claim_resume_with_wrong_verified_email_fails_generically(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="intended-for-someone@example.com",
    )
    client = Client()
    client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))

    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "someone-else-entirely@example.com"
    )
    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 200
    assert "This claim link is not available" in resume_response.content.decode()

    registration.refresh_from_db()
    assert registration.person_id is None
    assert PENDING_CLAIM_SESSION_KEY not in client.session


def test_claim_resume_for_an_expired_claim_fails_generically(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="expired-via-otp@example.com",
    )
    OnBehalfClaim.objects.filter(registration=registration).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    client = Client()
    client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))
    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "expired-via-otp@example.com"
    )
    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 200
    assert "This claim link is not available" in resume_response.content.decode()


def test_claim_resume_for_an_already_claimed_token_fails_generically(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    from apps.invitations.services import claim_on_behalf_registration
    from apps.people.services import resolve_or_create_participant_for_email

    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="reused-via-otp@example.com",
    )
    person = resolve_or_create_participant_for_email("reused-via-otp@example.com")
    claim_on_behalf_registration(
        raw_claim_token=raw_token, verified_email="reused-via-otp@example.com", person=person
    )

    client = Client()
    client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))
    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "reused-via-otp@example.com"
    )
    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 200
    assert "This claim link is not available" in resume_response.content.decode()


def test_claim_resume_for_a_revoked_claim_fails_generically(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="revoked-via-otp@example.com",
    )
    OnBehalfClaim.objects.filter(registration=registration).update(
        status=OnBehalfClaimStatus.REVOKED
    )
    client = Client()
    client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))
    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "revoked-via-otp@example.com"
    )
    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 200
    assert "This claim link is not available" in resume_response.content.decode()


def test_claim_resume_for_a_deleted_claim_fails_generically(
    event, organization, creator, django_capture_on_commit_callbacks
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=creator,
        intended_email="deleted-via-otp@example.com",
    )
    OnBehalfClaim.objects.filter(registration=registration).delete()
    client = Client()
    client.get(reverse("invitations:on-behalf-claim", kwargs={"token": raw_token}))
    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "deleted-via-otp@example.com"
    )
    resume_response = client.get(verify_response.url)
    assert resume_response.status_code == 200
    assert "This claim link is not available" in resume_response.content.decode()


def test_a_direct_hit_on_resume_with_nothing_pending_lands_on_the_ordinary_workspace(
    django_capture_on_commit_callbacks,
) -> None:
    client = Client()
    verify_response = _verify_otp(
        client, django_capture_on_commit_callbacks, "no-pending-claim@example.com"
    )
    assert verify_response.status_code == 302
    assert verify_response.url == reverse("registrations:workspace")
