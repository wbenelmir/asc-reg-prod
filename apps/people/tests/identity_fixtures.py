"""Synthetic identity fixtures for tests in any app (owner decision IDV-Q1).

Since IDV-Q1 an approved registration is eligible downstream (passes,
physical badges, entry, offline packages) only with a current, valid,
verified identity. Factories that build an APPROVED registration directly,
without going through submission and identity review, call
`make_verified_identity_case` to give it one. Everything here is invented
test data: no real document number, and no provider was asked.
"""

from __future__ import annotations

import datetime
import uuid

from django.utils import timezone


def _country(code: str):
    from apps.core.models import Country

    country, _created = Country.objects.get_or_create(code=code, defaults={"name": code})
    return country


def make_verified_identity_case(
    registration,
    *,
    source: str = "MANUAL_PASSPORT",
    status: str = "MANUALLY_VERIFIED",
    identifier_status: str = "VERIFIED",
    expires_at: datetime.date | None = None,
    route: str = "PASSPORT",
):
    """An identity case for `registration`, verified by default (synthetic).

    The case gets its own synthetic passport identifier (issuing country FR,
    a number derived from a fresh UUID), one revision with a placeholder
    fingerprint, and the requested status and source. Pass a non-verified
    `status` (with `source=""`) or another `identifier_status` to build the
    negative cases. Idempotent per registration: an existing case is
    returned unchanged.
    """
    from apps.people.models import (
        IdentifierType,
        IdentityRevision,
        IdentityRevisionSource,
        IdentityStatus,
        IdentityVerification,
    )
    from apps.people.services import create_identity_identifier

    existing = IdentityVerification.objects.filter(registration=registration).first()
    if existing is not None:
        return existing
    if registration.person_id is None:
        # An identity belongs to a person: attach a synthetic participant to
        # an unclaimed test registration (without changing its version).
        from apps.people.services import resolve_or_create_participant_for_email
        from apps.registrations.models import Registration

        person = resolve_or_create_participant_for_email(
            f"idv-fixture-{uuid.uuid4().hex[:10]}@example.test"
        )
        Registration.objects.filter(pk=registration.pk).update(person=person)
        registration.person = person
    now = timezone.now()
    country = _country("FR")
    identifier = create_identity_identifier(
        person=registration.person,
        identifier_type=IdentifierType.PASSPORT,
        country_code_id=country.pk,
        raw_value=f"T{uuid.uuid4().hex[:10].upper()}",
        status=identifier_status,
    )
    identifier.expires_at = expires_at or (now.date() + datetime.timedelta(days=900))
    identifier.save(update_fields=["expires_at"])
    verified = status in IdentityStatus.verified_statuses()
    case = IdentityVerification.objects.create(
        registration=registration,
        person=registration.person,
        event_edition_id=registration.event_edition_id,
        organization_id=registration.source_organization_id,
        nationality=country,
        route=route,
        status=status,
        verification_source=source if verified else "",
        submitted_at=now,
        status_changed_at=now,
        decided_at=now if verified else None,
    )
    revision = IdentityRevision.objects.create(
        verification=case,
        number=1,
        source=IdentityRevisionSource.SUBMISSION,
        identifier=identifier,
        fingerprint="0" * 64,
        fingerprint_key_version=1,
    )
    case.current_revision = revision
    case.save(update_fields=["current_revision", "updated_at"])
    return case


def bulk_verified_identity_cases(registrations) -> int:
    """Verified identity cases for many factory registrations at once (the
    performance harness fillers). Synthetic: placeholder blind indexes, which
    no lookup ever matches. Returns the number created."""
    from apps.people.models import (
        IdentifierStatus,
        IdentifierType,
        IdentityIdentifier,
        IdentityRevision,
        IdentityRevisionSource,
        IdentityStatus,
        IdentityVerification,
    )

    registrations = list(registrations)
    if not registrations:
        return 0
    now = timezone.now()
    country = _country("FR")
    identifiers = IdentityIdentifier.objects.bulk_create(
        IdentityIdentifier(
            person_id=registration.person_id,
            identifier_type=IdentifierType.PASSPORT,
            country_code=country,
            value_encrypted=f"B{uuid.uuid4().hex[:10].upper()}",
            search_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            hash_key_version=1,
            masked_value="***BULK",
            expires_at=now.date() + datetime.timedelta(days=900),
            status=IdentifierStatus.VERIFIED,
            verified_at=now,
        )
        for registration in registrations
    )
    cases = IdentityVerification.objects.bulk_create(
        IdentityVerification(
            registration_id=registration.pk,
            person_id=registration.person_id,
            event_edition_id=registration.event_edition_id,
            organization_id=registration.source_organization_id,
            nationality=country,
            route="PASSPORT",
            status=IdentityStatus.MANUALLY_VERIFIED,
            verification_source="MANUAL_PASSPORT",
            submitted_at=now,
            status_changed_at=now,
            decided_at=now,
        )
        for registration in registrations
    )
    revisions = IdentityRevision.objects.bulk_create(
        IdentityRevision(
            verification=case,
            number=1,
            source=IdentityRevisionSource.SUBMISSION,
            identifier=identifier,
            fingerprint="0" * 64,
            fingerprint_key_version=1,
        )
        for case, identifier in zip(cases, identifiers, strict=True)
    )
    for case, revision in zip(cases, revisions, strict=True):
        case.current_revision = revision
    IdentityVerification.objects.bulk_update(cases, ["current_revision"])
    return len(cases)


def assign_approval_prerequisites(registration, actor) -> None:
    """The Participant Role, Badge Type and Access Profile assignments that
    approval requires (synthetic codes, unique per call)."""
    from apps.accreditation.models import AccessProfile, BadgeType, ParticipantRole
    from apps.accreditation.services import assign

    suffix = uuid.uuid4().hex[:6].upper()
    event = registration.event_edition
    role = ParticipantRole.objects.create(event_edition=event, code=f"R{suffix}", name="Role")
    badge = BadgeType.objects.create(event_edition=event, code=f"B{suffix}", name="Badge")
    profile = AccessProfile.objects.create(event_edition=event, code=f"A{suffix}", name="Access")
    assign(kind="PARTICIPANT_ROLE", registration=registration, reference_obj=role, actor=actor)
    assign(kind="BADGE_TYPE", registration=registration, reference_obj=badge, actor=actor)
    assign(kind="ACCESS_PROFILE", registration=registration, reference_obj=profile, actor=actor)
    # Approval also needs the event's attendance days and opening-day capacity
    # (synthetic test values; a test that configures its own keeps them).
    from apps.accreditation.models import AttendancePolicy
    from apps.accreditation.tests.attendance_fixtures import configure_attendance

    if not AttendancePolicy.objects.filter(event_edition=event).exists():
        configure_attendance(event)
