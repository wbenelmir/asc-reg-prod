"""Automatic temporary external-security account expiry and gate scoping
(Phase 3 Prompt 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import (
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accounts.policies import has_scoped_permission
from apps.accounts.selectors import scope_filtered_queryset
from apps.accounts.services import (
    ExternalSecurityAccountError,
    create_external_security_account,
    deactivate_external_security_account,
    expire_lapsed_temporary_accounts,
)
from apps.audit.models import AuditEvent
from apps.entry.models import EntryOperatorSession
from apps.entry.tests import factories
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed(db):
    factories.seed_reference_values()


@pytest.fixture
def event():
    return factories.make_event("ACCEXP")


@pytest.fixture
def layout(event):
    return factories.VenueLayout(event)


@pytest.fixture
def administrator():
    return factories.make_user("admin@example.test")


def _external(event, layout, *, expires_in=timedelta(days=1)):
    user = factories.make_user(
        "ext@example.test",
        group_name="Entry Operators",
        event=event,
        gate=layout.gate_a,
        account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
        active_until=timezone.now() + expires_in,
    )
    return user


class TestAutomaticExpiry:
    def test_lapsed_account_is_marked_expired_and_loses_everything(
        self, event, layout, administrator
    ):
        device, _ = factories.enroll_device(
            event=event,
            layout=layout,
            admin=factories.make_user(
                "da@example.test", group_name="Entry Device Administrators", event=event
            ),
        )
        user = _external(event, layout)
        factories.open_checkpoint(device=device, user=user, zone=layout.main)
        later = timezone.now() + timedelta(days=2)
        assert expire_lapsed_temporary_accounts(now=later) == 1
        user.refresh_from_db()
        assert user.status == OperationalUserStatus.EXPIRED
        assert not ScopedGroupMembership.objects.filter(user=user, active_until__gt=later).exists()
        assert not EntryOperatorSession.objects.filter(user=user, ended_at__isnull=True).exists()
        assert (
            AuditEvent.objects.filter(
                action_code="ACC_EXTERNAL_SECURITY_ACCOUNT_EXPIRED", target_uuid=user.pk
            ).count()
            == 1
        )
        # Idempotent.
        assert expire_lapsed_temporary_accounts(now=later) == 0

    def test_unexpired_and_internal_accounts_are_untouched(self, event, layout):
        user = _external(event, layout, expires_in=timedelta(days=3))
        internal = factories.make_user(
            "internal@example.test", active_until=timezone.now() - timedelta(days=1)
        )
        assert expire_lapsed_temporary_accounts() == 0
        user.refresh_from_db()
        internal.refresh_from_db()
        assert user.status == OperationalUserStatus.ACTIVE
        assert internal.status == OperationalUserStatus.ACTIVE

    def test_expiry_is_enforced_before_the_sweep_runs(self, event, layout):
        user = _external(event, layout)
        user.active_until = timezone.now() - timedelta(seconds=1)
        user.save()
        assert user.is_active is False
        assert not has_scoped_permission(
            user,
            "entry.verify_entry",
            event_edition_id=event.pk,
            venue_id=layout.venue.pk,
            gate_id=layout.gate_a.pk,
        )

    def test_management_command_runs_both_sweeps(self, event, layout, capsys):
        user = _external(event, layout)
        user.active_until = timezone.now() - timedelta(seconds=1)
        user.save()
        call_command("expire_entry_access")
        assert "expired accounts: 1" in capsys.readouterr().out

    def test_deactivation_ends_entry_sessions(self, event, layout, administrator):
        device, _ = factories.enroll_device(
            event=event,
            layout=layout,
            admin=factories.make_user(
                "da2@example.test", group_name="Entry Device Administrators", event=event
            ),
        )
        user = _external(event, layout)
        factories.open_checkpoint(device=device, user=user, zone=layout.main)
        deactivate_external_security_account(user=user, actor=administrator, reason="Shift ended")
        assert not EntryOperatorSession.objects.filter(user=user, ended_at__isnull=True).exists()


class TestGateScopedExternalAccounts:
    def test_account_can_be_created_with_a_gate_scope(self, event, layout, administrator):
        user = create_external_security_account(
            email="guard@example.test",
            display_name="Synthetic Guard",
            expires_at=timezone.now() + timedelta(days=1),
            created_by=administrator,
            event_edition=event,
            gate=layout.gate_a,
        )
        membership = ScopedGroupMembership.objects.get(user=user)
        assert membership.gate == layout.gate_a
        assert membership.venue == layout.venue
        assert membership.active_until == user.active_until

    def test_gate_scope_requires_the_matching_event(self, event, layout, administrator):
        other = factories.make_event("ACCEXP2")
        with pytest.raises(ExternalSecurityAccountError):
            create_external_security_account(
                email="guard2@example.test",
                display_name="Synthetic Guard",
                expires_at=timezone.now() + timedelta(days=1),
                created_by=administrator,
                event_edition=other,
                gate=layout.gate_a,
            )


class TestGateScopeNeverLeaksIntoOtherApps:
    def test_gate_scoped_view_registration_grant_shows_no_registrations(self, event, layout):
        """A misconfigured gate-scoped membership granting a back-office
        permission must not open event-wide registration browsing."""
        from django.contrib.auth.models import Group, Permission

        factories.make_registration(event=event, person=factories.make_person())
        group, _ = Group.objects.get_or_create(name="Synthetic misconfigured group")
        group.permissions.add(
            Permission.objects.get(
                content_type__app_label="registrations", codename="view_registration"
            )
        )
        user = factories.make_user("misconfigured@example.test")
        factories.grant(user, "Synthetic misconfigured group", event=event, gate=layout.gate_a)
        visible = scope_filtered_queryset(
            user,
            Registration.objects.all(),
            app_label="registrations",
            codename="view_registration",
            organization_field="source_organization_id",
        )
        assert visible.count() == 0
        assert not has_scoped_permission(user, "registrations.view_registration")
