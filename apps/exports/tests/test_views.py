"""Export workspace authorization tests (Phase 2 Prompt 5 §4.5)."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.exports.models import ExportRequest, ExportStatus

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


def test_workspace_requires_sign_in():
    client = Client()
    response = client.get(reverse("exports:workspace"))
    assert response.status_code == 302


def test_same_scope_coordinator_can_generate_an_export(event, organization):
    make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="export-same-scope@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "organization_id": str(organization.pk),
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Weekly report",
        },
    )
    assert response.status_code == 302
    assert ExportRequest.objects.filter(event_edition=event, status=ExportStatus.READY).exists()


def test_cross_organization_denial(event, organization, other_organization):
    make_registration(event=event, organization=other_organization)
    coordinator = make_operational_user_with_membership(
        email="export-cross-org@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,  # scoped to the WRONG organization
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "organization_id": str(other_organization.pk),
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Attempted cross-org export",
        },
    )
    assert response.status_code == 409
    assert not ExportRequest.objects.filter(organization=other_organization).exists()


def test_cross_event_scope_never_leaks_into_the_generated_file(event, other_event, organization):
    """Even a caller scoped broadly enough to submit the form for `event`
    can never pull in a DIFFERENT event's registrations -- the service
    layer re-derives scope regardless of what the view assembled."""
    make_registration(event=other_event, organization=organization)
    in_scope = make_registration(event=event, organization=organization)
    coordinator = make_operational_user_with_membership(
        email="export-cross-event@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "organization_id": str(organization.pk),
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Scope isolation check",
        },
    )
    export_request = ExportRequest.objects.get(event_edition=event)
    assert export_request.target_registration_ids == [str(in_scope.pk)]


def test_view_only_permission_cannot_generate_an_export(event, organization):
    coordinator = make_operational_user_with_membership(
        email="export-view-only@example.com",
        group_name="Registration Reviewers",  # holds no export permission
        event_edition=event,
        organization=organization,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    response = client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "organization_id": str(organization.pk),
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Should be denied",
        },
    )
    # Signed in without the permission: the 403 page, never a sign-in
    # redirect (UI/UX Completion Gate F3).
    assert response.status_code == 403
    assert not ExportRequest.objects.exists()


def test_direct_download_url_denied_for_out_of_scope_export(
    event, organization, other_organization
):
    owner = make_operational_user_with_membership(
        email="export-owner@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    registration = make_registration(event=event, organization=organization)
    from apps.exports.services import request_export

    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Owner-only export",
        requested_by=owner,
        registration_ids=[str(registration.pk)],
    )

    outsider = make_operational_user_with_membership(
        email="export-outsider@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=other_organization,  # different organization scope
    )
    client = Client()
    sign_in_operational(client, outsider.email_normalized)
    response = client.get(reverse("exports:download", kwargs={"pk": export_request.pk}))
    assert response.status_code == 404
