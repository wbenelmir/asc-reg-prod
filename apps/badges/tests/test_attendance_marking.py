"""Attendance marking of handed-over physical badges and attendance days on
the participant pass (`apps.accreditation.attendance`). Synthetic data."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accreditation.models import AttendanceCategory
from apps.accreditation.tests.attendance_fixtures import configure_attendance, grant_entitlement
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import BadgeIssuance
from apps.badges.services import (
    AttendanceMarkingError,
    issue_badge,
    new_operation_id,
    record_attendance_marking,
)

from .conftest import sign_in_operational
from .test_stock_services import _receive_into

pytestmark = pytest.mark.django_db

ALL = AttendanceCategory.ALL_CONFERENCE_DAYS
FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS


@pytest.fixture
def stocked(event, badge_type, stock_location, stock_admin):
    _receive_into(
        event=event, badge_type=badge_type, location=stock_location, quantity=5, actor=stock_admin
    )
    return stock_location


def _issue(assignment, location, actor):
    return issue_badge(
        badge_assignment=assignment,
        badge_type=assignment.badge_type,
        location=location,
        actor=actor,
        operation_id=new_operation_id(),
    )


def test_the_marking_must_equal_the_current_attendance_days(
    event, eligible_registration, current_badge_assignment, stocked, stock_issuer
):
    configure_attendance(event)
    grant_entitlement(eligible_registration, FOLLOWING, actor=stock_issuer)
    issuance = _issue(current_badge_assignment, stocked, stock_issuer)
    assert issuance.attendance_marking == ""
    with pytest.raises(AttendanceMarkingError) as wrong:
        record_attendance_marking(
            issuance=issuance,
            attendance_marking=ALL,
            actor=stock_issuer,
            expected_lock_version=issuance.version,
        )
    assert wrong.value.code == "MISMATCH"
    marked = record_attendance_marking(
        issuance=issuance,
        attendance_marking=FOLLOWING,
        actor=stock_issuer,
        expected_lock_version=issuance.version,
    )
    assert marked.attendance_marking == FOLLOWING
    assert marked.attendance_marking_recorded_by == stock_issuer
    assert AuditEvent.objects.filter(
        action_code=action_codes.BADGE_ATTENDANCE_MARKING_RECORDED, target_uuid=issuance.pk
    ).exists()


def test_an_unclassified_registration_gets_no_badge_once_enforced(
    event, eligible_registration, current_badge_assignment, stocked, stock_issuer
):
    configure_attendance(event, active=True)
    with pytest.raises(AttendanceMarkingError) as refused:
        _issue(current_badge_assignment, stocked, stock_issuer)
    assert refused.value.code == "UNCLASSIFIED"
    assert not BadgeIssuance.objects.filter(registration=eligible_registration).exists()


def test_the_badge_page_tells_the_marking_and_records_it_with_the_handover(
    client, event, eligible_registration, current_badge_assignment, stocked, stock_issuer
):
    configure_attendance(event)
    grant_entitlement(eligible_registration, FOLLOWING, actor=stock_issuer)
    sign_in_operational(client, stock_issuer.email_normalized)
    page_url = reverse(
        "badges:registration-badge-issuance", kwargs={"pk": eligible_registration.pk}
    )
    page = client.get(page_url).content.decode()
    assert 'data-attendance-mark="FOLLOWING_TWO_DAYS"' in page
    assert "Days 2 and 3 only, not the opening day" in page
    assert 'name="attendance_marking" value="FOLLOWING_TWO_DAYS"' in page
    response = client.post(
        reverse("badges:badge-issue", kwargs={"pk": eligible_registration.pk}),
        {
            "operation_id": new_operation_id(),
            "badge_type_id": str(current_badge_assignment.badge_type_id),
            "location_id": str(stocked.pk),
            "attendance_marking": FOLLOWING,
        },
    )
    assert response.status_code == 302
    issuance = BadgeIssuance.objects.get(registration=eligible_registration)
    assert issuance.attendance_marking == FOLLOWING


def test_a_later_change_shows_the_badge_marking_as_outdated_until_recorded(
    client, event, eligible_registration, current_badge_assignment, stocked, stock_issuer
):
    configure_attendance(event)
    grant_entitlement(eligible_registration, ALL, actor=stock_issuer)
    issuance = _issue(current_badge_assignment, stocked, stock_issuer)
    issuance = record_attendance_marking(
        issuance=issuance,
        attendance_marking=ALL,
        actor=stock_issuer,
        expected_lock_version=issuance.version,
    )
    grant_entitlement(eligible_registration, FOLLOWING, actor=stock_issuer)
    sign_in_operational(client, stock_issuer.email_normalized)
    page_url = reverse(
        "badges:registration-badge-issuance", kwargs={"pk": eligible_registration.pk}
    )
    page = client.get(page_url).content.decode()
    assert "Outdated: does not match the current attendance days" in page
    response = client.post(
        reverse(
            "badges:badge-issuance-attendance-marking",
            kwargs={"registration_pk": eligible_registration.pk, "pk": issuance.pk},
        ),
        {"expected_lock_version": issuance.version, "attendance_marking": FOLLOWING},
    )
    assert response.status_code == 302
    issuance.refresh_from_db()
    assert issuance.attendance_marking == FOLLOWING
    assert "Outdated" not in client.get(page_url).content.decode()
