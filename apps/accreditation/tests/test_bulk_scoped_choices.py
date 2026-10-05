"""UI/UX Completion Gate F8: bulk accreditation chooses what to assign from
the objects the caller may bulk-assign, never a typed database id.

* The choice list contains only active objects of events where the caller
  holds the kind's `add_*` permission.
* A forged, unknown, malformed, inactive, other-event or other-kind id is
  refused with the same validation message and creates no operation (no
  404, no server error), so ids cannot be enumerated.
* Preview and result tables show public references, never registration ids.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.accreditation.models import BulkAssignmentOperation, ParticipantRole

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


@pytest.fixture
def coordinator(event, organization):
    return make_operational_user_with_membership(
        email="f8-bulk@example.test",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )


def _client(user) -> Client:
    client = Client()
    sign_in_operational(client, user.email_normalized)
    return client


def _preview(client, kind, reference_id):
    return client.post(
        reverse("accreditation:bulk-preview"),
        {"kind": kind, "reference_object_id": reference_id, "reason": "F8 check"},
    )


def test_choices_are_limited_to_assignable_objects(coordinator, role, other_event):
    other_event_role = ParticipantRole.objects.create(
        event_edition=other_event, code="F8-OTHER", name="Other Event Role"
    )
    inactive = ParticipantRole.objects.create(
        event_edition=role.event_edition, code="F8-INACTIVE", name="Inactive Role", is_active=False
    )
    response = _client(coordinator).get(reverse("accreditation:bulk-preview"))
    content = response.content.decode()
    assert response.status_code == 200
    assert f'value="{role.pk}"' in content
    assert f'value="{other_event_role.pk}"' not in content
    assert f'value="{inactive.pk}"' not in content
    assert "Reference object id" not in content
    assert 'type="text" id="id_reference_object_id"' not in content


def test_an_assignable_object_previews(coordinator, role, event, organization):
    approved = make_registration(event=event, organization=organization, public_reference="F8-OK-1")
    approved.public_status = "APPROVED"
    approved.save(update_fields=["public_status"])
    response = _preview(_client(coordinator), "PARTICIPANT_ROLE", str(role.pk))
    assert response.status_code == 200
    operation = response.context["operation"]
    assert str(approved.pk) in operation.target_registration_ids
    content = response.content.decode()
    # The preview shows the reference staff know, never the database id.
    assert "F8-OK-1" in content
    assert str(approved.pk) not in content


@pytest.mark.parametrize(
    "case", ["other_event", "inactive", "unknown", "malformed", "wrong_kind", "empty"]
)
def test_forged_reference_ids_are_refused_identically(
    case, coordinator, role, badge_type, other_event
):
    other_event_role = ParticipantRole.objects.create(
        event_edition=other_event, code=f"F8-{case}", name="Other Event Role"
    )
    inactive = ParticipantRole.objects.create(
        event_edition=role.event_edition, code=f"F8-IN-{case}", name="Inactive", is_active=False
    )
    kind, value = {
        "other_event": ("PARTICIPANT_ROLE", str(other_event_role.pk)),
        "inactive": ("PARTICIPANT_ROLE", str(inactive.pk)),
        "unknown": ("PARTICIPANT_ROLE", str(uuid.uuid4())),
        "malformed": ("PARTICIPANT_ROLE", "1 OR 1=1"),
        "wrong_kind": ("PARTICIPANT_ROLE", str(badge_type.pk)),
        "empty": ("PARTICIPANT_ROLE", ""),
    }[case]
    response = _preview(_client(coordinator), kind, value)
    assert response.status_code == 200
    assert "operation" not in response.context
    assert BulkAssignmentOperation.objects.count() == 0
    assert response.context["form"].errors["reference_object_id"]


def test_a_user_without_bulk_permission_gets_403(event, organization, role):
    reviewer = make_operational_user_with_membership(
        email="f8-bulk-denied@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    response = _preview(_client(reviewer), "PARTICIPANT_ROLE", str(role.pk))
    assert response.status_code == 403
    assert BulkAssignmentOperation.objects.count() == 0


def test_malformed_reference_on_a_single_assignment_is_a_404_not_a_server_error(
    event, organization, registration
):
    manager = make_operational_user_with_membership(
        email="f8-single@example.test",
        group_name="Accreditation Coordinators",
        event_edition=event,
        organization=organization,
    )
    response = _client(manager).post(
        reverse(
            "accreditation:assignment-assign",
            kwargs={"pk": registration.pk, "kind": "PARTICIPANT_ROLE"},
        ),
        {"reference_object_id": "not-a-uuid"},
    )
    assert response.status_code == 404
