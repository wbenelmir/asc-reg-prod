"""Privacy-safe writes for contact points and official identifiers."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.core.concurrency import (
    ADVISORY_LOCK_CLASS_CONTACT_IDENTITY,
    ADVISORY_LOCK_CLASS_OFFICIAL_IDENTIFIER,
    acquire_advisory_locks,
    lock_key,
)
from apps.core.crypto import compute_blind_indexes_for_active_versions, get_key_provider
from apps.people.models import (
    ContactPoint,
    ContactPointStatus,
    ContactPointType,
    IdentifierStatus,
    IdentityIdentifier,
    ParticipantAccount,
    ParticipantAccountStatus,
    Person,
)


class PhoneValidationError(ValidationError):
    """Raised when a mobile number cannot be parsed/validated for its declared country."""


def normalize_mobile_e164(raw_value: str, country_code: str) -> str:
    """Parse `raw_value` as a phone number in `country_code` and return its E.164 form.

    Uses the `phonenumbers` library (Prompt 4 final closure pass §3) --
    never accepts a number that `phonenumbers` cannot parse or considers
    invalid for the declared country, and never stores anything but the
    normalized E.164 representation.
    """
    import phonenumbers

    try:
        parsed = phonenumbers.parse(raw_value, country_code)
    except phonenumbers.NumberParseException:
        raise PhoneValidationError(
            "Enter a valid mobile number for the selected country."
        ) from None
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneValidationError("Enter a valid mobile number for the selected country.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


#: Separators people type or paste inside a phone number (S-10).
_PHONE_SEPARATORS = str.maketrans("", "", " \u00a0\u2009\u202f.-()/")


def parse_mobile_number(raw_value: str, selected_region: str) -> tuple[str, str]:
    """UX-2 (S-10, D-14): return `(E.164, region)` for a typed or pasted number.

    Arabic-Indic digits read as ASCII and separators are dropped. A value that
    starts with `+` or `00` is parsed on its own, and the region comes from the
    number itself; any other value is parsed as a national number of
    `selected_region` (with or without its trunk prefix). A shared calling code
    (+1, +7, +262, ...) keeps `selected_region` when the number is valid there.
    Only MOBILE and FIXED_LINE_OR_MOBILE numbers are accepted. Format validity
    is never proof of ownership.
    """
    import phonenumbers

    from apps.core.normalization import normalize_digits

    value = normalize_digits(raw_value or "").translate(_PHONE_SEPARATORS)
    if not value or len(value) > 20:
        raise PhoneValidationError("Enter a valid mobile number.")
    international = value.startswith("+") or value.startswith("00")
    if value.startswith("00"):
        value = "+" + value[2:]
    try:
        parsed = phonenumbers.parse(value, None if international else selected_region)
    except phonenumbers.NumberParseException:
        raise PhoneValidationError("Enter a valid mobile number.") from None
    if not phonenumbers.is_valid_number(parsed):
        raise PhoneValidationError("Enter a valid mobile number.")
    number_type = phonenumbers.number_type(parsed)
    if number_type not in (
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    ):
        raise PhoneValidationError("Enter a mobile number.")
    region = selected_region
    if not (selected_region and phonenumbers.is_valid_number_for_region(parsed, selected_region)):
        region = phonenumbers.region_code_for_number(parsed) or ""
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), region


#: Left-to-right isolate and pop directional isolate (Unicode bidi).
LTR_ISOLATE, POP_ISOLATE = "⁦", "⁩"


def calling_code_label(country) -> str:
    """ "Algeria (+213)" for the calling-code selector (S-10).

    The "(+213)" token is wrapped in a left-to-right isolate (UXR-C1,
    UXR-F04), so an Arabic option reads "الجزائر (+213)" and not "(213+)".
    An option cannot hold `<bdi>`, hence the Unicode controls. This is a
    display label only: the submitted and stored value is the country code.
    """
    import phonenumbers

    code = phonenumbers.country_code_for_region(country.pk)
    name = country.localized_name
    return f"{name} {LTR_ISOLATE}(+{code}){POP_ISOLATE}" if code else name


def normalize_contact_value(contact_type: str, raw_value: str) -> str:
    value = raw_value.strip()
    if contact_type == ContactPointType.EMAIL:
        return value.lower()
    return "".join(value.split())


def _mask_contact(contact_type: str, normalized: str) -> str:
    if contact_type == ContactPointType.EMAIL and "@" in normalized:
        local, domain = normalized.split("@", 1)
        return f"{local[:1]}***@{domain}"
    return f"***{normalized[-4:]}"


def _lock_blind_indexes(lock_class: int, hashes: dict[int, str]) -> None:
    acquire_advisory_locks(
        connection,
        [lock_key(lock_class, bytes.fromhex(digest)) for digest in hashes.values()],
    )


@transaction.atomic
def create_contact_point(
    *,
    person: Person,
    contact_type: str,
    raw_value: str,
    is_verified: bool = False,
    login_enabled: bool = False,
    is_primary: bool = False,
    country_code_id: str | None = None,
) -> ContactPoint:
    """Create a contact using encrypted plaintext and rotation-safe lookup data."""
    if login_enabled and (contact_type != ContactPointType.EMAIL or not is_verified):
        raise ValidationError("Only a verified email may be enabled for login.")
    normalized = normalize_contact_value(contact_type, raw_value)
    provider = get_key_provider()
    hashes = compute_blind_indexes_for_active_versions(normalized, provider=provider)
    _lock_blind_indexes(ADVISORY_LOCK_CLASS_CONTACT_IDENTITY, hashes)

    if contact_type == ContactPointType.EMAIL and is_verified and login_enabled:
        conflict = ContactPoint.objects.filter(
            type=ContactPointType.EMAIL,
            status=ContactPointStatus.ACTIVE,
            is_verified=True,
            login_enabled=True,
            value_hash__in=hashes.values(),
        ).exists()
        if conflict:
            raise ValidationError("This verified login contact is already in use.")

    write_version = provider.current_blind_index_key_version()
    return ContactPoint.objects.create(
        person=person,
        type=contact_type,
        value_encrypted=normalized,
        value_hash=hashes[write_version],
        value_hash_key_version=write_version,
        masked_value=_mask_contact(contact_type, normalized),
        country_code_id=country_code_id,
        is_primary=is_primary,
        is_verified=is_verified,
        verified_at=timezone.now() if is_verified else None,
        login_enabled=login_enabled,
    )


def normalize_identifier_value(raw_value: str) -> str:
    return "".join(raw_value.upper().split())


@transaction.atomic
def create_identity_identifier(
    *,
    person: Person,
    identifier_type: str,
    country_code_id: str,
    raw_value: str,
    status: str = IdentifierStatus.DECLARED,
) -> IdentityIdentifier:
    """Create an encrypted identifier with cross-version uniqueness protection."""
    normalized = normalize_identifier_value(raw_value)
    scoped_value = f"{identifier_type}:{country_code_id.upper()}:{normalized}"
    provider = get_key_provider()
    hashes = compute_blind_indexes_for_active_versions(scoped_value, provider=provider)
    _lock_blind_indexes(ADVISORY_LOCK_CLASS_OFFICIAL_IDENTIFIER, hashes)

    if status == IdentifierStatus.VERIFIED:
        conflict = IdentityIdentifier.objects.filter(
            identifier_type=identifier_type,
            country_code_id=country_code_id.upper(),
            status=IdentifierStatus.VERIFIED,
            search_hash__in=hashes.values(),
        ).exists()
        if conflict:
            raise ValidationError("This verified identifier is already in use.")

    write_version = provider.current_blind_index_key_version()
    return IdentityIdentifier.objects.create(
        person=person,
        identifier_type=identifier_type,
        country_code_id=country_code_id.upper(),
        value_encrypted=normalized,
        search_hash=hashes[write_version],
        hash_key_version=write_version,
        masked_value=f"***{normalized[-4:]}",
        status=status,
        verified_at=timezone.now() if status == IdentifierStatus.VERIFIED else None,
    )


@transaction.atomic
def resolve_or_create_participant_for_email(
    raw_email: str, *, default_language: str = "en"
) -> Person:
    """Resolve the Person for a verified-OTP email, creating one for a first-time participant.

    Called only after `apps.accounts.otp.consume_challenge()` returns
    `CONSUMED` (AF-AUTH-01) -- never before an OTP has actually been
    verified. V1 keeps at most one active `ParticipantAccount` per Person;
    an existing verified login-enabled email always resolves to that same
    Person, never a new one.
    """
    normalized = normalize_contact_value(ContactPointType.EMAIL, raw_email)
    # Inlined rather than imported from apps.people.selectors, which itself
    # imports from this module -- avoids a circular import.
    candidate_hashes = compute_blind_indexes_for_active_versions(normalized).values()
    existing_contact = (
        ContactPoint.objects.filter(
            type=ContactPointType.EMAIL,
            value_hash__in=candidate_hashes,
            status=ContactPointStatus.ACTIVE,
            is_verified=True,
            login_enabled=True,
        )
        .select_related("person")
        .first()
    )
    if existing_contact is not None:
        person = existing_contact.person
        ParticipantAccount.objects.get_or_create(
            person=person,
            defaults={
                "primary_login_contact": existing_contact,
                "email_verified_at": timezone.now(),
                "status": ParticipantAccountStatus.ACTIVE,
            },
        )
        return person

    person = Person.objects.create(
        display_name=normalized.split("@", 1)[0], preferred_language=default_language
    )
    contact = create_contact_point(
        person=person,
        contact_type=ContactPointType.EMAIL,
        raw_value=normalized,
        is_verified=True,
        login_enabled=True,
        is_primary=True,
    )
    ParticipantAccount.objects.create(
        person=person,
        primary_login_contact=contact,
        email_verified_at=timezone.now(),
        status=ParticipantAccountStatus.ACTIVE,
    )
    return person


def example_mobile_national(region_code: str) -> str:
    """A national-format example mobile number for `region_code`, from the
    `phonenumbers` metadata; "" when the region has no mobile example.

    Used only for a form placeholder, so the value is metadata, never a real
    person's number. It is not validated or stored.
    """
    import phonenumbers

    example = phonenumbers.example_number_for_type(region_code, phonenumbers.PhoneNumberType.MOBILE)
    if example is None:
        return ""
    return phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.NATIONAL)
