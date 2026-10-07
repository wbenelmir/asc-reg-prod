"""Attendance entitlements: approval choice, opening-day capacity, legacy
classification, changes, notifications, readiness and activation
(`apps.accreditation.attendance`). Synthetic data, PostgreSQL."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.accreditation import attendance
from apps.accreditation.models import (
    AttendanceCategory,
    AttendanceEntitlement,
    AttendanceEntitlementOrigin,
    AttendanceEntitlementStatus,
    AttendancePolicy,
)
from apps.accreditation.tests.attendance_fixtures import (
    TEST_OPENING_DAY,
    configure_attendance,
    grant_entitlement,
)
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.people.tests.identity_fixtures import (
    assign_approval_prerequisites,
    make_verified_identity_case,
)
from apps.registrations.models import RegistrationPublicStatus
from apps.reviews.services import (
    cancel_registration_operationally,
    open_review_case,
    record_approved_decision,
    reopen_registration,
    withdraw_registration,
)

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db

ALL = AttendanceCategory.ALL_CONFERENCE_DAYS
FOLLOWING = AttendanceCategory.FOLLOWING_TWO_DAYS


@pytest.fixture
def manager(event):
    return make_operational_user_with_membership(
        email="attendance-manager@example.test",
        group_name="Accreditation Managers",
        event_edition=event,
    )


@pytest.fixture
def policy_manager(event):
    return make_operational_user_with_membership(
        email="attendance-policy@example.test",
        group_name="Attendance Policy Managers",
        event_edition=event,
    )


def approvable(event, actor, organization=None):
    registration = make_registration(event=event, organization=organization)
    assign_approval_prerequisites(registration, actor)
    make_verified_identity_case(registration)
    registration.refresh_from_db()
    return registration


def approve(registration, actor, category):
    registration.refresh_from_db()
    return record_approved_decision(
        registration=registration,
        expected_version=registration.version,
        decided_by=actor,
        attendance_category=category,
    )


def legacy_approved(event, actor, organization=None):
    """An approval recorded before attendance existed: APPROVED, no entitlement."""
    registration = approvable(event, actor, organization)
    registration.public_status = RegistrationPublicStatus.APPROVED
    registration.save(update_fields=["public_status"])
    return registration


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("choice", [None, "", "SOME_DAYS"])
def test_an_approval_without_a_valid_choice_is_refused_and_writes_nothing(event, manager, choice):
    configure_attendance(event)
    registration = approvable(event, manager)
    with pytest.raises(attendance.AttendanceChoiceRequiredError):
        approve(registration, manager, choice)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    assert not registration.decisions.exists()
    assert not AttendanceEntitlement.objects.filter(registration=registration).exists()
    assert AuditEvent.objects.filter(
        action_code=action_codes.REVIEW_APPROVAL_BLOCKED_ATTENDANCE,
        target_uuid=registration.pk,
        reason_code="CHOICE_REQUIRED",
        result="DENIED",
    ).exists()


def test_an_approval_needs_configured_days_and_capacity(event, manager):
    registration = approvable(event, manager)
    AttendancePolicy.objects.filter(event_edition=event).update(opening_day_capacity=None)
    with pytest.raises(attendance.AttendanceNotConfiguredError):
        approve(registration, manager, FOLLOWING)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED


@pytest.mark.parametrize("category", [ALL, FOLLOWING])
def test_both_choices_record_the_entitlement_with_the_decision(event, manager, category):
    configure_attendance(event)
    registration = approvable(event, manager)
    decision = approve(registration, manager, category)
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.APPROVED
    entitlement = AttendanceEntitlement.objects.get(registration=registration)
    assert entitlement.category == category
    assert entitlement.status == AttendanceEntitlementStatus.CURRENT
    assert entitlement.origin == AttendanceEntitlementOrigin.APPROVAL
    assert entitlement.decision_id == decision.pk
    assert entitlement.decided_by == manager
    granted = AuditEvent.objects.get(
        action_code=action_codes.ATTENDANCE_ENTITLEMENT_GRANTED, target_uuid=registration.pk
    )
    assert granted.after_summary["category"] == category


def test_the_last_opening_day_place_is_given_once_and_never_downgraded(event, manager):
    configure_attendance(event, capacity=1)
    first = approvable(event, manager)
    second = approvable(event, manager)
    approve(first, manager, ALL)
    with pytest.raises(attendance.OpeningDayCapacityReachedError):
        approve(second, manager, ALL)
    second.refresh_from_db()
    # Refused as a whole: not approved, not downgraded, and the refusal audited.
    assert second.public_status == RegistrationPublicStatus.SUBMITTED
    assert not AttendanceEntitlement.objects.filter(registration=second).exists()
    assert AuditEvent.objects.filter(
        action_code=action_codes.ATTENDANCE_OPENING_CAPACITY_REFUSED,
        target_uuid=second.pk,
        result="DENIED",
    ).exists()
    # The operator may then explicitly choose the two following days.
    approve(second, manager, FOLLOWING)
    assert attendance.current_entitlement(second.pk).category == FOLLOWING
    assert attendance.opening_allocation_count(event.pk) == 1


def test_every_registration_origin_counts_once(event, manager, organization):
    from apps.registrations.models import RegistrationSourceKind

    configure_attendance(event, capacity=2)
    open_one = approvable(event, manager)
    delegated = approvable(event, manager, organization)
    delegated.source_kind = RegistrationSourceKind.DELEGATION
    delegated.save(update_fields=["source_kind"])
    approve(open_one, manager, ALL)
    approve(delegated, manager, ALL)
    assert attendance.opening_allocation_count(event.pk) == 2
    third = approvable(event, manager)
    with pytest.raises(attendance.OpeningDayCapacityReachedError):
        approve(third, manager, ALL)


@pytest.mark.parametrize("closing", ["withdraw", "cancel", "reopen"])
def test_closing_a_registration_releases_its_place_exactly_once(event, manager, closing):
    configure_attendance(event, capacity=1)
    holder = approvable(event, manager)
    approve(holder, manager, ALL)
    holder.refresh_from_db()
    if closing == "withdraw":
        withdraw_registration(
            registration=holder, person=holder.person, expected_version=holder.version
        )
    elif closing == "cancel":
        cancel_registration_operationally(
            registration=holder, expected_version=holder.version, reason="Test", actor=manager
        )
    else:
        reopen_registration(
            registration=holder, expected_version=holder.version, reason="Test", actor=manager
        )
    assert attendance.opening_allocation_count(event.pk) == 0
    # Closing the same registration again cannot release another place.
    holder.refresh_from_db()
    if closing == "withdraw":
        withdraw_registration(
            registration=holder, person=holder.person, expected_version=holder.version
        )
    assert attendance.opening_allocation_count(event.pk) == 0
    newcomer = approvable(event, manager)
    approve(newcomer, manager, ALL)
    assert attendance.opening_allocation_count(event.pk) == 1


def test_a_reapproval_after_reopening_needs_a_new_choice_and_supersedes(event, manager):
    configure_attendance(event)
    registration = approvable(event, manager)
    approve(registration, manager, ALL)
    registration.refresh_from_db()
    reopen_registration(
        registration=registration,
        expected_version=registration.version,
        reason="Re-check",
        actor=manager,
    )
    approve(registration, manager, FOLLOWING)
    rows = AttendanceEntitlement.objects.filter(registration=registration).order_by(
        "effective_from"
    )
    assert [(r.category, r.status) for r in rows] == [
        (ALL, AttendanceEntitlementStatus.SUPERSEDED),
        (FOLLOWING, AttendanceEntitlementStatus.CURRENT),
    ]
    assert rows[1].supersedes_id == rows[0].pk


def test_the_approval_message_states_the_authorized_days(
    event, manager, django_capture_on_commit_callbacks
):
    configure_attendance(event)
    registration = approvable(event, manager)
    with django_capture_on_commit_callbacks(execute=False):
        approve(registration, manager, FOLLOWING)
    message = CommunicationMessage.objects.get(
        registration=registration, template_version__template__code="APPROVAL_ATTENDANCE"
    )
    assert message.idempotency_key.startswith("decision-status:")
    body = message.rendered_body_encrypted
    assert "Sunday 6 December 2026" in body and "Monday 7 December 2026" in body
    assert "does not include the opening day, Saturday 5 December 2026" in body
    assert "entry pass is already available" in body  # approval never promises a pass
    assert not CommunicationMessage.objects.filter(
        registration=registration, template_version__template__code="DECISION_STATUS"
    ).exists()


# ---------------------------------------------------------------------------
# Legacy classification and later changes
# ---------------------------------------------------------------------------


def test_existing_approvals_stay_unclassified_until_an_operator_decides(event, manager):
    configure_attendance(event)
    legacy = legacy_approved(event, manager)
    assert attendance.current_entitlement(legacy.pk) is None
    assert list(attendance.unclassified_registrations(event.pk)) == [legacy]
    view = attendance.participant_attendance(legacy)
    assert view.is_pending and "confirming which conference days" in view.statement


def test_classification_records_actor_reason_and_notifies_once(
    event, manager, django_capture_on_commit_callbacks
):
    configure_attendance(event)
    legacy = legacy_approved(event, manager)
    with django_capture_on_commit_callbacks(execute=False):
        result = attendance.change_entitlement(
            registration=legacy,
            category=FOLLOWING,
            actor=manager,
            reason="Confirmed by the programme team",
            expected_entitlement_id="",
        )
    assert result.changed and result.previous_category is None
    assert result.entitlement.origin == AttendanceEntitlementOrigin.LEGACY_CLASSIFICATION
    event_row = AuditEvent.objects.get(
        action_code=action_codes.ATTENDANCE_ENTITLEMENT_CLASSIFIED, target_uuid=legacy.pk
    )
    assert event_row.actor_user_id == manager.pk
    assert event_row.before_summary == {"category": None}
    assert event_row.after_summary["category"] == FOLLOWING
    assert event_row.reason_code == "Confirmed by the programme team"
    messages = CommunicationMessage.objects.filter(
        registration=legacy, template_version__template__code="ATTENDANCE_CHANGE"
    )
    assert messages.count() == 1
    assert "Sunday 6 December 2026" in messages.get().rendered_body_encrypted
    # Re-submitting the same category changes nothing and sends nothing.
    again = attendance.change_entitlement(
        registration=legacy,
        category=FOLLOWING,
        actor=manager,
        reason="Same again",
        expected_entitlement_id=str(result.entitlement.pk),
    )
    assert not again.changed
    assert messages.count() == 1
    assert AttendanceEntitlement.objects.filter(registration=legacy).count() == 1


def test_upgrade_needs_a_free_place_and_downgrade_releases_it(event, manager):
    configure_attendance(event, capacity=1)
    holder = approvable(event, manager)
    approve(holder, manager, ALL)
    waiting = approvable(event, manager)
    approve(waiting, manager, FOLLOWING)
    current = attendance.current_entitlement(waiting.pk)
    with pytest.raises(attendance.OpeningDayCapacityReachedError):
        attendance.change_entitlement(
            registration=waiting,
            category=ALL,
            actor=manager,
            reason="Upgrade",
            expected_entitlement_id=str(current.pk),
        )
    assert attendance.current_entitlement(waiting.pk).category == FOLLOWING
    downgrade = attendance.change_entitlement(
        registration=holder,
        category=FOLLOWING,
        actor=manager,
        reason="Cannot attend the opening",
        expected_entitlement_id=str(attendance.current_entitlement(holder.pk).pk),
    )
    assert downgrade.changed and downgrade.previous_category == ALL
    assert attendance.opening_allocation_count(event.pk) == 0
    upgrade = attendance.change_entitlement(
        registration=waiting,
        category=ALL,
        actor=manager,
        reason="Place released",
        expected_entitlement_id=str(current.pk),
    )
    assert upgrade.entitlement.origin == AttendanceEntitlementOrigin.CHANGE
    assert attendance.opening_allocation_count(event.pk) == 1


def test_a_change_from_a_stale_page_is_refused(event, manager):
    configure_attendance(event)
    registration = approvable(event, manager)
    approve(registration, manager, ALL)
    with pytest.raises(attendance.AttendanceConcurrencyError):
        attendance.change_entitlement(
            registration=registration,
            category=FOLLOWING,
            actor=manager,
            reason="Stale page",
            expected_entitlement_id="",
        )


def test_only_an_approved_registration_can_be_classified(event, manager):
    configure_attendance(event)
    submitted = approvable(event, manager)
    with pytest.raises(attendance.AttendanceStateError):
        attendance.change_entitlement(
            registration=submitted,
            category=ALL,
            actor=manager,
            reason="Not approved",
            expected_entitlement_id="",
        )


# ---------------------------------------------------------------------------
# Settings, readiness and activation
# ---------------------------------------------------------------------------


def _update(event, actor, **values):
    policy = attendance.policy_for(event.pk)
    defaults = {
        "opening_date": TEST_OPENING_DAY,
        "second_date": TEST_OPENING_DAY.replace(day=6),
        "third_date": TEST_OPENING_DAY.replace(day=7),
        "opening_day_capacity": 10,
        "expected_version": policy.version if policy else None,
        "reason": "Programme confirmed",
    }
    return attendance.update_policy(event_edition_id=event.pk, actor=actor, **(defaults | values))


def test_settings_are_validated_and_audited(event, policy_manager):
    with pytest.raises(attendance.AttendancePolicyError) as order:
        _update(event, policy_manager, second_date=TEST_OPENING_DAY)
    assert order.value.code == "DAYS_ORDER"
    with pytest.raises(attendance.AttendancePolicyError) as partial:
        _update(event, policy_manager, third_date=None)
    assert partial.value.code == "DAYS_INCOMPLETE"
    policy, changed = _update(event, policy_manager)
    assert changed and policy.opening_day_capacity == 10
    assert AuditEvent.objects.filter(
        action_code=action_codes.ATTENDANCE_POLICY_UPDATED, target_uuid=event.pk
    ).exists()
    _policy, unchanged = _update(event, policy_manager)
    assert not unchanged


def test_capacity_cannot_drop_below_allocations_and_days_freeze(event, manager, policy_manager):
    configure_attendance(event, capacity=5)
    for _ in range(2):
        approve(approvable(event, manager), manager, ALL)
    with pytest.raises(attendance.AttendancePolicyError) as below:
        _update(event, policy_manager, opening_day_capacity=1)
    assert below.value.code == "CAPACITY_BELOW_ALLOCATED"
    assert attendance.policy_for(event.pk).opening_day_capacity == 5
    _policy, changed = _update(event, policy_manager, opening_day_capacity=2)
    assert changed
    with pytest.raises(attendance.AttendancePolicyError) as frozen:
        _update(event, policy_manager, opening_date=TEST_OPENING_DAY.replace(day=4))
    assert frozen.value.code == "DAYS_LOCKED"


def test_activation_is_refused_until_every_prerequisite_holds(
    event, manager, policy_manager, organization
):
    from apps.badges.models import BadgeIssuance

    configure_attendance(event, capacity=5)
    legacy = legacy_approved(event, manager)
    policy = attendance.policy_for(event.pk)
    with pytest.raises(attendance.AttendanceActivationRefused) as refused:
        attendance.activate_enforcement(
            event_edition=event, actor=policy_manager, expected_version=policy.version
        )
    assert attendance.READY_CLASSIFIED in refused.value.readiness.blocking_codes
    assert AuditEvent.objects.filter(
        action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATION_REFUSED, result="DENIED"
    ).exists()
    grant_entitlement(legacy, ALL, actor=manager)
    # A handed-over badge without the attendance marking still blocks.
    badge = _issued_badge(legacy, manager)
    readiness = attendance.readiness(event)
    assert readiness.blocking_codes == (attendance.READY_BADGES,)
    BadgeIssuance.objects.filter(pk=badge.pk).update(attendance_marking=ALL)
    activated = attendance.activate_enforcement(
        event_edition=event, actor=policy_manager, expected_version=policy.version
    )
    assert activated.enforcement_active and activated.enforcement_activated_by == policy_manager
    assert AuditEvent.objects.filter(
        action_code=action_codes.ATTENDANCE_ENFORCEMENT_ACTIVATED, target_uuid=event.pk
    ).exists()
    # The same capacity rules keep applying, and switching off is audited.
    deactivated = attendance.deactivate_enforcement(
        event_edition=event,
        actor=policy_manager,
        expected_version=activated.version,
        reason="Rollback drill",
    )
    assert not deactivated.enforcement_active and deactivated.enforcement_deactivated_at
    assert attendance.current_entitlement(legacy.pk).category == ALL


def test_capacity_overrun_blocks_activation(event, manager, policy_manager):
    configure_attendance(event, capacity=2)
    for _ in range(2):
        approve(approvable(event, manager), manager, ALL)
    AttendancePolicy.objects.filter(event_edition=event).update(opening_day_capacity=1)
    assert attendance.READY_WITHIN_CAPACITY in attendance.readiness(event).blocking_codes


def _issued_badge(registration, actor):
    from django.utils import timezone

    from apps.accreditation.models import AssignmentStatus, BadgeTypeAssignment
    from apps.badges.models import BadgeIssuance, StockLocation

    assignment = BadgeTypeAssignment.objects.get(
        registration=registration, status=AssignmentStatus.CURRENT
    )
    location = StockLocation.objects.create(
        event_edition=registration.event_edition, code="DESK", name="Desk", location_type="CENTRAL"
    )
    return BadgeIssuance.objects.create(
        badge_assignment=assignment,
        registration=registration,
        badge_type=assignment.badge_type,
        location=location,
        issued_at=timezone.now(),
        issued_by=actor,
    )


# ---------------------------------------------------------------------------
# Participant wording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category, included, excluded",
    [
        (ALL, ["Saturday 5 December 2026", "Sunday 6 December 2026", "Monday 7 December 2026"], ""),
        (
            FOLLOWING,
            ["Sunday 6 December 2026", "Monday 7 December 2026"],
            "Saturday 5 December 2026",
        ),
    ],
)
def test_the_participant_sees_the_exact_dates(event, manager, category, included, excluded):
    configure_attendance(event)
    registration = approvable(event, manager)
    approve(registration, manager, category)
    registration.refresh_from_db()
    view = attendance.participant_attendance(registration)
    assert list(view.days) == included
    assert view.excluded_day == excluded
    for day in included:
        assert day in view.statement
    if excluded:
        assert f"does not include the opening day, {excluded}" in view.statement


def test_a_submitted_registration_shows_no_attendance(event, manager):
    configure_attendance(event)
    registration = approvable(event, manager)
    assert attendance.participant_attendance(registration) is None


# ---------------------------------------------------------------------------
# Operations views
# ---------------------------------------------------------------------------


def _case_for(registration):
    return open_review_case(registration=registration, case_type="STANDARD", queue_code="GENERAL")


def test_the_approval_panel_explains_both_choices_without_a_default(event, manager):
    configure_attendance(event, capacity=3)
    registration = approvable(event, manager)
    case = _case_for(registration)
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    page = client.get(reverse("reviews:case-detail", kwargs={"pk": case.pk})).content.decode()
    assert 'name="attendance_category" id="attendance-all" value="ALL_CONFERENCE_DAYS"' in page
    assert 'value="FOLLOWING_TWO_DAYS"' in page
    assert "checked" not in page.split("data-attendance-choice", 1)[1].split("</form>", 1)[0]
    assert "Opening-day places left: 3 of 3." in page
    assert "Saturday 5 December 2026" in page


def test_the_approval_view_refuses_a_missing_choice_with_a_message(event, manager):
    configure_attendance(event)
    registration = approvable(event, manager)
    case = _case_for(registration)
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version},
        follow=True,
    )
    assert "Choose the attendance days" in response.content.decode()
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED


def test_the_approval_view_refuses_a_full_opening_day(event, manager):
    configure_attendance(event, capacity=0)
    registration = approvable(event, manager)
    case = _case_for(registration)
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    page = client.get(reverse("reviews:case-detail", kwargs={"pk": case.pk})).content.decode()
    assert 'id="attendance-all" value="ALL_CONFERENCE_DAYS" disabled' in page
    response = client.post(
        reverse("reviews:case-decision-approve", kwargs={"pk": case.pk}),
        {"expected_version": registration.version, "attendance_category": ALL},
        follow=True,
    )
    assert "The opening day is full" in response.content.decode()
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED


def test_the_worklist_classifies_an_unclassified_approval(event, manager):
    configure_attendance(event)
    legacy = legacy_approved(event, manager)
    client = Client()
    sign_in_operational(client, manager.email_normalized)
    url = reverse("accreditation:attendance-worklist", kwargs={"event_pk": event.pk})
    page = client.get(url).content.decode()
    assert f'data-attendance-row="{legacy.public_reference}"' in page
    assert 'data-attendance-category="UNCLASSIFIED"' in page
    response = client.post(
        reverse("accreditation:attendance-change", kwargs={"pk": legacy.pk}),
        {
            "attendance_category": ALL,
            "reason": "Confirmed list",
            "expected_entitlement_id": "",
            "next": url,
        },
    )
    assert response.status_code == 302 and response["Location"] == url
    assert attendance.current_entitlement(legacy.pk).category == ALL
    assert (
        f'data-attendance-row="{legacy.public_reference}"' not in client.get(url).content.decode()
    )


def test_a_reader_without_the_change_permission_cannot_classify(event, manager):
    configure_attendance(event)
    legacy = legacy_approved(event, manager)
    coordinator = make_operational_user_with_membership(
        email="attendance-coordinator@example.test",
        group_name="Accreditation Coordinators",
        event_edition=event,
    )
    client = Client()
    sign_in_operational(client, coordinator.email_normalized)
    worklist = client.get(
        reverse("accreditation:attendance-worklist", kwargs={"event_pk": event.pk})
    )
    assert worklist.status_code == 200
    assert "data-attendance-change" not in worklist.content.decode()
    response = client.post(
        reverse("accreditation:attendance-change", kwargs={"pk": legacy.pk}),
        {"attendance_category": ALL, "reason": "Direct POST", "expected_entitlement_id": ""},
    )
    assert response.status_code == 403
    assert attendance.current_entitlement(legacy.pk) is None


def test_another_events_manager_cannot_reach_the_registration(event, other_event, manager):
    configure_attendance(event)
    legacy = legacy_approved(event, manager)
    outsider = make_operational_user_with_membership(
        email="attendance-outsider@example.test",
        group_name="Accreditation Managers",
        event_edition=other_event,
    )
    client = Client()
    sign_in_operational(client, outsider.email_normalized)
    response = client.post(
        reverse("accreditation:attendance-change", kwargs={"pk": legacy.pk}),
        {"attendance_category": ALL, "reason": "Out of scope", "expected_entitlement_id": ""},
    )
    assert response.status_code == 404
    assert attendance.current_entitlement(legacy.pk) is None


def test_the_policy_page_saves_settings_and_only_managers_may(event, manager, policy_manager):
    client = Client()
    sign_in_operational(client, policy_manager.email_normalized)
    url = reverse("accreditation:attendance-policy", kwargs={"event_pk": event.pk})
    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {
            "opening_date": "2026-12-05",
            "second_date": "2026-12-06",
            "third_date": "2026-12-07",
            "opening_day_capacity": "40",
            "reason": "Programme confirmed",
        },
    )
    assert response.status_code == 302
    assert attendance.policy_for(event.pk).opening_day_capacity == 40
    # A decision maker may read the settings but not change them.
    reader = Client()
    sign_in_operational(reader, manager.email_normalized)
    assert reader.get(url).status_code == 200
    assert (
        reader.post(
            url,
            {
                "opening_date": "2026-12-05",
                "second_date": "2026-12-06",
                "third_date": "2026-12-07",
                "opening_day_capacity": "99",
                "reason": "Not allowed",
            },
        ).status_code
        == 403
    )
    assert (
        reader.post(
            reverse("accreditation:attendance-activate", kwargs={"event_pk": event.pk}),
            {"expected_version": 2},
        ).status_code
        == 403
    )
    assert attendance.policy_for(event.pk).opening_day_capacity == 40
