"""StoredObject/Document invariant tests (Schema §7.2-§7.3)."""

from __future__ import annotations

import pytest
from django.db import connection
from django.utils import timezone

from apps.documents.models import Document, DocumentType, StoredObject
from apps.events.models import EventEdition
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


def test_stored_object_table_has_no_binary_content_column() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'documents_stored_object'"
        )
        columns = {row[0] for row in cursor.fetchall()}
    # Only opaque metadata columns exist -- never a bytes/blob column.
    assert "content" not in columns
    assert "bytes" not in columns
    assert "data" not in columns
    assert "storage_key" in columns


def test_document_references_stored_object_and_registration() -> None:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="DOCTEST", name="Doc Test", timezone="UTC", starts_at=now, ends_at=now
    )
    registration = Registration.objects.create(
        public_reference="DOCTEST-R-000001",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="ctx",
    )
    stored_object = StoredObject.objects.create(
        storage_key="abc123",
        bucket_class="RESTRICTED",
        content_type="image/jpeg",
        size_bytes=1024,
        sha256="a" * 64,
    )
    document = Document.objects.create(
        stored_object=stored_object,
        registration=registration,
        document_type=DocumentType.PROFILE_PHOTO,
        purpose_code="PROFILE_PHOTO",
    )
    assert document.stored_object == stored_object
    assert document.registration == registration


def test_stored_object_key_is_unique() -> None:
    StoredObject.objects.create(
        storage_key="dup-key",
        bucket_class="RESTRICTED",
        content_type="image/jpeg",
        size_bytes=10,
        sha256="b" * 64,
    )
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        StoredObject.objects.create(
            storage_key="dup-key",
            bucket_class="RESTRICTED",
            content_type="image/jpeg",
            size_bytes=20,
            sha256="c" * 64,
        )
