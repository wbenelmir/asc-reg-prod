"""The restricted verified-identities export: who may generate and download it,
what its file contains, and how its file is removed."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus
from apps.audit.models import AuditEvent
from apps.documents.storage import get_private_storage
from apps.exports.apps import EXPORT_ADMINISTRATORS_PERMISSIONS
from apps.exports.models import (
    ExportDatasetKind,
    ExportRequest,
    ExportStatus,
)
from apps.exports.services import (
    ExportAuthorizationError,
    ExportExpiredError,
    ExportNotReadyError,
    expire_due_exports,
    request_export,
    retrieve_export,
)
from apps.people.models import ContactPoint, ContactPointType
from apps.people.tests.identity_fixtures import make_verified_identity_case
from apps.registrations.models import RegistrationProfile

from .conftest import (
    TEST_OPERATIONAL_PASSWORD,
    make_operational_user_with_membership,
    make_registration,
    sign_in_operational,
)

pytestmark = pytest.mark.django_db

HOLDERS_GROUP = "Test Verified Identity Exporters"


def _holder(email, *, event, organization=None):
    group, created = Group.objects.get_or_create(name=HOLDERS_GROUP)
    if created:
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="exports",
                codename__in=[
                    "add_exportrequest",
                    "view_exportrequest",
                    "export_verified_identities",
                ],
            )
        )
    return make_operational_user_with_membership(
        email=email, group_name=HOLDERS_GROUP, event_edition=event, organization=organization
    )


def _verified_registration(event, organization, **case_kwargs):
    registration = make_registration(event=event, organization=organization)
    case = make_verified_identity_case(registration, **case_kwargs)
    registration.refresh_from_db()
    RegistrationProfile.objects.create(
        registration=registration, submitted_given_names="Given", submitted_family_name="Family"
    )
    return registration, case


def _email_of(registration) -> str:
    return ContactPoint.objects.get(
        person_id=registration.person_id, type=ContactPointType.EMAIL
    ).value_encrypted


def _request(user, event, registrations, **kwargs):
    return request_export(
        event_edition=event,
        dataset_kind=ExportDatasetKind.VERIFIED_IDENTITIES,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Approved participant list",
        requested_by=user,
        registration_ids=[registration.pk for registration in registrations],
        **kwargs,
    )


def _rows(export_request, actor):
    content = retrieve_export(export_request=export_request, actor=actor).read()
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


def test_the_permission_is_not_part_of_export_administrators():
    assert ("exports", "export_verified_identities") not in EXPORT_ADMINISTRATORS_PERMISSIONS
    assert not (
        Group.objects.get(name="Export Administrators")
        .permissions.filter(codename="export_verified_identities")
        .exists()
    )


def test_the_file_holds_full_values_of_every_approved_method(event, organization):
    holder = _holder("holder-values@example.com", event=event)
    passport_registration, passport_case = _verified_registration(event, organization)
    ministry_registration, _ = _verified_registration(
        event, organization, source="MINISTRY_API", status="API_VERIFIED"
    )

    export_request = _request(holder, event, [passport_registration, ministry_registration])
    rows = {row["registration_reference"]: row for row in _rows(export_request, holder)}

    assert export_request.row_count == 2
    row = rows[passport_registration.public_reference]
    assert row["first_name"] == "Given"
    assert row["family_name"] == "Family"
    assert row["email"] == _email_of(passport_registration)
    assert "***" not in row["email"]
    number = passport_case.current_revision.identifier.value_encrypted
    assert row["passport_number"] == number
    assert row["nin_or_national_id"] == ""
    assert row["verification_method"] == "MANUAL_PASSPORT"
    assert row["verification_date"] == passport_case.decided_at.isoformat()
    assert rows[ministry_registration.public_reference]["verification_method"] == "MINISTRY_API"


def test_unverified_and_simulated_cases_are_left_out(event, organization):
    holder = _holder("holder-filter@example.com", event=event)
    verified, _ = _verified_registration(event, organization)
    pending, _ = _verified_registration(event, organization, source="", status="PENDING")
    simulated, _ = _verified_registration(
        event, organization, source="SIMULATED_API", status="API_VERIFIED"
    )

    export_request = _request(holder, event, [verified, pending, simulated])

    assert export_request.target_registration_ids == [str(verified.pk)]
    assert [row["registration_reference"] for row in _rows(export_request, holder)] == [
        verified.public_reference
    ]


def test_export_administrator_without_the_permission_is_refused(event, organization):
    administrator = make_operational_user_with_membership(
        email="admin-refused@example.com", group_name="Export Administrators", event_edition=event
    )
    registration, _ = _verified_registration(event, organization)
    with pytest.raises(ExportAuthorizationError):
        _request(administrator, event, [registration])
    assert not ExportRequest.objects.exists()


def test_holder_for_another_event_gets_no_rows_from_this_one(event, other_event, organization):
    holder = _holder("holder-other-event@example.com", event=other_event)
    registration, _ = _verified_registration(event, organization)
    with pytest.raises(ExportAuthorizationError):
        _request(holder, event, [registration])


def test_only_the_requester_can_download_and_a_refusal_is_audited(event, organization):
    requester = _holder("holder-requester@example.com", event=event)
    other_holder = _holder("holder-other@example.com", event=event)
    administrator = make_operational_user_with_membership(
        email="admin-download@example.com", group_name="Export Administrators", event_edition=event
    )
    superuser = OperationalUser.objects.create_superuser(
        email="root-download@example.com", password=TEST_OPERATIONAL_PASSWORD
    )
    registration, _ = _verified_registration(event, organization)
    export_request = _request(requester, event, [registration])

    for actor in (other_holder, administrator, superuser):
        with pytest.raises(ExportAuthorizationError):
            retrieve_export(export_request=export_request, actor=actor)
    assert (
        AuditEvent.objects.filter(
            action_code="EXP_RETRIEVAL_DENIED", target_uuid=export_request.pk
        ).count()
        == 3
    )
    assert _rows(export_request, requester)
    assert AuditEvent.objects.filter(
        action_code="EXP_RETRIEVED", target_uuid=export_request.pk
    ).exists()


def test_generation_is_audited_without_identity_values(event, organization):
    holder = _holder("holder-audit@example.com", event=event)
    registration, case = _verified_registration(event, organization)
    export_request = _request(holder, event, [registration])

    event_row = AuditEvent.objects.get(
        action_code="EXP_REQUEST_READY", target_uuid=export_request.pk
    )
    assert event_row.after_summary["dataset_kind"] == "VERIFIED_IDENTITIES"
    summary = str(event_row.after_summary)
    assert case.current_revision.identifier.value_encrypted not in summary
    assert _email_of(registration) not in summary


def test_the_file_expires_after_24_hours_and_is_deleted(event, organization):
    holder = _holder("holder-expiry@example.com", event=event)
    registration, _ = _verified_registration(event, organization)
    export_request = _request(holder, event, [registration], retention_days=30)
    storage_key = export_request.storage_key

    assert export_request.expires_at <= timezone.now() + timedelta(hours=24)
    assert get_private_storage().exists(storage_key)

    assert expire_due_exports(now=timezone.now() + timedelta(hours=25)) == 1

    export_request.refresh_from_db()
    assert export_request.status == ExportStatus.EXPIRED
    assert export_request.storage_key == ""
    assert not get_private_storage().exists(storage_key)
    with pytest.raises(ExportNotReadyError):
        retrieve_export(export_request=export_request, actor=holder)


def test_a_late_download_deletes_the_file(event, organization):
    holder = _holder("holder-late@example.com", event=event)
    registration, _ = _verified_registration(event, organization)
    export_request = _request(holder, event, [registration])
    storage_key = export_request.storage_key
    ExportRequest.objects.filter(pk=export_request.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    export_request.refresh_from_db()

    with pytest.raises(ExportExpiredError):
        retrieve_export(export_request=export_request, actor=holder)

    assert not get_private_storage().exists(storage_key)


def test_the_summary_export_is_unchanged_for_a_holder(event, organization):
    holder = _holder("holder-summary@example.com", event=event)
    registration, _ = _verified_registration(event, organization)
    export_request = request_export(
        event_edition=event,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Summary",
        requested_by=holder,
        registration_ids=[registration.pk],
    )
    assert export_request.storage_key
    content = retrieve_export(export_request=export_request, actor=holder).read().decode()
    assert _email_of(registration) not in content
    assert "passport_number" not in content


def test_workspace_offers_the_dataset_only_to_a_holder(event, organization):
    _verified_registration(event, organization)
    holder = _holder("holder-workspace@example.com", event=event)
    administrator = make_operational_user_with_membership(
        email="admin-workspace@example.com", group_name="Export Administrators", event_edition=event
    )

    holder_client = Client()
    sign_in_operational(holder_client, holder.email_normalized)
    assert "VERIFIED_IDENTITIES" in holder_client.get(reverse("exports:workspace")).content.decode()

    admin_client = Client()
    sign_in_operational(admin_client, administrator.email_normalized)
    assert (
        "VERIFIED_IDENTITIES" not in admin_client.get(reverse("exports:workspace")).content.decode()
    )


def test_holder_generates_and_downloads_through_the_workspace(event, organization):
    registration, _ = _verified_registration(event, organization)
    holder = _holder("holder-flow@example.com", event=event)
    client = Client()
    sign_in_operational(client, holder.email_normalized)

    response = client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "dataset_kind": "VERIFIED_IDENTITIES",
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Approved participant list",
        },
    )
    assert response.status_code == 302
    export_request = ExportRequest.objects.get(dataset_kind="VERIFIED_IDENTITIES")

    download = client.get(reverse("exports:download", kwargs={"pk": export_request.pk}))
    assert download.status_code == 200
    assert download["Cache-Control"] == "private, no-store"
    content = b"".join(download.streaming_content).decode("utf-8-sig")
    assert _email_of(registration) in content


def test_a_superuser_generates_and_downloads_through_the_workspace(event, organization):
    registration, _ = _verified_registration(event, organization)
    superuser = OperationalUser.objects.create_superuser(
        email="root-flow@example.com", password=TEST_OPERATIONAL_PASSWORD
    )
    client = Client()
    sign_in_operational(client, superuser.email_normalized)

    response = client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "dataset_kind": "VERIFIED_IDENTITIES",
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Approved participant list",
        },
    )

    assert response.status_code == 302
    export_request = ExportRequest.objects.get(dataset_kind="VERIFIED_IDENTITIES")
    assert export_request.row_count == 1
    download = client.get(reverse("exports:download", kwargs={"pk": export_request.pk}))
    assert _email_of(registration) in b"".join(download.streaming_content).decode("utf-8-sig")


def test_a_forged_dataset_from_an_administrator_yields_only_the_summary(event, organization):
    registration, _ = _verified_registration(event, organization)
    administrator = make_operational_user_with_membership(
        email="admin-forged@example.com", group_name="Export Administrators", event_edition=event
    )
    client = Client()
    sign_in_operational(client, administrator.email_normalized)

    client.post(
        reverse("exports:workspace"),
        {
            "event_edition_id": str(event.pk),
            "dataset_kind": "VERIFIED_IDENTITIES",
            "purpose_code": "OPERATIONAL_REPORTING",
            "reason": "Forged dataset",
        },
    )

    assert not ExportRequest.objects.filter(dataset_kind="VERIFIED_IDENTITIES").exists()
    export_request = ExportRequest.objects.get(event_edition=event)
    content = retrieve_export(export_request=export_request, actor=administrator).read().decode()
    assert _email_of(registration) not in content


def test_another_holder_cannot_see_or_download_through_the_workspace(event, organization):
    registration, _ = _verified_registration(event, organization)
    requester = _holder("holder-owner@example.com", event=event)
    export_request = _request(requester, event, [registration])
    other = _holder("holder-viewer@example.com", event=event)
    assert other.status == OperationalUserStatus.ACTIVE
    client = Client()
    sign_in_operational(client, other.email_normalized)

    download_url = reverse("exports:download", kwargs={"pk": export_request.pk})
    assert download_url not in client.get(reverse("exports:workspace")).content.decode()
    assert client.get(download_url).status_code == 404
