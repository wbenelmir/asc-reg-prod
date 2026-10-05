"""`manage.py provision_staging_uat`: safeguards, preview, apply, repeat, scope and retire.

Runs on the PostgreSQL test database with the test settings (the only settings
besides `config.settings.staging` that enable the command). Synthetic data only.
"""

from __future__ import annotations

import io

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.accounts.models import (
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
    ScopedGroupMembershipStatus,
)
from apps.accounts.policies import has_scoped_permission
from apps.audit.models import AuditEvent
from apps.core.services import staging_provisioning as provisioning
from apps.events.models import EventEdition, EventEditionStatus, PublicRegistrationMode
from apps.invitations.models import (
    InvitationCampaign,
    InvitationCampaignStatus,
    InvitationLink,
    InvitationLinkStatus,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _approved_country_catalog():
    """A deployed database has the approved catalog (`core.0005`); provisioning
    only checks it. A `transaction=True` test elsewhere may have flushed it."""
    from apps.core.models import Country
    from apps.core.reference_data import countries_v1
    from apps.core.services.country_catalog import reconcile_countries

    reconcile_countries(Country, countries_v1, apply=True)


HOST = "localhost"
PATTERN = "uat-{key}@uat-mail.example.invalid"


def _run(*extra: str) -> str:
    out = io.StringIO()
    call_command(
        "provision_staging_uat",
        "--confirm-host",
        HOST,
        "--email-pattern",
        PATTERN,
        *extra,
        stdout=out,
    )
    return out.getvalue()


def _email(key: str) -> str:
    return PATTERN.format(key=key)


def _snapshot() -> tuple:
    """The row count of every table, plus the states provisioning could change.

    Equal before and after a run proves that the run wrote nothing: no row in
    any table (audit and ownership records included) and no status change.
    """
    from django.apps import apps

    return (
        sorted(
            (model._meta.db_table, model._base_manager.count())
            for model in apps.get_models()
            if not model._meta.proxy
        ),
        sorted(
            OperationalUser.objects.values_list(
                "email_normalized", "status", "is_superuser", "is_staff"
            )
        ),
        sorted(ScopedGroupMembership.objects.values_list("id", "status")),
        sorted(InvitationCampaign.objects.values_list("public_reference", "status")),
        sorted(InvitationLink.objects.values_list("id", "status")),
        sorted(EventEdition.objects.values_list("code", "status")),
    )


@pytest.mark.parametrize(
    ("extra_settings", "host", "pattern"),
    [
        ({"STAGING_PROVISIONING_ENABLED": False}, HOST, PATTERN),
        ({}, "registration.example.invalid", PATTERN),
        ({"DATABASE_PROCESS_ROLE": "migration"}, HOST, PATTERN),
        ({}, HOST, "uat@uat-mail.example.invalid"),
        ({}, HOST, "uat-{key}-{key}@uat-mail.example.invalid"),
        ({}, HOST, "uat-{key}"),
    ],
)
def test_every_safeguard_refuses_before_reading_or_writing(extra_settings, host, pattern) -> None:
    before = _snapshot()
    with override_settings(**extra_settings), pytest.raises(CommandError) as excinfo:
        call_command(
            "provision_staging_uat",
            "--confirm-host",
            host,
            "--email-pattern",
            pattern,
            "--apply",
            stdout=io.StringIO(),
        )
    assert excinfo.value.returncode == 2
    assert _snapshot() == before


def test_the_preview_writes_nothing() -> None:
    before = _snapshot()
    text = _run()
    assert "PREVIEW" in text and "nothing was written" in text
    assert "[create   ] account" in text
    assert _snapshot() == before


def test_apply_creates_exactly_the_documented_matrix() -> None:
    text = _run("--apply")
    users = {
        key: OperationalUser.objects.get(email_normalized=_email(key))
        for key in (provisioning.PROVISIONING_KEY, *(spec.key for spec in provisioning.ACCOUNTS))
    }
    for key, user in users.items():
        assert user.has_usable_password() is False, key
        assert user.is_superuser is False and user.is_staff is False, key
    assert users[provisioning.PROVISIONING_KEY].status == OperationalUserStatus.SUSPENDED
    assert users["external"].account_type == OperationalUserAccountType.EXTERNAL_SECURITY
    assert users["temp"].active_until is not None

    e1 = EventEdition.objects.get(code=provisioning.E1_CODE)
    e2 = EventEdition.objects.get(code=provisioning.E2_CODE)
    assert e2.status == EventEditionStatus.REGISTRATION_CLOSED
    assert e2.public_registration_mode == PublicRegistrationMode.INVITATION_ONLY
    assert e2.interest_topics.count() == e1.interest_topics.count()
    assert EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).count() == 1

    for spec in provisioning.ACCOUNTS:
        granted = {
            (m.group.name, m.event_edition.code, m.organization_id is not None)
            for m in ScopedGroupMembership.objects.filter(
                user=users[spec.key], reason=provisioning.PROVISIONING_REASON
            )
        }
        expected = {
            (m.group, e1.code if m.event == "E1" else e2.code, m.organization)
            for m in spec.memberships
        }
        assert granted == expected, spec.key

    for reference, _event, _name in provisioning.CAMPAIGNS:
        campaign = InvitationCampaign.objects.get(public_reference=reference)
        assert campaign.status == InvitationCampaignStatus.ACTIVE
        assert (
            InvitationLink.objects.filter(
                campaign=campaign, status=InvitationLinkStatus.ACTIVE
            ).count()
            == 1
        )
        assert f"{reference}: http://localhost:8000/invite/" in text

    assert f"python manage.py changepassword {_email('reviewer')}" in text
    summaries = " ".join(
        str(event.after_summary)
        for event in AuditEvent.objects.filter(action_code__startswith="ACC_STAGING_UAT")
    )
    assert "@" not in summaries
    assert AuditEvent.objects.filter(
        action_code="ACC_STAGING_UAT_MEMBERSHIP_GRANTED"
    ).count() == sum(len(spec.memberships) for spec in provisioning.ACCOUNTS)


def test_roles_have_their_scope_and_nothing_more() -> None:
    _run("--apply")
    e1 = EventEdition.objects.get(code=provisioning.E1_CODE)
    e2 = EventEdition.objects.get(code=provisioning.E2_CODE)
    other = EventEdition.objects.create(
        code="UNRELATED",
        name="Unrelated",
        timezone="UTC",
        starts_at=e1.starts_at,
        ends_at=e1.ends_at,
    )

    def can(key: str, permission: str, event) -> bool:
        user = OperationalUser.objects.get(email_normalized=_email(key))
        return has_scoped_permission(user, permission, event_edition_id=event.pk)

    assert can("reviewer", "people.view_identityverification", e1)
    assert can("reviewer", "people.view_identityverification", e2)
    assert not can("reviewer", "people.view_identityverification", other)
    assert not can("reviewer", "people.reject_identity", e1)
    assert can("manager", "people.reject_identity", e1)
    assert not can("intake", "people.view_identityverification", e1)
    assert can("intake", "registrations.view_registration", e1)
    assert can("outsider", "people.view_identityverification", e2)
    assert not can("outsider", "people.view_identityverification", e1)
    assert not can("admin", "registrations.view_registration", e1)
    assert not can("comms", "people.view_identityverification", e1)


def test_a_repeat_run_reuses_everything_and_shows_no_new_link() -> None:
    _run("--apply")
    before = (
        OperationalUser.objects.count(),
        ScopedGroupMembership.objects.count(),
        InvitationLink.objects.count(),
    )
    text = _run("--apply")
    after = (
        OperationalUser.objects.count(),
        ScopedGroupMembership.objects.count(),
        InvitationLink.objects.count(),
    )
    assert before == after
    assert "[create" not in text
    assert "/invite/" not in text


def test_a_conflicting_existing_record_stops_the_run_and_writes_nothing() -> None:
    OperationalUser.objects.create_superuser(email=_email("reviewer"), password=None)
    before = _snapshot()
    with pytest.raises(CommandError) as excinfo:
        call_command(
            "provision_staging_uat",
            "--confirm-host",
            HOST,
            "--email-pattern",
            PATTERN,
            "--apply",
            stdout=io.StringIO(),
        )
    assert excinfo.value.returncode == 1
    assert _snapshot() == before


def test_a_second_open_edition_is_a_conflict() -> None:
    e1 = EventEdition.objects.get(code=provisioning.E1_CODE)
    EventEdition.objects.create(
        code="ALSO-OPEN",
        name="Also open",
        timezone="UTC",
        starts_at=e1.starts_at,
        ends_at=e1.ends_at,
        status=EventEditionStatus.REGISTRATION_OPEN,
    )
    before = _snapshot()
    with pytest.raises(CommandError) as excinfo:
        _run("--apply")
    assert excinfo.value.returncode == 1
    assert _snapshot() == before


def test_the_operator_sets_a_password_with_changepassword(monkeypatch) -> None:
    _run("--apply")
    import django.contrib.auth.management.commands.changepassword as changepassword

    chosen = "Synthetic-Uat-Passphrase-2026!"
    monkeypatch.setattr(changepassword.getpass, "getpass", lambda prompt="": chosen)
    call_command("changepassword", _email("reviewer"), stdout=io.StringIO())
    user = OperationalUser.objects.get(email_normalized=_email("reviewer"))
    assert user.check_password(chosen)
    assert user.is_active


def test_retire_disables_accounts_closes_campaigns_and_deletes_nothing() -> None:
    _run("--apply")
    preview = _run("--retire")
    assert "[retire" in preview
    assert OperationalUser.objects.filter(status=OperationalUserStatus.DISABLED).count() == 0
    _run("--retire", "--apply")
    for spec in provisioning.ACCOUNTS:
        user = OperationalUser.objects.get(email_normalized=_email(spec.key))
        assert user.status == OperationalUserStatus.DISABLED
        assert not ScopedGroupMembership.objects.filter(
            user=user, status=ScopedGroupMembershipStatus.ACTIVE
        ).exists()
    for reference, _event, _name in provisioning.CAMPAIGNS:
        campaign = InvitationCampaign.objects.get(public_reference=reference)
        assert campaign.status == InvitationCampaignStatus.CLOSED
        assert not campaign.links.filter(status=InvitationLinkStatus.ACTIVE).exists()
    assert EventEdition.objects.get(code=provisioning.E2_CODE).status == EventEditionStatus.ARCHIVED
    assert AuditEvent.objects.filter(action_code="CORE_STAGING_UAT_RETIRED").count() == 1
    # A later apply with the same pattern refuses instead of silently re-enabling.
    with pytest.raises(CommandError) as excinfo:
        _run("--apply")
    assert excinfo.value.returncode == 1


def test_production_refuses_to_enable_provisioning() -> None:
    from config.settings.validation import (
        DeploymentConfigurationError,
        check_staging_provisioning_flag,
    )

    check_staging_provisioning_flag("config.settings.staging", True)
    check_staging_provisioning_flag("config.settings.production", False)
    for module in ("config.settings.production", "config.settings.production_migration"):
        with pytest.raises(DeploymentConfigurationError):
            check_staging_provisioning_flag(module, True)


def test_groups_exist_for_every_documented_role() -> None:
    names = {m.group for spec in provisioning.ACCOUNTS for m in spec.memberships}
    assert names <= set(Group.objects.values_list("name", flat=True))


# -- ownership: only records the provisioning set created are reused or retired --


def _apply_exit_code(pattern: str = PATTERN) -> int:
    try:
        call_command(
            "provision_staging_uat",
            "--confirm-host",
            HOST,
            "--email-pattern",
            pattern,
            "--apply",
            stdout=io.StringIO(),
        )
    except CommandError as exc:
        return exc.returncode
    return 0


def _grantor() -> OperationalUser:
    return OperationalUser.objects.create_user(
        email="grantor@unrelated.example.invalid",
        password=None,
        display_name="Unrelated synthetic grantor",
        status=OperationalUserStatus.ACTIVE,
    )


def _grant(user, group_name: str, event) -> ScopedGroupMembership:
    grantor = OperationalUser.objects.filter(
        email_normalized="grantor@unrelated.example.invalid"
    ).first()
    return ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        granted_by=grantor or _grantor(),
        reason="Unrelated synthetic grant",
    )


def _unrelated_account(key: str, *, scoped_group: str | None = None) -> OperationalUser:
    user = OperationalUser.objects.create_user(
        email=_email(key),
        password=None,
        display_name="Unrelated synthetic account",
        status=OperationalUserStatus.ACTIVE,
    )
    if scoped_group:
        _grant(user, scoped_group, EventEdition.objects.get(code=provisioning.E1_CODE))
    return user


@pytest.mark.parametrize("scoped_group", [None, "Accreditation Managers"])
def test_an_unrelated_account_matching_the_pattern_stops_apply(scoped_group) -> None:
    user = _unrelated_account("reviewer", scoped_group=scoped_group)
    before = _snapshot()
    exit_code = _apply_exit_code()
    assert not ScopedGroupMembership.objects.filter(
        user=user, reason=provisioning.PROVISIONING_REASON
    ).exists()
    assert _snapshot() == before
    assert exit_code == 1


def test_retire_never_touches_an_unrelated_account_matching_the_pattern() -> None:
    manager = _unrelated_account("manager", scoped_group="Accreditation Managers")
    administrator = OperationalUser.objects.create_superuser(email=_email("admin"), password=None)
    before = _snapshot()

    report = provisioning.retire(email_pattern=PATTERN, confirm_host=HOST, apply=True)

    manager.refresh_from_db()
    administrator.refresh_from_db()
    assert manager.status == OperationalUserStatus.ACTIVE
    assert administrator.status == OperationalUserStatus.ACTIVE
    assert ScopedGroupMembership.objects.get(user=manager).status == (
        ScopedGroupMembershipStatus.ACTIVE
    )
    assert _snapshot() == before
    assert report.conflicts
    with pytest.raises(CommandError) as excinfo:
        _run("--retire", "--apply")
    assert excinfo.value.returncode == 1


def test_retire_suspends_only_the_memberships_provisioning_granted() -> None:
    _run("--apply")
    reviewer = OperationalUser.objects.get(email_normalized=_email("reviewer"))
    extra = _grant(
        reviewer, "Communication Operators", EventEdition.objects.get(code=provisioning.E1_CODE)
    )
    _run("--retire", "--apply")
    reviewer.refresh_from_db()
    extra.refresh_from_db()
    assert reviewer.status == OperationalUserStatus.DISABLED
    assert extra.status == ScopedGroupMembershipStatus.ACTIVE
    assert not ScopedGroupMembership.objects.filter(
        user=reviewer,
        reason=provisioning.PROVISIONING_REASON,
        status=ScopedGroupMembershipStatus.ACTIVE,
    ).exists()


def _collide(kind: str):
    """An unrelated record that carries one of the provisioning set's fixed identifiers."""
    from apps.core.models import Country
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        issue_initial_link,
    )
    from apps.organizations.services import create_organization

    e1 = EventEdition.objects.get(code=provisioning.E1_CODE)
    if kind == "event":
        return EventEdition.objects.create(
            code=provisioning.E2_CODE,
            name="Unrelated edition",
            timezone="UTC",
            starts_at=e1.starts_at,
            ends_at=e1.ends_at,
            status=EventEditionStatus.REGISTRATION_CLOSED,
        )
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    if kind == "organization":
        return create_organization(
            official_name=provisioning.ORGANIZATION_NAME,
            organization_type="STARTUP",
            country_code_id="DZ",
        )
    organization = create_organization(
        official_name="Unrelated synthetic organization",
        organization_type="STARTUP",
        country_code_id="DZ",
    )
    grantor = _grantor()
    campaign = create_campaign(
        event_edition=e1,
        organization=organization,
        name="Unrelated campaign",
        public_reference=provisioning.CAMPAIGNS[0][0],
        capacity=10,
        created_by=grantor,
    )
    campaign = change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE, actor=grantor)
    issue_initial_link(campaign, actor=grantor)
    return campaign


@pytest.mark.parametrize("kind", ["event", "organization", "campaign"])
def test_an_unrelated_record_with_a_provisioning_identifier_stops_apply(kind) -> None:
    _collide(kind)
    before = _snapshot()
    exit_code = _apply_exit_code()
    assert _snapshot() == before
    assert exit_code == 1


@pytest.mark.parametrize("kind", ["event", "campaign"])
def test_retire_never_closes_or_archives_an_unrelated_record(kind) -> None:
    record = _collide(kind)
    before = _snapshot()
    report = provisioning.retire(email_pattern=PATTERN, confirm_host=HOST, apply=True)
    record.refresh_from_db()
    if kind == "event":
        assert record.status == EventEditionStatus.REGISTRATION_CLOSED
    else:
        assert record.status == InvitationCampaignStatus.ACTIVE
        assert record.links.filter(status=InvitationLinkStatus.ACTIVE).count() == 1
    assert _snapshot() == before
    assert report.conflicts


def test_a_second_provisioning_set_cannot_take_over_the_first() -> None:
    _run("--apply")
    before = _snapshot()
    exit_code = _apply_exit_code("uat2-{key}@uat-mail.example.invalid")
    assert _snapshot() == before
    assert exit_code == 1
    report = provisioning.retire(
        email_pattern="uat2-{key}@uat-mail.example.invalid", confirm_host=HOST, apply=True
    )
    assert report.conflicts
    assert _snapshot() == before


def test_a_repeat_run_changes_no_ownership_record() -> None:
    from apps.core.models import StagingProvisionedObject, StagingProvisioningSet

    _run("--apply")
    owned = sorted(StagingProvisionedObject.objects.values_list("object_type", "object_id"))
    assert StagingProvisioningSet.objects.count() == 1
    _run("--apply")
    assert sorted(StagingProvisionedObject.objects.values_list("object_type", "object_id")) == owned


def test_a_suspended_provisioned_membership_is_not_granted_again() -> None:
    _run("--apply")
    reviewer = OperationalUser.objects.get(email_normalized=_email("reviewer"))
    ScopedGroupMembership.objects.filter(user=reviewer).update(
        status=ScopedGroupMembershipStatus.SUSPENDED
    )
    before = _snapshot()
    exit_code = _apply_exit_code()
    assert _snapshot() == before
    assert exit_code == 1


def test_retire_stops_when_a_provisioned_account_was_made_privileged() -> None:
    _run("--apply")
    OperationalUser.objects.filter(email_normalized=_email("reviewer")).update(
        is_superuser=True, is_staff=True
    )
    before = _snapshot()
    report = provisioning.retire(email_pattern=PATTERN, confirm_host=HOST, apply=True)
    assert _snapshot() == before
    assert report.conflicts
    with pytest.raises(CommandError) as excinfo:
        _run("--retire", "--apply")
    assert excinfo.value.returncode == 1


def test_retire_retires_exactly_the_provisioned_set() -> None:
    from apps.invitations.services import rotate_link

    _run("--apply")
    unrelated = OperationalUser.objects.create_user(
        email="someone@unrelated.example.invalid",
        password=None,
        display_name="Unrelated synthetic account",
        status=OperationalUserStatus.ACTIVE,
    )
    unrelated_grant = _grant(
        unrelated, "Registration Reviewers", EventEdition.objects.get(code=provisioning.E1_CODE)
    )
    e1_campaign = InvitationCampaign.objects.get(public_reference=provisioning.CAMPAIGNS[0][0])
    rotate_link(e1_campaign, actor=None)  # a tester rotated the UAT link during UAT

    _run("--retire", "--apply")

    for key in (spec.key for spec in provisioning.ACCOUNTS):
        user = OperationalUser.objects.get(email_normalized=_email(key))
        assert user.status == OperationalUserStatus.DISABLED, key
        assert not ScopedGroupMembership.objects.filter(
            user=user, status=ScopedGroupMembershipStatus.ACTIVE
        ).exists(), key
    assert not InvitationLink.objects.filter(
        campaign__public_reference__in=[reference for reference, *_ in provisioning.CAMPAIGNS],
        status=InvitationLinkStatus.ACTIVE,
    ).exists()
    unrelated.refresh_from_db()
    unrelated_grant.refresh_from_db()
    assert unrelated.status == OperationalUserStatus.ACTIVE
    assert unrelated_grant.status == ScopedGroupMembershipStatus.ACTIVE
    from apps.core.models import StagingProvisioningSet

    assert StagingProvisioningSet.objects.get().retired_at is not None
