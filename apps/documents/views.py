"""Authenticated, policy-checked, audited protected-document streaming
(Prompt 5 correction pass §3).

`Document.pk` (an opaque, high-entropy UUIDv7) is the externally safe
identifier used in the URL -- never `StoredObject.storage_key`, never a
filesystem path, never an S3 key. Every failure mode (nonexistent,
unauthorized, wrong-owner, wrong-scope, inactive/replaced/rejected/purged,
missing storage object) returns the SAME generic 404, so a caller can never
learn from the response which failure occurred or whether the identifier
even exists.
"""

from __future__ import annotations

from django.http import FileResponse, Http404, HttpRequest
from django.utils import timezone

from apps.accounts import participant_auth
from apps.accounts.policies import has_scoped_permission
from apps.accounts.selectors import registrations_visible_to
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.core.middleware.correlation import get_correlation_id
from apps.documents.models import (
    IDENTITY_EVIDENCE_DOCUMENT_TYPES,
    Document,
    DocumentStatus,
    MalwareScanStatus,
)
from apps.documents.storage import ObjectNotFound, PrivateStorageError, get_private_storage

_GENERIC_NOT_FOUND = "Document not found."


def scan_unavailable_message() -> str:
    """The participant-facing message for an upload refused because the malware
    scanner gave no verdict. Nothing was stored; a retry later may succeed."""
    from django.utils.translation import gettext as _

    return _(
        "The file could not be checked for safety at the moment. Nothing was saved. "
        "Please try again in a few minutes."
    )


def _document_or_404(document_id) -> Document:
    document = (
        Document.objects.filter(pk=document_id)
        .select_related("stored_object", "registration")
        .first()
    )
    if document is None:
        raise Http404(_GENERIC_NOT_FOUND)
    return document


def _is_accessible(document: Document) -> bool:
    """Deny inactive, replaced, rejected, and purged documents unconditionally
    (Prompt 5 correction pass §3) -- applies to every caller, participant or
    operational. A REJECTED scan is defence in depth: `save_profile_photo`/
    `save_passport_identity_page` never commit an ACTIVE Document row for a
    rejected scan in the first place, so this should never actually trigger,
    but the check costs nothing and never assumes that invariant holds."""
    if document.status != DocumentStatus.ACTIVE:
        return False
    return document.stored_object.malware_scan_status == MalwareScanStatus.CLEAN


def _valid_participant_owner_id(request: HttpRequest, document: Document) -> str | None:
    """Return the participant's person id IF their session is valid AND
    they own this document's Registration -- `None` otherwise.

    Uses `get_valid_participant_person_id`, never the raw, expiry-blind
    `get_participant_person_id`/`is_participant_authenticated` (Prompt 5
    correction pass §1): an expired or malformed participant session must
    never authorize document access, exactly like every other denied
    request -- a generic 404, not a 500 or a distinguishable response.
    """
    person_id = participant_auth.get_valid_participant_person_id(request)
    if person_id is None:
        return None
    if str(document.registration.person_id) != str(person_id):
        return None
    return person_id


def _authorized_operational_user(request: HttpRequest, document: Document):
    """Return the authorized `OperationalUser`, or `None`.

    Reuses the SAME scope+permission-checked selector the operations
    intake list/detail views use -- a document is visible to an
    operational user only when its Registration is (Prompt 4 final
    closure pass §6's scoped-permission model applies unchanged here).
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    if not registrations_visible_to(user).filter(pk=document.registration_id).exists():
        return None
    if document.document_type in IDENTITY_EVIDENCE_DOCUMENT_TYPES and not has_scoped_permission(
        user,
        "people.view_identity_evidence",
        event_edition_id=document.registration.event_edition_id,
        organization_id=document.registration.source_organization_id,
    ):
        # IDV-3 (A13-05): identity evidence needs its own scoped permission;
        # seeing the registration is not enough.
        return None
    return user


def _record_access_audit(
    document: Document,
    *,
    actor_type: str,
    operational_user=None,
    participant_person_id: str | None = None,
) -> None:
    """Bounded, append-only evidence of a successful access -- never a
    storage key, filename, identity value, or document byte (Prompt 5
    correction pass §3).

    `actor_type` is the audience whose authorization ACTUALLY granted this
    access -- determined independently by the caller, never re-derived
    here from "is a participant session key merely present" (Prompt 5
    correction pass §3: "chooses the audit actor merely because a
    participant key exists" is exactly the bug this signature prevents).

    `participant_person_id` is the person id RESOLVED ONCE at authorization
    time and threaded through unchanged -- this function never re-evaluates
    participant session validity itself (Prompt 5 correction pass §1
    follow-up, "close the participant audit identity race"). Re-checking
    validity here, after `storage.open()` has already run, would let a
    session expiring mid-request silently turn an already-authorized
    participant access into `actor_person_id=None`.
    """
    if actor_type == "OPERATIONAL_USER":
        actor_person_id = None
        actor_user_id = operational_user.id
    else:
        actor_person_id = participant_person_id
        actor_user_id = None

    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_person_id=actor_person_id,
            action_code="DOCUMENT_STREAMED",
            target_type="Document",
            target_uuid=document.pk,
            event_edition_id=document.registration.event_edition_id,
            result="SUCCESS",
            correlation_id=get_correlation_id() or "",
            occurred_at=timezone.now(),
        )
    )


# Generic, never-personal-data filename by content type -- never the
# original upload's filename, which could itself carry personal
# information (Prompt 5 correction pass §3: "filenames containing
# sensitive values" must never appear, including in audit/log output).
_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
}


def document_stream(request: HttpRequest, document_id):
    document = _document_or_404(document_id)

    if not _is_accessible(document):
        raise Http404(_GENERIC_NOT_FOUND)

    # Evaluated INDEPENDENTLY, never short-circuited by `or` (Prompt 5
    # correction pass §3): the operational path is preferred when it is
    # itself valid and authorized, so a request from a browser session
    # that happens to also carry a participant key is never misattributed
    # to that participant merely because the key exists.
    operational_user = _authorized_operational_user(request, document)
    participant_owner_id = _valid_participant_owner_id(request, document)

    if operational_user is not None:
        actor_type = "OPERATIONAL_USER"
    elif participant_owner_id is not None:
        actor_type = "PARTICIPANT"
    else:
        raise Http404(_GENERIC_NOT_FOUND)

    storage = get_private_storage()
    stored_object = document.stored_object
    try:
        stream = storage.open(stored_object.storage_key)
    except (ObjectNotFound, PrivateStorageError):  # fmt: skip
        # A missing/corrupt storage object is handled the same generic way
        # as every other failure -- never a 500, never a detail that could
        # help enumerate storage state (Prompt 5 correction pass §3).
        raise Http404(_GENERIC_NOT_FOUND) from None

    _record_access_audit(
        document,
        actor_type=actor_type,
        operational_user=operational_user,
        participant_person_id=participant_owner_id,
    )

    extension = _EXTENSION_BY_CONTENT_TYPE.get(stored_object.content_type, "bin")
    response = FileResponse(
        stream,
        content_type=stored_object.content_type,
        as_attachment=True,
        filename=f"document.{extension}",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response
