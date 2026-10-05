"""Real multi-connection PostgreSQL proofs for Phase 4 Prompt 3 correction 5.

* D2, option A2 -- a reason committed between the package watermark and the
  projection's catalogue query (probe P8) is IN the package while its INSERT
  is uncovered: a conforming override stays a changed-rule conflict, and only
  an override violating the reason's only state is a policy violation.
* D3, option S1 -- admission decisions serialize on the Registration row: an
  online decision, or another device's synchronization, that waits behind an
  unlinked admission's commit reads it and cannot admit a duplicate.
* R-02 -- the unauthenticated refusal audit is serialized by an advisory lock
  on (device, reason) that is only ever TRIED: exactly one row per window
  under concurrency, isolation between devices and reasons, window rollover,
  no waiting on contention, and genuine or authenticated evidence unaffected.

Every thread uses its OWN PostgreSQL connection, with real commits
(`transaction=True`). No process-local lock replaces a database lock. All
data is synthetic.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.audit import action_codes
from apps.entry.models import EntryEvent, EntryOverrideReason, ReconciliationCase, SyncOperation
from apps.entry.offline_contract import TYP_PACKAGE_MANIFEST
from apps.entry.services import offline_sync
from apps.entry.services.offline_devices import OfflineRequestRejected
from apps.entry.tests import factories, offline_factories
from apps.entry.tests.test_offline_admission_evidence import (
    BLOCKER,
    NOTE,
    REASON,
    admission,
    assert_unlinked,
    linked_operations,
    override,
    remove_packaged_reason,
    second_device,
    single_entry,
    sync,
)
from apps.entry.tests.test_offline_sync import (
    _admit,
    _batches_received,
    _forged,
    _quarantine_payload,
    _refusals,
    _revoke,
)
from tests.concurrency.test_offline_concurrency import _run_in_threads, _wait_for_lock_waiter

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

_WARNING = "Unauthenticated quarantine upload refused."


def _grant(world, user, layout):
    checkpoint = factories.open_checkpoint(device=world.device, user=user, zone=layout.main)
    grant, _ = offline_factories.issue_grant(world.synthetic, checkpoint=checkpoint)
    assert grant.may_override
    return grant


def _closing(function):
    """Run `function` on this thread's own connection, then close it."""

    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            connection.close()

    return run


# ---------------------------------------------------------------------------
# D2 (A2): the READ COMMITTED watermark-to-projection window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conforming", [True, False])
def test_a_reason_committed_inside_the_watermark_window_is_packaged_yet_uncovered(
    offline_world, supervisor, layout, event, monkeypatch, conforming
):
    from apps.entry.services import offline_packages
    from apps.entry.services.offline_journal import capture_watermark, journal_changes

    grant = _grant(offline_world, supervisor, layout)
    inserted = {}

    @_closing
    def insert_on_another_connection():
        inserted["reason"] = EntryOverrideReason.objects.create(
            event_edition=event,
            code="SYNTHETIC_WINDOW",
            name="Synthetic reason committed in the window",
            overridable_reason_codes=[BLOCKER],
            requires_note=True,
        )

    def capture_then_commit_a_reason():
        watermark = capture_watermark()  # the snapshot the package records
        writer = threading.Thread(target=insert_on_another_connection)
        writer.start()
        writer.join(timeout=30)
        assert not writer.is_alive() and "reason" in inserted
        return watermark

    monkeypatch.setattr(offline_packages, "_dispatch_build", lambda build_id: None)
    monkeypatch.setattr(offline_packages, "capture_watermark", capture_then_commit_a_reason)
    previous = offline_world.package.package_version
    offline_packages.request_package_build(
        offline_world.device, reason="MANUAL", seen_version=previous
    )
    offline_factories.run_pending_builds()
    monkeypatch.setattr(offline_packages, "capture_watermark", capture_watermark)
    offline_world.package, raw = offline_factories.download(offline_world.synthetic)
    assert offline_world.package.package_version > previous
    reason = inserted["reason"]
    _manifest, body = offline_world.synthetic.open_response(
        raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG"
    )
    # The projection read a state newer than the snapshot (probe P8): the
    # reason IS in the package, while its INSERT is uncovered by it.
    assert reason.code in {item["code"] for item in body["override_reasons"]}
    assert journal_changes(offline_world.package).override_reason_operations == {
        str(reason.pk): {"I"}
    }
    blockers = (BLOCKER,) if conforming else ("WRONG_ZONE",)
    op = override(offline_world, grant, reason.code, blockers=blockers)
    ack, row = sync(offline_world, op, note=NOTE)
    assert row.server_evaluation["override_catalogue_evidence"] == "INSERTED_ONLY"
    if conforming:
        # Never an accusation: an uncovered INSERT is not absence.
        assert (ack.status, ack.conflict_type, ack.outcome) == (
            "CONFLICT",
            "ACCESS_CHANGED",
            "HISTORICAL_ACCESS_UNCERTAIN",
        )
        event_row = EntryEvent.objects.get(sync_operation=row)
        assert event_row.offline_conflict and event_row.override.reason_id == reason.pk
    else:
        # The reason's only state never permitted this blocker.
        assert (ack.status, ack.conflict_type, ack.outcome) == (
            "SECURITY_CONFLICT",
            "POLICY_VIOLATION",
            "OVERRIDE_NOT_PERMITTED",
        )
        assert not EntryEvent.objects.exists()


# ---------------------------------------------------------------------------
# D3 (S1): the Registration row serializes every admission decision
# ---------------------------------------------------------------------------


def _hold_processing_in_the_registration_lock(monkeypatch, row_id):
    """Pause `process_operation(row_id)` after classification, while it holds
    the Registration row lock, until `release` is set."""
    classified, release = threading.Event(), threading.Event()
    real = offline_sync._classify

    def classify_then_wait(row, registration, *, now):
        outcome = real(row, registration, now=now)
        if row.pk == row_id:
            classified.set()
            assert release.wait(timeout=30)
        return outcome

    monkeypatch.setattr(offline_sync, "_classify", classify_then_wait)
    return classified, release


def _intake(world, op, *, note=None):
    intake = offline_sync._intake(
        world.device,
        world.store.store_id,
        world.store.envelope(op, note=note),
        quarantined=False,
        now=timezone.now(),
    )
    assert intake.row_id is not None
    return intake.row_id


def test_an_online_decision_waiting_behind_an_unlinked_admission_is_refused(
    offline_world, supervisor, layout, event, setup, device_admin, monkeypatch
):
    from apps.entry.services.decisions import (
        StaleVerificationError,
        pending_from_assessment,
        record_entry_decision,
    )
    from apps.entry.services.verification import verify_qr

    single_entry(setup)
    grant = _grant(offline_world, supervisor, layout)
    remove_packaged_reason(offline_world, event)
    device, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    online_user = factories.make_user(
        "online.concurrent@example.test",
        group_name="Entry Operators",
        event=event,
        gate=layout.gate_a,
    )
    online = factories.open_checkpoint(device=device, user=online_user, zone=layout.main)
    # Verified BEFORE the unlinked admission commits: ALLOWED at that moment.
    token = factories.token_for(offline_world.credential)
    assessment = verify_qr(checkpoint=online, raw_token=token).assessment
    assert assessment.result == "ALLOWED"
    pending = pending_from_assessment(assessment=assessment, checkpoint=online, method="QR")
    row_id = _intake(offline_world, override(offline_world, grant, REASON), note=NOTE)
    classified, release = _hold_processing_in_the_registration_lock(monkeypatch, row_id)

    @_closing
    def decide():
        assert classified.wait(timeout=30)
        return record_entry_decision(checkpoint=online, pending=pending, decision="ADMIT")

    with ThreadPoolExecutor(max_workers=2) as pool:
        processing = pool.submit(_closing(offline_sync.process_operation), row_id)
        deciding = pool.submit(decide)
        assert classified.wait(timeout=30)
        assert _wait_for_lock_waiter(), "the online decision must wait on the Registration row"
        release.set()
        ack = processing.result(timeout=60)
        with pytest.raises(StaleVerificationError):
            deciding.result(timeout=60)
    assert_unlinked(ack, SyncOperation.objects.get(pk=row_id))
    assert not EntryEvent.objects.filter(decision="ADMIT").exists()


def test_a_second_device_waiting_behind_an_unlinked_admission_records_a_duplicate(
    offline_world, supervisor, layout, event, setup, device_admin, monkeypatch
):
    single_entry(setup)
    grant = _grant(offline_world, supervisor, layout)
    remove_packaged_reason(offline_world, event)
    second = second_device(offline_world, event=event, layout=layout, device_admin=device_admin)
    first_id = _intake(offline_world, override(offline_world, grant, REASON), note=NOTE)
    second_id = _intake(
        second, admission(second, occurred_at=timezone.now() + timedelta(seconds=10))
    )
    classified, release = _hold_processing_in_the_registration_lock(monkeypatch, first_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_closing(offline_sync.process_operation), first_id)
        assert classified.wait(timeout=30)
        other = pool.submit(_closing(offline_sync.process_operation), second_id)
        assert _wait_for_lock_waiter(), "the second device must wait on the Registration row"
        release.set()
        first_ack = first.result(timeout=60)
        second_ack = other.result(timeout=60)
    assert_unlinked(first_ack, SyncOperation.objects.get(pk=first_id))
    # The later decision read the committed unlinked admission.
    assert (second_ack.status, second_ack.conflict_type, second_ack.outcome) == (
        "CONFLICT",
        "DUPLICATE_ENTRY",
        "SINGLE_ENTRY_EXCEEDED",
    )
    case = ReconciliationCase.objects.get(sync_operation_id=second_id, case_type="DUPLICATE_ENTRY")
    assert linked_operations(case) == {second_id, first_id}
    assert EntryEvent.objects.get().sync_operation_id == second_id


# ---------------------------------------------------------------------------
# R-02: the unauthenticated refusal audit under real concurrency
# ---------------------------------------------------------------------------


def _refused(payload) -> str:
    try:
        offline_sync.synchronize_quarantine(payload=payload)
    except OfflineRequestRejected as exc:
        return exc.code
    return "ACCEPTED"


def _warnings(caplog) -> int:
    return sum(1 for record in caplog.records if record.getMessage() == _WARNING)


def test_concurrent_identical_refusals_record_exactly_one_audit_row(
    offline_world, device_admin, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger="apps.entry.services.offline_sync")
    device = _revoke(offline_world, device_admin)
    payload = _quarantine_payload(offline_world, _forged(offline_world, 3))
    clients = 6
    entered, release = threading.Event(), threading.Event()
    holders, finished = [], []
    real_audit = offline_sync.audit

    def audit_inside_the_critical_section(**kwargs):
        # Probe P9's hold, inverted: the one lock holder waits between its
        # existence check and its insert until every other request returned.
        if kwargs.get("action_code") == action_codes.OFFLINE_SYNC_REFUSED:
            holders.append(1)
            entered.set()
            assert release.wait(timeout=30)
        return real_audit(**kwargs)

    monkeypatch.setattr(offline_sync, "audit", audit_inside_the_critical_section)
    barrier = threading.Barrier(clients)

    @_closing
    def refuse():
        barrier.wait(timeout=15)
        code = _refused(payload)
        finished.append(code)
        return code

    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(refuse) for _ in range(clients)]
        assert entered.wait(timeout=30)
        deadline = time.monotonic() + 30
        while len(finished) < clients - 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        returned_while_held = len(finished)
        release.set()
        codes = [future.result(timeout=60) for future in futures]
    assert returned_while_held == clients - 1  # nobody queued on the lock
    assert set(codes) == {"QUARANTINE_REFUSED"}  # refusal behaviour unchanged
    assert len(holders) == 1
    assert [row.reason_code for row in _refusals(device)] == ["INVALID_SIGNATURE"]
    assert _warnings(caplog) == 1  # one WARNING line per recorded refusal only
    assert not SyncOperation.objects.exists() and _batches_received(device) == 0


def test_refusal_audits_are_isolated_per_device_and_reason_in_a_concurrent_burst(
    offline_world, device_admin, event, layout, caplog
):
    from apps.entry.services.devices import revoke_device

    caplog.set_level(logging.WARNING, logger="apps.entry.services.offline_sync")
    device = _revoke(offline_world, device_admin)
    other, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    revoke_device(
        device=other, actor=device_admin, expected_version=other.version, reason_code="LOST"
    )
    store = offline_world.store.store_id
    payloads = {
        (device, "INVALID_SIGNATURE"): _quarantine_payload(
            offline_world, _forged(offline_world, 2)
        ),
        (device, "MALFORMED_BATCH"): {
            "device": device.public_id,
            "store": "short",
            "operations": [],
        },
        (other, "INVALID_SIGNATURE"): {
            "device": other.public_id,
            "store": store,
            "operations": _forged(offline_world, 2),
        },
    }
    keys = {offline_sync._refusal_lock_key(target, code) for target, code in payloads}
    assert len(keys) == len(payloads)  # three distinct advisory-lock keys
    callables = [
        (lambda payload=payload: _refused(payload))
        for payload in payloads.values()
        for _ in range(6)
    ]
    results, errors = _run_in_threads(callables)
    assert not errors, errors
    assert set(results) == {"QUARANTINE_REFUSED", "MALFORMED_BATCH"}
    assert [row.reason_code for row in _refusals(device)] == [
        "INVALID_SIGNATURE",
        "MALFORMED_BATCH",
    ]
    assert [row.reason_code for row in _refusals(other)] == ["INVALID_SIGNATURE"]
    assert _warnings(caplog) == 3


def test_the_refusal_window_rolls_over_with_exactly_one_new_row(
    offline_world, device_admin, settings, monkeypatch
):
    device = _revoke(offline_world, device_admin)
    payload = _quarantine_payload(offline_world, _forged(offline_world, 2))
    window = settings.ENTRY_OFFLINE_SYNC_WINDOW_SECONDS
    start = timezone.now().replace(microsecond=0)
    moments = (start, start + timedelta(seconds=window - 1), start + timedelta(seconds=window + 1))
    for moment, expected in zip(moments, (1, 1, 2), strict=True):
        monkeypatch.setattr(timezone, "now", lambda moment=moment: moment)
        results, errors = _run_in_threads([lambda: _refused(payload)] * 6)
        assert not errors, errors
        assert set(results) == {"QUARANTINE_REFUSED"}
        assert len(_refusals(device)) == expected
    assert sorted(row.occurred_at for row in _refusals(device)) == [moments[0], moments[2]]


def test_a_refusal_that_finds_the_lock_held_returns_without_waiting(offline_world, device_admin):
    device = _revoke(offline_world, device_admin)
    payload = _quarantine_payload(offline_world, _forged(offline_world, 2))
    classid, objid = offline_sync._refusal_lock_key(device, "INVALID_SIGNATURE")
    outcome = {}

    @_closing
    def refuse():
        outcome["code"] = _refused(payload)

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [classid, objid])
        worker = threading.Thread(target=refuse)
        worker.start()
        worker.join(timeout=20)
        # It finished while this transaction still holds the lock: it never
        # queued, it was refused as before, and it wrote nothing.
        assert not worker.is_alive()
        assert outcome["code"] == "QUARANTINE_REFUSED"
        assert not _refusals(device)
    # Once the lock is free, the next refusal is recorded -- once.
    assert _refused(payload) == "QUARANTINE_REFUSED"
    assert _refused(payload) == "QUARANTINE_REFUSED"
    assert [row.reason_code for row in _refusals(device)] == ["INVALID_SIGNATURE"]


def test_genuine_quarantine_evidence_and_authenticated_audits_stay_correct(
    offline_world, device_admin
):
    # Authenticated refusals keep one audit row per request, also concurrently.
    first, second = _admit(offline_world), _admit(offline_world)
    unordered = [offline_world.store.envelope(second), offline_world.store.envelope(first)]

    def authenticated_refusal():
        try:
            offline_factories.sync(offline_world.synthetic, offline_world.store, unordered)
        except OfflineRequestRejected as exc:
            return exc.code
        return "ACCEPTED"

    results, errors = _run_in_threads([authenticated_refusal] * 4)
    assert not errors, errors
    assert results == ["UNORDERED_BATCH"] * 4
    assert [row.reason_code for row in _refusals(offline_world.device)] == ["UNORDERED_BATCH"] * 4
    # Revoked: a genuine quarantine upload races a flood of forged ones.
    device = _revoke(offline_world, device_admin)
    genuine = _quarantine_payload(
        offline_world, [offline_world.store.envelope(_admit(offline_world))]
    )
    forged = _quarantine_payload(offline_world, _forged(offline_world, 2))

    def quarantine(payload):
        try:
            return offline_sync.synchronize_quarantine(payload=payload)
        except OfflineRequestRejected as exc:
            return exc.code

    results, errors = _run_in_threads(
        [lambda: quarantine(genuine)] + [lambda: quarantine(forged)] * 6
    )
    assert not errors, errors
    acknowledgements = results[0].acknowledgements
    assert [ack.status for ack in acknowledgements] == ["QUARANTINED"]
    assert acknowledgements[0].durable
    assert set(results[1:]) == {"QUARANTINE_REFUSED"}
    assert SyncOperation.objects.filter(status="QUARANTINED").count() == 1
    assert _batches_received(device) == 1 and not EntryEvent.objects.exists()
    reasons = [row.reason_code for row in _refusals(device)]
    assert reasons.count("INVALID_SIGNATURE") == 1  # the whole forged flood
    assert reasons.count("UNORDERED_BATCH") == 4  # the authenticated requests
