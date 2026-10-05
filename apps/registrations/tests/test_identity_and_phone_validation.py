"""Identity and phone validation/reuse tests (Prompt 4 final closure pass §2/§3)."""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.people.models import (
    ContactPointStatus,
    ContactPointType,
    IdentifierStatus,
    IdentifierType,
    IdentityVerificationAttempt,
)
from apps.people.services import resolve_or_create_participant_for_email
from apps.registrations.services import (
    get_or_create_active_draft,
    save_contact_step,
    save_identity_step,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _seed_reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    return EventEdition.objects.create(
        code="IDVALID",
        name="Identity Validation Test",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


@pytest.fixture
def draft(event: EventEdition):
    person = resolve_or_create_participant_for_email("id-validation@example.com")
    return get_or_create_active_draft(person=person, event_edition=event)


VALID_NIN = "123456789012345678"


def _refetch_profile(draft):
    from apps.registrations.models import RegistrationProfile

    return RegistrationProfile.objects.get(registration=draft)


def _save_identity(draft, **overrides):
    defaults = dict(
        registration=draft,
        given_names="Amine",
        family_name="Benali",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value=VALID_NIN,
    )
    defaults.update(overrides)
    return save_identity_step(**defaults)


# ---------------------------------------------------------------------------
# §2: NIN format, nationality gating, date validation
# ---------------------------------------------------------------------------


def test_nin_must_be_exactly_18_ascii_digits(draft) -> None:
    with pytest.raises(ValidationError):
        _save_identity(draft, nin_value="1234567890123456")  # 16 digits


def test_nin_rejects_non_digit_characters_of_correct_length(draft) -> None:
    with pytest.raises(ValidationError):
        _save_identity(draft, nin_value="12345678901234567A")  # 18 chars, one non-digit


def test_nin_path_requires_algerian_nationality(draft) -> None:
    with pytest.raises(ValidationError):
        _save_identity(draft, nationality_code_id="FR", country_of_residence_id="FR")


def test_date_of_birth_must_be_in_the_past(draft) -> None:
    future = timezone.now().date() + datetime.timedelta(days=1)
    with pytest.raises(ValidationError):
        _save_identity(draft, date_of_birth=future)


def test_passport_expiry_must_be_in_the_future(draft) -> None:
    past = timezone.now().date() - datetime.timedelta(days=1)
    with pytest.raises(ValidationError):
        _save_identity(
            draft,
            nationality_code_id="FR",
            country_of_residence_id="FR",
            identity_path="PASSPORT",
            nin_value="",
            passport_number="X1234567",
            passport_country_code_id="FR",
            passport_expires_at=past,
        )


# ---------------------------------------------------------------------------
# §2: reuse-if-unchanged, never a duplicate row
# ---------------------------------------------------------------------------


def test_resaving_an_unchanged_nin_reuses_the_same_row(draft) -> None:
    # IDV-2 (A13-14): the draft step only declares the NIN; verification runs
    # after the final submission, so the identifier stays DECLARED here.
    _save_identity(draft)
    person = draft.person
    first_identifier = person.identity_identifiers.get(identifier_type=IdentifierType.NIN)
    assert first_identifier.status == IdentifierStatus.DECLARED

    _save_identity(draft)  # identical resubmission
    identifiers = person.identity_identifiers.filter(identifier_type=IdentifierType.NIN)
    assert identifiers.count() == 1
    assert identifiers.get().pk == first_identifier.pk


def test_changing_the_nin_creates_a_new_row_not_an_overwrite(draft) -> None:
    _save_identity(draft, nin_value=VALID_NIN)
    _save_identity(draft, nin_value="222222222222222224")
    person = draft.person
    assert person.identity_identifiers.filter(identifier_type=IdentifierType.NIN).count() == 2


def test_resaving_an_unchanged_passport_reuses_the_same_row(draft) -> None:
    future = timezone.now().date() + datetime.timedelta(days=365)
    kwargs = dict(
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        nin_value="",
        passport_number="X1234567",
        passport_country_code_id="FR",
        passport_expires_at=future,
    )
    _save_identity(draft, **kwargs)
    person = draft.person
    first = person.identity_identifiers.get(identifier_type=IdentifierType.PASSPORT)
    _save_identity(draft, **kwargs)
    identifiers = person.identity_identifiers.filter(identifier_type=IdentifierType.PASSPORT)
    assert identifiers.count() == 1
    assert identifiers.get().pk == first.pk


# ---------------------------------------------------------------------------
# IDV-2 (A13-14): the draft identity step never calls the identity provider.
# The former draft-step provider-failure and metadata tests are superseded:
# the worker's own tests cover failures and retained data
# (apps/people/tests/test_idv_worker.py).
# ---------------------------------------------------------------------------


class _ExplodingProvider:
    """Test-only provider that fails the test if it is ever called."""

    PROVIDER_CODE = "EXPLODING_TEST_DOUBLE"
    IS_OFFICIAL = False

    def configuration_problems(self):
        return []

    def lookup(self, nin):  # pragma: no cover - must never run
        raise AssertionError("The draft identity step called the identity provider.")


@override_settings(NIN_PROVIDER_BACKEND=f"{__name__}._ExplodingProvider")
def test_the_identity_step_never_calls_the_identity_provider(draft) -> None:
    _save_identity(draft)
    _save_identity(draft, nin_value="222222222222222224")
    identifier = draft.person.identity_identifiers.exclude(status=IdentifierStatus.REPLACED).first()
    assert identifier.status == IdentifierStatus.DECLARED
    assert not IdentityVerificationAttempt.objects.filter(registration=draft).exists()
    draft.refresh_from_db()
    assert draft.internal_status == "PENDING_ASSIGNMENT"


def test_algerian_nationals_cannot_use_the_passport_path(draft) -> None:
    """A13-01 (SCOPE-01): no self-service passport route for Algerian nationals."""
    with pytest.raises(ValidationError):
        _save_identity(
            draft,
            identity_path="PASSPORT",
            nin_value="",
            passport_number="X1234567",
            passport_country_code_id="FR",
            passport_expires_at=timezone.now().date() + datetime.timedelta(days=365),
        )


def test_identifier_never_stores_the_clear_nin_value(draft) -> None:
    _save_identity(draft)
    identifier = draft.person.identity_identifiers.get(identifier_type=IdentifierType.NIN)
    assert VALID_NIN not in identifier.masked_value
    # value_encrypted decrypts to the raw value at the Python attribute level
    # (by field-type design) -- but nothing outside this one encrypted column
    # (masked_value, search_hash, any snapshot/log) may ever contain it.
    assert identifier.masked_value.startswith("***")


# ---------------------------------------------------------------------------
# §3: phone validation with `phonenumbers`
# ---------------------------------------------------------------------------


def test_algerian_mobile_number_is_normalized_to_e164(draft) -> None:
    _save_identity(draft)
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    profile = _refetch_profile(draft)
    contact = profile.declared_mobile_contact
    assert contact.value_encrypted == "+213551234567"


def test_international_mobile_number_is_normalized_to_e164(draft) -> None:
    _save_identity(
        draft,
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        nin_value="",
        passport_number="X1234567",
        passport_country_code_id="FR",
        passport_expires_at=timezone.now().date() + datetime.timedelta(days=365),
    )
    save_contact_step(
        registration=draft, mobile_number="06 12 34 56 78", mobile_country_code_id="FR"
    )
    profile = _refetch_profile(draft)
    contact = profile.declared_mobile_contact
    assert contact.value_encrypted == "+33612345678"


def test_malformed_mobile_number_is_rejected(draft) -> None:
    from apps.people.services import PhoneValidationError

    with pytest.raises(PhoneValidationError):
        save_contact_step(
            registration=draft, mobile_number="not-a-number", mobile_country_code_id="DZ"
        )


def test_retry_with_a_corrected_number_succeeds_after_a_malformed_attempt(draft) -> None:
    from apps.people.services import PhoneValidationError

    _save_identity(draft)
    with pytest.raises(PhoneValidationError):
        save_contact_step(registration=draft, mobile_number="123", mobile_country_code_id="DZ")
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    profile = _refetch_profile(draft)
    assert profile.declared_mobile_contact is not None


def test_unverified_self_declared_mobile_is_never_assigned_to_verified_mobile_contact(
    draft,
) -> None:
    _save_identity(draft)
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    profile = _refetch_profile(draft)
    assert profile.verified_mobile_contact_id is None
    assert profile.declared_mobile_contact_id is not None
    assert profile.declared_mobile_contact.is_verified is False


def test_resaving_the_same_mobile_number_reuses_the_existing_contact_point(draft) -> None:
    _save_identity(draft)
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    first_contact_id = _refetch_profile(draft).declared_mobile_contact_id
    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    assert _refetch_profile(draft).declared_mobile_contact_id == first_contact_id
    assert (
        draft.person.contact_points.filter(type=ContactPointType.MOBILE)
        .exclude(status=ContactPointStatus.REPLACED)
        .count()
        == 1
    )
