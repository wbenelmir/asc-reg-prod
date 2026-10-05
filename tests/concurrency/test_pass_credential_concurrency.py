"""Real multi-connection PostgreSQL concurrency for Digital Entry Pass credentials.

Phase 3 Prompt 2. Two real threads, each on its OWN PostgreSQL connection,
released together from a `threading.Barrier` so they genuinely overlap --
never a mock, never a sequential call. Matches the established pattern in
`tests/concurrency/test_registration_submission_idempotency.py`.

Three guarantees are proven here, each of which a single-threaded test
cannot establish:

1. two simultaneous generations produce exactly one credential;
2. two simultaneous replacements produce exactly one replacement, and the
   credential version sequence advances by exactly one;
3. a retried command carrying the same `operation_id` never mints a second
   credential even when both requests are genuinely in flight together.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accreditation.models import (
    AccessProfile,
    AccessProfileAssignment,
    AssignmentStatus,
    BadgeType,
    BadgeTypeAssignment,
    ParticipantRole,
    ParticipantRoleAssignment,
)
from apps.badges.models import (
    NON_TERMINAL_STATUSES,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PassCredentialSeries,
    PassLifecycleOperation,
    PassReasonCode,
)
from apps.badges.services import (
    OperationConflictError,
    PassConcurrencyError,
    PassServiceError,
    PassStateError,
    activate_pass,
    generate_pass,
    new_operation_id,
    promote_verification_key,
    publish_verification_key,
    replace_pass,
    revoke_pass,
    suspend_pass,
)
from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    set_signing_key_provider_for_testing,
)
from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.people.models import Person, PersonStatus
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


@pytest.fixture
def scenario():
    """Build one eligible registration with an ACTIVE verification key."""
    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"CONC{uuid.uuid4().hex[:6].upper()}",
        name="Concurrency Edition",
        timezone="UTC",
        starts_at=now,
        ends_at=now + _days(30),
        status="EVENT_OPERATIONS",
    )
    organization = Organization.objects.create(
        official_name=f"Conc Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"conc org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    actor = OperationalUser.objects.create_user(
        email=f"conc.{uuid.uuid4().hex[:8]}@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=actor,
        group=Group.objects.get(name="Pass Administrators"),
        event_edition=event,
        organization=organization,
        granted_by=actor,
    )
    custodian = OperationalUser.objects.create_user(
        email=f"cust.{uuid.uuid4().hex[:8]}@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )

    person = Person.objects.create(status=PersonStatus.ACTIVE)
    registration = Registration.objects.create(
        public_reference=f"CONC-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=RegistrationPublicStatus.APPROVED,
        internal_status=RegistrationInternalStatus.QUALIFICATION_COMPLETE,
        submitted_at=now,
    )
    # Owner decision IDV-Q1: an approved context needs a verified identity.
    from apps.people.tests.identity_fixtures import make_verified_identity_case

    make_verified_identity_case(registration)

    role = ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")
    badge_type = BadgeType.objects.create(event_edition=event, code="STANDARD", name="Standard")
    access_profile = AccessProfile.objects.create(
        event_edition=event, code="EXHIBITOR", name="Exhibitor"
    )
    common = {
        "event_edition": event,
        "organization": organization,
        "status": AssignmentStatus.CURRENT,
        "effective_from": now,
        "created_by": actor,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    BadgeTypeAssignment.objects.create(registration=registration, badge_type=badge_type, **common)
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )

    key = publish_verification_key(
        key_id="v1",
        public_key_pem=provider.public_key_pem("v1").decode("ascii"),
        actor=custodian,
    )
    promote_verification_key(key=key, actor=custodian)

    yield {"registration": registration, "actor": actor, "event": event}

    set_signing_key_provider_for_testing(None)


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


def test_two_simultaneous_generations_produce_exactly_one_credential(scenario):
    registration = scenario["registration"]
    actor = scenario["actor"]

    def _generate():
        return generate_pass(
            registration=registration, actor=actor, operation_id=new_operation_id()
        )

    results, errors = _run_callables_in_threads([_generate, _generate])

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one generation must win, got {results!r}"
    assert len(errors) == 1, f"exactly one worker must be refused, got {errors!r}"
    # A domain refusal, never a raw database error leaking through.
    assert isinstance(errors[0], (PassStateError, PassServiceError))
    assert not isinstance(errors[0], IntegrityError)

    assert DigitalEntryPass.objects.filter(registration=registration).count() == 1
    assert PassCredentialSeries.objects.filter(registration=registration).count() == 1
    series = PassCredentialSeries.objects.get(registration=registration)
    assert series.next_credential_version == 2


def test_the_same_operation_id_in_flight_twice_resolves_as_original_plus_replay(scenario):
    """Both workers must succeed: one original, one equivalent replay.

    Before the transaction-scoped idempotency lock, both requests missed the
    existence check and then raced to insert, so one surfaced a raw
    `IntegrityError`. Asserting only "one credential exists" would have passed
    even then -- which is why both outcomes are captured and asserted here.
    """
    registration = scenario["registration"]
    actor = scenario["actor"]
    operation_id = new_operation_id()

    def _generate():
        return generate_pass(registration=registration, actor=actor, operation_id=operation_id)

    results, errors = _run_callables_in_threads([_generate, _generate])

    assert errors == [], f"no worker may raise: {errors!r}"
    assert all(result is not None for result in results)

    replayed = sorted(result.replayed for result in results)
    assert replayed == [False, True], (
        f"exactly one original and one replay expected, got {replayed!r}"
    )
    assert results[0].credential.pk == results[1].credential.pk
    assert DigitalEntryPass.objects.filter(registration=registration).count() == 1

    series = PassCredentialSeries.objects.get(registration=registration)
    assert series.next_credential_version == 2, "the version sequence advances exactly once"


def test_a_conflicting_operation_id_reuse_fails_safely(scenario):
    """A reused identifier for a DIFFERENT command must never silently replay."""
    registration = scenario["registration"]
    actor = scenario["actor"]

    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()

    shared = new_operation_id()
    suspend_pass(
        credential=credential,
        actor=actor,
        operation_id=shared,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    credential.refresh_from_db()

    with pytest.raises(OperationConflictError):
        revoke_pass(
            credential=credential,
            actor=actor,
            operation_id=shared,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )

    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED


def test_two_simultaneous_replacements_produce_exactly_one_replacement(scenario):
    registration = scenario["registration"]
    actor = scenario["actor"]

    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    expected_jti = credential.jti
    expected_version = credential.version

    def _replace():
        return replace_pass(
            credential=credential,
            actor=actor,
            operation_id=new_operation_id(),
            expected_jti=expected_jti,
            expected_lock_version=expected_version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )

    results, errors = _run_callables_in_threads([_replace, _replace])

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1, f"exactly one replacement must win, got {results!r}"
    assert len(errors) == 1, f"exactly one worker must be refused, got {errors!r}"
    assert isinstance(errors[0], PassConcurrencyError)
    assert not isinstance(errors[0], IntegrityError)

    credentials = DigitalEntryPass.objects.filter(registration=registration)
    assert credentials.count() == 2
    assert credentials.filter(status=DigitalEntryPassStatus.REPLACED).count() == 1
    assert credentials.filter(status__in=NON_TERMINAL_STATUSES).count() == 1

    series = PassCredentialSeries.objects.get(registration=registration)
    assert series.next_credential_version == 3, "the version sequence advances exactly once"


def test_the_one_current_credential_invariant_holds_under_concurrency(scenario):
    """Whatever interleaving occurs, one registration keeps one live credential."""
    registration = scenario["registration"]
    actor = scenario["actor"]

    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential

    def _replace():
        return replace_pass(
            credential=credential,
            actor=actor,
            operation_id=new_operation_id(),
            expected_jti=credential.jti,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.ADMINISTRATIVE_ERROR,
        )

    def _revoke():
        from apps.badges.services import revoke_pass

        return revoke_pass(
            credential=credential,
            actor=actor,
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
        )

    _results, _errors = _run_callables_in_threads([_replace, _revoke])

    live = DigitalEntryPass.objects.filter(
        registration=registration, status__in=NON_TERMINAL_STATUSES
    )
    assert live.count() <= 1, "the one-current-credential invariant must never break"


def test_an_identical_concurrent_reasoned_command_resolves_cleanly(scenario):
    """A fully identical command -- reason text included -- still replays.

    Binding `reason_text` into the fingerprint must not break the ordinary
    retry path: two genuinely identical in-flight suspends resolve as one
    original plus one replay, with no worker exception.
    """
    registration = scenario["registration"]
    actor = scenario["actor"]

    credential = generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()

    operation_id = new_operation_id()
    expected_version = credential.version

    def _suspend():
        return suspend_pass(
            credential=credential,
            actor=actor,
            operation_id=operation_id,
            expected_lock_version=expected_version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
            reason_text="Reported lost at gate 3",
        )

    results, errors = _run_callables_in_threads([_suspend, _suspend])

    assert errors == [], f"no worker may raise: {errors!r}"
    assert all(result is not None for result in results)
    replayed = sorted(result.replayed for result in results)
    assert replayed == [False, True], f"expected one original and one replay, got {replayed!r}"

    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED
    assert credential.status_reason_text == "Reported lost at gate 3"
    assert PassLifecycleOperation.objects.filter(operation_id=operation_id).count() == 1, (
        "exactly one lifecycle row for one operation identifier"
    )
