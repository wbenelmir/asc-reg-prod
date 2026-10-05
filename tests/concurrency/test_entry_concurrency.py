"""Real multi-connection PostgreSQL concurrency for online entry decisions
(Phase 3 Prompt 4, ADR-0021).

Two real threads, each on its OWN PostgreSQL connection, released together
from a `threading.Barrier` -- the pattern established by
`tests/concurrency/test_badge_stock_concurrency.py`. Three guarantees a
single-threaded test cannot establish:

1. the same decision submitted twice at the same instant (double tap,
   network retry) creates exactly ONE Entry Event;
2. two operators admitting the same Registration Context at the same
   instant from two separate verifications never both succeed silently:
   the Registration row lock serializes them and the second is refused as
   STALE, so it can never admit without seeing the first admission;
3. under an explicit single-entry rule, two simultaneous admissions can
   never both be recorded.
"""

from __future__ import annotations

import threading

import pytest
from django.db import connection

from apps.accreditation.models import ReentryPolicy
from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    set_signing_key_provider_for_testing,
)
from apps.entry.models import EntryDecision, EntryEvent
from apps.entry.services.decisions import (
    StaleVerificationError,
    pending_from_assessment,
    record_entry_decision,
)
from apps.entry.services.sessions import join_checkpoint_session, resolve_checkpoint
from apps.entry.services.verification import verify_qr
from apps.entry.tests import factories

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_in_threads(callables):
    barrier = threading.Barrier(len(callables))
    results: list = [None] * len(callables)
    errors: list[BaseException] = []

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(callables))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


@pytest.fixture
def world():
    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)
    factories.seed_reference_values()
    event = factories.make_event("ENTCONC")
    layout = factories.VenueLayout(event)
    setup = factories.AccreditationSetup(event, layout)
    staff = factories.make_user("staff@example.test")
    factories.publish_key(provider=provider, actor=staff)
    registration = factories.make_registration(event=event, person=factories.make_person())
    factories.assign(registration=registration, setup=setup, actor=staff)
    credential = factories.issue_active_pass(registration=registration, actor=staff)
    admin = factories.make_user(
        "da@example.test", group_name="Entry Device Administrators", event=event
    )
    operator = factories.make_user(
        "op1@example.test", group_name="Entry Operators", event=event, gate=layout.gate_a
    )
    second_operator = factories.make_user(
        "op2@example.test", group_name="Entry Operators", event=event, gate=layout.gate_a
    )
    device, _ = factories.enroll_device(event=event, layout=layout, admin=admin)
    first = factories.open_checkpoint(device=device, user=operator, zone=layout.main)
    joined = join_checkpoint_session(device_session=first.device_session, user=second_operator)
    second = resolve_checkpoint(
        device=device,
        device_session_id=first.device_session.pk,
        operator_session_id=joined.pk,
        user=second_operator,
    )
    yield {"first": first, "second": second, "credential": credential, "setup": setup}
    set_signing_key_provider_for_testing(None)


def _pending(checkpoint, credential):
    outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential))
    return pending_from_assessment(
        assessment=outcome.assessment, checkpoint=checkpoint, method="QR"
    )


def test_simultaneous_identical_decisions_create_one_event(world):
    checkpoint = world["first"]
    pending = _pending(checkpoint, world["credential"])
    results, errors = _run_in_threads(
        [
            lambda: record_entry_decision(
                checkpoint=checkpoint, pending=pending, decision=EntryDecision.ADMIT
            )
        ]
        * 2
    )
    assert errors == []
    assert EntryEvent.objects.count() == 1
    assert sorted(r.replayed for r in results) == [False, True]
    assert results[0].entry_event.pk == results[1].entry_event.pk


def test_two_operators_admitting_the_same_context_are_serialized(world):
    first, second = world["first"], world["second"]
    pending_one = _pending(first, world["credential"])
    pending_two = _pending(second, world["credential"])
    results, errors = _run_in_threads(
        [
            lambda: record_entry_decision(
                checkpoint=first, pending=pending_one, decision=EntryDecision.ADMIT
            ),
            lambda: record_entry_decision(
                checkpoint=second, pending=pending_two, decision=EntryDecision.ADMIT
            ),
        ]
    )
    assert EntryEvent.objects.count() == 1
    assert len(errors) == 1
    assert isinstance(errors[0], StaleVerificationError)


def test_single_entry_rule_holds_under_concurrency(world):
    profile = world["setup"].access_profile
    profile.reentry_policy = ReentryPolicy.SINGLE_ENTRY
    profile.save()
    first, second = world["first"], world["second"]
    pending_one = _pending(first, world["credential"])
    pending_two = _pending(second, world["credential"])
    _results, errors = _run_in_threads(
        [
            lambda: record_entry_decision(
                checkpoint=first, pending=pending_one, decision=EntryDecision.ADMIT
            ),
            lambda: record_entry_decision(
                checkpoint=second, pending=pending_two, decision=EntryDecision.ADMIT
            ),
        ]
    )
    assert EntryEvent.objects.filter(decision=EntryDecision.ADMIT).count() == 1
    assert len(errors) == 1
