"""Conflict classification of synchronized offline admissions (Phase 4 Prompt
3, ADR-0024, Flow §11.8-§11.9, approved plan §6 mapping table).

Every conflict keeps the physical fact (an offline Entry Event marked
`offline_conflict`, or -- for what the device must never have done -- no
event at all), what the device knew and what the server knew, and an open
reconciliation case. Nothing is silently turned into a normal admission.
PostgreSQL; all data is synthetic.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import PassReasonCode
from apps.badges.services import new_operation_id, replace_pass, revoke_pass, suspend_pass
from apps.entry.models import (
    EntryEvent,
    EntryOverride,
    EntryOverrideReason,
    ReconciliationCase,
    SyncOperation,
)
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db


def _op(world, *, grant=None, credential=None, **overrides):
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=grant or world.grant,
        credential=credential or world.credential,
        occurred_at=overrides.pop("occurred_at", timezone.now() + timedelta(seconds=2)),
        **overrides,
    )
    return world.store.next(op)


def _sync(world, op, *, note=None, now=None):
    envelope = world.store.envelope(op, note=note)
    result = offline_factories.sync(world.synthetic, world.store, [envelope], now=now)
    ack = result.acknowledgements[0]
    assert ack.durable
    return ack, SyncOperation.objects.get(operation_id=op["operation_id"])


def _case(row):
    return ReconciliationCase.objects.get(sync_operation=row, case_type=row.conflict_type)


#: Conflicts where the server's own evaluation of the pass refuses it (the
#: others concern the device's authority, or a second admission).
_SERVER_REFUSES = frozenset(
    {"PASS_REVOKED", "PASS_SUSPENDED", "PASS_REPLACED", "RESTRICTION_ADDED", "ACCESS_CHANGED"}
)


def _assert_conflicted_admission(world, ack, row, *, status, case_type):
    assert (ack.status, ack.conflict_type) == (status, case_type)
    event = EntryEvent.objects.get(sync_operation=row)
    # The physical fact is kept, marked, never presented as a normal admission.
    assert event.decision == "ADMIT" and event.offline and event.offline_conflict
    assert event.result == "ALLOWED"  # what the device knew
    case = _case(row)
    assert case.status == "OPEN" and case.entry_event == event
    assert case.registration == world.credential.registration
    assert case.device_known_state["local"]["result"] == "ALLOWED"
    if case_type in _SERVER_REFUSES:
        assert case.server_known_state["result"] != "ALLOWED"
    assert case.operational_fact["decision"] == "ADMIT"
    assert case.gate == world.package.scope.gate
    return event, case


# ---------------------------------------------------------------------------
# The pass, as it stood at the admission
# ---------------------------------------------------------------------------


def test_pass_revoked_after_the_last_sync_is_a_conflict(offline_world, staff):
    credential = offline_world.credential
    revoke_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _event, case = _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="PASS_REVOKED"
    )
    server = case.server_known_state
    assert server["pass_status_at_occurrence"] == "REVOKED"  # noqa: S105 - lifecycle status
    # Revoked after (or within the same second as) the device's last data.
    assert server["pass_status_changed_at"] >= server["knowledge_at"]


def test_pass_revoked_only_after_the_admission_stays_applied(offline_world, staff):
    op = _op(offline_world, occurred_at=timezone.now())
    offline_world.credential.refresh_from_db()
    credential = offline_world.credential
    # A package built before the admission already held the pass as ACTIVE;
    # revoke it only now, after the admission physically happened.
    import time

    time.sleep(1.1)
    revoke_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    ack, row = _sync(offline_world, op)
    assert (ack.status, ack.conflict_type) == ("APPLIED", "")
    assert row.server_evaluation["pass_status_now"] == "REVOKED"  # noqa: S105 - lifecycle status
    assert row.server_evaluation["pass_status_at_occurrence"] == "ACTIVE"  # noqa: S105
    assert not EntryEvent.objects.get().offline_conflict


def test_pass_suspended_is_a_conflict(offline_world, staff):
    credential = offline_world.credential
    suspend_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="PASS_SUSPENDED"
    )


def test_pass_replaced_before_the_admission_requires_reconciliation(offline_world, staff):
    credential = offline_world.credential
    replace_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _assert_conflicted_admission(
        offline_world, ack, row, status="RECONCILIATION_REQUIRED", case_type="PASS_REPLACED"
    )


def test_a_non_admission_of_a_revoked_pass_is_simply_applied(offline_world, staff):
    credential = offline_world.credential
    revoke_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    op = _op(
        offline_world,
        decision="DO_NOT_ADMIT",
        decision_reason="FOLLOWS_RESULT",
        local={
            "code": "REVOKED",
            "result": "DENIED",
            "reason": "PASS_REVOKED",
            "blockers": ["PASS_REVOKED"],
            "advisories": [],
            "latency_ms": 5,
        },
    )
    ack, row = _sync(offline_world, op)
    assert (ack.status, ack.conflict_type) == ("APPLIED", "")
    event = EntryEvent.objects.get()
    assert event.result == "DENIED" and event.reason_code == "PASS_REVOKED"
    assert not ReconciliationCase.objects.exists()


# ---------------------------------------------------------------------------
# Changed access rule or restriction discovered by the server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["DENY_ENTRY", "MANUAL_REVIEW"])
def test_a_restriction_found_at_synchronization_is_a_conflict(offline_world, event, severity):
    from apps.entry.models import SecurityRestriction

    SecurityRestriction.objects.create(
        registration=offline_world.credential.registration,
        event_edition=event,
        severity=severity,
        category="SECURITY_CONCERN",
        starts_at=timezone.now() - timedelta(minutes=1),
        reason_encrypted="synthetic reason",
        created_by=offline_world.operator,
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _event, case = _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="RESTRICTION_ADDED"
    )
    assert "synthetic reason" not in str(case.server_known_state)


def test_an_access_change_before_the_admission_is_a_conflict(offline_world):
    from apps.accreditation.models import AccessProfileAssignment

    AccessProfileAssignment.objects.filter(
        registration=offline_world.credential.registration
    ).update(status="REVOKED", effective_until=timezone.now() - timedelta(seconds=1))
    ack, row = _sync(offline_world, _op(offline_world))
    _event, case = _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="ACCESS_CHANGED"
    )
    assert "NO_ACCESS_ASSIGNMENT" in case.server_known_state["blockers"]


def test_a_single_entry_rule_makes_a_second_admission_a_duplicate_entry(offline_world, setup):
    setup.access_profile.reentry_policy = "SINGLE_ENTRY"
    setup.access_profile.save(update_fields=["reentry_policy"])
    # The device must have actually received this rule in its package.
    from apps.entry.services.offline_packages import request_package_build
    from apps.entry.tests import offline_factories

    request_package_build(
        offline_world.device,
        reason="RULE_CHANGED",
        seen_version=offline_world.package.package_version,
    )
    offline_factories.run_pending_builds()
    offline_world.package, _raw = offline_factories.download(offline_world.synthetic)
    first_ack, first = _sync(offline_world, _op(offline_world))
    assert first_ack.status == "APPLIED"
    ack, row = _sync(
        offline_world, _op(offline_world, occurred_at=timezone.now() + timedelta(seconds=30))
    )
    _event, case = _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="DUPLICATE_ENTRY"
    )
    assert case.related_entry_event == EntryEvent.objects.get(sync_operation=first)


def test_close_admissions_on_two_devices_raise_an_advisory(
    offline_world, event, layout, device_admin, supervisor
):
    from apps.entry.services.decisions import pending_from_assessment, record_entry_decision
    from apps.entry.services.verification import verify_qr

    other_device, _secret = factories.enroll_device(event=event, layout=layout, admin=device_admin)
    checkpoint = factories.open_checkpoint(device=other_device, user=supervisor, zone=layout.main)
    outcome = verify_qr(
        checkpoint=checkpoint, raw_token=factories.token_for(offline_world.credential)
    )
    pending = pending_from_assessment(
        assessment=outcome.assessment, checkpoint=checkpoint, method="QR"
    )
    online = record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
    ack, row = _sync(offline_world, _op(offline_world))
    assert (ack.status, ack.conflict_type) == ("APPLIED", "DUPLICATE_ADMISSION_ADVISORY")
    event_row = EntryEvent.objects.get(sync_operation=row)
    assert not event_row.offline_conflict  # an advisory, not a conflict
    case = _case(row)
    assert case.severity == "ADVISORY" and case.related_entry_event == online.entry_event


# ---------------------------------------------------------------------------
# What the device must never have done: no Entry Event at all
# ---------------------------------------------------------------------------


def _assert_security_conflict(ack, row, case_type):
    assert (ack.status, ack.conflict_type) == ("SECURITY_CONFLICT", case_type)
    assert not EntryEvent.objects.filter(sync_operation=row).exists()
    assert not EntryOverride.objects.exists()
    case = _case(row)
    assert case.severity == "SECURITY" and case.entry_event is None
    return case


def test_an_admission_on_stale_data_is_a_security_conflict(offline_world, monkeypatch):
    package = offline_world.package
    at = package.stale_at.replace(microsecond=0) + timedelta(seconds=1)
    # Standard package staleness and the original grant both occur near two
    # hours. Renew online before that boundary to isolate package staleness.
    renewed_at = package.stale_at - timedelta(minutes=10)
    with monkeypatch.context() as clock:
        clock.setattr(timezone, "now", lambda: renewed_at)
        grant, _compact = offline_factories.issue_grant(
            offline_world.synthetic, checkpoint=offline_world.checkpoint
        )
        assert grant.issued_at <= at < grant.expires_at
        assert package.stale_at <= at < package.expires_at
        clock.setattr(timezone, "now", lambda: at)
        ack, row = _sync(offline_world, _op(offline_world, grant=grant, occurred_at=at), now=at)
    case = _assert_security_conflict(ack, row, "PACKAGE_STALE")
    assert row.outcome_code == "ADMITTED_ON_STALE_DATA"
    assert case.server_known_state["band_at_occurrence"] == "STALE"


@pytest.mark.parametrize("decision", ["ADMIT", "DO_NOT_ADMIT"])
def test_stale_data_with_an_expired_grant_preserves_the_grant_rejection(
    offline_world, monkeypatch, decision
):
    package, grant = offline_world.package, offline_world.grant
    at = max(package.stale_at, grant.expires_at).replace(microsecond=0) + timedelta(seconds=1)
    assert package.stale_at <= at < package.expires_at
    assert at >= grant.expires_at
    with monkeypatch.context() as clock:
        clock.setattr(timezone, "now", lambda: at)
        op = _op(
            offline_world,
            occurred_at=at,
            decision=decision,
            decision_reason="FOLLOWS_RESULT" if decision == "DO_NOT_ADMIT" else "",
        )
        ack, row = _sync(offline_world, op, now=at)
    case = _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "GRANT_NOT_VALID"
    assert case.server_known_state["band_at_occurrence"] == "STALE"
    assert case.server_known_state["grant_expires_at"] == int(grant.expires_at.timestamp())


def test_an_admission_on_expired_data_is_a_security_conflict(offline_world):
    at = offline_world.package.expires_at + timedelta(seconds=1)
    ack, row = _sync(offline_world, _op(offline_world, occurred_at=at), now=at)
    _assert_security_conflict(ack, row, "PACKAGE_EXPIRED")


def test_expired_data_cannot_record_a_resolved_denial(offline_world):
    at = offline_world.package.expires_at + timedelta(seconds=1)
    ack, row = _sync(
        offline_world,
        _op(
            offline_world, occurred_at=at, decision="DO_NOT_ADMIT", decision_reason="FOLLOWS_RESULT"
        ),
        now=at,
    )
    _assert_security_conflict(ack, row, "PACKAGE_EXPIRED")


def test_admission_cannot_ignore_a_known_rebuild_delta(offline_world, layout):
    from apps.entry.tests.test_offline_packages import _delta

    layout.gate_a.name = "Synthetic changed checkpoint"
    layout.gate_a.save(update_fields=["name"])
    delta, _raw = _delta(offline_world.synthetic, offline_world.package)
    assert delta.rebuild_required
    ack, row = _sync(offline_world, _op(offline_world, delta=delta))
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "DELTA_REQUIRES_REBUILD"


def test_an_admission_recorded_in_the_stale_state_is_a_policy_violation(offline_world):
    ack, row = _sync(offline_world, _op(offline_world, state="STALE", band="STALE"))
    case = _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "STATE_FORBIDS_ADMISSION"
    assert case.device_known_state["state"] == "STALE"


def test_admitting_a_non_admittable_local_result_is_a_policy_violation(offline_world):
    op = _op(
        offline_world,
        local={
            "code": "VALID",
            "result": "DENIED",
            "reason": "WRONG_ZONE",
            "blockers": ["WRONG_ZONE"],
            "advisories": [],
            "latency_ms": 5,
        },
    )
    ack, row = _sync(offline_world, op)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "LOCAL_RESULT_NOT_ADMITTABLE"


def test_an_admission_after_the_grant_expired_is_a_policy_violation(
    offline_world, operator, layout
):
    session = offline_world.checkpoint.operator_session
    type(session).objects.filter(pk=session.pk).update(
        expires_at=timezone.now() + timedelta(minutes=10)
    )
    checkpoint = factories.open_checkpoint(
        device=offline_world.device, user=operator, zone=layout.main
    )
    type(checkpoint.operator_session).objects.filter(pk=checkpoint.operator_session.pk).update(
        expires_at=timezone.now() + timedelta(minutes=10)
    )
    checkpoint.operator_session.refresh_from_db()
    grant, _ = offline_factories.issue_grant(offline_world.synthetic, checkpoint=checkpoint)
    at = grant.expires_at + timedelta(seconds=5)
    ack, row = _sync(offline_world, _op(offline_world, grant=grant, occurred_at=at), now=at)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "GRANT_NOT_VALID"


# ---------------------------------------------------------------------------
# Authority withdrawn while the device was disconnected
# ---------------------------------------------------------------------------


def test_an_admission_after_the_operator_authorization_ended_is_a_conflict(offline_world):
    from apps.entry.services.sessions import end_operator_session

    end_operator_session(
        operator_session=offline_world.checkpoint.operator_session, reason="SIGNED_OUT"
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="AUTHORIZATION_WITHDRAWN"
    )


def test_an_admission_after_offline_use_was_blocked_is_a_conflict(offline_world, device_admin):
    from apps.entry.services.offline_devices import block_offline_use

    device = offline_world.device
    device.refresh_from_db()
    block_offline_use(
        device=device, actor=device_admin, expected_version=device.version, reason_code="LOST"
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="PACKAGE_UNUSABLE"
    )


def test_an_admission_after_a_rescope_is_a_wrong_scope_conflict(
    offline_world, device_admin, layout
):
    device = offline_world.device
    device.refresh_from_db()
    offline_factories.make_offline_capable(
        device, admin=device_admin, layout=layout, zones=[layout.main]
    )
    ack, row = _sync(offline_world, _op(offline_world))
    _assert_conflicted_admission(
        offline_world, ack, row, status="CONFLICT", case_type="WRONG_SCOPE"
    )


def test_a_pass_of_another_event_is_a_security_conflict(offline_world, other_event, staff):
    other_layout = factories.VenueLayout(other_event, code="V2")
    other_setup = factories.AccreditationSetup(other_event, other_layout, suffix="X")
    registration = factories.make_registration(event=other_event, person=factories.make_person())
    factories.assign(registration=registration, setup=other_setup, actor=staff)
    foreign = factories.issue_active_pass(registration=registration, actor=staff)
    ack, row = _sync(offline_world, _op(offline_world, credential=foreign))
    case = _assert_security_conflict(ack, row, "WRONG_EVENT")
    assert row.registration_id is None and row.digital_entry_pass_id is None
    assert case.registration_id is None


def test_later_rule_relaxation_requires_explicit_reconciliation(offline_world, setup):
    setup.access_profile.is_active = False
    setup.access_profile.save(update_fields=["is_active"])
    op = _op(offline_world)
    setup.access_profile.is_active = True
    setup.access_profile.save(update_fields=["is_active"])
    ack, row = _sync(offline_world, op)
    assert ack.status == "CONFLICT" and ack.conflict_type == "ACCESS_CHANGED"
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    assert _case(row).server_known_state["historical_access_uncertain"]


def test_a_revoked_qr_key_is_not_silently_accepted(offline_world):
    from apps.badges.models import VerificationKey

    VerificationKey.objects.filter(key_id=offline_world.credential.signing_key_id).update(
        status="REVOKED",
        revoked_at=timezone.now() - timedelta(seconds=1),
        revocation_reason_code="COMPROMISED",
    )
    ack, row = _sync(offline_world, _op(offline_world))
    assert ack.status == "CONFLICT" and ack.conflict_type == "PACKAGE_UNUSABLE"
    assert row.outcome_code == "QR_SIGNING_KEY_UNUSABLE"


# ---------------------------------------------------------------------------
# Offline overrides (grant-bound, catalogue-bound)
# ---------------------------------------------------------------------------


@pytest.fixture
def supervisor_grant(offline_world, supervisor, layout):
    checkpoint = factories.open_checkpoint(
        device=offline_world.device, user=supervisor, zone=layout.main
    )
    grant, _ = offline_factories.issue_grant(offline_world.synthetic, checkpoint=checkpoint)
    assert grant.may_override
    return grant


@pytest.fixture
def override_reason(event):
    return EntryOverrideReason.objects.create(
        event_edition=event,
        code="SUPERVISOR_CHECKED",
        name="Supervisor checked the participant",
        overridable_reason_codes=["OUTSIDE_TIME_WINDOW"],
        requires_note=True,
    )


def _override_op(world, grant, reason, note):
    digest_input = None
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=grant,
        credential=world.credential,
        occurred_at=timezone.now() + timedelta(seconds=2),
        local={
            "code": "VALID",
            "result": "DENIED",
            "reason": "OUTSIDE_TIME_WINDOW",
            "blockers": ["OUTSIDE_TIME_WINDOW"],
            "advisories": [],
            "latency_ms": 7,
        },
        override={"code": reason.code, "note_digest": ""},
    )
    digest_input = f"{op['operation_id']}\n{note}".encode()
    op["override"]["note_digest"] = hashlib.sha256(digest_input).hexdigest()
    return world.store.next(op)


def test_a_permitted_offline_override_records_override_and_event(
    offline_world, supervisor_grant, override_reason
):
    from apps.entry.offline_contract import TYP_PACKAGE_MANIFEST
    from apps.entry.services.offline_journal import journal_changes
    from apps.entry.services.offline_packages import request_package_build

    # The reason fixture is created after the initial package. A permitted
    # override must use a package that actually contains its approved reason.
    assert "OVERRIDE_CATALOGUE_CHANGED" in journal_changes(offline_world.package).rebuild_reasons
    previous_version = offline_world.package.package_version
    request_package_build(
        offline_world.device,
        reason="OVERRIDE_CATALOGUE_CHANGED",
        seen_version=previous_version,
    )
    offline_factories.run_pending_builds()
    offline_world.package, raw = offline_factories.download(offline_world.synthetic)
    assert offline_world.package.package_version > previous_version
    _manifest, body = offline_world.synthetic.open_response(
        raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG"
    )
    reason = next(item for item in body["override_reasons"] if item["code"] == override_reason.code)
    assert reason["overridable_reason_codes"] == ["OUTSIDE_TIME_WINDOW"]
    assert reason["requires_note"] is True
    assert not journal_changes(offline_world.package).rebuild_reasons

    note = "Checked the accreditation letter"
    op = _override_op(offline_world, supervisor_grant, override_reason, note)
    ack, row = _sync(offline_world, op, note=note)
    assert ack.status == "APPLIED"
    override = EntryOverride.objects.get()
    assert override.offline and override.sync_operation == row
    assert override.original_result == "DENIED" and override.note_encrypted == note
    event = EntryEvent.objects.get()
    assert event.override == override and event.decision == "ADMIT"
    assert event.offline and not event.offline_conflict
    assert not ReconciliationCase.objects.filter(sync_operation=row).exists()
    # The note never enters the stored operation body in clear.
    assert note not in str(row.payload_json)
    assert row.note_encrypted == note


def test_override_catalogue_change_after_package_requires_reconciliation(
    offline_world, supervisor_grant, override_reason
):
    from apps.entry.services.offline_journal import journal_changes

    assert "OVERRIDE_CATALOGUE_CHANGED" in journal_changes(offline_world.package).rebuild_reasons
    note = "Synthetic override with an outdated catalogue"
    op = _override_op(offline_world, supervisor_grant, override_reason, note)
    ack, row = _sync(offline_world, op, note=note)

    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    event = EntryEvent.objects.get(sync_operation=row)
    override = EntryOverride.objects.get(sync_operation=row)
    assert event.offline and event.offline_conflict and event.decision == "ADMIT"
    assert event.override == override and override.offline
    case = _case(row)
    assert case.status == "OPEN" and case.entry_event == event
    assert case.server_known_state["historical_access_uncertain"] is True
    # Correction 4 (R-01): the reason was added after the package, so the
    # server cannot know it from that package's catalogue.
    assert case.server_known_state["override_catalogue_uncertain"] is True
    assert case.device_known_state["local"]["result"] == "DENIED"
    assert row.payload_json["package"]["id"] == offline_world.package.public_id
    assert note not in str(row.payload_json)
    assert row.note_encrypted == override.note_encrypted == note


def test_an_override_note_that_does_not_match_its_digest_is_refused(
    offline_world, supervisor_grant, override_reason
):
    op = _override_op(offline_world, supervisor_grant, override_reason, "original note")
    ack, row = _sync(offline_world, op, note="a different note")
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"


def test_an_override_under_an_operator_grant_is_refused(offline_world, override_reason):
    note = "note"
    op = _override_op(offline_world, offline_world.grant, override_reason, note)
    ack, row = _sync(offline_world, op, note=note)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")


def test_an_override_despite_a_non_overrideable_restriction_is_a_security_conflict(
    offline_world, supervisor_grant, override_reason, event
):
    from apps.entry.models import SecurityRestriction

    SecurityRestriction.objects.create(
        registration=offline_world.credential.registration,
        event_edition=event,
        severity="MANUAL_REVIEW",
        category="IDENTITY_DISPUTE",
        is_overrideable=False,
        starts_at=timezone.now() - timedelta(minutes=1),
        reason_encrypted="synthetic",
        created_by=offline_world.operator,
    )
    note = "note"
    op = _override_op(offline_world, supervisor_grant, override_reason, note)
    ack, row = _sync(offline_world, op, note=note)
    _assert_security_conflict(ack, row, "NON_OVERRIDEABLE_RESTRICTION")


# ---------------------------------------------------------------------------
# Override catalogue revalidation (Prompt 3 correction 4, R-01): an offline
# override is bound to the catalogue of the device's OWN package
# ---------------------------------------------------------------------------

_BLOCKER = "OUTSIDE_TIME_WINDOW"


def _catalogue_reason(event, code="SYNTHETIC_OVERRIDE", **fields):
    values = {
        "name": "Synthetic override reason",
        "overridable_reason_codes": [_BLOCKER, "WRONG_ZONE"],
        "requires_note": True,
        "is_active": True,
        **fields,
    }
    return EntryOverrideReason.objects.create(event_edition=event, code=code, **values)


def _rebuilt_catalogue(world) -> dict:
    """Build and download a NEW package through the production lifecycle, as
    the device would, and return the override catalogue it actually carries."""
    from apps.entry.offline_contract import TYP_PACKAGE_MANIFEST
    from apps.entry.services.offline_journal import journal_changes
    from apps.entry.services.offline_packages import request_package_build

    previous = world.package.package_version
    request_package_build(world.device, reason="OVERRIDE_CATALOGUE_CHANGED", seen_version=previous)
    offline_factories.run_pending_builds()
    world.package, raw = offline_factories.download(world.synthetic)
    assert world.package.package_version > previous
    assert not journal_changes(world.package).rebuild_reasons
    _manifest, body = world.synthetic.open_response(raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG")
    return {item["code"]: item for item in body["override_reasons"]}


def _catalogue_op(world, grant, code, *, note="", blockers=(_BLOCKER,), result="DENIED", **fields):
    """An override exactly as the runtime's `commitDecision` records it: the
    note digest is set only when there is a note."""
    edit = fields.pop("edit", None)
    op = offline_factories.base_operation(
        device=world.device,
        package=world.package,
        grant=grant,
        credential=world.credential,
        occurred_at=fields.pop("occurred_at", timezone.now() + timedelta(seconds=2)),
        local={
            "code": "VALID",
            "result": result,
            "reason": blockers[0] if blockers else "",
            "blockers": list(blockers),
            "advisories": [],
            "latency_ms": 7,
        },
        override={"code": code, "note_digest": ""},
        **fields,
    )
    if note:
        digest = hashlib.sha256(f"{op['operation_id']}\n{note}".encode()).hexdigest()
        op["override"]["note_digest"] = digest
    if edit is not None:
        edit(op)
    return world.store.next(op)


def _assert_replayed(world, op, note, first):
    """A retry returns the original durable outcome and applies nothing new."""
    counts = (EntryEvent.objects.count(), EntryOverride.objects.count())
    cases = ReconciliationCase.objects.count()
    again, row = _sync(world, op, note=note or None)
    assert again.replayed and again.durable
    assert (again.status, again.outcome, again.conflict_type) == (
        first.status,
        first.outcome,
        first.conflict_type,
    )
    assert row.duplicate_submissions == 1
    assert (EntryEvent.objects.count(), EntryOverride.objects.count()) == counts
    assert ReconciliationCase.objects.count() == cases


def test_an_unchanged_permitted_override_is_applied_from_its_package_catalogue(
    offline_world, supervisor_grant, event
):
    reason = _catalogue_reason(event, requires_note=False)
    catalogue = _rebuilt_catalogue(offline_world)
    assert catalogue[reason.code]["requires_note"] is False
    op = _catalogue_op(offline_world, supervisor_grant, reason.code)  # no note needed
    ack, row = _sync(offline_world, op)
    assert (ack.status, ack.outcome, ack.conflict_type) == ("APPLIED", "ADMISSION_APPLIED", "")
    override = EntryOverride.objects.get(sync_operation=row)
    assert override.reason_id == reason.pk and override.note_encrypted == ""
    event_row = EntryEvent.objects.get(sync_operation=row)
    assert event_row.override == override and not event_row.offline_conflict
    assert "override_catalogue_uncertain" not in row.server_evaluation
    assert not ReconciliationCase.objects.filter(sync_operation=row).exists()
    _assert_replayed(offline_world, op, "", ack)


def test_a_reason_inactive_when_the_package_was_built_is_never_applied(
    offline_world, supervisor_grant, event
):
    # Before correction 4 this override was APPLIED as a clean admission.
    reason = _catalogue_reason(event, is_active=False)
    catalogue = _rebuilt_catalogue(offline_world)
    assert reason.code not in catalogue  # the package never offered it
    note = "Synthetic note for a disabled reason"
    op = _catalogue_op(offline_world, supervisor_grant, reason.code, note=note)
    ack, row = _sync(offline_world, op, note=note)
    case = _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"
    assert case.status == "OPEN" and case.device_known_state["override"] == reason.code
    assert "override_catalogue_uncertain" not in row.server_evaluation
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_SECURITY_CONFLICT)
    assert audit.reason_code == "OVERRIDE_NOT_PERMITTED" and audit.result == "DENIED"
    _assert_replayed(offline_world, op, note, ack)
    assert not EntryEvent.objects.exists() and not EntryOverride.objects.exists()


@pytest.mark.parametrize("absence", ["never_configured", "configured_for_another_event"])
def test_a_reason_absent_from_the_event_catalogue_is_a_policy_violation(
    offline_world, supervisor_grant, other_event, absence
):
    code = "SYNTHETIC_ABSENT"
    if absence == "configured_for_another_event":
        _catalogue_reason(other_event, code=code)  # tenant isolation: never ours
    assert code not in _rebuilt_catalogue(offline_world)
    note = "Synthetic note"
    op = _catalogue_op(offline_world, supervisor_grant, code, note=note)
    ack, row = _sync(offline_world, op, note=note)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"
    _assert_replayed(offline_world, op, note, ack)


def test_an_unrelated_catalogue_change_cannot_mask_a_reason_the_package_never_had(
    offline_world, supervisor_grant, event
):
    from apps.entry.services.offline_journal import journal_changes

    disabled = _catalogue_reason(event, code="SYNTHETIC_DISABLED", is_active=False)
    other = _catalogue_reason(event, code="SYNTHETIC_OTHER")
    catalogue = _rebuilt_catalogue(offline_world)
    assert disabled.code not in catalogue and other.code in catalogue
    EntryOverrideReason.objects.filter(pk=other.pk).update(name="Synthetic renamed reason")
    assert journal_changes(offline_world.package).override_reason_ids == {str(other.pk)}
    note = "Synthetic note"
    op = _catalogue_op(offline_world, supervisor_grant, disabled.code, note=note)
    ack, row = _sync(offline_world, op, note=note)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"


#: (state when the package is built, change after its snapshot, the note a
#: conforming device records under that package).
_TIGHTENED_AFTER_SNAPSHOT = {
    "codes_narrowed": ({}, {"overridable_reason_codes": ["WRONG_ZONE"]}, "Synthetic note"),
    "note_requirement_tightened": ({"requires_note": False}, {"requires_note": True}, ""),
    "deactivated": ({}, {"is_active": False}, "Synthetic note"),
}


@pytest.mark.parametrize("change", sorted(_TIGHTENED_AFTER_SNAPSHOT))
def test_a_catalogue_tightened_after_the_package_is_a_changed_rule_conflict(
    offline_world, supervisor_grant, event, change
):
    from apps.entry.services.offline_journal import journal_changes

    initial, later, note = _TIGHTENED_AFTER_SNAPSHOT[change]
    reason = _catalogue_reason(event, **initial)
    packaged = _rebuilt_catalogue(offline_world)[reason.code]
    assert _BLOCKER in packaged["overridable_reason_codes"]
    assert packaged["requires_note"] is reason.requires_note
    EntryOverrideReason.objects.filter(pk=reason.pk).update(**later)
    assert str(reason.pk) in journal_changes(offline_world.package).override_reason_ids
    # The device followed ITS package, which permitted exactly this override.
    op = _catalogue_op(offline_world, supervisor_grant, reason.code, note=note)
    ack, row = _sync(offline_world, op, note=note or None)
    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    # The physical admission is kept, marked and linked, never a normal one.
    event_row = EntryEvent.objects.get(sync_operation=row)
    override = EntryOverride.objects.get(sync_operation=row)
    assert event_row.decision == "ADMIT" and event_row.result == "DENIED"
    assert event_row.offline and event_row.offline_conflict and event_row.override == override
    assert override.offline and override.reason_id == reason.pk and override.note_encrypted == note
    case = _case(row)
    assert case.status == "OPEN" and case.severity == "CONFLICT" and case.entry_event == event_row
    assert case.server_known_state["override_catalogue_uncertain"] is True
    assert case.server_known_state["historical_access_uncertain"] is True
    assert case.device_known_state["override"] == reason.code
    assert not ReconciliationCase.objects.filter(severity="SECURITY").exists()
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_CONFLICT)
    assert audit.reason_code == "HISTORICAL_ACCESS_UNCERTAIN"
    _assert_replayed(offline_world, op, note, ack)


def test_a_reason_removed_after_the_package_keeps_the_admission_in_its_case(
    offline_world, supervisor_grant, event
):
    from apps.entry.models import VerificationSample
    from apps.entry.services.offline_journal import journal_changes

    reason = _catalogue_reason(event)
    assert reason.code in _rebuilt_catalogue(offline_world)
    removed = str(reason.pk)
    reason.delete()  # never used on the server, so the catalogue may drop it
    assert removed in journal_changes(offline_world.package).override_reason_ids
    note = "Synthetic note for a removed reason"
    op = _catalogue_op(offline_world, supervisor_grant, reason.code, note=note)
    ack, row = _sync(offline_world, op, note=note)
    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    # An Entry Override must name a catalogue reason (Schema §11.5), and an
    # admission on a denied result must name its override: the fact stays in
    # the immutable operation and in the open case, never as an admission.
    assert not EntryEvent.objects.exists() and not EntryOverride.objects.exists()
    assert row.result_reference is None and row.note_encrypted == note
    assert row.payload_json["override"]["code"] == reason.code
    case = _case(row)
    assert case.status == "OPEN" and case.severity == "CONFLICT" and case.entry_event is None
    assert case.registration == offline_world.credential.registration
    assert case.operational_fact["decision"] == "ADMIT"
    assert case.device_known_state["override"] == reason.code
    assert case.server_known_state["override_reason_unavailable"] is True
    assert case.server_known_state["override_catalogue_uncertain"] is True
    assert VerificationSample.objects.filter(mode="OFFLINE").count() == 1
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_CONFLICT)
    assert audit.target_type == "SyncOperation" and audit.target_uuid == row.pk
    _assert_replayed(offline_world, op, note, ack)


def _catalogue_made_uncertain(world, event):
    """A reason the package carried (including a never-overrideable code, a
    misconfiguration the service must still refuse), narrowed after the
    snapshot: its package state is now unknown to the server."""
    from apps.entry.services.offline_journal import journal_changes

    reason = _catalogue_reason(event, overridable_reason_codes=[_BLOCKER, "PASS_REVOKED"])
    assert reason.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=reason.pk).update(overridable_reason_codes=["WRONG_ZONE"])
    assert str(reason.pk) in journal_changes(world.package).override_reason_ids
    return reason


_INDEPENDENT_VIOLATIONS = {
    "operator_grant_cannot_override": ("POLICY_VIOLATION", "OVERRIDE_NOT_PERMITTED"),
    "note_digest_mismatch": ("POLICY_VIOLATION", "OVERRIDE_NOT_PERMITTED"),
    "never_overrideable_blocker": ("POLICY_VIOLATION", "OVERRIDE_NOT_PERMITTED"),
    "admittable_local_result": ("POLICY_VIOLATION", "OVERRIDE_NOT_PERMITTED"),
    "non_overrideable_restriction": (
        "NON_OVERRIDEABLE_RESTRICTION",
        "NON_OVERRIDEABLE_RESTRICTION",
    ),
    "recorded_while_stale": ("POLICY_VIOLATION", "STATE_FORBIDS_ADMISSION"),
    "known_rebuild_delta": ("POLICY_VIOLATION", "DELTA_REQUIRES_REBUILD"),
    "expired_grant": ("POLICY_VIOLATION", "GRANT_NOT_VALID"),
}


@pytest.mark.parametrize("violation", sorted(_INDEPENDENT_VIOLATIONS))
def test_catalogue_uncertainty_never_bypasses_an_independent_violation(
    offline_world, supervisor_grant, event, monkeypatch, violation
):
    from apps.entry.models import SecurityRestriction
    from apps.entry.tests.test_offline_packages import _delta

    reason = _catalogue_made_uncertain(offline_world, event)
    note = "Synthetic note"
    sent_note = note
    grant = supervisor_grant
    kwargs = {}
    if violation == "operator_grant_cannot_override":
        grant = offline_world.grant
    elif violation == "note_digest_mismatch":
        sent_note = "A different synthetic note"
    elif violation == "never_overrideable_blocker":
        kwargs["blockers"] = ("PASS_REVOKED",)
    elif violation == "admittable_local_result":
        kwargs.update(blockers=(), result="ALLOWED")
    elif violation == "non_overrideable_restriction":
        SecurityRestriction.objects.create(
            registration=offline_world.credential.registration,
            event_edition=event,
            severity="MANUAL_REVIEW",
            category="IDENTITY_DISPUTE",
            is_overrideable=False,
            starts_at=timezone.now() - timedelta(minutes=1),
            reason_encrypted="synthetic",
            created_by=offline_world.operator,
        )
    elif violation == "recorded_while_stale":
        kwargs.update(state="STALE", band="STALE")
    elif violation == "known_rebuild_delta":
        delta, _raw = _delta(offline_world.synthetic, offline_world.package)
        assert delta.rebuild_required  # it names OVERRIDE_CATALOGUE_CHANGED
        kwargs["delta"] = delta
    if violation == "expired_grant":
        at = grant.expires_at.replace(microsecond=0) + timedelta(seconds=5)
        assert at < offline_world.package.expires_at
        with monkeypatch.context() as clock:
            clock.setattr(timezone, "now", lambda: at)
            op = _catalogue_op(offline_world, grant, reason.code, note=note, occurred_at=at)
            ack, row = _sync(offline_world, op, note=sent_note, now=at)
    else:
        op = _catalogue_op(offline_world, grant, reason.code, note=note, **kwargs)
        ack, row = _sync(offline_world, op, note=sent_note)
    case_type, outcome = _INDEPENDENT_VIOLATIONS[violation]
    _assert_security_conflict(ack, row, case_type)
    assert row.outcome_code == outcome


_INTAKE_REFUSALS = {
    "forged_operation_signature": "INVALID_SIGNATURE",
    "another_device_binding": "WRONG_DEVICE",
    "unknown_package_binding": "UNKNOWN_PACKAGE",
    "unknown_grant": "UNKNOWN_GRANT",
    "malformed_override": "MALFORMED_OPERATION",
}


@pytest.mark.parametrize("refusal", sorted(_INTAKE_REFUSALS))
def test_catalogue_uncertainty_never_bypasses_intake_verification(
    offline_world, supervisor_grant, event, refusal
):
    from cryptography.hazmat.primitives.asymmetric import ec

    reason = _catalogue_made_uncertain(offline_world, event)
    note = "Synthetic note"
    edits = {
        "another_device_binding": lambda op: op.update(device="D" * 22),
        "unknown_package_binding": lambda op: op["package"].update(id="P" * 22),
        "unknown_grant": lambda op: op.update(grant="G" * 22),
        "malformed_override": lambda op: op["override"].update(note_digest="not-a-digest"),
    }
    op = _catalogue_op(
        offline_world, supervisor_grant, reason.code, note=note, edit=edits.get(refusal)
    )
    key = (
        ec.generate_private_key(ec.SECP256R1()) if refusal == "forged_operation_signature" else None
    )
    envelope = offline_world.store.envelope(op, note=note, key=key)
    result = offline_factories.sync(offline_world.synthetic, offline_world.store, [envelope])
    ack = result.acknowledgements[0]
    assert (ack.status, ack.outcome) == ("REJECTED", _INTAKE_REFUSALS[refusal]) and ack.durable
    row = SyncOperation.objects.get(operation_id=op["operation_id"])
    assert row.status == "REJECTED" and row.processed_at is not None
    assert not EntryEvent.objects.exists() and not EntryOverride.objects.exists()
    assert ReconciliationCase.objects.filter(sync_operation=row).exists()


# ---------------------------------------------------------------------------
# What the journal proves about the reason's entry in the device's package
# (Prompt 3 correction 5, developer decision D2, option A2). The journal keeps
# the operation kinds of every changed reason, never previous values. The
# watermark-to-projection window (probe P8) is covered with a real second
# connection in `test_offline_evidence_concurrency.py`.
# ---------------------------------------------------------------------------

_NOTE = "Synthetic note"

#: A reason inserted AFTER the package snapshot and never changed: its current
#: values are the only ones it ever had, so an override violating them is the
#: device's policy violation, whether or not the package carried the reason.
_INSERTED_AND_NONCONFORMING = {
    "inserted_inactive": ({"is_active": False}, _NOTE),
    "inserted_without_the_blocker": ({"overridable_reason_codes": ["WRONG_ZONE"]}, _NOTE),
    "inserted_requiring_a_note": ({"requires_note": True}, ""),
}


@pytest.mark.parametrize("variant", sorted(_INSERTED_AND_NONCONFORMING))
def test_an_override_violating_the_only_state_of_an_inserted_reason_is_a_policy_violation(
    offline_world, supervisor_grant, event, variant
):
    from apps.entry.services.offline_journal import journal_changes

    fields, note = _INSERTED_AND_NONCONFORMING[variant]
    reason = _catalogue_reason(event, **fields)  # inserted after the package snapshot
    changes = journal_changes(offline_world.package)
    assert changes.override_reason_operations == {str(reason.pk): {"I"}}
    op = _catalogue_op(offline_world, supervisor_grant, reason.code, note=note)
    ack, row = _sync(offline_world, op, note=note or None)
    # Before correction 5 this was CONFLICT / ACCESS_CHANGED, and the Entry
    # Override linked a reason whose only state never permitted it (P6).
    case = _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"
    assert row.server_evaluation["override_catalogue_evidence"] == "INSERTED_ONLY"
    assert "override_catalogue_uncertain" not in row.server_evaluation
    assert case.device_known_state["override"] == reason.code
    audit = AuditEvent.objects.get(action_code=action_codes.OFFLINE_SECURITY_CONFLICT)
    assert audit.reason_code == "OVERRIDE_NOT_PERMITTED"
    _assert_replayed(offline_world, op, note, ack)
    assert not EntryEvent.objects.exists() and not EntryOverride.objects.exists()


def test_a_conforming_override_on_an_inserted_reason_stays_a_changed_rule_conflict(
    offline_world, supervisor_grant, event
):
    # Conforming to the reason's only state, but the package may or may not
    # have carried it (the watermark-to-projection window): never a
    # violation, never a clean admission.
    reason = _catalogue_reason(event)
    op = _catalogue_op(offline_world, supervisor_grant, reason.code, note=_NOTE)
    ack, row = _sync(offline_world, op, note=_NOTE)
    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    assert row.server_evaluation["override_catalogue_evidence"] == "INSERTED_ONLY"
    assert row.server_evaluation["override_catalogue_uncertain"] is True
    event_row = EntryEvent.objects.get(sync_operation=row)
    assert event_row.offline_conflict and event_row.override.reason_id == reason.pk
    case = _case(row)
    assert case.severity == "CONFLICT" and case.entry_event == event_row
    _assert_replayed(offline_world, op, _NOTE, ack)


def test_a_code_without_a_row_after_only_insertions_is_provably_absent(
    offline_world, supervisor_grant, event
):
    from apps.entry.services.offline_journal import journal_changes

    added = _catalogue_reason(event, code="SYNTHETIC_ADDED")  # an unrelated INSERT
    assert journal_changes(offline_world.package).override_reason_operations == {
        str(added.pk): {"I"}
    }
    # A row that carried this code in the package would have needed an
    # uncovered rename, move or deletion: there is none, so it never did.
    op = _catalogue_op(offline_world, supervisor_grant, "SYNTHETIC_FABRICATED", note=_NOTE)
    ack, row = _sync(offline_world, op, note=_NOTE)
    _assert_security_conflict(ack, row, "POLICY_VIOLATION")
    assert row.outcome_code == "OVERRIDE_NOT_PERMITTED"
    assert row.server_evaluation["override_catalogue_evidence"] == "ABSENT"
    _assert_replayed(offline_world, op, _NOTE, ack)


def _inserted_then_updated(world, event):
    reason = _catalogue_reason(event)
    EntryOverrideReason.objects.filter(pk=reason.pk).update(is_active=False)
    return reason.code, True


def _inserted_while_another_reason_changed(world, event):
    other = _catalogue_reason(event, code="SYNTHETIC_OTHER")
    assert other.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=other.pk).update(name="Synthetic label edit")
    # The UPDATE could have been a rename that freed this code, so the new
    # row's only state is not the only state the package could have carried.
    reason = _catalogue_reason(event, code="SYNTHETIC_NEW", is_active=False)
    return reason.code, True


def _code_without_row_after_an_update(world, event):
    other = _catalogue_reason(event, code="SYNTHETIC_OTHER")
    assert other.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=other.pk).update(name="Synthetic label edit")
    return "SYNTHETIC_FABRICATED", False  # probe P7: not provably absent


def _renamed_after_the_package(world, event):
    reason = _catalogue_reason(event, code="SYNTHETIC_ORIGINAL")
    assert reason.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=reason.pk).update(code="SYNTHETIC_RENAMED")
    return "SYNTHETIC_ORIGINAL", False  # probe P2


def _moved_to_another_event(world, event, other_event):
    reason = _catalogue_reason(event, code="SYNTHETIC_MOVED")
    assert reason.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=reason.pk).update(event_edition=other_event)
    return "SYNTHETIC_MOVED", False


def _code_reassigned_to_a_new_row(world, event):
    first = _catalogue_reason(event, code="SYNTHETIC_REUSED")
    assert first.code in _rebuilt_catalogue(world)
    EntryOverrideReason.objects.filter(pk=first.pk).update(code="SYNTHETIC_RETIRED")
    # A new, inactive row takes the code the package carried from `first`.
    _catalogue_reason(event, code="SYNTHETIC_REUSED", is_active=False)
    return "SYNTHETIC_REUSED", True


_UNKNOWN_AFTER_A_CHANGE = {
    "inserted_then_updated": _inserted_then_updated,
    "inserted_while_another_reason_changed": _inserted_while_another_reason_changed,
    "code_without_row_after_an_update": _code_without_row_after_an_update,
    "renamed_after_the_package": _renamed_after_the_package,
    "moved_to_another_event": _moved_to_another_event,
    "code_reassigned_to_a_new_row": _code_reassigned_to_a_new_row,
}


@pytest.mark.parametrize("change", sorted(_UNKNOWN_AFTER_A_CHANGE))
def test_an_uncovered_update_or_deletion_keeps_the_catalogue_entry_unknown(
    offline_world, supervisor_grant, event, other_event, change
):
    setup = _UNKNOWN_AFTER_A_CHANGE[change]
    if change == "moved_to_another_event":
        code, has_row = setup(offline_world, event, other_event)
    else:
        code, has_row = setup(offline_world, event)
    op = _catalogue_op(offline_world, supervisor_grant, code, note=_NOTE)
    ack, row = _sync(offline_world, op, note=_NOTE)
    # The device followed ITS package; the server cannot show otherwise.
    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    assert row.server_evaluation["override_catalogue_evidence"] == "CHANGED"
    assert row.server_evaluation["override_catalogue_uncertain"] is True
    assert not ReconciliationCase.objects.filter(severity="SECURITY").exists()
    if has_row:
        event_row = EntryEvent.objects.get(sync_operation=row)
        assert event_row.offline_conflict and event_row.override is not None
    else:
        # The reason cannot be linked (D3): the fact stays in the operation.
        assert not EntryEvent.objects.exists() and row.result_reference is None
        assert row.server_evaluation["override_reason_unavailable"] is True
    _assert_replayed(offline_world, op, _NOTE, ack)


def _retention_elapsed(settings, event):
    settings.ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS = 0


def _bulk_change(settings, event):
    settings.ENTRY_OFFLINE_DELTA_MAX_CHANGES = 1
    for code in ("SYNTHETIC_EXTRA_1", "SYNTHETIC_EXTRA_2"):  # more uncovered rows than the cap
        _catalogue_reason(event, code=code)


def _untracked_change(settings, event):
    from apps.accreditation.models import AccessProfile

    AccessProfile.objects.create(event_edition=event, code="SYNTHETIC_TMP", name="Temporary")
    AccessProfile.objects.filter(code="SYNTHETIC_TMP").delete()  # journal: UNTRACKED_CHANGE


_INCOMPLETE_HISTORY = {
    "retention_elapsed": _retention_elapsed,
    "bulk_change": _bulk_change,
    "untracked_change": _untracked_change,
}


@pytest.mark.parametrize("history", sorted(_INCOMPLETE_HISTORY))
@pytest.mark.parametrize("named", ["inserted_inactive_reason", "code_without_a_row"])
def test_incomplete_journal_history_proves_nothing_about_the_catalogue(
    offline_world, supervisor_grant, event, settings, history, named
):
    # With complete history both would be policy violations (INSERTED_ONLY
    # and ABSENT); without it the server cannot tell.
    code = "SYNTHETIC_FABRICATED"
    if named == "inserted_inactive_reason":
        code = _catalogue_reason(event, is_active=False).code
    _INCOMPLETE_HISTORY[history](settings, event)
    op = _catalogue_op(offline_world, supervisor_grant, code, note=_NOTE)
    ack, row = _sync(offline_world, op, note=_NOTE)
    assert (ack.status, ack.conflict_type) == ("CONFLICT", "ACCESS_CHANGED")
    assert row.outcome_code == "HISTORICAL_ACCESS_UNCERTAIN"
    assert row.server_evaluation["override_catalogue_evidence"] == "UNATTRIBUTABLE"
    assert row.server_evaluation["override_catalogue_uncertain"] is True
    assert EntryEvent.objects.filter(sync_operation=row).exists() is (
        named == "inserted_inactive_reason"
    )
