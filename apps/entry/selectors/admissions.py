"""Admission evidence shared by every admission decision (Phase 4 Prompt 3
correction 5, developer decisions D3-A, D3-B option S1 and D3-C; no
migration).

An Entry Event is the normal record of a physical admission. A synchronized
offline admission can nevertheless be kept ONLY in its immutable
`SyncOperation`: a changed-rule conflict whose override reason can no longer
be linked (deleted, renamed or moved after the package snapshot), because an
Entry Event on a non-admittable result must name an Entry Override, and an
Entry Override must name a catalogue reason. This module is the ONE
definition of that "unlinked admission evidence". The online evaluator, the
occurrence-time evaluation at synchronization, the synchronization
classifier and the Offline Package projection all read it, so such an
admission still counts as a prior admission for single-entry denial and for
the re-entry advisories.

Qualifying evidence is exactly an operation that:

* is an ENTRY_DECISION reporting ADMIT;
* is durable and processed, in an approved conflict status (CONFLICT or
  RECONCILIATION_REQUIRED);
* belongs to the registration's own event;
* has NO linked Entry Event (checked through the one-to-one
  `EntryEvent.sync_operation` link itself, not only `result_reference`).

PENDING, REJECTED, QUARANTINED and SECURITY_CONFLICT operations never
qualify: security conflicts keep their approved behaviour (decision D3-C).
An operation WITH a linked Entry Event is already counted through that
event, so nothing is counted twice. Reconciliation cases are not consulted:
closing a case never erases the physical admission.

Reads only. Nothing here creates an Entry Event, invents an override reason
or changes an operation's outcome, and reporting (Entry Event history,
attendance, exports) is unchanged: it still reads Entry Events only.
"""

from __future__ import annotations

from django.db.models import Max

from apps.entry.models import (
    EntryDecision,
    SyncOperation,
    SyncOperationStatus,
    SyncOperationType,
)

#: The approved conflict statuses of a physical admission the server kept.
UNLINKED_ADMISSION_STATUSES: tuple[str, ...] = (
    SyncOperationStatus.CONFLICT,
    SyncOperationStatus.RECONCILIATION_REQUIRED,
)

#: Only what the consumers need (never the payload, note or signature).
_FIELDS = ("pk", "public_id", "device_id", "registration_id", "occurred_at")


def unlinked_admissions():
    """Every qualifying unlinked admission, across registrations."""
    return SyncOperation.objects.filter(
        operation_type=SyncOperationType.ENTRY_DECISION,
        status__in=UNLINKED_ADMISSION_STATUSES,
        processed_at__isnull=False,
        occurred_at__isnull=False,
        payload_json__decision=EntryDecision.ADMIT,
        entry_event__isnull=True,
    )


def unlinked_admissions_for(registration, *, before=None, exclude=None):
    """The qualifying unlinked admissions of ONE Registration Context, in its
    own event. `before` keeps only those that occurred strictly earlier
    (occurrence-time evaluation); `exclude` is the operation being assessed."""
    queryset = unlinked_admissions().filter(
        registration_id=registration.pk, event_edition_id=registration.event_edition_id
    )
    if before is not None:
        queryset = queryset.filter(occurred_at__lt=before)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude)
    return queryset.only(*_FIELDS)


def latest_unlinked_admission(registration, *, before=None, exclude=None):
    """The most recent qualifying unlinked admission, or None."""
    return (
        unlinked_admissions_for(registration, before=before, exclude=exclude)
        .order_by("-occurred_at", "-pk")
        .first()
    )


def last_unlinked_admission_times(registration_ids, *, event_edition_id) -> dict:
    """{registration id: latest occurrence} of qualifying unlinked admissions
    (the Offline Package projection's `last_admitted_at`)."""
    registration_ids = list(registration_ids)
    if not registration_ids:
        return {}
    return dict(
        unlinked_admissions()
        .filter(registration_id__in=registration_ids, event_edition_id=event_edition_id)
        .values("registration_id")
        .annotate(last=Max("occurred_at"))
        .values_list("registration_id", "last")
    )
