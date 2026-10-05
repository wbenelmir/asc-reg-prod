"""Phase 3 Prompt 8 (P8-08): organization-scoped memberships grant no
checkpoint capability.

A checkpoint is scoped by event edition, venue and gate -- never by an
organization. The shared `has_scoped_permission` applies no organization
filter when the caller names none, so before this correction a membership
narrowed to ONE organization authorized verification, identity lookups,
admission, overrides and the monitor for EVERY participant at every gate of
the event. All data is synthetic.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission

from apps.accounts.models import OperationalUserAccountType
from apps.accounts.policies import has_scoped_permission
from apps.entry.models import VerificationMethod
from apps.entry.policies import checkpoint_permission, has_checkpoint_permission
from apps.entry.services import EntryPermissionError
from apps.entry.services.sessions import (
    CheckpointUnavailable,
    resolve_checkpoint,
    start_checkpoint_session,
)
from apps.entry.tests import factories
from apps.organizations.models import Organization, OrganizationType

pytestmark = pytest.mark.django_db

CHECKPOINT_CODENAMES = [
    "verify_entry",
    "lookup_identity_reference",
    "lookup_registration_reference",
    "manual_search_entry",
    "override_entry",
    "view_entryevent",
    "view_restriction_reason",
]


@pytest.fixture
def organization(db):
    return Organization.objects.create(
        official_name="Synthetic Partner Org",
        normalized_name="synthetic partner org",
        organization_type=OrganizationType.INSTITUTION,
    )


def _scope(layout, gate=None):
    gate = gate or layout.gate_a
    return {
        "event_edition_id": gate.venue.event_edition_id,
        "venue_id": gate.venue_id,
        "gate_id": gate.pk,
    }


def _grant_everything(user, *, event, layout, organization=None, gate=None):
    """Both entry groups, together holding every checkpoint permission."""
    for group_name in ("Entry Operators", "Entry Supervisors"):
        factories.grant(
            user,
            group_name,
            event=event,
            organization=organization,
            gate=gate if gate is not None else layout.gate_a,
        )


# ---------------------------------------------------------------------------
# Policy boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codename", CHECKPOINT_CODENAMES)
def test_an_organization_scoped_membership_grants_no_checkpoint_permission(
    codename, event, layout, organization
):
    user = factories.make_user("org.operator@example.test")
    _grant_everything(user, event=event, layout=layout, organization=organization)
    assert has_checkpoint_permission(user, codename, **_scope(layout)) is False


def test_an_event_wide_organization_scoped_membership_is_denied_too(event, layout, organization):
    user = factories.make_user("org.eventwide@example.test")
    factories.grant(user, "Entry Supervisors", event=event, organization=organization)
    for codename in CHECKPOINT_CODENAMES:
        assert has_checkpoint_permission(user, codename, **_scope(layout)) is False


def test_an_organization_scoped_external_security_membership_is_denied(event, layout, organization):
    external = factories.make_user(
        "external.security@example.test",
        account_type=OperationalUserAccountType.EXTERNAL_SECURITY,
    )
    # The temporary external-security group carries no permission by
    # default; give it verification here so ONLY the organization scope can
    # be what refuses it.
    group = Group.objects.get(name="External Security (Temporary)")
    group.permissions.add(
        Permission.objects.get(content_type__app_label="entry", codename="verify_entry")
    )
    factories.grant(
        external, "External Security (Temporary)", event=event, organization=organization
    )
    factories.grant(external, "Entry Operators", event=event, organization=organization)
    assert has_checkpoint_permission(external, "verify_entry", **_scope(layout)) is False


def test_a_separate_organization_unscoped_gate_membership_is_allowed(event, layout, organization):
    user = factories.make_user("dual.membership@example.test")
    _grant_everything(user, event=event, layout=layout, organization=organization)
    _grant_everything(user, event=event, layout=layout, organization=None)
    # `view_restriction_reason` is deliberately held by neither entry group
    # (FR-ENT-018: a separate, explicit grant), so it stays refused here.
    for codename in CHECKPOINT_CODENAMES:
        expected = codename != "view_restriction_reason"
        assert has_checkpoint_permission(user, codename, **_scope(layout)) is expected


def test_wrong_event_venue_or_gate_is_still_denied(event, other_event, layout):
    user = factories.make_user("gate.a.only@example.test")
    _grant_everything(user, event=event, layout=layout, organization=None)
    other_venue = factories.VenueLayout(event, code="V2")
    other_edition_layout = factories.VenueLayout(other_event, code="V9")

    assert has_checkpoint_permission(user, "verify_entry", **_scope(layout)) is True
    # Same venue, another gate.
    assert has_checkpoint_permission(user, "verify_entry", **_scope(layout, layout.gate_b)) is False
    # Another venue of the same event.
    assert (
        has_checkpoint_permission(user, "verify_entry", **_scope(other_venue, other_venue.gate_a))
        is False
    )
    # Another event edition altogether.
    assert (
        has_checkpoint_permission(
            user, "verify_entry", **_scope(other_edition_layout, other_edition_layout.gate_a)
        )
        is False
    )


def test_no_permission_composition_across_unrelated_memberships(event, layout, organization):
    """An org-scoped membership that HAS the permission plus an unscoped one
    that does NOT must never combine into a grant."""
    user = factories.make_user("composition@example.test")
    factories.grant(user, "Entry Operators", event=event, organization=organization)
    # The temporary external-security group holds no permission at all.
    factories.grant(user, "External Security (Temporary)", event=event, gate=layout.gate_a)
    assert has_checkpoint_permission(user, "verify_entry", **_scope(layout)) is False


def test_superuser_behavior_is_explicit_and_unchanged(event, layout):
    from apps.accounts.models import OperationalUser

    admin = OperationalUser.objects.create_superuser(
        email="root@example.test", password=factories.TEST_PASSWORD
    )
    assert has_checkpoint_permission(admin, "verify_entry", **_scope(layout)) is True


def test_organization_back_office_authorization_is_unchanged(event, organization):
    """The shared primitive -- used by every organization workspace -- still
    honours organization-scoped memberships exactly as before."""
    user = factories.make_user(
        "org.backoffice@example.test",
        group_name="Pass Administrators",
        event=event,
        organization=organization,
    )
    assert has_scoped_permission(
        user,
        "badges.view_digitalentrypass",
        event_edition_id=event.pk,
        organization_id=organization.pk,
    )
    assert has_scoped_permission(user, "badges.view_digitalentrypass", event_edition_id=event.pk)


def test_inactive_or_anonymous_users_are_denied(event, layout):
    from django.contrib.auth.models import AnonymousUser

    assert has_checkpoint_permission(AnonymousUser(), "verify_entry", **_scope(layout)) is False


# ---------------------------------------------------------------------------
# Checkpoint surfaces
# ---------------------------------------------------------------------------


def test_an_organization_scoped_operator_cannot_open_a_checkpoint(
    device, event, layout, organization
):
    user = factories.make_user("org.shift@example.test")
    _grant_everything(user, event=event, layout=layout, organization=organization)
    with pytest.raises(EntryPermissionError):
        start_checkpoint_session(
            device=device, user=user, gate_id=layout.gate_a.pk, zone_id=layout.main.pk
        )


def test_an_open_checkpoint_ends_once_only_an_organization_scope_remains(
    device, event, layout, organization
):
    user = factories.make_user("scope.loss@example.test")
    unscoped = factories.grant(user, "Entry Operators", event=event, gate=layout.gate_a)
    factories.grant(user, "Entry Operators", event=event, organization=organization)
    device_session, operator_session = start_checkpoint_session(
        device=device, user=user, gate_id=layout.gate_a.pk, zone_id=layout.main.pk
    )
    unscoped.delete()
    with pytest.raises(CheckpointUnavailable) as caught:
        resolve_checkpoint(
            device=device,
            device_session_id=device_session.pk,
            operator_session_id=operator_session.pk,
            user=user,
        )
    assert caught.value.reason == "OPERATOR_NOT_AUTHORIZED"


def test_lookup_admission_override_and_monitor_capabilities_follow_the_rule(
    checkpoint, event, layout, organization
):
    """With the legitimate unscoped membership removed and an org-scoped
    supervisor membership in its place, every capability is refused."""
    from apps.entry.policies import method_is_permitted

    user = checkpoint.user
    user.scoped_memberships.all().delete()
    _grant_everything(user, event=event, layout=layout, organization=organization)
    for method in VerificationMethod.values:
        assert method_is_permitted(user, method, checkpoint) is False
    for codename in ("verify_entry", "override_entry", "view_entryevent"):
        assert checkpoint_permission(user, codename, checkpoint) is False


def test_devices_visible_to_and_checkpoint_authorization_agree(event, layout, organization):
    """Both surfaces ignore an organization-scoped membership."""
    from apps.entry.selectors import devices_visible_to

    user = factories.make_user("consistency@example.test")
    factories.grant(user, "Entry Device Administrators", event=event, organization=organization)
    _grant_everything(user, event=event, layout=layout, organization=organization)
    factories.enroll_device(
        event=event,
        layout=layout,
        admin=factories.make_user(
            "device.owner@example.test", group_name="Entry Device Administrators", event=event
        ),
    )
    assert devices_visible_to(user).count() == 0
    assert has_checkpoint_permission(user, "verify_entry", **_scope(layout)) is False
