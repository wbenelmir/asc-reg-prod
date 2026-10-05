"""UX-4 registration channel policy, control and enforcement (M24, D-12, S-17).

Synthetic data only. The real-browser journey and the concurrent
close-versus-submit race are in `tests/browser/test_ux4_channels_and_human_check.py`
and `tests/concurrency/test_ux4_channel_close_race.py`.
"""

from __future__ import annotations

import datetime
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import participant_auth
from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.audit.models import AuditEvent
from apps.core.models import Country, Sector
from apps.events.apps import REGISTRATION_CHANNEL_MANAGER_GROUP_NAME
from apps.events.models import EventEdition, EventEditionStatus, PublicRegistrationMode
from apps.events.policies.registration_channels import (
    ChannelState,
    RegistrationChannelClosed,
    decide_for_source,
    effective_channel_state,
)
from apps.events.services import (
    RegistrationChannelChangeConflict,
    RegistrationChannelChangeDenied,
    RegistrationChannelChangeError,
    change_registration_channel,
)
from apps.invitations.models import (
    InvitationCampaignStatus,
    InvitationUse,
    OnBehalfClaim,
    OnBehalfClaimStatus,
)
from apps.invitations.services import (
    ClaimUnavailable,
    InvitationLinkUnavailable,
    change_campaign_status,
    claim_on_behalf_registration,
    create_campaign,
    create_invited_draft_registration,
    create_on_behalf_draft,
    issue_initial_link,
    resolve_invitation_link,
)
from apps.organizations.models import Organization, OrganizationType
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    AcceptanceRecord,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import Registration, RegistrationSubmission
from apps.registrations.services import (
    get_or_create_active_draft,
    initial_submission_operation_key,
    submit_full_registration,
)
from apps.registrations.tests.factories import (
    notices_post_data,
    walk_draft_through_every_step,
)

pytestmark = pytest.mark.django_db

PASSWORD = "__test_password__"  # noqa: S105
NOW = timezone.now()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reference_data(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    # The only open edition, so `current_event_edition()` resolves to it.
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    return EventEdition.objects.create(
        code="UX4TEST",
        name="UX-4 Test",
        timezone="Africa/Algiers",
        starts_at=NOW + timedelta(days=30),
        ends_at=NOW + timedelta(days=32),
        status=EventEditionStatus.REGISTRATION_OPEN,
    )


@pytest.fixture
def other_event() -> EventEdition:
    return EventEdition.objects.create(
        code="UX4OTHER",
        name="UX-4 Other",
        timezone="UTC",
        starts_at=NOW,
        ends_at=NOW,
        status=EventEditionStatus.DRAFT,
    )


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(
        official_name="Synthetic Ministry",
        normalized_name="synthetic ministry",
        organization_type=OrganizationType.MINISTRY,
    )


@pytest.fixture
def legal_versions():
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label="ux4-v1",
                content=f"{code} synthetic text",
                content_hash=("a" if code == "TERMS" else "b") * 64,
                effective_from=NOW - timedelta(days=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return versions


def _user(email: str, *, group: str | None = None, event=None, organization=None):
    user = OperationalUser.objects.create_user(
        email=email, password=PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    if group:
        ScopedGroupMembership.objects.create(
            user=user,
            group=Group.objects.get(name=group),
            event_edition=event,
            organization=organization,
            granted_by=user,
        )
    return user


@pytest.fixture
def manager(event):
    return _user(
        "ux4-manager@example.com", group=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME, event=event
    )


def _set_mode(event, mode, *, opens_at=None, closes_at=None):
    EventEdition.objects.filter(pk=event.pk).update(
        public_registration_mode=mode,
        registration_opens_at=opens_at,
        registration_closes_at=closes_at,
    )
    event.refresh_from_db()


def _active_campaign(event, organization, reference="CAMP-UX4-0001"):
    campaign = create_campaign(
        event_edition=event, organization=organization, name="UX-4", public_reference=reference
    )
    return change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)


def _submit(registration, legal_versions):
    privacy, terms = legal_versions
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="ux4",
        idempotency_key=initial_submission_operation_key(registration.pk),
    )


def _participant_client(email: str) -> tuple[Client, object]:
    """A participant signed in through the real OTP flow (the session carries
    the expiry timestamps `participant_required` checks)."""
    from django.test import override_settings

    from apps.accounts.otp import DeterministicTestOtpGenerator
    from apps.core.testing import otp_request_data

    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    person = resolve_or_create_participant_for_email(email)
    assert client.session.get(participant_auth.PARTICIPANT_SESSION_KEY) == str(person.id)
    return client, person


# ---------------------------------------------------------------------------
# Policy: modes, windows, boundaries
# ---------------------------------------------------------------------------


def test_default_mode_is_open_and_keeps_the_previous_behaviour(event) -> None:
    assert event.public_registration_mode == PublicRegistrationMode.OPEN
    assert effective_channel_state(event) == ChannelState.PUBLIC_OPEN


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (PublicRegistrationMode.OPEN, ChannelState.PUBLIC_OPEN),
        (PublicRegistrationMode.INVITATION_ONLY, ChannelState.INVITATION_ONLY),
        (PublicRegistrationMode.CLOSED, ChannelState.CLOSED),
    ],
)
def test_each_manual_mode_gives_its_state(event, mode, state) -> None:
    _set_mode(event, mode)
    assert effective_channel_state(event) == state


def test_window_bounds_are_inclusive_at_opening_and_exclusive_at_closing(event) -> None:
    opens = NOW.replace(microsecond=0)
    closes = opens + timedelta(hours=2)
    _set_mode(event, PublicRegistrationMode.OPEN, opens_at=opens, closes_at=closes)
    second = timedelta(seconds=1)
    assert effective_channel_state(event, now=opens - second) == ChannelState.INVITATION_ONLY
    assert effective_channel_state(event, now=opens) == ChannelState.PUBLIC_OPEN
    assert effective_channel_state(event, now=closes - second) == ChannelState.PUBLIC_OPEN
    assert effective_channel_state(event, now=closes) == ChannelState.INVITATION_ONLY


def test_the_most_restrictive_state_wins_inside_the_window(event) -> None:
    _set_mode(
        event,
        PublicRegistrationMode.CLOSED,
        opens_at=NOW - timedelta(days=1),
        closes_at=NOW + timedelta(days=1),
    )
    assert effective_channel_state(event) == ChannelState.CLOSED


def test_an_unknown_stored_mode_fails_closed(event) -> None:
    EventEdition.objects.filter(pk=event.pk).update(public_registration_mode="FORGED")
    event.refresh_from_db()
    assert effective_channel_state(event) == ChannelState.CLOSED


@pytest.mark.parametrize(
    ("mode", "source", "allowed"),
    [
        (PublicRegistrationMode.OPEN, "OPEN", True),
        (PublicRegistrationMode.OPEN, "INVITATION", True),
        (PublicRegistrationMode.INVITATION_ONLY, "OPEN", False),
        (PublicRegistrationMode.INVITATION_ONLY, "INVITATION", True),
        (PublicRegistrationMode.INVITATION_ONLY, "ON_BEHALF", True),
        (PublicRegistrationMode.INVITATION_ONLY, "DELEGATION", True),
        (PublicRegistrationMode.CLOSED, "OPEN", False),
        (PublicRegistrationMode.CLOSED, "INVITATION", False),
        (PublicRegistrationMode.CLOSED, "ON_BEHALF", False),
        (PublicRegistrationMode.CLOSED, "DELEGATION", False),
        # A source that is neither public nor restricted fails closed.
        (PublicRegistrationMode.OPEN, "UNKNOWN_SOURCE", False),
    ],
)
def test_decision_matrix_by_source(event, mode, source, allowed) -> None:
    _set_mode(event, mode)
    assert decide_for_source(event, source).allowed is allowed


def test_the_window_order_is_enforced_by_the_database(event) -> None:
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        EventEdition.objects.filter(pk=event.pk).update(
            registration_opens_at=NOW, registration_closes_at=NOW - timedelta(minutes=1)
        )


# ---------------------------------------------------------------------------
# Channel control service: authorization, reason, concurrency, audit
# ---------------------------------------------------------------------------


def _change(event, actor, **overrides):
    values = {
        "event_edition_id": event.pk,
        "actor": actor,
        "mode": PublicRegistrationMode.INVITATION_ONLY,
        "opens_at": None,
        "closes_at": None,
        "reason": "Public quota reached; invitations only.",
        "expected_settings_version": EventEdition.objects.get(pk=event.pk).settings_version,
    }
    values.update(overrides)
    return change_registration_channel(**values)


def test_an_authorized_change_is_applied_versioned_and_audited(event, manager) -> None:
    before = event.settings_version
    result = _change(event, manager)
    event.refresh_from_db()
    assert result.changed is True
    assert event.public_registration_mode == PublicRegistrationMode.INVITATION_ONLY
    assert event.settings_version == before + 1
    audit = AuditEvent.objects.filter(action_code="EVT_REGISTRATION_CHANNEL_CHANGED").get()
    assert audit.actor_user_id == manager.pk
    assert audit.before_summary["mode"] == "OPEN"
    assert audit.after_summary["mode"] == "INVITATION_ONLY"
    assert audit.after_summary["reason"] == "Public quota reached; invitations only."


def test_resending_the_same_state_is_idempotent_and_not_audited_twice(event, manager) -> None:
    _change(event, manager)
    again = _change(event, manager)
    assert again.changed is False
    assert AuditEvent.objects.filter(action_code="EVT_REGISTRATION_CHANNEL_CHANGED").count() == 1


def test_a_stale_page_is_refused(event, manager) -> None:
    stale = EventEdition.objects.get(pk=event.pk).settings_version
    _change(event, manager)
    with pytest.raises(RegistrationChannelChangeConflict):
        _change(event, manager, mode=PublicRegistrationMode.CLOSED, expected_settings_version=stale)


@pytest.mark.parametrize("reason", ["", "    ", "abc"])
def test_a_reason_is_mandatory(event, manager, reason) -> None:
    with pytest.raises(RegistrationChannelChangeError):
        _change(event, manager, reason=reason)


def test_an_overlong_reason_is_refused(event, manager) -> None:
    with pytest.raises(RegistrationChannelChangeError):
        _change(event, manager, reason="x" * 301)


def test_a_window_that_closes_before_it_opens_is_refused(event, manager) -> None:
    with pytest.raises(RegistrationChannelChangeError):
        _change(event, manager, opens_at=NOW, closes_at=NOW - timedelta(hours=1))


def test_an_unknown_mode_is_refused(event, manager) -> None:
    with pytest.raises(RegistrationChannelChangeError):
        _change(event, manager, mode="EVERYONE_WELCOME")


def test_no_permission_means_no_change_and_a_denial_audit(event) -> None:
    stranger = _user("ux4-stranger@example.com")
    with pytest.raises(RegistrationChannelChangeDenied):
        _change(event, stranger)
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.OPEN
    assert AuditEvent.objects.filter(
        action_code="EVT_REGISTRATION_CHANNEL_CHANGE_DENIED", result="DENIED"
    ).exists()


def test_a_membership_for_another_event_does_not_authorize(event, other_event) -> None:
    elsewhere = _user(
        "ux4-elsewhere@example.com",
        group=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME,
        event=other_event,
    )
    with pytest.raises(RegistrationChannelChangeDenied):
        _change(event, elsewhere)


def test_an_organization_scoped_membership_does_not_authorize(event, organization) -> None:
    org_user = _user(
        "ux4-org@example.com",
        group=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME,
        event=event,
        organization=organization,
    )
    with pytest.raises(RegistrationChannelChangeDenied):
        _change(event, org_user)


def test_other_operational_groups_do_not_carry_the_permission() -> None:
    for group in Group.objects.exclude(name=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME):
        assert not group.permissions.filter(codename="manage_registration_channels").exists()


def test_reopening_restores_public_registration_without_touching_drafts(event, manager) -> None:
    person = resolve_or_create_participant_for_email("ux4-reopen@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    _change(event, manager, mode=PublicRegistrationMode.CLOSED)
    assert Registration.objects.filter(pk=draft.pk, public_status="DRAFT").exists()
    _change(event, manager, mode=PublicRegistrationMode.OPEN, reason="Reopened by decision.")
    assert get_or_create_active_draft(person=person, event_edition=event).pk == draft.pk


# ---------------------------------------------------------------------------
# Enforcement on every channel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", [PublicRegistrationMode.INVITATION_ONLY, PublicRegistrationMode.CLOSED]
)
def test_no_new_public_draft_when_public_registration_is_not_open(event, mode) -> None:
    _set_mode(event, mode)
    person = resolve_or_create_participant_for_email("ux4-public@example.com")
    with pytest.raises(RegistrationChannelClosed):
        get_or_create_active_draft(person=person, event_edition=event)
    assert not Registration.objects.filter(event_edition=event).exists()
    event.refresh_from_db()
    assert event.next_registration_sequence == 1  # no reference was consumed


def test_no_new_public_draft_outside_the_window(event) -> None:
    _set_mode(
        event,
        PublicRegistrationMode.OPEN,
        opens_at=NOW - timedelta(days=10),
        closes_at=NOW - timedelta(days=1),
    )
    person = resolve_or_create_participant_for_email("ux4-window@example.com")
    with pytest.raises(RegistrationChannelClosed) as caught:
        get_or_create_active_draft(person=person, event_edition=event)
    assert caught.value.decision.reason == "PUBLIC_REGISTRATION_CLOSED"


def test_invitations_continue_in_invitation_only_mode(event, organization) -> None:
    campaign = _active_campaign(event, organization)
    link, token = issue_initial_link(campaign)
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    assert resolve_invitation_link(token).pk == link.pk
    result = create_invited_draft_registration(link, preferred_language="en")
    assert result.registration.source_kind == "INVITATION"


def test_invitations_are_refused_while_closed_without_leaving_a_trace(event, organization) -> None:
    campaign = _active_campaign(event, organization)
    link, token = issue_initial_link(campaign)
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(InvitationLinkUnavailable):
        resolve_invitation_link(token)
    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(link, preferred_language="en")
    assert not Registration.objects.filter(event_edition=event).exists()
    assert not InvitationUse.objects.filter(campaign=campaign).exists()
    assert AuditEvent.objects.filter(
        action_code="INV_LINK_REJECTED", reason_code="REGISTRATION_CLOSED"
    ).exists()


def test_a_link_opened_before_closure_is_refused_when_consumed(event, organization) -> None:
    campaign = _active_campaign(event, organization)
    link, token = issue_initial_link(campaign)
    resolved = resolve_invitation_link(token)  # valid while open
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(InvitationLinkUnavailable):
        create_invited_draft_registration(resolved, preferred_language="en")


def _creator(event, organization):
    return _user(
        "ux4-registrar@example.com",
        group="On-Behalf Registrar",
        event=event,
        organization=organization,
    )


def test_staff_on_behalf_creation_continues_when_invitation_only(event, organization) -> None:
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    registration, _token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=_creator(event, organization),
        intended_email="ux4-onbehalf@example.com",
    )
    assert registration.source_kind == "ON_BEHALF"


def test_staff_on_behalf_creation_is_refused_while_closed(event, organization) -> None:
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(RegistrationChannelClosed):
        create_on_behalf_draft(
            event_edition=event,
            source_organization=organization,
            created_by=_creator(event, organization),
            intended_email="ux4-onbehalf-closed@example.com",
        )
    assert not Registration.objects.filter(event_edition=event).exists()
    assert not OnBehalfClaim.objects.exists()


def test_a_claim_is_refused_while_closed_and_works_again_after_reopening(
    event, organization
) -> None:
    registration, raw_token = create_on_behalf_draft(
        event_edition=event,
        source_organization=organization,
        created_by=_creator(event, organization),
        intended_email="ux4-claimant@example.com",
    )
    person = resolve_or_create_participant_for_email("ux4-claimant@example.com")
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(ClaimUnavailable):
        claim_on_behalf_registration(
            raw_claim_token=raw_token, verified_email="ux4-claimant@example.com", person=person
        )
    claim = OnBehalfClaim.objects.get(registration=registration)
    assert claim.status == OnBehalfClaimStatus.PENDING
    registration.refresh_from_db()
    assert registration.person_id is None

    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    claimed = claim_on_behalf_registration(
        raw_claim_token=raw_token, verified_email="ux4-claimant@example.com", person=person
    )
    assert claimed.person_id == person.pk


def test_delegation_apply_is_refused_while_closed(event, organization) -> None:
    from apps.invitations.models import DelegationBatch
    from apps.invitations.services import apply_delegation_batch

    batch = DelegationBatch(event_edition=event, organization=organization)
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(RegistrationChannelClosed):
        apply_delegation_batch(batch)


def test_a_public_draft_cannot_be_submitted_after_closure(event, legal_versions) -> None:
    person = resolve_or_create_participant_for_email("ux4-stale@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    with pytest.raises(RegistrationChannelClosed):
        _submit(draft, legal_versions)
    draft.refresh_from_db()
    assert draft.public_status == "DRAFT"
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()
    assert not AcceptanceRecord.objects.filter(registration=draft).exists()


def test_an_invited_draft_can_still_be_submitted_when_invitation_only(
    event, organization, legal_versions
) -> None:
    campaign = _active_campaign(event, organization, reference="CAMP-UX4-0002")
    link, _token = issue_initial_link(campaign)
    person = resolve_or_create_participant_for_email("ux4-invited@example.com")
    draft = create_invited_draft_registration(
        link, preferred_language="en", person_id=person.pk
    ).registration
    walk_draft_through_every_step(draft)
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    submission = _submit(draft, legal_versions)
    assert submission.registration_id == draft.pk


def test_an_invited_draft_cannot_be_submitted_while_closed(
    event, organization, legal_versions
) -> None:
    campaign = _active_campaign(event, organization, reference="CAMP-UX4-0003")
    link, _token = issue_initial_link(campaign)
    person = resolve_or_create_participant_for_email("ux4-invited-closed@example.com")
    draft = create_invited_draft_registration(
        link, preferred_language="en", person_id=person.pk
    ).registration
    walk_draft_through_every_step(draft)
    _set_mode(event, PublicRegistrationMode.CLOSED)
    with pytest.raises(RegistrationChannelClosed):
        _submit(draft, legal_versions)
    assert not InvitationUse.objects.filter(registration=draft, use_kind="SUBMITTED").exists()


def test_a_retried_submission_after_closure_returns_the_original_result(
    event, legal_versions
) -> None:
    person = resolve_or_create_participant_for_email("ux4-retry@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    first = _submit(draft, legal_versions)
    _set_mode(event, PublicRegistrationMode.CLOSED)
    assert _submit(draft, legal_versions).pk == first.pk


# ---------------------------------------------------------------------------
# Participant views: direct URLs, forged posts, stale tabs, fallback
# ---------------------------------------------------------------------------


WIZARD_URLS = [
    "registrations:step-identity",
    "registrations:step-contact",
    "registrations:step-professional",
    "registrations:step-interests",
    "registrations:step-review",
    "registrations:step-notices",
]


@pytest.mark.parametrize("url_name", WIZARD_URLS)
def test_every_wizard_url_is_refused_for_a_public_draft_after_closure(event, url_name) -> None:
    client, person = _participant_client(f"ux4-view-{url_name.split('-')[-1]}@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    session = client.session
    session[participant_auth.ACTIVE_REGISTRATION_SESSION_KEY] = str(draft.pk)
    session.save()
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    get = client.get(reverse(url_name))
    post = client.post(reverse(url_name), {"given_names": "Forged", "accept_terms": "on"})
    for response in (get, post):
        assert response.status_code == 403
        assert b'data-registration-closed="public"' in response.content
    draft.refresh_from_db()
    assert draft.public_status == "DRAFT"


def test_a_stale_notices_page_cannot_submit_after_closure(event, legal_versions) -> None:
    client, person = _participant_client("ux4-stale-tab@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    session = client.session
    session[participant_auth.ACTIVE_REGISTRATION_SESSION_KEY] = str(draft.pk)
    session.save()
    assert client.get(reverse("registrations:step-notices")).status_code == 200
    _set_mode(event, PublicRegistrationMode.CLOSED)
    response = client.post(
        reverse("registrations:step-notices"),
        notices_post_data(),
    )
    assert response.status_code == 403
    assert b'data-registration-closed="all"' in response.content
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()


def test_a_new_participant_sees_the_closed_page_instead_of_a_new_draft(event) -> None:
    client, _person = _participant_client("ux4-new@example.com")
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    response = client.get(reverse("registrations:step-identity"))
    assert response.status_code == 403
    assert not Registration.objects.filter(event_edition=event).exists()


def test_the_workspace_keeps_working_and_hides_the_start_button(event) -> None:
    client, person = _participant_client("ux4-workspace@example.com")
    _set_mode(event, PublicRegistrationMode.CLOSED)
    response = client.get(reverse("registrations:workspace"))
    assert response.status_code == 200
    assert reverse("registrations:step-identity").encode() not in response.content
    assert b"Public registration is closed" in response.content


def test_the_workspace_marks_a_closed_draft_and_keeps_it(event) -> None:
    client, person = _participant_client("ux4-kept@example.com")
    get_or_create_active_draft(person=person, event_edition=event)
    _set_mode(event, PublicRegistrationMode.INVITATION_ONLY)
    response = client.get(reverse("registrations:workspace"))
    assert b"data-draft-closed" in response.content
    assert b"Continue registration" not in response.content


def test_submitted_confirmation_stays_available_after_closure(event, legal_versions) -> None:
    client, person = _participant_client("ux4-confirmed@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    _submit(draft, legal_versions)
    draft.refresh_from_db()
    _set_mode(event, PublicRegistrationMode.CLOSED)
    response = client.get(
        reverse("registrations:confirmation", kwargs={"reference": draft.public_reference})
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("mode", "offered"),
    [
        (PublicRegistrationMode.OPEN, True),
        (PublicRegistrationMode.INVITATION_ONLY, False),
        (PublicRegistrationMode.CLOSED, False),
    ],
)
def test_the_invalid_invitation_fallback_follows_the_public_channel(event, mode, offered) -> None:
    _set_mode(event, mode)
    response = Client().get("/invite/not-a-real-token/")
    assert response.status_code == 200
    assert (b"You can still register directly." in response.content) is offered


def test_the_otp_pages_stay_generic_and_available_when_closed(event) -> None:
    from apps.core.testing import otp_request_data

    _set_mode(event, PublicRegistrationMode.CLOSED)
    client = Client()
    assert client.get(reverse("accounts:otp-request")).status_code == 200
    response = client.post(
        reverse("accounts:otp-request"), otp_request_data("ux4-otp@example.com", client=client)
    )
    assert response.status_code == 302
    assert response.url == reverse("accounts:otp-verify")


# ---------------------------------------------------------------------------
# Operations screen
# ---------------------------------------------------------------------------


def _signed_in(email: str) -> Client:
    client = Client()
    client.post(reverse("accounts:operational-sign-in"), {"email": email, "password": PASSWORD})
    return client


def test_the_operations_screen_changes_the_mode_with_a_reason(event, manager) -> None:
    client = _signed_in("ux4-manager@example.com")
    url = reverse("events:channel-detail", kwargs={"pk": event.pk})
    page = client.get(url)
    assert page.status_code == 200
    assert b'data-channel-state="PUBLIC_OPEN"' in page.content
    response = client.post(
        url,
        {
            "mode": "CLOSED",
            "opens_at": "",
            "closes_at": "",
            "reason": "Closed for the capacity review.",
            "expected_settings_version": event.settings_version,
        },
    )
    assert response.status_code == 302
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.CLOSED


def test_the_window_is_read_in_the_event_timezone(event, manager) -> None:
    client = _signed_in("ux4-manager@example.com")
    url = reverse("events:channel-detail", kwargs={"pk": event.pk})
    client.post(
        url,
        {
            "mode": "OPEN",
            "opens_at": "2026-10-01T09:00",
            "closes_at": "2026-10-20T18:00",
            "reason": "Public window for the edition.",
            "expected_settings_version": event.settings_version,
        },
    )
    event.refresh_from_db()
    # Africa/Algiers is UTC+1 all year.
    assert event.registration_opens_at == datetime.datetime(2026, 10, 1, 8, 0, tzinfo=datetime.UTC)
    assert event.registration_closes_at == datetime.datetime(
        2026, 10, 20, 17, 0, tzinfo=datetime.UTC
    )
    assert b'value="2026-10-01T09:00"' in client.get(url).content


def test_the_operations_screen_rejects_a_missing_reason(event, manager) -> None:
    client = _signed_in("ux4-manager@example.com")
    url = reverse("events:channel-detail", kwargs={"pk": event.pk})
    response = client.post(
        url,
        {"mode": "CLOSED", "reason": "", "expected_settings_version": event.settings_version},
    )
    assert response.status_code == 400
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.OPEN


def test_a_user_without_the_permission_gets_403(event) -> None:
    _user("ux4-plain@example.com", group="Registration Intake", event=event)
    client = _signed_in("ux4-plain@example.com")
    assert client.get(reverse("events:channel-list")).status_code == 403
    response = client.post(
        reverse("events:channel-detail", kwargs={"pk": event.pk}),
        {"mode": "CLOSED", "reason": "Forged change.", "expected_settings_version": 1},
    )
    assert response.status_code == 403
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.OPEN


def test_an_event_outside_the_scope_is_a_404(event, other_event, manager) -> None:
    client = _signed_in("ux4-manager@example.com")
    assert (
        client.get(reverse("events:channel-detail", kwargs={"pk": other_event.pk})).status_code
        == 404
    )
    listing = client.get(reverse("events:channel-list")).content
    assert b"UX4TEST" in listing and b"UX4OTHER" not in listing
