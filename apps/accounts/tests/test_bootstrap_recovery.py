"""The first administrator can be recovered when the setup email fails (R04).

`bootstrap_account_administrator` commits the account before sending the
email. When sending fails, the unsent link is revoked and audited, the
command says so, and `--resend` issues a new link for that same pending
account only. SMTP is mocked; no email leaves the test process."""

from __future__ import annotations

import re
import smtplib
from datetime import timedelta

import pytest
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
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.audit import action_codes
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

FIRST = "first-admin@example.test"
CHOSEN = "first-admin-Chosen-Battery-9"
ESTABLISHED = "established-Admin-Choice-1"
OPS_ADMIN = "operations-area-Admin-Choice-3"


@pytest.fixture
def smtp_down(monkeypatch):
    def refuse(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected("synthetic outage")

    monkeypatch.setattr("django.core.mail.send_mail", refuse)


def _bootstrap_with_failed_email(smtp_down):
    with pytest.raises(CommandError) as failed:
        call_command("bootstrap_account_administrator", "--email", FIRST)
    return failed.value


def _printed_link(capsys) -> str:
    out = capsys.readouterr().out
    return re.search(r"/accounts/setup/(\S+)/", out).group(1)


def _complete(link: str, password: str = CHOSEN):
    client = Client()
    assert (
        client.get(reverse("accounts:credential-setup-start", kwargs={"token": link})).status_code
        == 302
    )
    return client.post(
        reverse("accounts:credential-setup"),
        {"new_password1": password, "new_password2": password},
    )


def test_a_failed_email_is_reported_and_the_unsent_link_is_revoked(smtp_down):
    error = _bootstrap_with_failed_email(smtp_down)
    message = str(error)
    assert "could not be emailed" in message and "SMTPServerDisconnected" in message
    assert "--resend" in message
    assert "/accounts/setup/" not in message
    user = OperationalUser.objects.get(email_normalized=FIRST)
    assert user.status == OperationalUserStatus.ACTIVE and not user.has_usable_password()
    assert user.pk in admin.unrestricted_administrator_ids()
    assert not CredentialSetupToken.objects.filter(user=user, revoked_at__isnull=True).exists()
    failure = AuditEvent.objects.get(
        action_code=action_codes.STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED
    )
    assert failure.result == "FAILURE" and failure.reason_code == "SMTPServerDisconnected"
    assert failure.target_uuid == user.pk
    # Without the recovery, the account would be stranded: a plain rerun is refused.
    with pytest.raises(CommandError, match="ADMINISTRATOR_EXISTS"):
        call_command("bootstrap_account_administrator", "--email", FIRST, "--deliver", "terminal")


def test_resend_gives_the_pending_administrator_a_working_link(smtp_down, monkeypatch, capsys):
    _bootstrap_with_failed_email(smtp_down)
    call_command(
        "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
    )
    link = _printed_link(capsys)
    response = _complete(link)
    assert response.status_code == 302
    user = OperationalUser.objects.get(email_normalized=FIRST)
    assert user.check_password(CHOSEN)
    assert (
        AuditEvent.objects.filter(
            action_code=action_codes.STAFF_ACCOUNT_ADMINISTRATOR_SETUP_RESENT, target_uuid=user.pk
        ).count()
        == 1
    )
    # The membership was not duplicated: still exactly one administrator role.
    assert ScopedGroupMembership.objects.filter(user=user).count() == 1


def test_resend_by_email_once_the_mail_service_is_back(smtp_down, monkeypatch):
    _bootstrap_with_failed_email(smtp_down)
    monkeypatch.undo()
    call_command("bootstrap_account_administrator", "--email", FIRST, "--resend")
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [FIRST]
    link = re.search(r"/accounts/setup/(\S+)/", mail.outbox[0].body).group(1)
    assert _complete(link).status_code == 302


def test_resend_rotates_the_link_and_keeps_expiry(smtp_down, capsys):
    _bootstrap_with_failed_email(smtp_down)
    call_command(
        "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
    )
    first = _printed_link(capsys)
    call_command(
        "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
    )
    second = _printed_link(capsys)
    assert admin.find_open_token(first) is None  # replaced
    token = admin.find_open_token(second)
    assert token is not None
    assert token.expires_at <= timezone.now() + timedelta(
        seconds=admin.settings.OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS + 5
    )
    # An expired link stops working; a resend gives a fresh one.
    CredentialSetupToken.objects.filter(pk=token.pk).update(
        created_at=timezone.now() - timedelta(days=3),
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    assert admin.find_open_token(second) is None
    call_command(
        "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
    )
    assert admin.find_open_token(_printed_link(capsys)) is not None


def test_resend_is_refused_for_another_account(smtp_down):
    _bootstrap_with_failed_email(smtp_down)
    OperationalUser.objects.create_user(
        email="someone-else@example.test", password=None, status=OperationalUserStatus.ACTIVE
    )
    tokens_before = CredentialSetupToken.objects.count()
    with pytest.raises(CommandError, match="NOT_PENDING_BOOTSTRAP"):
        call_command(
            "bootstrap_account_administrator", "--email", "someone-else@example.test", "--resend"
        )
    with pytest.raises(CommandError, match="NOT_FOUND"):
        call_command(
            "bootstrap_account_administrator", "--email", "nobody@example.test", "--resend"
        )
    assert CredentialSetupToken.objects.count() == tokens_before
    assert (
        ScopedGroupMembership.objects.filter(
            user__email_normalized="someone-else@example.test"
        ).count()
        == 0
    )


def test_resend_is_refused_after_the_setup_was_completed(smtp_down, capsys):
    _bootstrap_with_failed_email(smtp_down)
    call_command(
        "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
    )
    _complete(_printed_link(capsys))
    with pytest.raises(CommandError, match="SETUP_COMPLETED"):
        call_command(
            "bootstrap_account_administrator", "--email", FIRST, "--resend", "--deliver", "terminal"
        )
    assert "/accounts/setup/" not in capsys.readouterr().out


def test_resend_is_refused_when_another_administrator_can_send_links(smtp_down):
    _bootstrap_with_failed_email(smtp_down)
    other = OperationalUser.objects.create_user(
        email="established-admin@example.test",
        password=ESTABLISHED,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=other, group=Group.objects.get(name="Account Administrators"), granted_by=other
    )
    with pytest.raises(CommandError, match="ADMINISTRATOR_EXISTS"):
        call_command("bootstrap_account_administrator", "--email", FIRST, "--resend")


def test_resend_is_refused_once_the_pending_account_was_suspended(smtp_down):
    _bootstrap_with_failed_email(smtp_down)
    OperationalUser.objects.filter(email_normalized=FIRST).update(
        status=OperationalUserStatus.SUSPENDED
    )
    with pytest.raises(CommandError, match="NOT_PENDING_BOOTSTRAP"):
        call_command("bootstrap_account_administrator", "--email", FIRST, "--resend")


def test_existing_and_resend_cannot_be_combined():
    with pytest.raises(CommandError):
        call_command("bootstrap_account_administrator", "--email", FIRST, "--resend", "--existing")


@pytest.mark.django_db(transaction=True)
def test_the_operations_area_reports_a_failed_setup_email(monkeypatch):
    """In the operations area the email is sent after commit; a failure is
    shown to the administrator, the unsent link is revoked and audited."""
    from apps.accounts.tests.sign_in import staff_sign_in

    administrator = OperationalUser.objects.create_user(
        email="ops-admin@example.test", password=OPS_ADMIN, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=administrator,
        group=Group.objects.get(name="Account Administrators"),
        granted_by=administrator,
    )
    target = admin.create_account(
        actor=administrator,
        email="invitee@example.test",
        display_name="Invitee",
        account_type="INTERNAL",
    )
    client = Client()
    assert staff_sign_in(client, administrator.email_normalized, OPS_ADMIN).status_code == 302

    def refuse(*args, **kwargs):
        raise smtplib.SMTPRecipientsRefused({})

    monkeypatch.setattr("django.core.mail.send_mail", refuse)
    response = client.post(
        reverse("staff_accounts:credential-link", kwargs={"pk": target.pk}),
        {"purpose": "INVITATION"},
        follow=True,
    )
    assert "could not be sent, so that link was cancelled" in response.content.decode()
    assert not CredentialSetupToken.objects.filter(user=target, revoked_at__isnull=True).exists()
    assert AuditEvent.objects.filter(
        action_code=action_codes.STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED,
        target_uuid=target.pk,
        reason_code="SMTPRecipientsRefused",
    ).exists()
