"""Attendance days at admission (`apps.accreditation.attendance`): online QR
and reference paths, the edition timezone day boundary, the other checks
kept intact, overrides refused, and the offline package / delta / synchronized
evaluation agreeing with online. Synthetic data, PostgreSQL."""

from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from django.utils import timezone

from apps.accreditation import attendance
from apps.accreditation.models import AttendanceCategory
from apps.accreditation.tests.attendance_fixtures import (
    configure_attendance,
    grant_entitlement,
    local_moment,
)
from apps.entry.models import NEVER_OVERRIDEABLE_REASON_CODES, EntryReasonCode, EntryResult
from apps.entry.offline_contract import TYP_DELTA_MANIFEST, TYP_PACKAGE_MANIFEST
from apps.entry.services.verification import lookup_reference, verify_qr
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db

ALL = AttendanceCategory.ALL_CONFERENCE_DAYS
FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS


def _opening_day(event):
    """An opening day inside the test event's dates and the pass window."""
    return attendance.local_date(timezone.now() + timedelta(days=3), event.timezone)


def _qr(checkpoint, credential, at):
    return verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential), now=at)


@pytest.fixture
def opening(event):
    return _opening_day(event)


@pytest.fixture
def enforced(event, opening):
    return configure_attendance(event, opening=opening, active=True)


# ---------------------------------------------------------------------------
# Online
# ---------------------------------------------------------------------------


def test_nothing_changes_while_enforcement_is_not_active(
    event, opening, checkpoint, active_pass, registration, staff
):
    configure_attendance(event, opening=opening, active=False)
    grant_entitlement(registration, FOLLOWING, actor=staff)
    outcome = _qr(checkpoint, active_pass, local_moment(event, opening, 10))
    assert outcome.result == EntryResult.ALLOWED


def test_the_two_following_days_are_refused_on_the_opening_day_even_with_an_active_qr(
    event, opening, enforced, checkpoint, active_pass, registration, staff
):
    grant_entitlement(registration, FOLLOWING, actor=staff)
    outcome = _qr(checkpoint, active_pass, local_moment(event, opening, 10))
    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED
    assert outcome.assessment.may_be_overridden is False
    second = opening + timedelta(days=1)
    third = opening + timedelta(days=2)
    assert _qr(checkpoint, active_pass, local_moment(event, second, 10)).result == (
        EntryResult.ALLOWED
    )
    assert _qr(checkpoint, active_pass, local_moment(event, third, 18)).result == (
        EntryResult.ALLOWED
    )


def test_all_three_days_are_admitted_every_conference_day(
    event, opening, enforced, checkpoint, active_pass, registration, staff
):
    grant_entitlement(registration, ALL, actor=staff)
    for offset in range(3):
        moment = local_moment(event, opening + timedelta(days=offset), 9)
        assert _qr(checkpoint, active_pass, moment).result == EntryResult.ALLOWED


def test_an_unclassified_approval_is_never_admitted_once_enforced(
    event, opening, enforced, checkpoint, active_pass
):
    for offset in (0, 1):
        outcome = _qr(checkpoint, active_pass, local_moment(event, opening + timedelta(offset), 9))
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.ATTENDANCE_UNCLASSIFIED


def test_a_day_outside_the_three_conference_days_is_refused(
    event, opening, enforced, checkpoint, active_pass, registration, staff
):
    grant_entitlement(registration, ALL, actor=staff)
    before = local_moment(event, opening - timedelta(days=1), 12)
    outcome = _qr(checkpoint, active_pass, before)
    assert outcome.reason_code == EntryReasonCode.ATTENDANCE_NOT_CONFERENCE_DAY


def test_the_day_boundary_is_the_local_midnight_of_the_event_timezone(
    event, opening, device, operator, layout, active_pass, registration, staff
):
    # Africa/Algiers is UTC+1: 23:05 UTC the evening before is already the
    # opening day locally, and 23:59 local on the opening day is still it.
    event.timezone = "Africa/Algiers"
    event.save(update_fields=["timezone"])
    device.refresh_from_db()
    checkpoint = factories.open_checkpoint(device=device, user=operator, zone=layout.main)
    configure_attendance(event, opening=opening, active=True)
    grant_entitlement(registration, FOLLOWING, actor=staff)
    just_after_midnight = local_moment(event, opening, 0, 5)
    assert just_after_midnight.astimezone(UTC).date() == opening - timedelta(days=1)
    assert _qr(checkpoint, active_pass, just_after_midnight).reason_code == (
        EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED
    )
    last_minute = local_moment(event, opening, 23, 59)
    assert _qr(checkpoint, active_pass, last_minute).reason_code == (
        EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED
    )
    next_day = local_moment(event, opening + timedelta(days=1), 0, 1)
    assert _qr(checkpoint, active_pass, next_day).result == EntryResult.ALLOWED


def test_the_reference_lookup_is_enforced_too(
    event, opening, enforced, checkpoint, active_pass, registration, staff
):
    grant_entitlement(registration, FOLLOWING, actor=staff)
    outcome = lookup_reference(
        checkpoint=checkpoint,
        raw_reference=registration.public_reference,
        now=local_moment(event, opening, 11),
    )
    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED


def test_attendance_never_widens_the_other_checks(
    event, opening, enforced, device_admin, operator, layout, active_pass, registration, staff
):
    grant_entitlement(registration, ALL, actor=staff)
    vip_device, _secret = factories.enroll_device(
        event=event, layout=layout, admin=device_admin, zones=[layout.main, layout.vip]
    )
    vip = factories.open_checkpoint(device=vip_device, user=operator, zone=layout.vip)
    outcome = _qr(vip, active_pass, local_moment(event, opening, 10))
    assert outcome.result == EntryResult.DENIED
    assert outcome.reason_code == EntryReasonCode.WRONG_ZONE


def test_a_downgrade_takes_effect_at_once_for_the_existing_credential(
    event, opening, checkpoint, active_pass, registration, staff
):
    configure_attendance(event, opening=opening, active=True)
    entitlement = grant_entitlement(registration, ALL, actor=staff)
    moment = local_moment(event, opening, 10)
    assert _qr(checkpoint, active_pass, moment).result == EntryResult.ALLOWED
    attendance.change_entitlement(
        registration=registration,
        category=FOLLOWING,
        actor=staff,
        reason="Downgrade",
        expected_entitlement_id=str(entitlement.pk),
    )
    # The same, still active credential is refused on the opening day under
    # the current entitlement: no replacement is needed for that.
    later_same_day = local_moment(event, opening, 22)
    assert _qr(checkpoint, active_pass, later_same_day).reason_code == (
        EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED
    )


def test_attendance_codes_are_never_overrideable():
    assert {
        EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED,
        EntryReasonCode.ATTENDANCE_UNCLASSIFIED,
        EntryReasonCode.ATTENDANCE_NOT_CONFERENCE_DAY,
    } <= NEVER_OVERRIDEABLE_REASON_CODES


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def _today_policy(event, *, as_opening: bool):
    """Enforced attendance whose opening day is today in the event timezone
    (or yesterday, making today the second day)."""
    today = attendance.local_date(timezone.now(), event.timezone)
    opening = today if as_opening else today - timedelta(days=1)
    return configure_attendance(
        event, opening=opening, active=True, activated_at=timezone.now() - timedelta(hours=1)
    )


@pytest.fixture
def day_safe_event(event):
    """A timezone with at least nine hours left in the local day, so a
    package built now is valid for the whole test (no midnight crossing)."""
    event.timezone = offline_factories.timezone_with_day_left(hours=9)
    event.save(update_fields=["timezone"])
    return event


def _packaged_jtis(offline_device):
    _package, raw = offline_factories.download(offline_device)
    _manifest, body = offline_device.open_response(raw, typ=TYP_PACKAGE_MANIFEST, kind="OPKG")
    return {entry["jti"] for entry in body["entries"]}, _package


def test_an_opening_day_package_holds_only_all_days_participants(
    day_safe_event, offline_device, setup, staff, key, active_pass, registration
):
    _today_policy(day_safe_event, as_opening=True)
    grant_entitlement(registration, ALL, actor=staff)
    following = factories.make_registration(
        event=day_safe_event, person=factories.make_person("Following Days Person")
    )
    factories.assign(registration=following, setup=setup, actor=staff)
    grant_entitlement(following, FOLLOWING, actor=staff)
    following_pass = factories.issue_active_pass(registration=following, actor=staff)
    unclassified = factories.make_registration(
        event=day_safe_event, person=factories.make_person("Unclassified Person")
    )
    factories.assign(registration=unclassified, setup=setup, actor=staff)
    unclassified_pass = factories.issue_active_pass(registration=unclassified, actor=staff)
    jtis, _package = _packaged_jtis(offline_device)
    assert jtis == {active_pass.jti}
    assert following_pass.jti not in jtis and unclassified_pass.jti not in jtis


def test_a_following_day_package_holds_both_categories(
    day_safe_event, offline_device, setup, staff, key, active_pass, registration
):
    _today_policy(day_safe_event, as_opening=False)
    grant_entitlement(registration, FOLLOWING, actor=staff)
    jtis, _package = _packaged_jtis(offline_device)
    assert jtis == {active_pass.jti}


def test_a_downgrade_after_the_cutoff_reaches_the_device_with_the_next_delta(
    day_safe_event, offline_device, staff, key, active_pass, registration
):
    _today_policy(day_safe_event, as_opening=True)
    entitlement = grant_entitlement(registration, ALL, actor=staff)
    jtis, package = _packaged_jtis(offline_device)
    assert jtis == {active_pass.jti}
    attendance.change_entitlement(
        registration=registration,
        category=FOLLOWING,
        actor=staff,
        reason="Downgrade",
        expected_entitlement_id=str(entitlement.pk),
    )
    body, nonce, signature = offline_device.signed("delta", {"package_id": package.public_id})
    from apps.entry.services.offline_packages import issue_delta

    _row, raw = issue_delta(
        device=offline_device.device,
        package_public_id=package.public_id,
        nonce=nonce,
        signature=signature,
        body=body,
    )
    _manifest, delta = offline_device.open_response(raw, typ=TYP_DELTA_MANIFEST, kind="ODELTA")
    assert {"jti": active_pass.jti, "reason": "ATTENDANCE_NOT_AUTHORIZED"} in delta[
        "access_withdrawn"
    ]


def test_without_enforcement_packages_and_deltas_are_unchanged(
    day_safe_event, offline_device, staff, key, active_pass, registration
):
    today = attendance.local_date(timezone.now(), day_safe_event.timezone)
    configure_attendance(day_safe_event, opening=today, active=False)
    grant_entitlement(registration, FOLLOWING, actor=staff)
    jtis, _package = _packaged_jtis(offline_device)
    assert jtis == {active_pass.jti}


def test_activation_revokes_the_current_offline_packages(
    day_safe_event, offline_device, staff, key, active_pass, registration
):
    from apps.entry.models import OfflinePackage, OfflinePackageStatus

    today = attendance.local_date(timezone.now(), day_safe_event.timezone)
    policy = configure_attendance(day_safe_event, opening=today, active=False)
    grant_entitlement(registration, ALL, actor=staff)
    _jtis, package = _packaged_jtis(offline_device)
    attendance.activate_enforcement(
        event_edition=day_safe_event, actor=staff, expected_version=policy.version
    )
    package.refresh_from_db()
    assert package.status == OfflinePackageStatus.REVOKED
    assert package.status_reason_code == attendance.OFFLINE_PACKAGE_REVOCATION_REASON
    assert not OfflinePackage.objects.filter(status=OfflinePackageStatus.READY).exists()


def test_a_synchronized_admission_is_judged_with_the_attendance_of_its_time(
    event, opening, layout, active_pass, registration, staff
):
    from apps.entry.services.offline_evaluation import evaluate_at

    configure_attendance(event, opening=opening, active=True)
    grant_entitlement(registration, FOLLOWING, actor=staff)
    on_opening = evaluate_at(
        registration=registration,
        credential=active_pass,
        event_edition=event,
        gate=layout.gate_a,
        zone=layout.main,
        at=local_moment(event, opening, 10),
    )
    assert EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED in on_opening.blocker_codes
    on_second = evaluate_at(
        registration=registration,
        credential=active_pass,
        event_edition=event,
        gate=layout.gate_a,
        zone=layout.main,
        at=local_moment(event, opening + timedelta(days=1), 10),
    )
    assert EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED not in on_second.blocker_codes


def test_a_queued_operation_keeps_the_enforcement_of_its_time_after_reactivation(
    event, opening, layout, checkpoint, active_pass, registration, staff, monkeypatch
):
    """Enforcement on 08:00-10:00, off, on again at 11:00: an offline
    admission that happened at 09:00 and is synchronized after the
    reactivation is still judged as enforced; one from 10:30 is not."""
    from apps.entry.services.offline_evaluation import evaluate_at

    configure_attendance(event, opening=opening, active=False)
    grant_entitlement(registration, FOLLOWING, actor=staff)

    def switch(on, hour, minute=0):
        moment = local_moment(event, opening, hour, minute)
        monkeypatch.setattr(timezone, "now", lambda: moment)
        policy = attendance.policy_for(event.pk)
        if on:
            attendance.activate_enforcement(
                event_edition=event, actor=staff, expected_version=policy.version
            )
        else:
            attendance.deactivate_enforcement(
                event_edition=event, actor=staff, expected_version=policy.version, reason="Drill"
            )

    switch(True, 8)
    switch(False, 10)
    switch(True, 11)

    def evaluated(hour, minute=0):
        return evaluate_at(
            registration=registration,
            credential=active_pass,
            event_edition=event,
            gate=layout.gate_a,
            zone=layout.main,
            at=local_moment(event, opening, hour, minute),
        ).blocker_codes

    assert EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED in evaluated(9)
    assert EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED not in evaluated(10, 30)
    assert EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED in evaluated(11, 30)
    # Online, now (enforcement active again): refused on the opening day.
    now = local_moment(event, opening, 11, 45)
    monkeypatch.setattr(timezone, "now", lambda: now)
    outcome = _qr(checkpoint, active_pass, now)
    assert outcome.reason_code == EntryReasonCode.ATTENDANCE_DAY_NOT_AUTHORIZED
