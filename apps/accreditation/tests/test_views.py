"""View-level authorization tests for accreditation assignments (Phase 2
Prompt 4 §3): same-scope, cross-event, cross-organization, view-only,
expired-membership, inactive-account, wrong-functional-group,
stale-version, direct-URL, and bulk-operation scope isolation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accreditation.models import AssignmentStatus, BulkAssignmentKind
from apps.accreditation.services import assign

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


def test_registration_detail_requires_sign_in():
    client = Client()
    response = client.get(
        reverse(
            "accreditation:registration-detail",
            kwargs={"pk": "00000000-0000-0000-0000-000000000000"},
        )
    )
    assert response.status_code == 302


def test_same_scope_coordinator_can_assign_a_role(event, organization, role):
    registration = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="same-scope@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    assert response.status_code == 302
    assert registration.role_assignments.filter(status=AssignmentStatus.CURRENT, role=role).exists()


def test_cross_organization_denial(event, organization, other_organization, role):
    registration = make_registration(event=event, organization=other_organization)
    coordinator = make_operational_user_with_membership(
        email="cross-org@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,  # scoped to the WRONG organization
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    assert response.status_code == 404
    assert registration.role_assignments.count() == 0


def test_permissions_from_different_scopes_cannot_be_composed(
    event, organization, other_organization, role
):
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership

    registration = make_registration(event=event, organization=other_organization)
    user = make_operational_user_with_membership(
        email="non-composable-scopes@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=other_organization,
    )
    assignment_group = Group.objects.create(name="Test Role Assigners")
    assignment_group.permissions.add(
        Permission.objects.get(
            content_type__app_label="accreditation",
            codename="add_participantroleassignment",
        )
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=assignment_group,
        event_edition=event,
        organization=organization,
        granted_by=user,
    )

    client = Client()
    sign_in_operational(client, user.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    assert response.status_code == 404
    assert registration.role_assignments.count() == 0


def test_cross_event_denial(event, other_event, organization, role):
    registration = make_registration(event=other_event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="cross-event@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,  # scoped to the WRONG event
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    assert response.status_code == 404
    assert registration.role_assignments.count() == 0


def test_view_only_denial(event, organization, role):
    registration = make_registration(event=event, organization=organization)
    viewer = make_operational_user_with_membership(
        email="view-only-acc@example.com",
        group_name="Registration Reviewers",  # holds NO accreditation permission at all
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, viewer.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    assert registration.role_assignments.count() == 0


def test_registration_view_permission_does_not_leak_accreditation_data(event, organization):
    registration = make_registration(event=event, organization=organization)
    reviewer = make_operational_user_with_membership(
        email="registration-only@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, reviewer.email_normalized)
    response = client.get(
        reverse("accreditation:registration-detail", kwargs={"pk": registration.pk})
    )
    assert response.status_code == 404


def test_expired_membership_denial(event, organization, role):
    from apps.accounts.models import ScopedGroupMembership

    registration = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="expired-membership@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    ScopedGroupMembership.objects.filter(user=coordinator).update(
        active_until=timezone.now() - timedelta(days=1)
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    assert registration.role_assignments.count() == 0


def test_inactive_account_denial(event, organization, role):
    from apps.accounts.models import OperationalUserStatus

    registration = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="inactive-account@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    coordinator.status = OperationalUserStatus.SUSPENDED
    coordinator.save(update_fields=["status"])
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    assert response.status_code == 302
    assert registration.role_assignments.count() == 0


def test_wrong_functional_group_denial(event, organization, role):
    registration = make_registration(event=event, organization=organization)
    manager = make_operational_user_with_membership(
        email="wrong-group@example.com",
        group_name="Accreditation Managers",  # Prompt 3 group -- no accreditation perms
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": str(role.pk)},
    )
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    assert registration.role_assignments.count() == 0


def test_stale_version_conflict_on_revoke(event, organization, role):
    registration = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="stale-revoke@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    assignment = assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse(
            "accreditation:assignment-revoke",
            kwargs={
                "pk": registration.pk,
                "kind": "PARTICIPANT_ROLE",
                "assignment_pk": assignment.pk,
            },
        ),
        {"reason": "test", "expected_version": assignment.version + 999},
    )
    assert response.status_code == 409


def test_direct_url_access_denied_for_out_of_scope_registration(
    event, organization, other_organization
):
    registration = make_registration(event=event, organization=other_organization)
    coordinator = make_operational_user_with_membership(
        email="direct-url@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.get(
        reverse("accreditation:registration-detail", kwargs={"pk": registration.pk})
    )
    assert response.status_code == 404


def test_registration_detail_query_count_is_bounded(
    event, organization, role, badge_type, django_assert_max_num_queries
):
    registration = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="query-count@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    assign(
        kind=BulkAssignmentKind.PARTICIPANT_ROLE,
        registration=registration,
        reference_obj=role,
        actor=coordinator,
    )
    assign(
        kind=BulkAssignmentKind.BADGE_TYPE,
        registration=registration,
        reference_obj=badge_type,
        actor=coordinator,
    )

    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    with django_assert_max_num_queries(20):
        response = client.get(
            reverse("accreditation:registration-detail", kwargs={"pk": registration.pk})
        )
    assert response.status_code == 200


def test_bulk_preview_only_targets_the_callers_own_scope(
    event, organization, other_organization, role
):
    in_scope = make_registration(
        event=event, organization=organization, public_reference="ACC-INSCOPE-01"
    )
    in_scope.public_status = "APPROVED"
    in_scope.save(update_fields=["public_status"])
    out_of_scope = make_registration(
        event=event, organization=other_organization, public_reference="ACC-OOS-01"
    )
    out_of_scope.public_status = "APPROVED"
    out_of_scope.save(update_fields=["public_status"])

    coordinator = make_operational_user_with_membership(
        email="bulk-scope@example.com",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse("accreditation:bulk-preview"),
        {"kind": "PARTICIPANT_ROLE", "reference_object_id": str(role.pk)},
    )
    assert response.status_code == 200
    operation = response.context["operation"]
    assert str(in_scope.pk) in operation.target_registration_ids
    assert str(out_of_scope.pk) not in operation.target_registration_ids
