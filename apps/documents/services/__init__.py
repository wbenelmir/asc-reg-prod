"""Purpose-bound document write services (Schema §7.2-§7.3, Prompt 4 final closure pass §4).

`save_profile_photo` is the only writer of `PROFILE_PHOTO` documents. It
never places file bytes in PostgreSQL (only `StoredObject` metadata and the
opaque `storage_key`), never produces a public URL (`PrivateStorage` has no
such method at all), and cleans up newly written storage bytes when a
later step in the same call fails.
"""

from __future__ import annotations

import hashlib
import io

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from apps.documents.models import (
    Document,
    DocumentStatus,
    DocumentType,
    MalwareScanStatus,
    StoredObject,
    StoredObjectBucketClass,
)
from apps.documents.scanning import ScannerUnavailable, get_scanner
from apps.documents.storage import get_private_storage

#: Reason code of an upload refused because the malware scanner gave no
#: verdict (outage, timeout, malformed answer, size limit). Nothing is stored;
#: the participant is asked to try again later.
SCAN_UNAVAILABLE = "scan_unavailable"

ALLOWED_PROFILE_PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png"}
PROFILE_PHOTO_PURPOSE_CODE = "PROFESSIONAL_PROFILE_PHOTO"

ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES = {"image/jpeg", "image/png"}
PASSPORT_IDENTITY_PAGE_PURPOSE_CODE = "PASSPORT_IDENTITY_PAGE"
# Duplicated locally rather than imported from `apps.registrations` (which
# itself imports FROM this module) -- avoids a reverse app dependency for
# one shared constant. Matches the identical duplication already present
# in `apps.registrations.forms`/`apps.registrations.services`.
_ALGERIA_COUNTRY_CODE = "DZ"


class ProfilePhotoValidationError(Exception):
    """Raised when an uploaded profile photograph fails validation. Never includes file bytes."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PassportIdentityPageValidationError(Exception):
    """Raised when an uploaded passport identity-page image fails validation
    (Prompt 5 correction pass §2). Never includes file bytes or the passport
    number/value."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _validate_image_upload(
    uploaded_file: UploadedFile,
    *,
    allowed_content_types: set[str],
    max_size_bytes: int,
    min_width: int = 0,
    min_height: int = 0,
    error_cls: type[Exception],
) -> None:
    if uploaded_file.content_type not in allowed_content_types:
        raise error_cls("unsupported_type")
    if uploaded_file.size is not None and uploaded_file.size > max_size_bytes:
        raise error_cls("too_large")

    from PIL import Image, UnidentifiedImageError

    uploaded_file.seek(0)
    try:
        with Image.open(uploaded_file) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):  # fmt: skip
        raise error_cls("invalid_image") from None
    uploaded_file.seek(0)
    try:
        with Image.open(uploaded_file) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):  # fmt: skip
        raise error_cls("invalid_image") from None
    finally:
        uploaded_file.seek(0)
    if width < min_width or height < min_height:
        raise error_cls("too_small")


def _validate_profile_photo(
    uploaded_file: UploadedFile, *, max_size_bytes: int, min_width: int, min_height: int
) -> None:
    _validate_image_upload(
        uploaded_file,
        allowed_content_types=ALLOWED_PROFILE_PHOTO_CONTENT_TYPES,
        max_size_bytes=max_size_bytes,
        min_width=min_width,
        min_height=min_height,
        error_cls=ProfilePhotoValidationError,
    )


def _validate_passport_identity_page(uploaded_file: UploadedFile, *, max_size_bytes: int) -> None:
    _validate_image_upload(
        uploaded_file,
        allowed_content_types=ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES,
        max_size_bytes=max_size_bytes,
        error_cls=PassportIdentityPageValidationError,
    )


@transaction.atomic
def _save_purpose_bound_document(
    *,
    registration,
    person,
    uploaded_file: UploadedFile,
    document_type: str,
    purpose_code: str,
    scan_rejected_error: Exception,
) -> Document:
    """Scan and store one purpose-bound document; replace any prior active one
    of the SAME `document_type` for this Registration. Shared by
    `save_profile_photo` and `save_passport_identity_page` -- the only
    difference between them is which validation ran before this is called
    and which `document_type`/`purpose_code` to write.
    """
    content_bytes = uploaded_file.read()
    try:
        scan_result = get_scanner().scan(io.BytesIO(content_bytes))
    except ScannerUnavailable:
        # No verdict is never a clean result: nothing is written to storage
        # or the database, and the caller shows a retry message.
        raise type(scan_rejected_error)(SCAN_UNAVAILABLE) from None

    storage = get_private_storage()
    storage_key = storage.save(io.BytesIO(content_bytes), uploaded_file.content_type)
    try:
        stored_object = StoredObject.objects.create(
            storage_key=storage_key,
            bucket_class=StoredObjectBucketClass.RESTRICTED,
            content_type=uploaded_file.content_type,
            size_bytes=len(content_bytes),
            sha256=hashlib.sha256(content_bytes).hexdigest(),
            malware_scan_status=(
                MalwareScanStatus.CLEAN if scan_result.clean else MalwareScanStatus.REJECTED
            ),
        )
        if not scan_result.clean:
            raise scan_rejected_error

        # Scoped by `purpose_code` too (Phase 2 Prompt 3): PROFILE_PHOTO and
        # PASSPORT_IDENTITY_PAGE each use one constant purpose code, so this
        # is unchanged for them, but REQUESTED_EVIDENCE gives each
        # `RequestItem` its OWN purpose code -- without this, uploading
        # evidence for one requested item would silently replace an
        # unrelated item's already-uploaded evidence merely for sharing the
        # same `document_type`.
        Document.objects.filter(
            registration=registration,
            document_type=document_type,
            purpose_code=purpose_code,
            status=DocumentStatus.ACTIVE,
        ).update(status=DocumentStatus.REPLACED)

        return Document.objects.create(
            stored_object=stored_object,
            registration=registration,
            person=person,
            document_type=document_type,
            purpose_code=purpose_code,
            status=DocumentStatus.ACTIVE,
        )
    except Exception:
        # Cleanup-safe: never leave newly written storage bytes behind when
        # the surrounding database transaction is about to roll back
        # (Prompt 4 final closure pass §4) -- best-effort only, mirroring
        # the same pattern already used by PrivateFileSystemStorage/
        # S3PrivateStorage's own internal cleanup.
        try:
            storage.delete(storage_key)
        except Exception:  # noqa: S110 - best-effort cleanup only
            pass
        raise


def save_profile_photo(*, registration, person, uploaded_file: UploadedFile) -> Document:
    """Validate, scan, and store one profile photograph; replace any prior active one.

    MUST run inside the same completeness-guarded submission path as every
    other required field (Prompt 4 final closure pass §1/§4) -- this
    function itself only handles storage-layer correctness, not "is a
    photo present" completeness, which is the domain service's job.
    """
    from django.conf import settings

    _validate_profile_photo(
        uploaded_file,
        max_size_bytes=settings.PROFILE_PHOTO_MAX_SIZE_BYTES,
        min_width=settings.PROFILE_PHOTO_MIN_WIDTH_PX,
        min_height=settings.PROFILE_PHOTO_MIN_HEIGHT_PX,
    )
    return _save_purpose_bound_document(
        registration=registration,
        person=person,
        uploaded_file=uploaded_file,
        document_type=DocumentType.PROFILE_PHOTO,
        purpose_code=PROFILE_PHOTO_PURPOSE_CODE,
        scan_rejected_error=ProfilePhotoValidationError("scan_rejected"),
    )


def _eligible_document(registration, *, document_type: str, purpose_code: str):
    """The registration's newest document that can satisfy a requirement:
    ACTIVE, scanned CLEAN, of exactly this type and purpose, and uploaded by
    the registration's own participant. Another document type, another
    registration's or another participant's upload, a replaced or purged
    document and a file the scan rejected or never cleared never count."""
    if registration.person_id is None:
        return None
    return (
        registration.documents.filter(
            document_type=document_type,
            purpose_code=purpose_code,
            person_id=registration.person_id,
            status=DocumentStatus.ACTIVE,
            stored_object__malware_scan_status=MalwareScanStatus.CLEAN,
        )
        .order_by("-created_at")
        .first()
    )


def active_profile_photo(registration) -> Document | None:
    """Return the current eligible profile-photo Document for `registration`,
    if any (`_eligible_document`). The photograph is required for every new
    submission; the completeness guard reads this."""
    return _eligible_document(
        registration,
        document_type=DocumentType.PROFILE_PHOTO,
        purpose_code=PROFILE_PHOTO_PURPOSE_CODE,
    )


def save_passport_identity_page(
    *, registration, person, uploaded_file: UploadedFile, allow_algerian_exception: bool = False
) -> Document:
    """Validate, scan, and store one passport identity-page image (Prompt 5
    correction pass §2/§6, REG-03); replace any prior active one.

    This IS the public/domain service boundary that actually saves a
    passport identity page, so it enforces every precondition itself,
    fail-closed -- it never relies solely on the caller having already
    checked the form, the view, or `save_identity_step`'s own inline
    checks (Prompt 5 correction pass §6):
      - the Registration genuinely belongs to the supplied Person;
      - the Registration's identity path is genuinely PASSPORT (a non-
        Algerian nationality on file is this project's own established
        proxy for "not the NIN path", matching
        `_require_complete_identity`'s identical branching elsewhere).

    IDV-3 (amendment A-13, A13-02, IDV-08): the identity page is required from
    every foreign participant, so the former per-event switch
    (`EventEdition.passport_identity_page_upload_enabled`) is no longer
    consulted. The column is kept, unused, so no data is rewritten.
    `allow_algerian_exception` lets the staff-requested correction flow accept
    an Algerian passport page as exception evidence (A13-01); it is never set
    by the registration wizard.
    """
    from django.conf import settings

    from apps.registrations.models import RegistrationProfile

    if registration.person_id != person.id:
        raise PassportIdentityPageValidationError("registration_person_mismatch")
    profile = RegistrationProfile.objects.filter(registration=registration).first()
    if profile is None or (
        profile.nationality_code_id == _ALGERIA_COUNTRY_CODE and not allow_algerian_exception
    ):
        raise PassportIdentityPageValidationError("not_passport_path")

    _validate_passport_identity_page(
        uploaded_file, max_size_bytes=settings.PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES
    )
    return _save_purpose_bound_document(
        registration=registration,
        person=person,
        uploaded_file=uploaded_file,
        document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
        purpose_code=PASSPORT_IDENTITY_PAGE_PURPOSE_CODE,
        scan_rejected_error=PassportIdentityPageValidationError("scan_rejected"),
    )


def active_passport_identity_page(registration) -> Document | None:
    """Return the current eligible passport identity-page Document for
    `registration`, if any (Prompt 5 correction pass §2; `_eligible_document`).
    Required for every new submission on the passport route."""
    return _eligible_document(
        registration,
        document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
        purpose_code=PASSPORT_IDENTITY_PAGE_PURPOSE_CODE,
    )


NATIONAL_ID_CARD_PURPOSE_CODE = "NATIONAL_ID_CARD"


class NationalIdCardValidationError(Exception):
    """Raised when an uploaded national identity card image fails validation
    (IDV-3, DOC-01). Never includes file bytes or the identifier."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def save_national_id_card(*, registration, person, uploaded_file: UploadedFile) -> Document:
    """Validate, scan and store the national identity card image of an Algerian
    NIN-route registration; replace any prior active one (IDV-3, DOC-01).

    Requested only for manual review, never by default (FR-IDV-006): callers
    are the participant's correction flow while an identity correction is
    open. Same formats, size limit, private storage and malware scan as the
    passport identity page (A13-05).
    """
    from django.conf import settings

    from apps.registrations.models import RegistrationProfile

    if registration.person_id != person.id:
        raise NationalIdCardValidationError("registration_person_mismatch")
    profile = RegistrationProfile.objects.filter(registration=registration).first()
    if profile is None or profile.nationality_code_id != _ALGERIA_COUNTRY_CODE:
        raise NationalIdCardValidationError("not_nin_path")
    _validate_image_upload(
        uploaded_file,
        allowed_content_types=ALLOWED_PASSPORT_IDENTITY_PAGE_CONTENT_TYPES,
        max_size_bytes=settings.PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES,
        error_cls=NationalIdCardValidationError,
    )
    return _save_purpose_bound_document(
        registration=registration,
        person=person,
        uploaded_file=uploaded_file,
        document_type=DocumentType.NATIONAL_ID_CARD,
        purpose_code=NATIONAL_ID_CARD_PURPOSE_CODE,
        scan_rejected_error=NationalIdCardValidationError("scan_rejected"),
    )


def active_identity_evidence(registration) -> list[Document]:
    """The registration's ACTIVE identity evidence (national identity card and
    passport identity page), clean or not, newest first. Only a CLEAN
    document can be relied on as reviewed evidence (A13-05); the caller
    decides with `is_reviewable_evidence`."""
    from apps.documents.models import IDENTITY_EVIDENCE_DOCUMENT_TYPES

    return list(
        registration.documents.filter(
            document_type__in=IDENTITY_EVIDENCE_DOCUMENT_TYPES, status=DocumentStatus.ACTIVE
        )
        .select_related("stored_object")
        .order_by("-created_at")
    )


def is_reviewable_evidence(document: Document) -> bool:
    """A document pending a scan, rejected by it, or no longer active is never
    reviewed evidence."""
    return (
        document.status == DocumentStatus.ACTIVE
        and document.stored_object.malware_scan_status == MalwareScanStatus.CLEAN
    )


class RequestedEvidenceValidationError(Exception):
    """Raised when a document uploaded against an Additional Information
    Request's `RequestItem` fails validation (Phase 2 Prompt 3 §7.3). Never
    includes file bytes."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


ALLOWED_REQUESTED_EVIDENCE_CONTENT_TYPES = {"image/jpeg", "image/png"}


def save_requested_evidence(
    *, registration, person, request_item, uploaded_file: UploadedFile
) -> Document:
    """Validate, scan, and store one Additional-Information-Request evidence
    upload (Phase 2 Prompt 3 §7.3), reusing the exact same purpose-bound
    document/storage boundary as `save_profile_photo`/
    `save_passport_identity_page` -- document bytes are never stored in
    PostgreSQL, and this document remains inaccessible to any caller
    lacking the separate document-access permission
    (`apps.documents.views.document_stream`'s own scope check), regardless
    of who holds a review permission.

    `purpose_code` is bound to `request_item.pk`, so uploading evidence for
    one requested item never replaces another item's already-uploaded
    evidence (see `_save_purpose_bound_document`'s purpose-scoped replace).
    """
    from django.conf import settings

    from apps.reviews.models import InformationRequestStatus, RequestItemKind

    if registration.person_id != person.id:
        raise RequestedEvidenceValidationError("registration_person_mismatch")
    if (
        request_item.information_request.registration_id != registration.id
        or request_item.kind != RequestItemKind.DOCUMENT_UPLOAD
        or request_item.document_type != DocumentType.REQUESTED_EVIDENCE
    ):
        raise RequestedEvidenceValidationError("request_item_mismatch")
    if request_item.information_request.status not in (
        InformationRequestStatus.SENT,
        InformationRequestStatus.RESPONSE_IN_PROGRESS,
        InformationRequestStatus.OVERDUE,
    ):
        raise RequestedEvidenceValidationError("request_not_accepting_response")

    _validate_image_upload(
        uploaded_file,
        allowed_content_types=ALLOWED_REQUESTED_EVIDENCE_CONTENT_TYPES,
        max_size_bytes=settings.PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES,
        error_cls=RequestedEvidenceValidationError,
    )
    return _save_purpose_bound_document(
        registration=registration,
        person=person,
        uploaded_file=uploaded_file,
        document_type=DocumentType.REQUESTED_EVIDENCE,
        purpose_code=f"REQUESTED_EVIDENCE:{request_item.pk}",
        scan_rejected_error=RequestedEvidenceValidationError("scan_rejected"),
    )
