"""Unlinked admission evidence counts for every later admission decision
(Phase 4 Prompt 3 correction 5; developer decisions D3-A, D3-B option S1 and
D3-C; `apps.entry.selectors.admissions`).

A conforming offline override whose reason is deleted or renamed after the
package snapshot is a changed-rule CONFLICT that the schema allows no Entry
Event (the D3 path). Before correction 5 that physical admission was
invisible to every later decision -- online, on another offline device and
in the next package (independent review probes P1, P2 and P4). These tests
pin the corrected behaviour, and that it invents no Entry Event, leaves
SECURITY_CONFLICT handling unchanged and counts nothing twice.
PostgreSQL; all data is synthetic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection
from django.utils import timezone

from apps.entry.models import (
    EntryDevice,
    EntryEvent,
    EntryOverrideReason,
    ReconciliationCase,
    ReconciliationCaseOperation,
    SyncOperation,
)
from apps.entry.offline_contract import TYP_PACKAGE_MANIFEST
from apps.entry.selectors.admissions import (
    last_unlinked_admission_times,
    latest_unlinked_admission,
    unlinked_admissions,
    unlinked_admissions_for,
)
from apps.entry.services.offline_evaluation import evaluate_at
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db

BLOCKER = "OUTSIDE_TIME_WINDOW"
NOTE = "Synthetic note"
REASON = "SYNTHETIC_OVERRIDE"


@pytest.fixture
def supervisor_grant(offline_world, supervisor, layout):
    checkpoint = factories.open_checkpoint(
        device=offline_world.device, user=supervisor, zone=layout.main
    )
    grant, _ = offline_factories.issue_grant(offline_world.synthetic, checkpoint=checkpoint)
    assert grant.may_override
    return grant


@pytest.fixture
def online(event, layout, device_admin):
    """An ONLINE checkpoint on another enrolled device, with its own operator."""
    device, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    user = factories.make_user(
        "online.operator@example.test",
        group_name="Entry Operators",
        event=event,
        gate=layout.gate_a,
    )
    return factories.open_checkpoint(device=device, user=user, zone=layout.main)


# ---------------------------------------------------------------------------
# Builders (shared with test_offline_evidence_concurrency.py)
# ---------------------------------------------------------------------------


def single_entry(setup) -> None:
    setup.access_profile.reentry_policy = "SINGLE_ENTRY"
    setup.access_profile.save(update_fields=["reentry_policy"])


def rebuild(world) -> dict:
    """A NEW package for the world's device through the production
    lifecycle; returns its verified, decrypted body."""
    from apps.entry.services.offline_packages import request_package_build

    previous = world.package.package_version
    request_package_build(world.device, reason="MANUAL", seen_version=previous)
    offline_factories.run_pending_builds()
    world.package, raw = offline_factories.download(world.synthetic)
    assert world.package.package_version > previous
    _manifest, body = world.synthetic.open_response(raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG")
    return body


def entry_for(body, credential) -> dict:
    return next(item for item in body["entries"] if item["jti"] == credential.jti)


def sync(world, op, *, note=None, now=None):
    envelope = world.store.envelope(op, note=note)
    result = offline_factories.sync(world.synthetic, world.store, [envelope], now=now)
    ack = result.acknowledgements[0]
    assert ack.durable
    return ack, SyncOperation.objects.get(operation_id=op["operation_id"])


def admission(world, *, occurred_at=None, **fields):
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=world.grant,
        credential=world.credential,
        occurred_at=occurred_at or timezone.now() + timedelta(seconds=2),
        **fields,
    )
    return world.store.next(op)


def override(world, grant, code, *, occurred_at=None, blockers=(BLOCKER,)):
    """A conforming override exactly as the runtime records it."""
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=grant,
        credential=world.credential,
        occurred_at=occurred_at or timezone.now() + timedelta(seconds=2),
        local={
            "code": "VALID",
            "result": "DENIED",
            "reason": blockers[0],
            "blockers": list(blockers),
            "advisories": [],
            "latency_ms": 7,
        },
        override={"code": code, "note_digest": ""},
    )
    op["override"]["note_digest"] = hashlib.sha256(
        f"{op['operation_id']}\n{NOTE}".encode()
    ).hexdigest()
    return world.store.next(op)


def remove_packaged_reason(world, event, *, change="deleted") -> None:
    """The world's package carries REASON; the server then deletes or renames it."""
    reason = EntryOverrideReason.objects.create(
        event_edition=event,
        code=REASON,
        name="Synthetic override reason",
        overridable_reason_codes=[BLOCKER],
        requires_note=True,
    )
    assert REASON in {item["code"] for item in rebuild(world)["override_reasons"]}
    if change == "deleted":
        reason.delete()
    else:
        EntryOverrideReason.objects.filter(pk=reason.pk).update(code="SYNTHETIC_RENAMED")


def assert_unlinked(ack, row) -> None:
    assert (ack.status, ack.conflict_type, ack.outcome) == (
        "CONFLICT",
        "ACCESS_CHANGED",
        "HISTORICAL_ACCESS_UNCERTAIN",
    )
    assert row.server_evaluation["override_reason_unavailable"] is True
    assert row.result_reference is None
    assert not EntryEvent.objects.filter(sync_operation=row).exists()


def sync_unlinked_admission(world, grant, *, occurred_at=None):
    """Synchronize a conforming override with the removed REASON: the D3 path."""
    op = override(world, grant, REASON, occurred_at=occurred_at)
    ack, row = sync(world, op, note=NOTE)
    assert_unlinked(ack, row)
    return row, op


def unlinked_admission(world, grant, event, *, change="deleted"):
    remove_packaged_reason(world, event, change=change)
    return sync_unlinked_admission(world, grant)


def second_device(world, *, event, layout, device_admin, email="second.operator@example.test"):
    """Another OFFLINE_READY device with its own package, operator and grant."""
    device, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    offline_factories.make_offline_capable(device, admin=device_admin, layout=layout)
    synthetic = offline_factories.SyntheticDevice(device=device)
    offline_factories.provision(synthetic, admin=device_admin)
    package, raw = offline_factories.download(synthetic)
    offline_factories.self_test(synthetic, admin=device_admin, package=package)
    user = factories.make_user(email, group_name="Entry Operators", event=event, gate=layout.gate_a)
    checkpoint = factories.open_checkpoint(device=synthetic.device, user=user, zone=layout.main)
    grant, _compact = offline_factories.issue_grant(synthetic, checkpoint=checkpoint)
    _manifest, body = synthetic.open_response(raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG")
    return SimpleNamespace(
        synthetic=synthetic,
        device=synthetic.device,
        package=package,
        body=body,
        checkpoint=checkpoint,
        grant=grant,
        store=offline_factories.SyntheticStore(synthetic),
        credential=world.credential,
        operator=user,
    )


def verify_online(checkpoint, credential):
    from apps.entry.services.verification import verify_qr

    return verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential)).assessment


def decide(checkpoint, assessment):
    from apps.entry.services.decisions import pending_from_assessment, record_entry_decision

    pending = pending_from_assessment(assessment=assessment, checkpoint=checkpoint, method="QR")
    return record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")


def linked_operations(case) -> set:
    return set(
        ReconciliationCaseOperation.objects.filter(case=case).values_list(
            "sync_operation_id", flat=True
        )
    )


# ---------------------------------------------------------------------------
# The shared definition
# ---------------------------------------------------------------------------


def test_only_durable_unlinked_admissions_qualify(
    offline_world, supervisor_grant, event, layout, device_admin
):
    from apps.entry.services import offline_sync

    remove_packaged_reason(offline_world, event)
    second = second_device(offline_world, event=event, layout=layout, device_admin=device_admin)
    row, _op = sync_unlinked_admission(offline_world, supervisor_grant)
    registration = row.registration
    # A conflicting admission WITH its Entry Event is counted through that
    # event, never a second time as an operation.
    _ack, with_event = sync(offline_world, admission(offline_world))
    assert with_event.status == "CONFLICT"
    assert EntryEvent.objects.filter(sync_operation=with_event).exists()
    # A quarantined admission of the same context never qualifies.
    EntryDevice.objects.filter(pk=second.device.pk).update(status="SUSPENDED")
    q_ack, quarantined = sync(second, admission(second))
    assert q_ack.status == "QUARANTINED" and quarantined.registration_id == registration.pk
    # Nor does a PENDING one (durable, not processed yet).
    pending_op = admission(offline_world)
    intake = offline_sync._intake(
        offline_world.device,
        offline_world.store.store_id,
        offline_world.store.envelope(pending_op),
        quarantined=False,
        now=timezone.now(),
    )
    assert SyncOperation.objects.get(pk=intake.row_id).status == "PENDING"
    assert list(unlinked_admissions()) == [row]
    assert latest_unlinked_admission(registration).pk == row.pk
    assert latest_unlinked_admission(registration, exclude=row.pk) is None
    assert last_unlinked_admission_times([registration.pk], event_edition_id=event.pk) == {
        registration.pk: row.occurred_at
    }


def test_the_unlinked_admission_lookup_uses_the_registration_index(registration):
    sql, params = (
        unlinked_admissions_for(registration).order_by("-occurred_at").query.sql_with_params()
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN " + sql, params)
        plan = "\n".join(line[0] for line in cursor.fetchall())
    # The durable property: with sequential scans disabled, the lookup is
    # still served by an index. Which index the planner picks depends on the
    # table statistics, so no index name is asserted (focused review F-01).
    assert "Seq Scan" not in plan, plan
    index_access = re.compile(
        r"(?:Index(?: Only)? Scan(?: Backward)? using \S+|Bitmap Heap Scan)"
        r" on entry_sync_operation\b"
    )
    assert index_access.search(plan), plan


# ---------------------------------------------------------------------------
# Online admission decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["deleted", "renamed"])
def test_an_unlinked_admission_blocks_a_later_single_entry_admission_online(
    offline_world, supervisor_grant, event, setup, online, change
):
    from apps.entry.services import EntryStateError

    single_entry(setup)
    row, _op = unlinked_admission(offline_world, supervisor_grant, event, change=change)
    assessment = verify_online(online, offline_world.credential)
    # Before correction 5: ALLOWED, and a second admission was recorded (P1, P2).
    assert (assessment.result, assessment.reason_code) == ("DENIED", "ALREADY_ADMITTED")
    assert assessment.prior_entry is None  # no Entry Event is invented
    assert assessment.unlinked_prior_admission.pk == row.pk
    with pytest.raises(EntryStateError):
        decide(online, assessment)
    assert not EntryEvent.objects.filter(decision="ADMIT").exists()


def test_the_default_reentry_policy_advises_of_an_unlinked_admission(
    offline_world, supervisor_grant, event, online
):
    unlinked_admission(offline_world, supervisor_grant, event)
    assessment = verify_online(online, offline_world.credential)
    # Before correction 5: ALLOWED with no advisory at all (P1).
    assert assessment.result == "ALLOWED_WITH_ADVISORY"
    assert set(assessment.advisory_codes) == {"PRIOR_ENTRY", "RECENT_REENTRY"}
    assert assessment.prior_entry is None
    admitted = decide(online, assessment).entry_event
    assert set(admitted.advisory_codes) == {"PRIOR_ENTRY", "RECENT_REENTRY"}
    assert admitted.prior_entry_event is None  # truthful: that admission has no Entry Event


def test_an_online_decision_is_refused_when_an_unlinked_admission_commits_after_verification(
    offline_world, supervisor_grant, event, online
):
    from apps.entry.services.decisions import (
        StaleVerificationError,
        pending_from_assessment,
        record_entry_decision,
    )

    remove_packaged_reason(offline_world, event)
    decide(online, verify_online(online, offline_world.credential))  # an earlier admission
    assessment = verify_online(online, offline_world.credential)
    assert set(assessment.advisory_codes) == {"PRIOR_ENTRY", "RECENT_REENTRY"}
    pending = pending_from_assessment(assessment=assessment, checkpoint=online, method="QR")
    sync_unlinked_admission(offline_world, supervisor_grant)
    # Same result and advisories, but a newer admission than the operator saw:
    # refused, exactly as a newer Entry Event would be.
    assert set(verify_online(online, offline_world.credential).advisory_codes) == {
        "PRIOR_ENTRY",
        "RECENT_REENTRY",
    }
    with pytest.raises(StaleVerificationError):
        record_entry_decision(checkpoint=online, pending=pending, decision="ADMIT")
    assert EntryEvent.objects.filter(decision="ADMIT").count() == 1


# ---------------------------------------------------------------------------
# Later packages and other offline devices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["deleted", "renamed"])
def test_a_later_package_carries_an_unlinked_admission(
    offline_world, supervisor_grant, event, setup, layout, device_admin, change
):
    single_entry(setup)
    row, _op = unlinked_admission(offline_world, supervisor_grant, event, change=change)
    expected = int(row.occurred_at.timestamp())
    # The originating device's next package...
    assert entry_for(rebuild(offline_world), offline_world.credential)["last_admitted_at"] == (
        expected
    )
    # ...and a device prepared after the synchronization (P4: null before).
    second = second_device(offline_world, event=event, layout=layout, device_admin=device_admin)
    entry = entry_for(second.body, offline_world.credential)
    assert entry["last_admitted_at"] == expected and entry["reentry"] == "SINGLE_ENTRY"


def test_a_second_device_admission_after_an_unlinked_admission_is_a_duplicate_entry(
    offline_world, supervisor_grant, event, setup, layout, device_admin
):
    single_entry(setup)
    remove_packaged_reason(offline_world, event)
    # Prepared BEFORE the unlinked admission synchronized (Flow §11.9).
    second = second_device(offline_world, event=event, layout=layout, device_admin=device_admin)
    assert entry_for(second.body, offline_world.credential)["last_admitted_at"] is None
    row, _op = sync_unlinked_admission(offline_world, supervisor_grant)
    op = admission(second, occurred_at=timezone.now() + timedelta(seconds=10))
    ack, other = sync(second, op)
    # Before correction 5: APPLIED / ADMISSION_APPLIED, with no conflict (P4).
    assert (ack.status, ack.conflict_type, ack.outcome) == (
        "CONFLICT",
        "DUPLICATE_ENTRY",
        "SINGLE_ENTRY_EXCEEDED",
    )
    event_row = EntryEvent.objects.get(sync_operation=other)
    assert event_row.offline_conflict and event_row.prior_entry_event is None
    case = ReconciliationCase.objects.get(sync_operation=other, case_type="DUPLICATE_ENTRY")
    assert case.related_entry_event is None  # the other admission has no Entry Event
    assert linked_operations(case) == {other.pk, row.pk}
    server = case.server_known_state
    assert "ALREADY_ADMITTED" in server["blockers"]
    assert server["prior_admission_at"] == int(row.occurred_at.timestamp())
    assert server["prior_unlinked_admission_at"] == int(row.occurred_at.timestamp())
    # A replay returns the original outcome, applies and links nothing new.
    again, _again_row = sync(second, op)
    assert again.replayed and (again.status, again.outcome) == (ack.status, ack.outcome)
    assert linked_operations(case) == {other.pk, row.pk}
    assert EntryEvent.objects.count() == 1


def test_a_second_device_admission_near_an_unlinked_admission_gets_the_advisory(
    offline_world, supervisor_grant, event, layout, device_admin
):
    remove_packaged_reason(offline_world, event)
    second = second_device(offline_world, event=event, layout=layout, device_admin=device_admin)
    row, _op = sync_unlinked_admission(offline_world, supervisor_grant)
    ack, other = sync(second, admission(second, occurred_at=timezone.now() + timedelta(seconds=10)))
    assert (ack.status, ack.conflict_type, ack.outcome) == (
        "APPLIED",
        "DUPLICATE_ADMISSION_ADVISORY",
        "ADMISSION_APPLIED",
    )
    assert not EntryEvent.objects.get(sync_operation=other).offline_conflict
    case = ReconciliationCase.objects.get(sync_operation=other)
    assert case.severity == "ADVISORY" and case.related_entry_event is None
    assert linked_operations(case) == {other.pk, row.pk}
    assert {"PRIOR_ENTRY", "RECENT_REENTRY"} <= set(case.server_known_state["advisories"])


# ---------------------------------------------------------------------------
# Reconciliation, replay, scope and time
# ---------------------------------------------------------------------------


def test_closing_the_case_or_replaying_never_erases_or_duplicates_the_evidence(
    offline_world, supervisor_grant, supervisor, event, setup, online
):
    from apps.entry.services.reconciliation import record_action

    single_entry(setup)
    row, op = unlinked_admission(offline_world, supervisor_grant, event)
    case = ReconciliationCase.objects.get(sync_operation=row, case_type="ACCESS_CHANGED")
    record_action(
        case=case, actor=supervisor, action="CLOSE", note="Reviewed.", expected_version=case.version
    )
    case.refresh_from_db()
    assert case.status == "CLOSED"
    # Closing the case does not erase the physical admission.
    assert verify_online(online, offline_world.credential).reason_code == "ALREADY_ADMITTED"
    body = rebuild(offline_world)
    assert entry_for(body, offline_world.credential)["last_admitted_at"] == int(
        row.occurred_at.timestamp()
    )
    # A replay returns the original durable outcome and adds nothing.
    counts = (
        SyncOperation.objects.count(),
        ReconciliationCase.objects.count(),
        EntryEvent.objects.count(),
    )
    again, replayed = sync(offline_world, op, note=NOTE)
    assert again.replayed and (again.status, again.outcome) == (
        "CONFLICT",
        "HISTORICAL_ACCESS_UNCERTAIN",
    )
    assert replayed.pk == row.pk and replayed.duplicate_submissions == 1
    assert (
        SyncOperation.objects.count(),
        ReconciliationCase.objects.count(),
        EntryEvent.objects.count(),
    ) == counts
    assert list(unlinked_admissions_for(row.registration)) == [row]  # counted once


def test_unlinked_evidence_is_scoped_to_its_context_and_its_occurrence_time(
    offline_world, supervisor_grant, event, other_event, setup, staff, online, settings
):
    other_registration = factories.make_registration(
        event=event, person=factories.make_person("Synthetic Other Participant")
    )
    factories.assign(registration=other_registration, setup=setup, actor=staff)
    other_pass = factories.issue_active_pass(registration=other_registration, actor=staff)
    single_entry(setup)
    row, _op = unlinked_admission(offline_world, supervisor_grant, event)
    registration = offline_world.credential.registration
    # Another Registration Context is unaffected (BR-ENT-001).
    assert not unlinked_admissions_for(other_registration).exists()
    assessment = verify_online(online, other_pass)
    assert assessment.result == "ALLOWED" and not assessment.advisory_codes
    # The same registration id under another event matches nothing.
    foreign = SimpleNamespace(pk=registration.pk, event_edition_id=other_event.pk)
    assert not unlinked_admissions_for(foreign).exists()

    def evaluation(at, **kwargs):
        return evaluate_at(
            registration=registration,
            credential=offline_world.credential,
            event_edition=event,
            gate=row.gate,
            zone=row.zone,
            at=at,
            **kwargs,
        )

    # Strictly earlier admissions only, never the operation being assessed.
    before = evaluation(row.occurred_at - timedelta(seconds=60))
    assert before.prior_admission_at is None
    assert "ALREADY_ADMITTED" not in before.blocker_codes
    assert evaluation(row.occurred_at).prior_unlinked_admission is None  # same second
    after = evaluation(row.occurred_at + timedelta(seconds=60))
    assert after.prior_unlinked_admission.pk == row.pk and after.prior_admission is None
    assert "ALREADY_ADMITTED" in after.blocker_codes
    assert after.as_json()["prior_admission_at"] == int(row.occurred_at.timestamp())
    excluded = evaluation(row.occurred_at + timedelta(seconds=60), exclude_operation=row.pk)
    assert excluded.prior_unlinked_admission is None
    # Under the default policy: an earlier admission is a prior entry, and a
    # recent re-entry only inside the configured window.
    setup.access_profile.reentry_policy = "REENTRY_ALLOWED"
    setup.access_profile.save(update_fields=["reentry_policy"])
    later = evaluation(
        row.occurred_at + timedelta(seconds=settings.ENTRY_RECENT_REENTRY_SECONDS + 60)
    )
    assert set(later.advisories) == {"PRIOR_ENTRY"} and not later.blocker_codes


def test_security_conflict_admissions_stay_outside_the_admission_evidence(
    offline_world, supervisor_grant, event, setup, online
):
    # Developer decision D3-C: the approved SECURITY_CONFLICT behaviour stays.
    single_entry(setup)
    EntryOverrideReason.objects.create(
        event_edition=event,
        code="SYNTHETIC_DISABLED",
        name="Synthetic disabled reason",
        overridable_reason_codes=[BLOCKER],
        requires_note=True,
        is_active=False,
    )
    rebuild(offline_world)  # the package never offers an inactive reason
    ack, violation = sync(
        offline_world, override(offline_world, supervisor_grant, "SYNTHETIC_DISABLED"), note=NOTE
    )
    assert (ack.status, ack.outcome) == ("SECURITY_CONFLICT", "OVERRIDE_NOT_PERMITTED")
    ack, stale = sync(offline_world, admission(offline_world, state="STALE", band="STALE"))
    assert (ack.status, ack.outcome) == ("SECURITY_CONFLICT", "STATE_FORBIDS_ADMISSION")
    assert not EntryEvent.objects.exists()
    assert not unlinked_admissions().exists()
    assessment = verify_online(online, offline_world.credential)
    assert assessment.result == "ALLOWED" and "ALREADY_ADMITTED" not in assessment.blocker_codes
    assert entry_for(rebuild(offline_world), offline_world.credential)["last_admitted_at"] is None
    for security_row in (violation, stale):
        security_row.refresh_from_db()
        assert security_row.status == "SECURITY_CONFLICT" and security_row.result_reference is None
