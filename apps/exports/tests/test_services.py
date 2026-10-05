"""Controlled export service tests (Phase 2 Prompt 5 §4.5/§7)."""

from __future__ import annotations

import csv
import io

import pytest
from django.utils import timezone

from apps.exports.models import ExportRequest, ExportStatus
from apps.exports.services import (
    ExportAuthorizationError,
    ExportExpiredError,
    ExportIdempotencyConflictError,
    ExportNotReadyError,
    InvalidExportPurposeError,
    InvalidExportReasonError,
    expire_due_exports,
    request_export,
    retrieve_export,
)

from .conftest import make_operational_user_with_membership, make_registration

pytestmark = pytest.mark.django_db


def test_invalid_purpose_is_rejected(event, organization):
    requester = make_operational_user_with_membership(
        email="requester@example.com", group_name="Export Administrators", event_edition=event
    )
    with pytest.raises(InvalidExportPurposeError):
        request_export(
            event_edition=event,
            organization=organization,
            purpose_code="NOT_APPROVED",
            reason="testing",
            requested_by=requester,
            registration_ids=[],
        )


def test_blank_reason_is_rejected(event, organization):
    requester = make_operational_user_with_membership(
        email="requester2@example.com", group_name="Export Administrators", event_edition=event
    )
    with pytest.raises(InvalidExportReasonError):
        request_export(
            event_edition=event,
            organization=organization,
            purpose_code="OPERATIONAL_REPORTING",
            reason="   ",
            requested_by=requester,
            registration_ids=[],
        )


def test_export_scope_is_re_derived_and_never_trusts_the_raw_id_list(
    event, other_event, organization
):
    """§4.5: authorization/scope is enforced at the SERVICE boundary. A
    registration id from a DIFFERENT event must never appear in the
    generated file even if it was included in `registration_ids`."""
    requester = make_operational_user_with_membership(
        email="requester3@example.com", group_name="Export Administrators", event_edition=event
    )
    in_scope = make_registration(event=event, organization=organization)
    out_of_scope = make_registration(event=other_event, organization=organization)

    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Quarterly report",
        requested_by=requester,
        registration_ids=[str(in_scope.pk), str(out_of_scope.pk)],
    )
    assert str(in_scope.pk) in export_request.target_registration_ids
    assert str(out_of_scope.pk) not in export_request.target_registration_ids
    assert export_request.row_count == 1
    assert export_request.status == ExportStatus.READY


def test_service_rejects_requester_without_export_permission(event, organization):
    requester = make_operational_user_with_membership(
        email="no-export-permission@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    with pytest.raises(ExportAuthorizationError):
        request_export(
            event_edition=event,
            organization=organization,
            purpose_code="OPERATIONAL_REPORTING",
            reason="Direct service bypass",
            requested_by=requester,
            registration_ids=[],
        )


def test_service_without_organization_intersects_actor_scope(
    event, organization, other_organization
):
    requester = make_operational_user_with_membership(
        email="organization-scoped@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    allowed = make_registration(event=event, organization=organization)
    forbidden = make_registration(event=event, organization=other_organization)
    export_request = request_export(
        event_edition=event,
        organization=None,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Organization-scoped direct call",
        requested_by=requester,
        registration_ids=[allowed.pk, forbidden.pk],
    )
    assert export_request.target_registration_ids == [str(allowed.pk)]


def test_service_rejects_out_of_scope_retrieval(event, organization, other_organization):
    from apps.audit.models import AuditEvent

    owner = make_operational_user_with_membership(
        email="export-owner-service@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    registration = make_registration(event=event, organization=organization)
    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Owner export",
        requested_by=owner,
        registration_ids=[registration.pk],
    )
    outsider = make_operational_user_with_membership(
        email="export-outsider-service@example.com",
        group_name="Export Administrators",
        event_edition=event,
        organization=other_organization,
    )
    with pytest.raises(ExportAuthorizationError):
        retrieve_export(export_request=export_request, actor=outsider)
    assert AuditEvent.objects.filter(
        action_code="EXP_RETRIEVAL_DENIED", reason_code="unauthorized"
    ).exists()


def test_generated_csv_contains_only_the_minimized_field_schema(event, organization):
    requester = make_operational_user_with_membership(
        email="requester4@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=organization)
    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Quarterly report",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
    )
    stream = retrieve_export(export_request=export_request, actor=requester)
    rows = list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8")))
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {
        "public_reference",
        "public_status",
        "event_name",
        "organization_name",
        "preferred_language",
        "submitted_at",
    }
    assert row["public_reference"] == registration.public_reference
    # No identity, contact, or document field ever appears.
    assert "nin" not in row
    assert "passport_number" not in row
    assert "destination_encrypted" not in row


def test_formula_shaped_organization_name_is_neutralized(event):
    from apps.organizations.models import Organization, OrganizationType

    dangerous_org = Organization.objects.create(
        official_name="=2+3",
        normalized_name="formula org",
        organization_type=OrganizationType.OTHER,
    )
    requester = make_operational_user_with_membership(
        email="requester5@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=dangerous_org)
    export_request = request_export(
        event_edition=event,
        organization=dangerous_org,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Formula-injection check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
    )
    stream = retrieve_export(export_request=export_request, actor=requester)
    rows = list(csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8")))
    assert rows[0]["organization_name"] == "'=2+3"


def test_request_export_is_idempotent_by_key(event, organization):
    requester = make_operational_user_with_membership(
        email="requester6@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=organization)
    first = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Idempotency check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
        idempotency_key="fixed-key-1",
    )
    second = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Idempotency check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
        idempotency_key="fixed-key-1",
    )
    assert first.pk == second.pk
    assert ExportRequest.objects.filter(idempotency_key="fixed-key-1").count() == 1


def test_idempotency_key_cannot_be_reused_for_different_scope(
    event, organization, other_organization
):
    requester = make_operational_user_with_membership(
        email="idempotency-scope@example.com",
        group_name="Export Administrators",
        event_edition=event,
    )
    request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="First request",
        requested_by=requester,
        registration_ids=[],
        idempotency_key="scope-conflict-key",
    )
    with pytest.raises(ExportIdempotencyConflictError):
        request_export(
            event_edition=event,
            organization=other_organization,
            purpose_code="OPERATIONAL_REPORTING",
            reason="Second request",
            requested_by=requester,
            registration_ids=[],
            idempotency_key="scope-conflict-key",
        )


def test_retrieval_is_audited_and_counted(event, organization):
    from apps.audit.models import AuditEvent

    requester = make_operational_user_with_membership(
        email="requester7@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=organization)
    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Audit check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
    )
    retrieve_export(export_request=export_request, actor=requester)
    export_request.refresh_from_db()
    assert export_request.retrieved_count == 1
    assert export_request.last_retrieved_at is not None
    assert AuditEvent.objects.filter(action_code="EXP_RETRIEVED").exists()


def test_expired_export_cannot_be_retrieved_and_records_a_denial(event, organization):
    from apps.audit.models import AuditEvent

    requester = make_operational_user_with_membership(
        email="requester8@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=organization)
    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Expiry check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
        retention_days=-1,
    )
    with pytest.raises(ExportExpiredError):
        retrieve_export(export_request=export_request, actor=requester)
    export_request.refresh_from_db()
    assert export_request.status == ExportStatus.EXPIRED
    assert AuditEvent.objects.filter(
        action_code="EXP_RETRIEVAL_DENIED", reason_code="expired"
    ).exists()


def test_expire_due_exports_is_idempotent(event, organization):
    requester = make_operational_user_with_membership(
        email="requester9@example.com", group_name="Export Administrators", event_edition=event
    )
    registration = make_registration(event=event, organization=organization)
    export_request = request_export(
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Sweep check",
        requested_by=requester,
        registration_ids=[str(registration.pk)],
        retention_days=-1,
    )
    first_count = expire_due_exports(now=timezone.now())
    second_count = expire_due_exports(now=timezone.now())
    export_request.refresh_from_db()
    assert export_request.status == ExportStatus.EXPIRED
    assert first_count == 1
    assert second_count == 0


def test_not_ready_export_cannot_be_retrieved(event, organization):
    requester = make_operational_user_with_membership(
        email="requester10@example.com", group_name="Export Administrators", event_edition=event
    )
    export_request = ExportRequest.objects.create(
        dataset_kind="REGISTRATIONS_SUMMARY",
        event_edition=event,
        organization=organization,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Not ready",
        requested_by=requester,
        idempotency_key="not-ready-1",
        status=ExportStatus.PENDING,
        expires_at=timezone.now(),
    )
    with pytest.raises(ExportNotReadyError):
        retrieve_export(export_request=export_request, actor=requester)
