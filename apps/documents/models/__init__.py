"""Stored object metadata and document records (Schema §7.2-§7.3).

`information_request_item_id` is omitted: `InformationRequest` (Schema
§8.4) is Phase 2+ scope. PostgreSQL never holds document bytes
here -- `StoredObject.storage_key` is an opaque handle into
`apps.documents.storage.PrivateStorage` (ADR-0013).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.models import UUIDPrimaryKeyModel


class StoredObjectBucketClass(models.TextChoices):
    RESTRICTED = "RESTRICTED", "Restricted"
    STANDARD_PROTECTED = "STANDARD_PROTECTED", "Standard protected"


class MalwareScanStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    CLEAN = "CLEAN", "Clean"
    REJECTED = "REJECTED", "Rejected"
    FAILED = "FAILED", "Failed"


class StoredObject(UUIDPrimaryKeyModel):
    """Metadata for one file held in `PrivateStorage` (Schema §7.2).

    PostgreSQL MUST NOT contain the document binary -- only this metadata
    row and the opaque `storage_key` handle.
    """

    storage_key = models.CharField(max_length=200, unique=True)
    bucket_class = models.CharField(max_length=24, choices=StoredObjectBucketClass.choices)
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    encryption_key_ref = models.CharField(max_length=200, blank=True, default="")
    malware_scan_status = models.CharField(
        max_length=16, choices=MalwareScanStatus.choices, default=MalwareScanStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    purge_after = models.DateTimeField(null=True, blank=True)
    legal_hold = models.BooleanField(default=False)

    class Meta:
        db_table = "documents_stored_object"


class DocumentType(models.TextChoices):
    PROFILE_PHOTO = "PROFILE_PHOTO", "Profile photo"
    PASSPORT_IDENTITY_PAGE = "PASSPORT_IDENTITY_PAGE", "Passport identity page"
    # IDV-3 (DOC-01, A13-05): requested only for the manual review of an
    # Algerian NIN-route identity, never by default.
    NATIONAL_ID_CARD = "NATIONAL_ID_CARD", "National identity card"
    REQUESTED_EVIDENCE = "REQUESTED_EVIDENCE", "Requested evidence"
    OTHER = "OTHER", "Other"


#: Identity evidence: streamed to staff only with `people.view_identity_evidence`.
IDENTITY_EVIDENCE_DOCUMENT_TYPES = frozenset({"PASSPORT_IDENTITY_PAGE", "NATIONAL_ID_CARD"})


class DocumentStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    REPLACED = "REPLACED", "Replaced"
    REJECTED = "REJECTED", "Rejected"
    PURGED = "PURGED", "Purged"


class Document(UUIDPrimaryKeyModel):
    """Purpose-bound document record (Schema §7.3). Access is purpose-bound
    and written to `AuditEvent` (Prompt 4 streaming view)."""

    stored_object = models.OneToOneField(
        StoredObject, on_delete=models.PROTECT, related_name="document"
    )
    registration = models.ForeignKey(
        "registrations.Registration", on_delete=models.PROTECT, related_name="documents"
    )
    person = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="documents"
    )
    document_type = models.CharField(max_length=32, choices=DocumentType.choices)
    purpose_code = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16, choices=DocumentStatus.choices, default=DocumentStatus.ACTIVE
    )
    replaced_by = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "documents_document"
        indexes = [
            models.Index(
                fields=["registration", "document_type", "status"], name="doc_document_reg_type_idx"
            ),
        ]
