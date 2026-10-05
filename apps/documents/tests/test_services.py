"""Profile-photograph storage service tests (Prompt 4 final closure pass §4)."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.documents.models import Document, DocumentStatus, DocumentType, MalwareScanStatus
from apps.documents.services import (
    PassportIdentityPageValidationError,
    ProfilePhotoValidationError,
    active_passport_identity_page,
    active_profile_photo,
    save_passport_identity_page,
    save_profile_photo,
)
from apps.documents.tests.factories import make_test_photo
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.registrations.models import Registration, RegistrationSourceKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def person() -> Person:
    return Person.objects.create(display_name="Photo Person")


@pytest.fixture
def registration(person: Person) -> Registration:
    from django.utils import timezone

    event = EventEdition.objects.create(
        code="DOCTEST",
        name="Doc Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
    )
    return Registration.objects.create(
        public_reference="DOCTEST-R-000001",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key="open",
    )


def test_save_profile_photo_stores_metadata_only_never_bytes_in_postgres(
    registration: Registration, person: Person
) -> None:
    document = save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    document.refresh_from_db()
    assert document.status == DocumentStatus.ACTIVE
    assert document.stored_object.malware_scan_status == MalwareScanStatus.CLEAN
    assert document.stored_object.size_bytes > 0
    # The StoredObject row carries only metadata and an opaque key -- never bytes.
    assert not hasattr(document.stored_object, "content")
    assert isinstance(document.stored_object.storage_key, str)
    assert active_profile_photo(registration).pk == document.pk


def test_save_profile_photo_rejects_unsupported_content_type(
    registration: Registration, person: Person
) -> None:
    bad_file = SimpleUploadedFile("evidence.gif", b"GIF89a", content_type="image/gif")
    with pytest.raises(ProfilePhotoValidationError):
        save_profile_photo(registration=registration, person=person, uploaded_file=bad_file)
    assert active_profile_photo(registration) is None


def test_save_profile_photo_rejects_oversized_file(
    registration: Registration, person: Person
) -> None:
    with override_settings(PROFILE_PHOTO_MAX_SIZE_BYTES=10):
        with pytest.raises(ProfilePhotoValidationError):
            save_profile_photo(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )


def test_save_profile_photo_rejects_undersized_dimensions(
    registration: Registration, person: Person
) -> None:
    tiny = make_test_photo(width=10, height=10)
    with pytest.raises(ProfilePhotoValidationError):
        save_profile_photo(registration=registration, person=person, uploaded_file=tiny)


def test_save_profile_photo_rejects_content_scanner_flags_as_infected(
    registration: Registration, person: Person
) -> None:
    with override_settings(MALWARE_SCANNER_BACKEND="apps.documents.scanning.RejectingStubScanner"):
        with pytest.raises(ProfilePhotoValidationError):
            save_profile_photo(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )
    # A rejected scan still creates the StoredObject evidence row (REJECTED),
    # but never an ACTIVE Document -- nothing becomes usable from a failed scan.
    assert active_profile_photo(registration) is None
    assert (
        Document.objects.filter(
            registration=registration, document_type=DocumentType.PROFILE_PHOTO
        ).count()
        == 0
    )


def test_uploading_a_new_photo_replaces_the_previous_active_one(
    registration: Registration, person: Person
) -> None:
    first = save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    second = save_profile_photo(
        registration=registration,
        person=person,
        uploaded_file=make_test_photo(width=400, height=400),
    )
    first.refresh_from_db()
    assert first.status == DocumentStatus.REPLACED
    assert second.status == DocumentStatus.ACTIVE
    assert active_profile_photo(registration).pk == second.pk


def test_failed_scan_does_not_leave_orphaned_storage_bytes(
    registration: Registration, person: Person, settings
) -> None:
    from apps.documents.storage import get_private_storage

    storage = get_private_storage()
    keys_before = set(_list_keys(settings.PRIVATE_STORAGE_ROOT))
    with override_settings(MALWARE_SCANNER_BACKEND="apps.documents.scanning.RejectingStubScanner"):
        with pytest.raises(ProfilePhotoValidationError):
            save_profile_photo(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )
    keys_after = set(_list_keys(settings.PRIVATE_STORAGE_ROOT))
    assert keys_after == keys_before, "rejected-scan bytes must be cleaned up, not left behind"
    del storage


def _list_keys(root) -> list[str]:
    if not root.exists():
        return []
    return [p.name for p in root.iterdir() if not p.name.endswith(".meta.json")]


@pytest.fixture
def passport_eligible_registration(registration: Registration) -> Registration:
    """A Registration that satisfies every fail-closed precondition
    `save_passport_identity_page` now enforces at its own boundary (Prompt
    5 correction pass §6): the event's policy switch is enabled, and a
    `RegistrationProfile` with a non-Algerian nationality is on file (this
    project's own established proxy for "the identity path is PASSPORT")."""
    from apps.core.models import Country
    from apps.registrations.models import RegistrationProfile

    registration.event_edition.passport_identity_page_upload_enabled = True
    registration.event_edition.save(update_fields=["passport_identity_page_upload_enabled"])
    country, _ = Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    RegistrationProfile.objects.create(registration=registration, nationality_code=country)
    return registration


def test_save_passport_identity_page_stores_metadata_only_never_bytes(
    passport_eligible_registration: Registration, person: Person
) -> None:
    registration = passport_eligible_registration
    document = save_passport_identity_page(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    document.refresh_from_db()
    assert document.document_type == DocumentType.PASSPORT_IDENTITY_PAGE
    assert document.status == DocumentStatus.ACTIVE
    assert document.stored_object.malware_scan_status == MalwareScanStatus.CLEAN
    assert not hasattr(document.stored_object, "content")
    assert active_passport_identity_page(registration).pk == document.pk


def test_save_passport_identity_page_rejects_unsupported_content_type(
    passport_eligible_registration: Registration, person: Person
) -> None:
    registration = passport_eligible_registration
    bad_file = SimpleUploadedFile("page.gif", b"GIF89a", content_type="image/gif")
    with pytest.raises(PassportIdentityPageValidationError):
        save_passport_identity_page(
            registration=registration, person=person, uploaded_file=bad_file
        )
    assert active_passport_identity_page(registration) is None


def test_save_passport_identity_page_rejects_oversized_file(
    passport_eligible_registration: Registration, person: Person
) -> None:
    registration = passport_eligible_registration
    with override_settings(PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES=10):
        with pytest.raises(PassportIdentityPageValidationError):
            save_passport_identity_page(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )


def test_save_passport_identity_page_rejects_scanner_flagged_content(
    passport_eligible_registration: Registration, person: Person
) -> None:
    registration = passport_eligible_registration
    with override_settings(MALWARE_SCANNER_BACKEND="apps.documents.scanning.RejectingStubScanner"):
        with pytest.raises(PassportIdentityPageValidationError):
            save_passport_identity_page(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )
    assert active_passport_identity_page(registration) is None
    assert (
        Document.objects.filter(
            registration=registration, document_type=DocumentType.PASSPORT_IDENTITY_PAGE
        ).count()
        == 0
    )


def test_uploading_a_new_passport_identity_page_replaces_the_previous_active_one(
    passport_eligible_registration: Registration, person: Person
) -> None:
    registration = passport_eligible_registration
    first = save_passport_identity_page(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    second = save_passport_identity_page(
        registration=registration,
        person=person,
        uploaded_file=make_test_photo(width=400, height=400),
    )
    first.refresh_from_db()
    assert first.status == DocumentStatus.REPLACED
    assert second.status == DocumentStatus.ACTIVE
    assert active_passport_identity_page(registration).pk == second.pk


def test_passport_identity_page_failed_scan_does_not_leave_orphaned_storage_bytes(
    passport_eligible_registration: Registration, person: Person, settings
) -> None:
    registration = passport_eligible_registration
    keys_before = set(_list_keys(settings.PRIVATE_STORAGE_ROOT))
    with override_settings(MALWARE_SCANNER_BACKEND="apps.documents.scanning.RejectingStubScanner"):
        with pytest.raises(PassportIdentityPageValidationError):
            save_passport_identity_page(
                registration=registration, person=person, uploaded_file=make_test_photo()
            )
    keys_after = set(_list_keys(settings.PRIVATE_STORAGE_ROOT))
    assert keys_after == keys_before


def test_save_passport_identity_page_no_longer_depends_on_the_event_switch(
    registration: Registration, person: Person
) -> None:
    """IDV-3 (amendment A-13, IDV-08): the identity page is required from every
    foreign participant, so the plain `registration` fixture's event switch at
    its default `False` no longer refuses it (superseding the Prompt 5 per-event
    policy). The service still refuses the NIN path and another person."""
    from apps.core.models import Country
    from apps.registrations.models import RegistrationProfile

    country, _ = Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    RegistrationProfile.objects.create(registration=registration, nationality_code=country)
    assert registration.event_edition.passport_identity_page_upload_enabled is False
    document = save_passport_identity_page(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    assert document.document_type == DocumentType.PASSPORT_IDENTITY_PAGE


def test_save_passport_identity_page_rejects_the_nin_path(
    registration: Registration, person: Person
) -> None:
    """A registration whose profile is on the NIN path (Algerian
    nationality) must be rejected at the service boundary itself, never
    merely by the form (Prompt 5 correction pass §6)."""
    from apps.core.models import Country
    from apps.registrations.models import RegistrationProfile

    registration.event_edition.passport_identity_page_upload_enabled = True
    registration.event_edition.save(update_fields=["passport_identity_page_upload_enabled"])
    country, _ = Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    RegistrationProfile.objects.create(registration=registration, nationality_code=country)
    with pytest.raises(PassportIdentityPageValidationError) as excinfo:
        save_passport_identity_page(
            registration=registration, person=person, uploaded_file=make_test_photo()
        )
    assert excinfo.value.reason_code == "not_passport_path"


def test_save_passport_identity_page_rejects_a_registration_belonging_to_another_person(
    passport_eligible_registration: Registration,
) -> None:
    """The service boundary itself verifies Registration ownership -- never
    trusts a caller-supplied `person` blindly (Prompt 5 correction pass §6)."""
    other_person = Person.objects.create(display_name="Someone Else")
    with pytest.raises(PassportIdentityPageValidationError) as excinfo:
        save_passport_identity_page(
            registration=passport_eligible_registration,
            person=other_person,
            uploaded_file=make_test_photo(),
        )
    assert excinfo.value.reason_code == "registration_person_mismatch"
    assert (
        Document.objects.filter(
            registration=passport_eligible_registration,
            document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
        ).count()
        == 0
    )


def test_profile_photo_and_passport_identity_page_do_not_interfere(
    passport_eligible_registration: Registration, person: Person
) -> None:
    """Replacing one purpose-bound document type must never affect the other."""
    registration = passport_eligible_registration
    photo = save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    page = save_passport_identity_page(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    assert active_profile_photo(registration).pk == photo.pk
    assert active_passport_identity_page(registration).pk == page.pk

    # Replacing the photo must not replace the passport page.
    save_profile_photo(
        registration=registration,
        person=person,
        uploaded_file=make_test_photo(width=400, height=400),
    )
    page.refresh_from_db()
    assert page.status == DocumentStatus.ACTIVE


def test_private_storage_object_is_never_publicly_addressable(
    registration: Registration, person: Person
) -> None:
    from apps.documents.storage import PrivateStorage

    document = save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    # Structural guarantee: the storage interface has no method that could
    # produce a public URL for this object at all (Schema §7.2/§17.2).
    method_names = {name for name in dir(PrivateStorage) if not name.startswith("_")}
    assert "url" not in method_names
    assert document.stored_object.storage_key not in {"", None}
