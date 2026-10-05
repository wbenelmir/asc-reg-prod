"""Passport identity-page upload tests (Prompt 5 correction pass §2, REG-03).

IDV-3 (amendment A-13, IDV-08): the identity page is now required from every
foreign participant, so the upload is always part of the passport path and the
former per-event switch (`passport_identity_page_upload_enabled`) is no longer
consulted. The tests that asserted the switch's effect are rewritten to the new
requirement; the fail-closed NIN-path guarantees are unchanged. The two event
fixtures keep both switch values to prove the switch has no effect any more."""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import participant_auth
from apps.accounts.otp import DeterministicTestOtpGenerator
from apps.core.models import Country, Sector
from apps.core.testing import otp_request_data
from apps.documents.models import Document, DocumentStatus, DocumentType
from apps.documents.tests.factories import make_test_photo
from apps.events.models import EventEdition, EventEditionStatus
from apps.people.services import resolve_or_create_participant_for_email
from apps.registrations.forms import IdentityStepForm
from apps.registrations.models import Registration
from apps.registrations.services import get_or_create_active_draft, save_identity_step

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def enabled_event() -> EventEdition:
    return EventEdition.objects.create(
        code="PPTEST-ON",
        name="Passport Page Test (enabled)",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status=EventEditionStatus.REGISTRATION_OPEN,
        passport_identity_page_upload_enabled=True,
    )


@pytest.fixture
def disabled_event() -> EventEdition:
    return EventEdition.objects.create(
        code="PPTEST-OFF",
        name="Passport Page Test (disabled)",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status=EventEditionStatus.REGISTRATION_OPEN,
        passport_identity_page_upload_enabled=False,
    )


def _future_date() -> datetime.date:
    return datetime.date.today() + datetime.timedelta(days=365)


def _save_passport_identity(draft: Registration, **overrides) -> None:
    kwargs = {
        "registration": draft,
        "given_names": "Sara",
        "family_name": "K",
        "date_of_birth": datetime.date(1991, 1, 1),
        "nationality_code_id": "FR",
        "country_of_residence_id": "FR",
        "identity_path": "PASSPORT",
        "passport_number": "X1234567",
        "passport_country_code_id": "FR",
        "passport_expires_at": _future_date(),
    }
    kwargs.update(overrides)
    save_identity_step(**kwargs)


# ---------------------------------------------------------------------------
# Form: always present (IDV-08), required on the passport path.
# ---------------------------------------------------------------------------


def test_field_is_always_present_on_the_form() -> None:
    form = IdentityStepForm()
    assert "passport_identity_page" in form.fields


def _passport_form_data() -> dict:
    return {
        "given_names": "Sara",
        "family_name": "Kaplan",
        "date_of_birth": "1991-01-01",
        "nationality_code": "FR",
        "country_of_residence": "FR",
        "identity_path": "PASSPORT",
        "passport_number": "X1234567",
        "passport_country_code": "FR",
        "passport_expires_at": _future_date().isoformat(),
    }


def test_form_requires_the_identity_page_on_the_passport_path() -> None:
    form = IdentityStepForm(data=_passport_form_data())
    assert not form.is_valid()
    assert "passport_identity_page" in form.errors


def test_form_accepts_the_passport_path_with_a_page_or_one_on_file() -> None:
    with_file = IdentityStepForm(
        data=_passport_form_data(), files={"passport_identity_page": make_test_photo()}
    )
    assert with_file.is_valid(), with_file.errors
    on_file = IdentityStepForm(data=_passport_form_data(), has_passport_identity_page=True)
    assert on_file.is_valid(), on_file.errors


def test_form_refuses_the_passport_path_for_an_algerian_national() -> None:
    data = {**_passport_form_data(), "nationality_code": "DZ"}
    form = IdentityStepForm(data=data, files={"passport_identity_page": make_test_photo()})
    assert not form.is_valid()
    assert "identity_path" in form.errors


def test_form_rejects_a_forced_upload_on_the_nin_path() -> None:
    """Prompt 5 correction pass §6: a real regression -- an
    `elif path == "NIN" and cleaned.get("passport_identity_page"):` branch
    positioned AFTER the unconditional `if path == "NIN":` branch was
    unreachable dead code, so a tampered POST forcing a file onto the NIN
    path was never actually rejected by the form at all. This proves the
    fix: the form is INVALID and the error is attached to the right field."""
    form = IdentityStepForm(
        data={
            "given_names": "Amine",
            "family_name": "Benali",
            "date_of_birth": "1990-01-01",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN",
            "nin_value": "123456789012345678",
        },
        files={"passport_identity_page": make_test_photo()},
    )
    assert not form.is_valid()
    assert "passport_identity_page" in form.errors


# ---------------------------------------------------------------------------
# Service layer: fail-closed defence in depth.
# ---------------------------------------------------------------------------


def test_service_accepts_the_upload_whatever_the_former_event_switch(
    disabled_event: EventEdition,
) -> None:
    """IDV-08: required for every foreign participant, so the old switch no
    longer refuses it."""
    person = resolve_or_create_participant_for_email("pp-disabled@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=disabled_event)
    _save_passport_identity(draft, passport_identity_page_file=make_test_photo())
    assert (
        Document.objects.filter(
            registration=draft,
            document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
            status=DocumentStatus.ACTIVE,
        ).count()
        == 1
    )


def test_service_never_accepts_the_upload_on_the_nin_path(enabled_event: EventEdition) -> None:
    person = resolve_or_create_participant_for_email("pp-nin@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=enabled_event)
    with pytest.raises(ValidationError):
        save_identity_step(
            registration=draft,
            given_names="Amine",
            family_name="Benali",
            date_of_birth=datetime.date(1990, 1, 1),
            nationality_code_id="DZ",
            country_of_residence_id="DZ",
            identity_path="NIN",
            nin_value="123456789012345678",
            passport_identity_page_file=make_test_photo(),
        )


def test_service_saves_the_upload_on_the_passport_path_when_enabled(
    enabled_event: EventEdition,
) -> None:
    person = resolve_or_create_participant_for_email("pp-enabled@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=enabled_event)
    _save_passport_identity(draft, passport_identity_page_file=make_test_photo())
    document = Document.objects.get(
        registration=draft, document_type=DocumentType.PASSPORT_IDENTITY_PAGE
    )
    assert document.status == DocumentStatus.ACTIVE


def test_resaving_identity_without_a_new_file_preserves_the_existing_upload(
    enabled_event: EventEdition,
) -> None:
    person = resolve_or_create_participant_for_email("pp-preserve@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=enabled_event)
    _save_passport_identity(draft, passport_identity_page_file=make_test_photo())
    document = Document.objects.get(
        registration=draft, document_type=DocumentType.PASSPORT_IDENTITY_PAGE
    )

    # Resave the identity step (e.g. correcting the passport number) WITHOUT
    # a new file -- the existing accepted upload must remain untouched.
    _save_passport_identity(draft, passport_number="Y7654321")

    document.refresh_from_db()
    assert document.status == DocumentStatus.ACTIVE
    assert (
        Document.objects.filter(
            registration=draft, document_type=DocumentType.PASSPORT_IDENTITY_PAGE
        ).count()
        == 1
    )


def test_a_new_upload_replaces_the_previous_one(enabled_event: EventEdition) -> None:
    person = resolve_or_create_participant_for_email("pp-replace@example.com")
    draft = get_or_create_active_draft(person=person, event_edition=enabled_event)
    _save_passport_identity(draft, passport_identity_page_file=make_test_photo())
    first = Document.objects.get(
        registration=draft, document_type=DocumentType.PASSPORT_IDENTITY_PAGE
    )
    _save_passport_identity(
        draft, passport_identity_page_file=make_test_photo(width=400, height=400)
    )
    first.refresh_from_db()
    assert first.status == DocumentStatus.REPLACED
    active = Document.objects.get(
        registration=draft,
        document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
        status=DocumentStatus.ACTIVE,
    )
    assert active.pk != first.pk


# ---------------------------------------------------------------------------
# HTTP/view level: field absence structurally verified in rendered HTML.
# ---------------------------------------------------------------------------


def _otp_login(client: Client, email: str, django_capture_on_commit_callbacks) -> None:
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )


def _set_active_event(client: Client, event: EventEdition, email: str) -> Registration:
    from apps.accounts import session_expiry

    person = resolve_or_create_participant_for_email(email)
    draft = get_or_create_active_draft(person=person, event_edition=event)
    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.id)
    session[participant_auth.ACTIVE_REGISTRATION_SESSION_KEY] = str(draft.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return draft


@pytest.mark.parametrize("language_code", ["en", "fr", "ar"])
@pytest.mark.parametrize("event_fixture", ["enabled_event", "disabled_event"])
def test_field_is_present_in_rendered_html_for_every_event(
    client: Client, request, event_fixture: str, language_code: str
) -> None:
    event = request.getfixturevalue(event_fixture)
    _set_active_event(client, event, f"pp-html-{event_fixture}-{language_code}@example.com")
    with override_settings(LANGUAGE_CODE=language_code):
        response = client.get(
            reverse("registrations:step-identity"), HTTP_ACCEPT_LANGUAGE=language_code
        )
    content = response.content.decode()
    assert 'name="passport_identity_page"' in content
    assert 'enctype="multipart/form-data"' in content
    assert response["Content-Type"].startswith("text/html")


def test_tampered_post_on_the_nin_path_never_returns_a_500_and_creates_no_document(
    client: Client, enabled_event: EventEdition
) -> None:
    """A real regression test for the dead-branch bug (Prompt 5 correction
    pass §6): forcing a file onto the NIN path, with the policy enabled,
    must return the ordinary form-validation re-render -- never HTTP 500 --
    and must never create a Document or StoredObject."""
    _set_active_event(client, enabled_event, "pp-tamper-nin@example.com")
    response = client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Amine",
            "family_name": "Benali",
            "date_of_birth": "1990-01-01",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN",
            "nin_value": "123456789012345678",
            "passport_identity_page": make_test_photo(),
        },
    )
    assert response.status_code == 200
    assert Document.objects.filter(document_type=DocumentType.PASSPORT_IDENTITY_PAGE).count() == 0


def test_tampered_post_leaves_no_orphaned_storage_bytes(
    client: Client, enabled_event: EventEdition, settings
) -> None:
    from apps.documents.storage import get_private_storage

    get_private_storage()  # ensure the root exists before snapshotting it
    root = settings.PRIVATE_STORAGE_ROOT
    keys_before = {p.name for p in root.iterdir()} if root.exists() else set()

    _set_active_event(client, enabled_event, "pp-tamper-orphan@example.com")
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
            "passport_identity_page": make_test_photo(),
        },
    )
    keys_after = {p.name for p in root.iterdir()} if root.exists() else set()
    assert keys_after == keys_before
