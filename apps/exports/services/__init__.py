"""Controlled export generation and retrieval (Phase 2 Prompt 5 §4.5).

`request_export` is authorization-enforced HERE, at the service boundary,
not only by whatever permission check a view happens to perform first
(§4.5 "Authorization must be enforced by the service/domain boundary, not
only hidden in the UI"): the caller-supplied `registration_ids` are
re-filtered to the declared `event_edition`/`organization` scope before a
single row is written to the generated file, exactly like
`apps.accreditation.services.preview_bulk_assignment` never trusts a raw,
unchecked id list.

The export is generated SYNCHRONOUSLY, inside the same request, and
written to `PrivateStorage` -- there is no separate "pending, then a
background job fills it in" step for this bounded dataset (a values-only
CSV over an already-scoped queryset), so `ExportStatus.PENDING` exists for
completeness/observability but a successful `request_export` call always
returns a `READY` row.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.policies import has_scoped_permission
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.core.spreadsheet_safety import neutralize_for_spreadsheet
from apps.documents.storage import ObjectNotFound, PrivateStorageError, get_private_storage
from apps.exports.models import (
    ALLOWED_EXPORT_PURPOSE_CODES,
    EXPORT_DATASET_FIELD_SCHEMAS,
    EXPORT_SCHEMA_VERSION,
    ExportDatasetKind,
    ExportRequest,
    ExportStatus,
)
from apps.registrations.models import Registration

DEFAULT_EXPORT_RETENTION_DAYS = 14


class InvalidExportPurposeError(Exception):
    """Raised when `purpose_code` is not in `ALLOWED_EXPORT_PURPOSE_CODES`."""


class InvalidExportReasonError(Exception):
    """Raised when no human-readable reason was supplied."""


class ExportNotReadyError(Exception):
    """Raised by `retrieve_export` for a not-yet-READY export."""


class ExportExpiredError(Exception):
    """Raised by `retrieve_export` for an export past its `expires_at`."""


class ExportAuthorizationError(Exception):
    """Raised when the actor lacks the required permission in the declared scope."""


class ExportIdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused for a different export request."""


def _registration_row(registration: Registration) -> dict[str, str]:
    raw = {
        "public_reference": registration.public_reference,
        "public_status": registration.public_status,
        "event_name": registration.event_edition.name,
        "organization_name": (
            registration.source_organization.official_name
            if registration.source_organization_id
            else ""
        ),
        "preferred_language": registration.preferred_language,
        "submitted_at": registration.submitted_at.isoformat() if registration.submitted_at else "",
    }
    return {key: neutralize_for_spreadsheet(str(value)) for key, value in raw.items()}


def _generate_csv_bytes(*, dataset_kind: str, registrations) -> tuple[bytes, int]:
    fieldnames = EXPORT_DATASET_FIELD_SCHEMAS[dataset_kind]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    row_count = 0
    for registration in registrations:
        writer.writerow(_registration_row(registration))
        row_count += 1
    return buffer.getvalue().encode("utf-8"), row_count


def request_export(
    *,
    event_edition,
    organization=None,
    dataset_kind: str = ExportDatasetKind.REGISTRATIONS_SUMMARY,
    purpose_code: str,
    reason: str,
    requested_by,
    registration_ids: list,
    retention_days: int = DEFAULT_EXPORT_RETENTION_DAYS,
    idempotency_key: str | None = None,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
) -> ExportRequest:
    if purpose_code not in ALLOWED_EXPORT_PURPOSE_CODES:
        raise InvalidExportPurposeError(f"{purpose_code!r} is not an approved export purpose.")
    if not reason.strip():
        raise InvalidExportReasonError("A controlled export requires a human-readable reason.")
    if not has_scoped_permission(
        requested_by,
        "exports.add_exportrequest",
        event_edition_id=event_edition.pk,
        organization_id=getattr(organization, "pk", None),
    ):
        raise ExportAuthorizationError("The requester is not authorized for this export scope.")

    audit_recorder = audit_recorder or PersistentAuditRecorder()
    key = idempotency_key or uuid.uuid4().hex
    existing = ExportRequest.objects.filter(idempotency_key=key).first()
    if existing is not None:
        same_request = (
            existing.requested_by_id == requested_by.pk
            and existing.event_edition_id == event_edition.pk
            and existing.organization_id == getattr(organization, "pk", None)
            and existing.dataset_kind == dataset_kind
            and existing.purpose_code == purpose_code
            and existing.reason == reason[:500]
        )
        if not same_request:
            raise ExportIdempotencyConflictError(
                "The idempotency key is already bound to a different export request."
            )
        return existing

    if dataset_kind not in EXPORT_DATASET_FIELD_SCHEMAS:
        raise ValueError("The requested export dataset is not supported.")

    # Re-derive the AUTHORIZED scope from the declared event/organization --
    # never trust the caller-supplied id list beyond this filter (§4.5).
    from apps.exports.selectors import registrations_visible_for_export

    scoped_registrations = (
        registrations_visible_for_export(requested_by)
        .select_related("event_edition", "source_organization")
        .filter(pk__in=registration_ids, event_edition=event_edition)
    )
    if organization is not None:
        scoped_registrations = scoped_registrations.filter(source_organization=organization)
    scoped_registrations = list(scoped_registrations.order_by("public_reference"))

    content_bytes, row_count = _generate_csv_bytes(
        dataset_kind=dataset_kind, registrations=scoped_registrations
    )
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    storage_key = get_private_storage().save(io.BytesIO(content_bytes), content_type="text/csv")

    now = timezone.now()
    try:
        with transaction.atomic():
            export_request = ExportRequest.objects.create(
                dataset_kind=dataset_kind,
                schema_version=EXPORT_SCHEMA_VERSION,
                event_edition=event_edition,
                organization=organization,
                purpose_code=purpose_code,
                reason=reason[:500],
                requested_by=requested_by,
                idempotency_key=key,
                target_registration_ids=[str(reg.pk) for reg in scoped_registrations],
                status=ExportStatus.READY,
                row_count=row_count,
                storage_key=storage_key,
                content_sha256=content_sha256,
                expires_at=now + timedelta(days=retention_days),
                ready_at=now,
            )
    except IntegrityError:
        # Concurrent duplicate request with the same idempotency_key -- the
        # file we just wrote is simply discarded; the earlier row wins.
        get_private_storage().delete(storage_key)
        winner = ExportRequest.objects.get(idempotency_key=key)
        if (
            winner.requested_by_id != requested_by.pk
            or winner.event_edition_id != event_edition.pk
            or winner.organization_id != getattr(organization, "pk", None)
            or winner.dataset_kind != dataset_kind
            or winner.purpose_code != purpose_code
            or winner.reason != reason[:500]
        ):
            raise ExportIdempotencyConflictError(
                "The idempotency key is already bound to a different export request."
            ) from None
        return winner

    audit_recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(requested_by, "pk", None),
            action_code=action_codes.EXPORT_REQUEST_READY,
            target_type="ExportRequest",
            target_uuid=export_request.pk,
            event_edition_id=getattr(event_edition, "pk", None),
            result="SUCCESS",
            after_summary={
                "purpose_code": purpose_code,
                "dataset_kind": dataset_kind,
                "row_count": row_count,
            },
            correlation_id=correlation_id,
        )
    )
    return export_request


def retrieve_export(
    *,
    export_request: ExportRequest,
    actor,
    audit_recorder: AuditRecorder | None = None,
    correlation_id: str = "",
):
    """Return a readable binary stream for `export_request`'s generated
    file, or raise. Every outcome -- success or denial -- is audited."""
    audit_recorder = audit_recorder or PersistentAuditRecorder()

    def _deny(reason_code: str):
        audit_recorder.record(
            AuditRecord(
                actor_type="OPERATIONAL_USER",
                actor_user_id=getattr(actor, "pk", None),
                action_code=action_codes.EXPORT_RETRIEVAL_DENIED,
                target_type="ExportRequest",
                target_uuid=export_request.pk,
                event_edition_id=export_request.event_edition_id,
                result="DENIED",
                reason_code=reason_code,
                correlation_id=correlation_id,
            )
        )

    if not has_scoped_permission(
        actor,
        "exports.view_exportrequest",
        event_edition_id=export_request.event_edition_id,
        organization_id=export_request.organization_id,
    ):
        _deny("unauthorized")
        raise ExportAuthorizationError("The actor is not authorized to retrieve this export.")

    from apps.exports.selectors import export_requests_visible_to

    if not export_requests_visible_to(actor).filter(pk=export_request.pk).exists():
        _deny("out_of_scope")
        raise ExportAuthorizationError("The actor is not authorized to retrieve this export.")

    if export_request.status != ExportStatus.READY:
        _deny("not_ready")
        raise ExportNotReadyError("This export is not ready for retrieval.")
    if export_request.expires_at <= timezone.now():
        ExportRequest.objects.filter(pk=export_request.pk, status=ExportStatus.READY).update(
            status=ExportStatus.EXPIRED
        )
        _deny("expired")
        raise ExportExpiredError("This export has expired.")

    try:
        stream = get_private_storage().open(export_request.storage_key)
    except (ObjectNotFound, PrivateStorageError):  # fmt: skip
        _deny("storage_object_missing")
        raise ExportNotReadyError("This export is not ready for retrieval.") from None

    ExportRequest.objects.filter(pk=export_request.pk).update(
        retrieved_count=F("retrieved_count") + 1, last_retrieved_at=timezone.now()
    )
    audit_recorder.record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_codes.EXPORT_RETRIEVED,
            target_type="ExportRequest",
            target_uuid=export_request.pk,
            event_edition_id=export_request.event_edition_id,
            result="SUCCESS",
            correlation_id=correlation_id,
        )
    )
    return stream


def expire_due_exports(*, now=None) -> int:
    """Idempotent sweep: transition every READY export past `expires_at` to
    EXPIRED. Safe to call repeatedly (a management command, e.g.) -- rows
    already EXPIRED are simply not matched again."""
    now = now or timezone.now()
    return ExportRequest.objects.filter(status=ExportStatus.READY, expires_at__lte=now).update(
        status=ExportStatus.EXPIRED
    )
