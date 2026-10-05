"""Synthetic builders for the online-entry suite (Phase 3 Prompt 4).

Everything here is synthetic: invented names, `example.test` addresses,
generated identity values, and in-memory signing keys that never touch
disk. Builders go through the real services wherever a service exists
(device registration and enrollment, pass generation and activation,
checkpoint sessions) so the tests exercise the same invariants production
code relies on.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth.models import Group
from django.utils import timezone

from apps.accounts.models import (
    OperationalUser,
    OperationalUserAccountType,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accreditation.models import (
    AccessProfile,
    AccessProfileAssignment,
    AccessRule,
    AssignmentStatus,
    BadgeType,
    BadgeTypeAssignment,
    ParticipantRole,
    ParticipantRoleAssignment,
)
from apps.core.models import Country, Sector
from apps.events.models import EventEdition, Gate, Venue, Zone
from apps.people.models import Person, PersonStatus
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
)

TEST_PASSWORD = "__test_password__"  # noqa: S105 - synthetic test credential

ALL_METHODS = ["QR", "NIN", "PASSPORT", "REFERENCE", "MANUAL"]


def seed_reference_values() -> None:
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Country.objects.get_or_create(code="TN", defaults={"name": "Tunisia"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


def make_event(code: str = "ENTTEST") -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code=code,
        name=f"Entry Test Edition {code}",
        timezone="UTC",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=20),
        status="EVENT_OPERATIONS",
    )


def make_user(
    email: str,
    *,
    group_name: str | None = None,
    event=None,
    organization=None,
    venue=None,
    gate=None,
    account_type: str = OperationalUserAccountType.INTERNAL,
    active_until=None,
) -> OperationalUser:
    user = OperationalUser.objects.create_user(
        email=email,
        password=TEST_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        display_name=email.split("@")[0],
        account_type=account_type,
        active_until=active_until,
    )
    if group_name is not None:
        grant(user, group_name, event=event, organization=organization, venue=venue, gate=gate)
    return user


def grant(user, group_name: str, *, event=None, organization=None, venue=None, gate=None):
    return ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        venue=venue if venue is not None else (gate.venue if gate is not None else None),
        gate=gate,
        granted_by=user,
        active_until=user.active_until,
    )


class VenueLayout:
    """One venue with MAIN (parent), HALL (child of MAIN), and VIP zones,
    and two entry gates A (default MAIN) and B (default VIP)."""

    def __init__(self, event, *, code: str = "V1"):
        self.venue = Venue.objects.create(event_edition=event, code=code, name=f"Venue {code}")
        self.main = Zone.objects.create(venue=self.venue, code="MAIN", name="Main area")
        self.hall = Zone.objects.create(
            venue=self.venue, code="HALL", name="Exhibition hall", parent=self.main
        )
        self.vip = Zone.objects.create(venue=self.venue, code="VIP", name="VIP lounge")
        self.gate_a = Gate.objects.create(
            venue=self.venue, code="GA", name="Gate A", default_zone=self.main
        )
        self.gate_b = Gate.objects.create(
            venue=self.venue, code="GB", name="Gate B", default_zone=self.vip
        )


class AccreditationSetup:
    """Role, Badge Type, and an Access Profile allowed into MAIN (and so,
    through the zone hierarchy, into HALL) -- but not into VIP."""

    def __init__(self, event, layout: VenueLayout, *, suffix: str = ""):
        self.role = ParticipantRole.objects.create(
            event_edition=event, code=f"DELEGATE{suffix}", name="Delegate"
        )
        self.badge_type = BadgeType.objects.create(
            event_edition=event, code=f"STANDARD{suffix}", name="Standard"
        )
        self.access_profile = AccessProfile.objects.create(
            event_edition=event, code=f"GENERAL{suffix}", name="General access"
        )
        self.main_rule = AccessRule.objects.create(
            event_edition=event,
            access_profile=self.access_profile,
            code=f"GENERAL_MAIN{suffix}",
            name="General access to main area",
            zone=layout.main,
        )


def make_person(display_name: str = "Synthetic Participant", nationality: str = "DZ") -> Person:
    return Person.objects.create(
        status=PersonStatus.ACTIVE, display_name=display_name, nationality_id=nationality
    )


def make_registration(*, event, person, public_status=RegistrationPublicStatus.APPROVED):
    registration = Registration.objects.create(
        public_reference=f"ENT-{uuid.uuid4().hex[:8].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        public_status=public_status,
        internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
        submitted_at=timezone.now(),
    )
    if person is not None:
        # Owner decision IDV-Q1: an approved context is eligible only with a
        # verified identity, so every factory registration gets one.
        from apps.people.tests.identity_fixtures import make_verified_identity_case

        make_verified_identity_case(registration)
    return registration


def assign(*, registration, setup: AccreditationSetup, actor):
    now = timezone.now()
    common = {
        "event_edition": registration.event_edition,
        "status": AssignmentStatus.CURRENT,
        "effective_from": now - timedelta(minutes=5),
        "created_by": actor,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=setup.role, **common)
    BadgeTypeAssignment.objects.create(
        registration=registration, badge_type=setup.badge_type, **common
    )
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=setup.access_profile, **common
    )


def publish_key(*, provider, actor):
    from apps.badges.services import promote_verification_key, publish_verification_key

    key = publish_verification_key(
        key_id="v1",
        public_key_pem=provider.public_key_pem("v1").decode("ascii"),
        actor=actor,
    )
    return promote_verification_key(key=key, actor=actor)


def issue_active_pass(*, registration, actor):
    from apps.badges.services import activate_pass, generate_pass, new_operation_id

    outcome = generate_pass(registration=registration, actor=actor, operation_id=new_operation_id())
    credential = outcome.credential
    return activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    ).credential


def token_for(credential) -> str:
    from apps.badges.services import issue_pass_token

    return issue_pass_token(credential)


def enroll_device(*, event, layout: VenueLayout, admin, zones=None, methods=None, gate=None):
    from apps.entry.services.devices import enroll_device_with_code, register_device

    registration = register_device(
        event_edition=event,
        public_name="Gate A tablet 1",
        expires_at=timezone.now() + timedelta(days=5),
        venue=layout.venue,
        gate=gate or layout.gate_a,
        zones=zones or [layout.main, layout.hall],
        verification_methods=methods or ALL_METHODS,
        actor=admin,
    )
    enrollment = enroll_device_with_code(raw_code=registration.activation_code, actor=admin)
    return enrollment.device, enrollment.device_secret


def open_checkpoint(*, device, user, zone, now=None):
    from apps.entry.services.sessions import resolve_checkpoint, start_checkpoint_session

    device_session, operator_session = start_checkpoint_session(
        device=device,
        user=user,
        gate_id=device.scopes.get(is_current=True).gate_id,
        zone_id=zone.pk,
    )
    return resolve_checkpoint(
        device=device,
        device_session_id=device_session.pk,
        operator_session_id=operator_session.pk,
        user=user,
        now=now,
    )


def add_identifier(*, person, identifier_type: str, country: str, value: str, verified=True):
    from apps.people.models import IdentifierStatus
    from apps.people.services import create_identity_identifier

    return create_identity_identifier(
        person=person,
        identifier_type=identifier_type,
        country_code_id=country,
        raw_value=value,
        status=IdentifierStatus.VERIFIED if verified else IdentifierStatus.DECLARED,
    )
