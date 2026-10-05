"""What a scan outcome means for a stored document and for access to it.

Uploads go through the real `ClamdScanner` against the simulated clamd of
`test_clamd_scanner.py` (not a live ClamAV). Only an explicit clean answer
stores a document as CLEAN; a positive result and every "no verdict" outcome
store nothing at all (no StoredObject row, no storage bytes); and a document
whose scan status is anything but CLEAN is never streamed, whoever asks.
"""

from __future__ import annotations

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import (
    Document,
    DocumentStatus,
    MalwareScanStatus,
    StoredObject,
)
from apps.documents.services import (
    SCAN_UNAVAILABLE,
    NationalIdCardValidationError,
    PassportIdentityPageValidationError,
    ProfilePhotoValidationError,
    active_passport_identity_page,
    save_national_id_card,
    save_passport_identity_page,
    save_profile_photo,
)
from apps.documents.tests.factories import make_test_photo
from apps.documents.tests.test_clamd_scanner import _answer, clamd  # noqa: F401
from apps.documents.tests.test_views import _login_participant, _stream_url
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.registrations.models import Registration, RegistrationProfile, RegistrationSourceKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def person() -> Person:
    return Person.objects.create(display_name="Scan Outcome Person")


def _registration(person: Person, *, nationality: str) -> Registration:
    from apps.core.models import Country

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"SCAN{nationality}", name="Scan test", timezone="UTC", starts_at=now, ends_at=now
    )
    registration = Registration.objects.create(
        public_reference=f"SCAN{nationality}-R-000001",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key="open",
    )
    RegistrationProfile.objects.create(
        registration=registration,
        nationality_code_id=nationality,
        country_of_residence_id=nationality,
    )
    return registration


def _use_clamd(address: dict):
    return override_settings(
        MALWARE_SCANNER_BACKEND="apps.documents.scanning.ClamdScanner",
        MALWARE_SCANNER_ALLOW_LOCAL_STUBS=False,
        CLAMD_SOCKET_PATH="",
        CLAMD_HOST=address["host"],
        CLAMD_PORT=address["port"],
        CLAMD_CONNECT_TIMEOUT_SECONDS=2,
        CLAMD_SCAN_TIMEOUT_SECONDS=5,
    )


def _nothing_stored() -> bool:
    return not StoredObject.objects.exists() and not Document.objects.exists()


def test_only_an_explicit_clean_answer_stores_a_clean_accessible_document(clamd, person) -> None:  # noqa: F811
    registration = _registration(person, nationality="FR")
    _recorder, address = clamd(_answer(b"stream: OK\0"))
    with _use_clamd(address):
        document = save_passport_identity_page(
            registration=registration, person=person, uploaded_file=make_test_photo()
        )
    document.refresh_from_db()
    assert document.status == DocumentStatus.ACTIVE
    assert document.stored_object.malware_scan_status == MalwareScanStatus.CLEAN
    assert active_passport_identity_page(registration).pk == document.pk


def test_a_positive_result_stores_nothing(clamd, person) -> None:  # noqa: F811
    registration = _registration(person, nationality="FR")
    _recorder, address = clamd(_answer(b"stream: Example-Test-Signature FOUND\0"))
    with _use_clamd(address), pytest.raises(PassportIdentityPageValidationError) as excinfo:
        save_passport_identity_page(
            registration=registration, person=person, uploaded_file=make_test_photo()
        )
    assert excinfo.value.reason_code == "scan_rejected"
    assert _nothing_stored()


@pytest.mark.parametrize(
    "answer",
    [
        b"stream: maybe\0",
        b"INSTREAM size limit exceeded. ERROR\0",
        b"Some scanner failure ERROR\0",
        b"stream: OK",
    ],
)
def test_no_verdict_stores_nothing_and_says_scan_unavailable(clamd, person, answer) -> None:  # noqa: F811
    registration = _registration(person, nationality="FR")
    _recorder, address = clamd(_answer(answer))
    with _use_clamd(address), pytest.raises(PassportIdentityPageValidationError) as excinfo:
        save_passport_identity_page(
            registration=registration, person=person, uploaded_file=make_test_photo()
        )
    assert excinfo.value.reason_code == SCAN_UNAVAILABLE
    assert _nothing_stored()


def test_an_unreachable_scanner_refuses_every_document_kind(person) -> None:
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    algerian = _registration(person, nationality="DZ")
    foreign_person = Person.objects.create(display_name="Second")
    foreign = _registration(foreign_person, nationality="FR")
    with _use_clamd({"host": "127.0.0.1", "port": port}):
        with pytest.raises(ProfilePhotoValidationError) as photo_error:
            save_profile_photo(
                registration=foreign, person=foreign_person, uploaded_file=make_test_photo()
            )
        with pytest.raises(NationalIdCardValidationError) as card_error:
            save_national_id_card(
                registration=algerian, person=person, uploaded_file=make_test_photo()
            )
    assert photo_error.value.reason_code == SCAN_UNAVAILABLE
    assert card_error.value.reason_code == SCAN_UNAVAILABLE
    assert _nothing_stored()


@pytest.mark.parametrize(
    "scan_status",
    [MalwareScanStatus.PENDING, MalwareScanStatus.REJECTED, MalwareScanStatus.FAILED],
)
def test_a_document_that_is_not_clean_is_never_streamed(
    client: Client, person, scan_status
) -> None:
    registration = _registration(person, nationality="FR")
    document = save_profile_photo(
        registration=registration, person=person, uploaded_file=make_test_photo()
    )
    _login_participant(client, person)
    assert client.get(_stream_url(document)).status_code == 200
    StoredObject.objects.filter(pk=document.stored_object_id).update(
        malware_scan_status=scan_status
    )
    assert client.get(_stream_url(document)).status_code == 404


def test_the_wizard_shows_a_retry_message_when_the_scanner_is_unavailable(
    client: Client, django_capture_on_commit_callbacks
) -> None:
    from apps.core.models import Country, Sector
    from apps.registrations.tests.test_views import _otp_login, _professional_step_payload

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    _otp_login(client, django_capture_on_commit_callbacks, "scan-outage@example.com")
    client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Amine",
            "family_name": "Benali",
            "date_of_birth": "1990-01-01",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN",
            "nin_value": "123456789012345678",
        },
    )
    client.post(
        reverse("registrations:step-contact"),
        {"mobile_country_code": "DZ", "mobile_number": "0551234567"},
    )
    mail.outbox.clear()
    with override_settings(
        MALWARE_SCANNER_BACKEND="apps.documents.scanning.UnavailableStubScanner"
    ):
        response = client.post(
            reverse("registrations:step-professional"),
            {**_professional_step_payload(), "profile_photo": make_test_photo()},
        )
    assert response.status_code == 200
    errors = response.context["form"].errors["profile_photo"]
    assert any("could not be checked for safety" in error for error in errors)
    assert not Document.objects.exists()
