"""UPD-07: participant accreditation settings and the bulk assignment.

Covered: the empty-configuration diagnosis (the specific missing
prerequisite and, for the superadmin, the management action), creating,
editing, deactivating and reactivating roles, badge types, access profiles
and access rules (audited, the code unique per event and immutable), ordinary
staff read-only, the bulk population limited to final-approved live
registrations, preview without any change, and execution that skips rows
whose eligibility or scope changed after the preview, with applied, skipped
and error counts. Synthetic data only.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.accreditation.models import (
    AccessProfile,
    AccessRule,
    AssignmentStatus,
    BadgeType,
    BulkAssignmentOperation,
    ParticipantRole,
)
from apps.audit.models import AuditEvent
from apps.registrations.models import Registration

from .conftest import (
    TEST_OPERATIONAL_PASSWORD,
    make_operational_user_with_membership,
    make_registration,
    record_final_approval,
)

pytestmark = pytest.mark.django_db

SETTINGS_URL = reverse("accreditation:settings")
BULK_URL = reverse("accreditation:bulk-preview")


@pytest.fixture
def superadmin():
    return OperationalUser.objects.create_user(
        email="upd07-super@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=True,
    )


def _client(user):
    from apps.people.tests.conftest import staff_client

    return staff_client(user)


@pytest.fixture
def empty_event(event):
    for model in (AccessRule, AccessProfile, BadgeType, ParticipantRole):
        model.objects.filter(event_edition=event).delete()
    return event


def test_an_empty_configuration_names_the_missing_prerequisite(superadmin, empty_event):
    page = _client(superadmin).get(BULK_URL).content.decode()
    assert 'data-bulk-empty="not-configured"' in page
    assert "data-bulk-manage-settings" in page


def test_all_settings_inactive_is_diagnosed(superadmin, event, role):
    for model in (AccessRule, AccessProfile, BadgeType, ParticipantRole):
        model.objects.filter(event_edition=event).update(is_active=False)
    page = _client(superadmin).get(BULK_URL).content.decode()
    assert 'data-bulk-empty="all-inactive"' in page


def test_the_superadmin_manages_every_kind_of_setting(superadmin, empty_event):
    client = _client(superadmin)
    event_query = f"?event={empty_event.pk}"
    for slug, extra in (
        ("role", {}),
        ("badge-type", {"participant_visible": "on"}),
        ("access-profile", {"reentry_policy": "REENTRY_ALLOWED"}),
        ("access-rule", {"event_type": "ENTRY", "effect": "ALLOW"}),
    ):
        url = reverse("accreditation:setting-create", kwargs={"kind_slug": slug}) + event_query
        response = client.post(
            url,
            {"code": f"upd07-{slug}", "name": f"Synthetic {slug}", "name_fr": "", "name_ar": "",
             "event": str(empty_event.pk), **extra},
        )  # fmt: skip
        assert response.status_code == 302, (slug, response.content.decode()[:500])
    role = ParticipantRole.objects.get(event_edition=empty_event)
    assert role.code == "UPD07-ROLE" and role.is_active
    assert BadgeType.objects.get(event_edition=empty_event).participant_visible is True
    assert AuditEvent.objects.filter(action_code="ACC_SETTING_CREATED").count() == 4

    # The code is unique per event.
    duplicate = client.post(
        reverse("accreditation:setting-create", kwargs={"kind_slug": "role"}) + event_query,
        {"code": "UPD07-ROLE", "name": "Again", "event": str(empty_event.pk)},
    )
    assert duplicate.status_code == 400
    # Editing never changes the code.
    client.post(
        reverse("accreditation:setting-edit", kwargs={"kind_slug": "role", "pk": role.pk}),
        {"code": "HACKED", "name": "Renamed role", "name_fr": "Rôle", "name_ar": ""},
    )
    role.refresh_from_db()
    assert (role.code, role.name, role.name_fr) == ("UPD07-ROLE", "Renamed role", "Rôle")
    # Deactivation hides it from the bulk page; reactivation offers it again.
    client.post(
        reverse("accreditation:setting-status", kwargs={"kind_slug": "role", "pk": role.pk}),
        {"active": "0"},
    )
    role.refresh_from_db()
    assert role.is_active is False
    form = client.get(BULK_URL).context["form"]
    offered = [
        value
        for _label, group in form.fields["reference_object_id"].choices[1:]
        for value, _ in group
    ]
    assert str(role.pk) not in offered
    client.post(
        reverse("accreditation:setting-status", kwargs={"kind_slug": "role", "pk": role.pk}),
        {"active": "1"},
    )
    role.refresh_from_db()
    assert role.is_active is True


def test_ordinary_staff_read_the_settings_but_cannot_change_them(event, organization, role):
    coordinator = make_operational_user_with_membership(
        email="upd07-coord@example.test",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    client = _client(coordinator)
    page = client.get(SETTINGS_URL).content.decode()
    assert role.code in page
    assert "data-setting-create" not in page and "data-setting-toggle" not in page
    create = reverse("accreditation:setting-create", kwargs={"kind_slug": "role"})
    assert client.post(f"{create}?event={event.pk}", {"code": "X1", "name": "X"}).status_code == 403
    toggle = reverse("accreditation:setting-status", kwargs={"kind_slug": "role", "pk": role.pk})
    assert client.post(toggle, {"active": "0"}).status_code == 403
    role.refresh_from_db()
    assert role.is_active


def _preview(client, role, reason="Synthetic"):
    return client.post(
        BULK_URL,
        {"kind": "PARTICIPANT_ROLE", "reference_object_id": str(role.pk), "reason": reason},
    )


def test_the_population_is_final_approved_live_registrations_only(
    superadmin, event, organization, role
):
    final = record_final_approval(make_registration(event=event, organization=organization))
    submitted = make_registration(event=event, organization=organization)
    approved_without_decision = make_registration(event=event, organization=organization)
    Registration.objects.filter(pk=approved_without_decision.pk).update(public_status="APPROVED")
    cancelled = record_final_approval(make_registration(event=event, organization=organization))
    Registration.objects.filter(pk=cancelled.pk).update(public_status="WITHDRAWN")

    response = _preview(_client(superadmin), role)
    operation = response.context["operation"]
    assert operation.target_registration_ids == [str(final.pk)]
    ids = {row["registration_id"] for row in operation.preview_results}
    assert str(submitted.pk) not in ids and str(approved_without_decision.pk) not in ids
    assert str(cancelled.pk) not in ids
    content = response.content.decode()
    assert "data-bulk-preview-summary" in content and role.code in content
    # The preview changed nothing.
    assert not final.role_assignments.exists()


def test_execution_skips_rows_whose_eligibility_changed_after_the_preview(
    superadmin, event, organization, role
):
    keep = record_final_approval(make_registration(event=event, organization=organization))
    withdrawn = record_final_approval(make_registration(event=event, organization=organization))
    decided = record_final_approval(make_registration(event=event, organization=organization))
    client = _client(superadmin)
    operation = _preview(client, role).context["operation"]
    assert len(operation.target_registration_ids) == 3

    Registration.objects.filter(pk=withdrawn.pk).update(public_status="WITHDRAWN")
    from apps.reviews.models import RegistrationDecision

    RegistrationDecision.objects.filter(registration=decided, is_current=True).update(
        outcome="NOT_APPROVED"
    )
    response = client.post(reverse("accreditation:bulk-execute", kwargs={"pk": operation.pk}))
    content = response.content.decode()
    assert 'data-bulk-applied="1"' in content
    assert 'data-bulk-skipped="2"' in content
    assert 'data-bulk-errors="0"' in content
    assert keep.role_assignments.filter(status=AssignmentStatus.CURRENT).count() == 1
    assert not withdrawn.role_assignments.exists() and not decided.role_assignments.exists()
    reasons = {
        row["registration_id"]: row["reason"]
        for row in BulkAssignmentOperation.objects.get(pk=operation.pk).execution_results
    }
    assert reasons[str(withdrawn.pk)] == reasons[str(decided.pk)] == "NO_LONGER_ELIGIBLE"
    event_row = AuditEvent.objects.get(
        action_code="ACC_BULK_ASSIGNMENT_EXECUTED", target_uuid=operation.pk
    )
    assert event_row.after_summary["applied_count"] == 1
    assert event_row.after_summary["skipped_count"] == 2


def test_a_row_that_left_the_executor_scope_is_skipped_not_applied(
    event, organization, other_organization, role
):
    coordinator = make_operational_user_with_membership(
        email="upd07-exec@example.test",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    inside = record_final_approval(make_registration(event=event, organization=organization))
    moving = record_final_approval(make_registration(event=event, organization=organization))
    client = _client(coordinator)
    operation = _preview(client, role).context["operation"]
    Registration.objects.filter(pk=moving.pk).update(source_organization=other_organization)
    response = client.post(reverse("accreditation:bulk-execute", kwargs={"pk": operation.pk}))
    assert response.status_code == 200
    reasons = {
        row["registration_id"]: row["reason"]
        for row in BulkAssignmentOperation.objects.get(pk=operation.pk).execution_results
    }
    assert reasons[str(moving.pk)] == "NOT_AUTHORIZED"
    assert inside.role_assignments.exists() and not moving.role_assignments.exists()


def test_staff_without_the_bulk_permission_are_refused(event, organization):
    reviewer = make_operational_user_with_membership(
        email="upd07-reviewer@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    assert _client(reviewer).get(BULK_URL).status_code == 403
    assert _client(reviewer).get(SETTINGS_URL).status_code == 403
