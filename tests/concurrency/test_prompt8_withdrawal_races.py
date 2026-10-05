"""Phase 3 Prompt 8 (P8-02, P8-06): withdrawal and cancellation race against
physical-badge issuance/replacement and pass activation/resumption.

Real threads, each on its OWN PostgreSQL connection (the established pattern
in `test_pass_credential_concurrency.py`). Two kinds of proof per command:

* **Withdrawal wins.** One connection withdraws (or cancels) and keeps its
  transaction -- and so the Registration row lock -- open for a moment; the
  command on the other connection must BLOCK on that lock and then refuse,
  because it re-checks the context under the lock. Before the correction the
  command did not lock the Registration at all and succeeded immediately.
* **Genuine overlap.** Both start together behind a barrier. Whichever wins,
  the outcome is serializable -- if the command succeeded, it did so before
  the withdrawal stamped the context -- and no deadlock or raw database
  error ever surfaces.

All data is synthetic.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import DatabaseError, connection, transaction
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
    BadgeIssuance,
    BadgeIssuanceReasonCode,
    BadgeIssuanceStatus,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PrintBatchStatus,
)
from apps.badges.services import (
    PassNotEligibleError,
    RegistrationNotEligibleError,
    activate_pass,
    change_batch_status,
    create_print_batch,
    create_stock_location,
    generate_pass,
    issue_badge,
    new_operation_id,
    promote_verification_key,
    publish_verification_key,
    receive_print_batch,
    replace_issuance,
    resume_pass,
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
from apps.reviews.services import cancel_registration_operationally, withdraw_registration

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

TEST_OPERATIONAL_PASSWORD = "__test_password__"  # noqa: S105
#: How long the ending transaction keeps the Registration row locked.
HOLD_SECONDS = 0.8


def _user(email: str, group_name: str, *, event, organization=None):
    user = OperationalUser.objects.create_user(
        email=email, password=TEST_OPERATIONAL_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        granted_by=user,
    )
    return user


@pytest.fixture
def world():
    """One approved context with assignments, a key, stock and an operator."""
    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})

    now = timezone.now()
    event = EventEdition.objects.create(
        code=f"RACE{uuid.uuid4().hex[:6].upper()}",
        name="Withdrawal Race Edition",
        timezone="UTC",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=30),
        status="EVENT_OPERATIONS",
    )
    organization = Organization.objects.create(
        official_name=f"Race Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"race org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    pass_admin = _user(
        f"pass.{uuid.uuid4().hex[:8]}@example.test",
        "Pass Administrators",
        event=event,
        organization=organization,
    )
    stock_admin = _user(
        f"stock.{uuid.uuid4().hex[:8]}@example.test", "Badge Stock Administrators", event=event
    )
    custodian = OperationalUser.objects.create_user(
        email=f"cust.{uuid.uuid4().hex[:8]}@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )
    person = Person.objects.create(status=PersonStatus.ACTIVE)
    registration = Registration.objects.create(
        public_reference=f"RACE-{uuid.uuid4().hex[:10].upper()}",
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
        "effective_from": now - timedelta(minutes=5),
        "created_by": pass_admin,
    }
    ParticipantRoleAssignment.objects.create(registration=registration, role=role, **common)
    assignment = BadgeTypeAssignment.objects.create(
        registration=registration, badge_type=badge_type, **common
    )
    AccessProfileAssignment.objects.create(
        registration=registration, access_profile=access_profile, **common
    )
    key = publish_verification_key(
        key_id="v1",
        public_key_pem=provider.public_key_pem("v1").decode("ascii"),
        actor=custodian,
    )
    promote_verification_key(key=key, actor=custodian)

    location = create_stock_location(
        event_edition=event,
        code="CENTRAL",
        name="Central Store",
        location_type="CENTRAL",
        actor=stock_admin,
        operation_id=new_operation_id(),
    )
    batch = create_print_batch(
        event_edition=event,
        badge_type=badge_type,
        planned_quantity=10,
        actor=stock_admin,
        operation_id=new_operation_id(),
        destination_location=location,
    )
    for status in (PrintBatchStatus.READY, PrintBatchStatus.IN_PRODUCTION):
        batch = change_batch_status(
            batch=batch,
            target_status=status,
            actor=stock_admin,
            operation_id=new_operation_id(),
            expected_lock_version=batch.version,
        )
    receive_print_batch(
        batch=batch,
        produced_quantity=10,
        accepted_quantity=10,
        damaged_quantity=0,
        actor=stock_admin,
        operation_id=new_operation_id(),
        expected_lock_version=batch.version,
    )
    yield {
        "registration": registration,
        "assignment": assignment,
        "badge_type": badge_type,
        "location": location,
        "pass_admin": pass_admin,
        "stock_admin": stock_admin,
    }
    set_signing_key_provider_for_testing(None)


def _end_context(kind, world):
    registration = Registration.objects.get(pk=world["registration"].pk)
    if kind == "withdrawal":
        withdraw_registration(
            registration=registration,
            person=registration.person,
            expected_version=registration.version,
        )
    else:
        cancel_registration_operationally(
            registration=registration,
            expected_version=registration.version,
            reason="SYNTHETIC_OPERATIONAL_CANCELLATION",
            actor=world["stock_admin"],
        )


def _ended_at(registration) -> object:
    registration = Registration.objects.get(pk=registration.pk)
    return registration.withdrawn_at or registration.cancelled_at


# ---------------------------------------------------------------------------
# Commands under test
# ---------------------------------------------------------------------------


def _prepare_issue(world):
    def run():
        return issue_badge(
            badge_assignment=world["assignment"],
            badge_type=world["badge_type"],
            location=world["location"],
            actor=world["stock_admin"],
            operation_id=new_operation_id(),
        )

    def succeeded_at():
        issuance = BadgeIssuance.objects.filter(status=BadgeIssuanceStatus.ISSUED).first()
        return issuance.issued_at if issuance else None

    return run, succeeded_at, RegistrationNotEligibleError


def _prepare_replace(world):
    original = issue_badge(
        badge_assignment=world["assignment"],
        badge_type=world["badge_type"],
        location=world["location"],
        actor=world["stock_admin"],
        operation_id=new_operation_id(),
    )

    def run():
        return replace_issuance(
            issuance=original,
            badge_type=world["badge_type"],
            actor=world["stock_admin"],
            operation_id=new_operation_id(),
            expected_lock_version=original.version,
            reason_code=BadgeIssuanceReasonCode.DAMAGED_BADGE,
            return_original=False,
        )

    def succeeded_at():
        replacement = BadgeIssuance.objects.exclude(pk=original.pk).first()
        return replacement.issued_at if replacement else None

    return run, succeeded_at, RegistrationNotEligibleError


def _prepare_activate(world):
    credential = generate_pass(
        registration=world["registration"],
        actor=world["pass_admin"],
        operation_id=new_operation_id(),
    ).credential

    def run():
        return activate_pass(
            credential=credential,
            actor=world["pass_admin"],
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
        )

    def succeeded_at():
        row = DigitalEntryPass.objects.get(pk=credential.pk)
        return row.activated_at if row.status == DigitalEntryPassStatus.ACTIVE else None

    return run, succeeded_at, PassNotEligibleError


def _prepare_resume(world):
    credential = generate_pass(
        registration=world["registration"],
        actor=world["pass_admin"],
        operation_id=new_operation_id(),
    ).credential
    credential = activate_pass(
        credential=credential,
        actor=world["pass_admin"],
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    ).credential
    credential = suspend_pass(
        credential=credential,
        actor=world["pass_admin"],
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code="SECURITY_CONCERN",
    ).credential

    def run():
        return resume_pass(
            credential=credential,
            actor=world["pass_admin"],
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
        )

    def succeeded_at():
        row = DigitalEntryPass.objects.get(pk=credential.pk)
        return row.resumed_at if row.status == DigitalEntryPassStatus.ACTIVE else None

    return run, succeeded_at, PassNotEligibleError


COMMANDS = {
    "issue": _prepare_issue,
    "replace": _prepare_replace,
    "activate": _prepare_activate,
    "resume": _prepare_resume,
}


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["withdrawal", "cancellation"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_committed_withdrawal_blocks_then_refuses_the_command(world, command, kind):
    run, succeeded_at, refusal = COMMANDS[command](world)
    locked = threading.Event()
    outcome: dict = {}

    def ender():
        try:
            with transaction.atomic():
                _end_context(kind, world)
                locked.set()
                time.sleep(HOLD_SECONDS)
        finally:
            connection.close()

    def commander():
        try:
            assert locked.wait(timeout=10)
            started = time.monotonic()
            try:
                run()
                outcome["result"] = "SUCCEEDED"
            except refusal:
                outcome["result"] = "REFUSED"
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                outcome["result"] = repr(exc)
            outcome["waited"] = time.monotonic() - started
        finally:
            connection.close()

    threads = [threading.Thread(target=ender), threading.Thread(target=commander)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # The command waited for the ending transaction's Registration lock...
    assert outcome["waited"] >= HOLD_SECONDS * 0.6, outcome
    # ...and, seeing the committed withdrawal/cancellation, refused.
    assert outcome["result"] == "REFUSED", outcome
    assert succeeded_at() is None


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_genuine_overlap_is_serializable_and_never_deadlocks(world, command):
    run, succeeded_at, refusal = COMMANDS[command](world)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: dict = {}

    def worker(name, action):
        try:
            barrier.wait(timeout=10)
            action()
            results[name] = "OK"
        except refusal:
            results[name] = "REFUSED"
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=("end", lambda: _end_context("withdrawal", world))),
        threading.Thread(target=worker, args=("command", run)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # No deadlock, lock timeout, or raw database error in either connection.
    assert not errors, errors
    assert not any(isinstance(e, DatabaseError) for e in errors)
    assert results["end"] == "OK"
    ended_at = _ended_at(world["registration"])
    assert ended_at is not None
    done_at = succeeded_at()
    if results["command"] == "OK":
        # The command won the lock: it completed BEFORE the context ended.
        assert done_at is not None and done_at < ended_at
    else:
        # The withdrawal won: nothing was issued, replaced, activated or resumed.
        assert done_at is None
