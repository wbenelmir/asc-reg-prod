"""Person/Account/Contact/Identity invariant tests (Schema §4, §16.1)."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.core.crypto import compute_blind_index
from apps.core.models import Country
from apps.people.models import (
    ContactPoint,
    ContactPointStatus,
    ContactPointType,
    IdentifierStatus,
    IdentifierType,
    IdentityIdentifier,
    ParticipantAccount,
    Person,
    PersonName,
    PersonNameSource,
    PersonNameType,
)
from apps.people.selectors import contact_points_for_value, identifiers_for_value
from apps.people.services import create_contact_point, create_identity_identifier

pytestmark = pytest.mark.django_db


@pytest.fixture
def algeria() -> Country:
    return Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})[0]


def _make_person(**kwargs) -> Person:
    return Person.objects.create(display_name="Test Participant", **kwargs)


def _make_contact_point(person: Person, email: str, **kwargs) -> ContactPoint:
    normalized = email.strip().lower()
    return ContactPoint.objects.create(
        person=person,
        type=ContactPointType.EMAIL,
        value_encrypted=normalized,
        value_hash=compute_blind_index(normalized, version=1),
        value_hash_key_version=1,
        masked_value=f"{normalized[0]}***@{normalized.split('@')[1]}",
        **kwargs,
    )


def test_person_cannot_self_merge() -> None:
    person = _make_person()
    person.merged_into_id = person.id
    with pytest.raises(IntegrityError), transaction.atomic():
        person.save(update_fields=["merged_into"])


def test_participant_account_is_at_most_one_per_person() -> None:
    person = _make_person()
    ParticipantAccount.objects.create(person=person)
    with pytest.raises(IntegrityError), transaction.atomic():
        ParticipantAccount.objects.create(person=person)


def test_disabling_account_does_not_allow_a_second_row_for_the_same_person() -> None:
    person = _make_person()
    account = ParticipantAccount.objects.create(person=person)
    account.status = "DISABLED"
    account.save(update_fields=["status"])
    with pytest.raises(IntegrityError), transaction.atomic():
        ParticipantAccount.objects.create(person=person)


def test_one_person_may_have_multiple_verified_login_enabled_emails() -> None:
    person = _make_person()
    first = _make_contact_point(
        person, "first@example.com", is_verified=True, login_enabled=True, is_primary=True
    )
    second = _make_contact_point(person, "second@example.com", is_verified=True, login_enabled=True)
    assert first.pk != second.pk
    assert (
        ContactPoint.objects.filter(person=person, login_enabled=True, is_verified=True).count()
        == 2
    )


def test_cross_account_normalized_email_uniqueness_for_active_login_enabled() -> None:
    person_a = _make_person()
    person_b = _make_person()
    _make_contact_point(person_a, "shared@example.com", is_verified=True, login_enabled=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_contact_point(person_b, "shared@example.com", is_verified=True, login_enabled=True)


def test_unverified_duplicate_email_across_accounts_is_allowed() -> None:
    person_a = _make_person()
    person_b = _make_person()
    _make_contact_point(person_a, "unverified@example.com", is_verified=False, login_enabled=False)
    # Does not raise: the partial unique index only applies to
    # active+verified+login-enabled rows.
    _make_contact_point(person_b, "unverified@example.com", is_verified=False, login_enabled=False)


def test_replaced_contact_point_frees_the_email_for_reuse() -> None:
    person_a = _make_person()
    person_b = _make_person()
    original = _make_contact_point(
        person_a, "handoff@example.com", is_verified=True, login_enabled=True
    )
    original.status = ContactPointStatus.REPLACED
    original.save(update_fields=["status"])
    # No longer ACTIVE, so it no longer occupies the partial-unique slot.
    _make_contact_point(person_b, "handoff@example.com", is_verified=True, login_enabled=True)


def _make_identifier(person: Person, country: Country, value: str, **kwargs) -> IdentityIdentifier:
    normalized = value.strip()
    return IdentityIdentifier.objects.create(
        person=person,
        identifier_type=IdentifierType.NIN,
        country_code=country,
        value_encrypted=normalized,
        search_hash=compute_blind_index(f"NIN:{country.code}:{normalized}", version=1),
        hash_key_version=1,
        masked_value="***" + normalized[-4:],
        **kwargs,
    )


def test_verified_active_identifier_is_unique_across_persons(algeria: Country) -> None:
    person_a = _make_person()
    person_b = _make_person()
    _make_identifier(person_a, algeria, "123456789012345", status=IdentifierStatus.VERIFIED)
    with pytest.raises(IntegrityError), transaction.atomic():
        _make_identifier(person_b, algeria, "123456789012345", status=IdentifierStatus.VERIFIED)


def test_declared_duplicate_identifiers_do_not_trigger_merge_or_conflict(algeria: Country) -> None:
    person_a = _make_person()
    person_b = _make_person()
    _make_identifier(person_a, algeria, "999999999999999", status=IdentifierStatus.DECLARED)
    # No IntegrityError: the partial unique index only applies to VERIFIED rows.
    _make_identifier(person_b, algeria, "999999999999999", status=IdentifierStatus.DECLARED)
    person_a.refresh_from_db()
    person_b.refresh_from_db()
    assert person_a.status != "MERGED"
    assert person_b.status != "MERGED"


def test_replaced_passport_coexists_with_current_passport(algeria: Country) -> None:
    person = _make_person()
    old = IdentityIdentifier.objects.create(
        person=person,
        identifier_type=IdentifierType.PASSPORT,
        country_code=algeria,
        value_encrypted="OLD123456",
        search_hash=compute_blind_index("PASSPORT:DZ:OLD123456", version=1),
        hash_key_version=1,
        masked_value="***3456",
        status=IdentifierStatus.REPLACED,
    )
    new = IdentityIdentifier.objects.create(
        person=person,
        identifier_type=IdentifierType.PASSPORT,
        country_code=algeria,
        value_encrypted="NEW654321",
        search_hash=compute_blind_index("PASSPORT:DZ:NEW654321", version=1),
        hash_key_version=1,
        masked_value="***4321",
        status=IdentifierStatus.VERIFIED,
    )
    assert IdentityIdentifier.objects.filter(person=person).count() == 2
    assert old.status == IdentifierStatus.REPLACED
    assert new.status == IdentifierStatus.VERIFIED


def test_one_current_legal_name_per_person_and_script() -> None:
    person = _make_person()
    PersonName.objects.create(
        person=person,
        name_type=PersonNameType.LEGAL,
        script_code="Latn",
        given_names="Amine",
        family_name="Benali",
        full_name="Amine Benali",
        is_current=True,
        source=PersonNameSource.APPLICANT,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PersonName.objects.create(
            person=person,
            name_type=PersonNameType.LEGAL,
            script_code="Latn",
            given_names="Amin",
            family_name="Ben Ali",
            full_name="Amin Ben Ali",
            is_current=True,
            source=PersonNameSource.OPERATOR_CORRECTION,
        )


def test_former_name_does_not_conflict_with_current_legal_name() -> None:
    person = _make_person()
    PersonName.objects.create(
        person=person,
        name_type=PersonNameType.LEGAL,
        script_code="Latn",
        given_names="Amine",
        family_name="Benali",
        full_name="Amine Benali",
        is_current=True,
        source=PersonNameSource.APPLICANT,
    )
    # A FORMER name with the same script never collides with the LEGAL
    # partial unique index, which is scoped to name_type=LEGAL.
    PersonName.objects.create(
        person=person,
        name_type=PersonNameType.FORMER,
        script_code="Latn",
        given_names="Amine",
        family_name="Old",
        full_name="Amine Old",
        is_current=False,
        source=PersonNameSource.APPLICANT,
    )


def test_encrypted_contact_value_round_trips_and_is_not_stored_in_plaintext(
    algeria: Country,
) -> None:
    person = _make_person()
    contact = _make_contact_point(person, "roundtrip@example.com")
    reloaded = ContactPoint.objects.get(pk=contact.pk)
    assert reloaded.value_encrypted == "roundtrip@example.com"

    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT value_encrypted FROM people_contact_point WHERE id = %s", [str(contact.pk)]
        )
        (raw_column_value,) = cursor.fetchone()
    assert "roundtrip@example.com" not in raw_column_value
    assert raw_column_value.startswith("v1:")


def test_masked_value_repr_never_leaks_plaintext(algeria: Country) -> None:
    person = _make_person()
    contact = _make_contact_point(person, "leaktest@example.com")
    assert "leaktest@example.com" not in str(contact)
    assert "leaktest@example.com" not in repr(contact)

    identifier = _make_identifier(person, algeria, "555555555555555")
    assert "555555555555555" not in str(identifier)
    assert "555555555555555" not in repr(identifier)


def test_contact_service_normalizes_encrypts_and_supports_safe_lookup() -> None:
    person = _make_person()
    contact = create_contact_point(
        person=person,
        contact_type=ContactPointType.EMAIL,
        raw_value="  Person@Example.COM ",
        is_verified=True,
        login_enabled=True,
    )
    contact.refresh_from_db()
    assert contact.value_encrypted == "person@example.com"
    assert contact.masked_value == "p***@example.com"
    assert list(contact_points_for_value(contact_type="EMAIL", raw_value="PERSON@example.com")) == [
        contact
    ]


def test_identifier_service_encrypts_and_supports_safe_lookup(algeria: Country) -> None:
    person = _make_person()
    identifier = create_identity_identifier(
        person=person,
        identifier_type=IdentifierType.NIN,
        country_code_id=algeria.pk,
        raw_value="123 456 789",
        status=IdentifierStatus.DECLARED,
    )
    identifier.refresh_from_db()
    assert identifier.value_encrypted == "123456789"
    assert list(
        identifiers_for_value(
            identifier_type=IdentifierType.NIN,
            country_code_id=algeria.pk,
            raw_value="123 456 789",
        )
    ) == [identifier]
