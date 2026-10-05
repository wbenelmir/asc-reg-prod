"""Rotation-safe exact-match selectors for protected identity values."""

from apps.core.crypto import compute_blind_index, compute_blind_indexes_for_active_versions
from apps.people.models import (
    ContactPoint,
    ContactPointStatus,
    IdentifierStatus,
    IdentityIdentifier,
)
from apps.people.services import normalize_contact_value, normalize_identifier_value


def contact_points_for_value(*, contact_type: str, raw_value: str):
    normalized = normalize_contact_value(contact_type, raw_value)
    hashes = compute_blind_indexes_for_active_versions(normalized)
    return ContactPoint.objects.filter(type=contact_type, value_hash__in=hashes.values())


def identifiers_for_value(*, identifier_type: str, country_code_id: str, raw_value: str):
    normalized = normalize_identifier_value(raw_value)
    scoped = f"{identifier_type}:{country_code_id.upper()}:{normalized}"
    hashes = compute_blind_indexes_for_active_versions(scoped)
    return IdentityIdentifier.objects.filter(
        identifier_type=identifier_type,
        country_code_id=country_code_id.upper(),
        search_hash__in=hashes.values(),
    )


def unchanged_contact_point_for_person(
    person, *, contact_type: str, raw_value: str
) -> ContactPoint | None:
    """Return this person's own most recent, non-replaced contact of `contact_type` IFF
    its value is identical to `raw_value` -- otherwise `None` (Prompt 4 final closure
    pass §2/§3: re-saving an unchanged value must reuse the existing row, never create
    another one)."""
    normalized = normalize_contact_value(contact_type, raw_value)
    existing = (
        person.contact_points.filter(type=contact_type)
        .exclude(status=ContactPointStatus.REPLACED)
        .order_by("-created_at")
        .first()
    )
    if existing is None:
        return None
    candidate_hash = compute_blind_index(normalized, version=existing.value_hash_key_version)
    return existing if candidate_hash == existing.value_hash else None


def unchanged_identifier_for_person(
    person, *, identifier_type: str, country_code_id: str, raw_value: str
) -> IdentityIdentifier | None:
    """Return this person's own most recent, non-replaced identifier of `identifier_type`
    (scoped to `country_code_id`) IFF its value is identical to `raw_value` -- otherwise
    `None` (Prompt 4 final closure pass §2)."""
    normalized = normalize_identifier_value(raw_value)
    scoped = f"{identifier_type}:{country_code_id.upper()}:{normalized}"
    existing = (
        person.identity_identifiers.filter(
            identifier_type=identifier_type, country_code_id=country_code_id.upper()
        )
        .exclude(status=IdentifierStatus.REPLACED)
        .order_by("-created_at")
        .first()
    )
    if existing is None:
        return None
    candidate_hash = compute_blind_index(scoped, version=existing.hash_key_version)
    return existing if candidate_hash == existing.search_hash else None
