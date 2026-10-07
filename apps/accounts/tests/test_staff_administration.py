"""Staff account administration (`apps.accounts.administration`): authorization
on every endpoint, scoped delegation, self-escalation, last administrator,
single-use credential setup links, and immediate loss of access. Synthetic
data, PostgreSQL, the test mail outbox only."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import administration as admin
from apps.accounts.models import (
    CredentialSetupToken,
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.events.models import EventEdition

pytestmark = pytest.mark.django_db

GOOD = "admin-tests-correct-horse-42"
CHOSEN = "chosen-by-staff-Battery-9"
OTHER_CHOSEN = "another-Password-77"


def _event(code):
    now = timezone.now()
    return EventEdition.objects.create(
        code=code, name=code, timezone="UTC", starts_at=now, ends_at=now + timedelta(days=3)
    )


@pytest.fixture
def event():
    return _event("ADM1")


@pytest.fixture
def other_event():
    return _event("ADM2")


def _staff(email, *, status=OperationalUserStatus.ACTIVE, password=GOOD, **extra):
    return OperationalUser.objects.create_user(
        email=email, password=password, status=status, display_name=email.split("@")[0], **extra
    )


def _grant(user, group_name, *, event=None, organization=None):
    return ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        granted_by=user,
    )


@pytest.fixture
def administrator():
    user = _staff("root-admin@example.test")
    _grant(user, "Account Administrators")
    return user


@pytest.fixture
def event_administrator(event):
    user = _staff("event-admin@example.test")
    _grant(user, "Account Administrators", event=event)
    return user


def _signed_in(user) -> Client:
    client = Client()
    assert staff_sign_in(client, user.email_normalized, GOOD).status_code == 302
    return client


def _setup_link() -> str:
    body = mail.outbox[-1].body
    return re.search(r"https?://\S+/accounts/setup/(\S+)/", body).group(1)


# ---------------------------------------------------------------------------
# Authorization on every endpoint
# ---------------------------------------------------------------------------


def _endpoints(target):
    return [
        ("get", reverse("staff_accounts:list"), {}),
        ("get", reverse("staff_accounts:create"), {}),
        ("post", reverse("staff_accounts:create"), {"email": "x@example.test"}),
        ("get", reverse("staff_accounts:detail", kwargs={"pk": target.pk}), {}),
        (
            "post",
            reverse("staff_accounts:status", kwargs={"pk": target.pk}),
            {"command": "suspend", "reason": "Test"},
        ),
        (
            "post",
            reverse("staff_accounts:update", kwargs={"pk": target.pk}),
            {"display_name": "X", "reason": "Test"},
        ),
        (
            "post",
            reverse("staff_accounts:credential-link", kwargs={"pk": target.pk}),
            {"purpose": "RESET"},
        ),
        (
            "post",
            reverse("staff_accounts:grant", kwargs={"pk": target.pk}),
            {"role": "registration-intake"},
        ),
        (
            "post",
            reverse("staff_accounts:revoke", kwargs={"pk": target.pk}),
            {"membership_id": str(uuid.uuid4()), "reason": "Test"},
        ),
    ]


def test_every_endpoint_refuses_a_signed_in_staff_member_without_the_permission(event):
    target = _staff("target@example.test")
    reviewer = _staff("not-admin@example.test")
    _grant(reviewer, "Registration Reviewers", event=event)
    client = _signed_in(reviewer)
    for method, url, data in _endpoints(target):
        response = getattr(client, method)(url, data)
        assert response.status_code == 403, url
    target.refresh_from_db()
    assert target.status == OperationalUserStatus.ACTIVE
    assert not mail.outbox and not CredentialSetupToken.objects.exists()


def test_every_endpoint_sends_an_anonymous_visitor_to_sign_in():
    target = _staff("target-anon@example.test")
    for method, url, data in _endpoints(target):
        response = getattr(Client(), method)(url, data)
        assert response.status_code == 302 and "/accounts/ops/sign-in/" in response["Location"]


# ---------------------------------------------------------------------------
# Lifecycle: create, invite, set a password, activate, grant -- separate steps
# ---------------------------------------------------------------------------


def test_create_invite_setup_activate_and_grant_are_separate_steps(
    administrator, event, django_capture_on_commit_callbacks
):
    client = _signed_in(administrator)
    response = client.post(
        reverse("staff_accounts:create"),
        {
            "email": "New.Staff@Example.test",
            "display_name": "New Staff",
            "account_type": "INTERNAL",
        },
    )
    account = OperationalUser.objects.get(email_normalized="new.staff@example.test")
    assert response.status_code == 302
    assert account.status == OperationalUserStatus.INVITED
    assert not account.has_usable_password()
    assert not ScopedGroupMembership.objects.filter(user=account).exists()

    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("staff_accounts:credential-link", kwargs={"pk": account.pk}),
            {"purpose": "INVITATION"},
        )
    raw = _setup_link()
    token = CredentialSetupToken.objects.get(user=account)
    assert token.token_digest != raw and raw not in token.token_digest
    # The link exists only in the email: never in an audit row or a message row.
    audit_text = " ".join(
        str(v) for v in AuditEvent.objects.values_list("after_summary", flat=True)
    )
    assert raw not in audit_text
    assert not CommunicationMessage.objects.exists()

    # Setting the password through the link neither activates nor signs in.
    visitor = Client()
    start = visitor.get(reverse("accounts:credential-setup-start", kwargs={"token": raw}))
    assert start.status_code == 302 and start["Location"] == reverse("accounts:credential-setup")
    assert start["Referrer-Policy"] == "no-referrer"
    page = visitor.get(reverse("accounts:credential-setup"))
    assert raw not in page.content.decode()
    done = visitor.post(
        reverse("accounts:credential-setup"),
        {"new_password1": CHOSEN, "new_password2": CHOSEN},
    )
    assert done.status_code == 302 and SESSION_KEY not in visitor.session
    account.refresh_from_db()
    assert account.check_password(CHOSEN) and account.status == OperationalUserStatus.INVITED
    assert staff_sign_in(Client(), account.email_normalized, CHOSEN).status_code == 200

    client.post(
        reverse("staff_accounts:status", kwargs={"pk": account.pk}),
        {"command": "activate", "reason": "Onboarded"},
    )
    staff = Client()
    assert staff_sign_in(staff, account.email_normalized, CHOSEN).status_code == 302
    # Active, but no role yet: the account opens no area.
    assert staff.get(reverse("registrations:ops-intake-list")).status_code == 403

    client.post(
        reverse("staff_accounts:grant", kwargs={"pk": account.pk}),
        {
            "role": "registration-intake",
            "event": str(event.pk),
            "organization": "*",
            "reason": "Intake desk",
        },
    )
    assert staff.get(reverse("registrations:ops-intake-list")).status_code == 200
    codes = set(AuditEvent.objects.values_list("action_code", flat=True))
    assert {
        action_codes.STAFF_ACCOUNT_CREATED,
        action_codes.STAFF_CREDENTIAL_SETUP_ISSUED,
        action_codes.STAFF_CREDENTIAL_SETUP_COMPLETED,
        action_codes.STAFF_ACCOUNT_STATUS_CHANGED,
        action_codes.STAFF_ACCOUNT_ROLE_GRANTED,
    } <= codes


def test_a_setup_link_works_once_and_a_newer_link_replaces_it(
    administrator, django_capture_on_commit_callbacks
):
    account = _staff("relink@example.test", status=OperationalUserStatus.INVITED, password=None)
    with django_capture_on_commit_callbacks(execute=True):
        admin.issue_credential_setup(actor=administrator, target=account, purpose="INVITATION")
    first = _setup_link()
    with django_capture_on_commit_callbacks(execute=True):
        admin.issue_credential_setup(actor=administrator, target=account, purpose="INVITATION")
    second = _setup_link()
    assert first != second
    assert admin.find_open_token(first) is None  # replaced
    token = admin.find_open_token(second)
    admin.complete_credential_setup(token_id=token.pk, password=CHOSEN)
    assert admin.find_open_token(second) is None  # used
    with pytest.raises(admin.AccountAdministrationError) as replay:
        admin.complete_credential_setup(token_id=token.pk, password=OTHER_CHOSEN)
    assert replay.value.code == "LINK_INVALID"
    account.refresh_from_db()
    assert account.check_password(CHOSEN)
    visitor = Client()
    response = visitor.get(reverse("accounts:credential-setup-start", kwargs={"token": second}))
    assert response.status_code == 302  # never a page at the secret-bearing URL
    assert visitor.get(response["Location"]).status_code == 404


def test_an_expired_link_is_refused(administrator, django_capture_on_commit_callbacks):
    account = _staff("expired-link@example.test", password=None)
    with django_capture_on_commit_callbacks(execute=True):
        token = admin.issue_credential_setup(actor=administrator, target=account, purpose="RESET")
    raw = _setup_link()
    CredentialSetupToken.objects.filter(pk=token.pk).update(
        created_at=timezone.now() - timedelta(days=3),
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert admin.find_open_token(raw) is None
    with pytest.raises(admin.AccountAdministrationError):
        admin.complete_credential_setup(token_id=token.pk, password=CHOSEN)


def test_a_weak_password_is_refused_and_the_link_stays_usable(
    administrator, django_capture_on_commit_callbacks
):
    account = _staff("weak@example.test", password=None)
    with django_capture_on_commit_callbacks(execute=True):
        admin.issue_credential_setup(actor=administrator, target=account, purpose="INVITATION")
    visitor = Client()
    visitor.get(reverse("accounts:credential-setup-start", kwargs={"token": _setup_link()}))
    response = visitor.post(
        reverse("accounts:credential-setup"), {"new_password1": "short", "new_password2": "short"}
    )
    assert response.status_code == 400
    assert 'value="short"' not in response.content.decode()
    assert CredentialSetupToken.objects.get(user=account).used_at is None


def test_a_reset_ends_existing_sessions(administrator, django_capture_on_commit_callbacks):
    account = _staff("reset-sessions@example.test")
    _grant(account, "Registration Intake")
    old_session = _signed_in(account)
    with django_capture_on_commit_callbacks(execute=True):
        token = admin.issue_credential_setup(actor=administrator, target=account, purpose="RESET")
    admin.complete_credential_setup(token_id=token.pk, password=CHOSEN)
    response = old_session.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302 and "/accounts/ops/sign-in/" in response["Location"]


# ---------------------------------------------------------------------------
# Loss of access takes effect immediately
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["suspend", "disable"])
def test_suspension_or_disablement_ends_the_next_request(administrator, command):
    account = _staff(f"{command}@example.test")
    _grant(account, "Registration Intake")
    session = _signed_in(account)
    assert session.get(reverse("registrations:ops-intake-list")).status_code == 200
    admin.change_status(actor=administrator, target=account, command=command, reason="Leaving")
    response = session.get(reverse("registrations:ops-intake-list"))
    assert response.status_code == 302 and SESSION_KEY not in session.session
    assert staff_sign_in(Client(), account.email_normalized, GOOD).status_code == 200


def test_an_expired_validity_ends_access(administrator):
    account = _staff("validity@example.test")
    _grant(account, "Registration Intake")
    session = _signed_in(account)
    admin.update_account(
        actor=administrator,
        target=account,
        display_name="Validity",
        active_from=None,
        active_until=timezone.now() - timedelta(seconds=1),
        reason="Contract ended",
    )
    assert session.get(reverse("registrations:ops-intake-list")).status_code == 302


def test_a_revoked_role_stops_applying_on_the_next_request(administrator):
    account = _staff("revoke@example.test")
    membership = _grant(account, "Registration Intake")
    session = _signed_in(account)
    assert session.get(reverse("registrations:ops-intake-list")).status_code == 200
    admin.revoke_role(
        actor=administrator, target=account, membership_id=membership.pk, reason="Moved team"
    )
    assert session.get(reverse("registrations:ops-intake-list")).status_code == 403
    membership.refresh_from_db()
    assert membership.active_until is not None  # kept as history, not deleted
    assert ScopedGroupMembership.objects.filter(pk=membership.pk).exists()


# ---------------------------------------------------------------------------
# Delegation, self-escalation and the last administrator
# ---------------------------------------------------------------------------


def test_administrators_never_change_themselves(administrator):
    with pytest.raises(admin.AccountAdministrationError) as status:
        admin.change_status(
            actor=administrator, target=administrator, command="suspend", reason="Self"
        )
    assert status.value.code == "SELF_CHANGE"
    with pytest.raises(admin.AccountAdministrationError) as role:
        admin.grant_role(
            actor=administrator,
            target=administrator,
            role_key="accreditation-manager",
            all_events=True,
            reason="Self",
        )
    assert role.value.code == "SELF_CHANGE"


def test_an_event_administrator_delegates_only_inside_their_event(
    event_administrator, event, other_event
):
    target = _staff("delegated@example.test")
    admin.grant_role(
        actor=event_administrator,
        target=target,
        role_key="registration-reviewer",
        event=event,
        reason="In scope",
    )
    for kwargs in ({"event": other_event}, {"all_events": True}):
        with pytest.raises(admin.AccountAdministrationError) as refused:
            admin.grant_role(
                actor=event_administrator,
                target=target,
                role_key="registration-reviewer",
                reason="Out of scope",
                **kwargs,
            )
        assert refused.value.code == "SCOPE_NOT_DELEGATABLE"
    # An account that works in another event is outside this administrator's
    # authority: no status change, no link.
    elsewhere = _staff("elsewhere@example.test")
    _grant(elsewhere, "Registration Reviewers", event=other_event)
    with pytest.raises(admin.AccountAdministrationError) as status:
        admin.change_status(
            actor=event_administrator, target=elsewhere, command="suspend", reason="Not mine"
        )
    assert status.value.code == "SCOPE_NOT_DELEGATABLE"


def test_an_empty_event_is_never_implied_and_every_event_needs_confirmation(administrator, event):
    target = _staff("broad@example.test")
    with pytest.raises(admin.AccountAdministrationError) as missing:
        admin.grant_role(
            actor=administrator, target=target, role_key="registration-intake", reason="Blank"
        )
    assert missing.value.code == "EVENT_REQUIRED"
    client = _signed_in(administrator)
    client.post(
        reverse("staff_accounts:grant", kwargs={"pk": target.pk}),
        {"role": "registration-intake", "event": "*", "organization": "*", "reason": "Broad"},
    )
    assert not ScopedGroupMembership.objects.filter(user=target).exists()
    client.post(
        reverse("staff_accounts:grant", kwargs={"pk": target.pk}),
        {
            "role": "registration-intake",
            "event": "*",
            "organization": "*",
            "reason": "Broad",
            "confirm_broad_scope": "on",
        },
    )
    assert ScopedGroupMembership.objects.get(user=target).event_edition_id is None


def test_role_scopes_are_controlled(administrator, event):
    from apps.organizations.models import Organization, OrganizationType

    organization = Organization.objects.create(
        official_name="Scoped Org",
        normalized_name="scoped org",
        organization_type=OrganizationType.MINISTRY,
    )
    target = _staff("scopes@example.test")
    with pytest.raises(admin.AccountAdministrationError) as refused:
        admin.grant_role(
            actor=administrator,
            target=target,
            role_key="attendance-policy-manager",
            event=event,
            organization=organization,
            reason="Edition-wide role",
        )
    assert refused.value.code == "INVALID_SCOPE"
    with pytest.raises(admin.AccountAdministrationError) as unknown:
        admin.grant_role(
            actor=administrator, target=target, role_key="superuser", event=event, reason="No"
        )
    assert unknown.value.code == "INVALID_ROLE"
    assert not target.is_superuser and not target.is_staff


def test_external_security_roles_never_outlive_the_account(administrator, event):
    with pytest.raises(admin.AccountAdministrationError) as needs_end:
        admin.create_account(
            actor=administrator,
            email="guard@example.test",
            display_name="Guard",
            account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
        )
    assert needs_end.value.code == "EXPIRY_REQUIRED"
    end = timezone.now() + timedelta(days=2)
    guard = admin.create_account(
        actor=administrator,
        email="guard@example.test",
        display_name="Guard",
        account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
        active_until=end,
    )
    membership = admin.grant_role(
        actor=administrator,
        target=guard,
        role_key="entry-operator",
        event=event,
        active_until=end + timedelta(days=30),
        reason="Gate duty",
    )
    assert membership.active_until == end


def test_one_of_two_administrators_can_be_removed_but_never_by_themselves(administrator):
    second = _staff("second-admin@example.test")
    second_membership = _grant(second, "Account Administrators")
    # Two unrestricted administrators: removing one is fine.
    admin.revoke_role(
        actor=administrator, target=second, membership_id=second_membership.pk, reason="Rotate"
    )
    assert admin.unrestricted_administrator_ids() == {administrator.pk}
    # A refused command leaves an audited refusal and changes nothing.
    client = _signed_in(administrator)
    client.post(
        reverse("staff_accounts:status", kwargs={"pk": administrator.pk}),
        {"command": "suspend", "reason": "Self"},
    )
    administrator.refresh_from_db()
    assert administrator.status == OperationalUserStatus.ACTIVE
    assert AuditEvent.objects.filter(
        action_code=action_codes.STAFF_ACCOUNT_ACTION_REFUSED, reason_code="SELF_CHANGE"
    ).exists()


def test_bootstrap_creates_the_first_administrator_once(django_capture_on_commit_callbacks):
    call_command("bootstrap_account_administrator", "--email", "first@example.test")
    first = OperationalUser.objects.get(email_normalized="first@example.test")
    assert first.status == OperationalUserStatus.ACTIVE and not first.has_usable_password()
    assert admin.unrestricted_administrator_ids() == {first.pk}
    assert "/accounts/setup/" in mail.outbox[-1].body
    with pytest.raises(CommandError):
        call_command("bootstrap_account_administrator", "--email", "second@example.test")
    assert not OperationalUser.objects.filter(email_normalized="second@example.test").exists()
