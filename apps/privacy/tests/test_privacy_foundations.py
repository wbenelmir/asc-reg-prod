"""Privacy-request lifecycle, retention category, and Legal Hold tests
(Phase 2 Prompt 5 §4.6/§7)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.accounts.models import (
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.events.models import EventEdition
from apps.people.models import Person
from apps.privacy.models import (
    LegalHold,
    PrivacyRequestStatus,
    PrivacyRequestType,
    RetentionCategory,
)
from apps.privacy.selectors import is_under_legal_hold
from apps.privacy.services import (
    InvalidPrivacyRequestTransitionError,
    LegalHoldError,
    PrivacyAuthorizationError,
    file_privacy_request,
    place_legal_hold,
    release_legal_hold,
    transition_privacy_request_status,
)
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db


@pytest.fixture
def person() -> Person:
    return Person.objects.create(display_name="Privacy Test Person")


@pytest.fixture
def registration() -> Registration:
    now = timezone.now()
    event = EventEdition.objects.create(
        code="PRVFOUND", name="Privacy Foundations Test", timezone="UTC", starts_at=now, ends_at=now
    )
    return Registration.objects.create(
        public_reference="PRVFOUND-R-000001",
        event_edition=event,
        source_kind="OPEN",
        source_context_key="ctx-privacy-foundations",
    )


@pytest.fixture
def operator() -> OperationalUser:
    from django.contrib.auth.models import Group

    user = OperationalUser.objects.create_user(
        email="privacy-admin@example.com", status=OperationalUserStatus.ACTIVE
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name="Privacy Administrators"),
        granted_by=user,
    )
    return user


@pytest.fixture
def unauthorized_operator() -> OperationalUser:
    return OperationalUser.objects.create_user(
        email="privacy-unauthorized@example.com", status=OperationalUserStatus.ACTIVE
    )


# ---------------------------------------------------------------------------
# Privacy request lifecycle
# ---------------------------------------------------------------------------


def test_filing_a_privacy_request_requires_a_scope_description(person):
    with pytest.raises(ValueError):
        file_privacy_request(
            person=person, request_type=PrivacyRequestType.ACCESS, scope_description="   "
        )


def test_privacy_request_lifecycle_happy_path(person, operator):
    request = file_privacy_request(
        person=person,
        request_type=PrivacyRequestType.ACCESS,
        scope_description="Export of my registration data",
    )
    assert request.status == PrivacyRequestStatus.RECEIVED
    in_progress = transition_privacy_request_status(
        request, PrivacyRequestStatus.IN_PROGRESS, actor=operator
    )
    assert in_progress.status == PrivacyRequestStatus.IN_PROGRESS
    completed = transition_privacy_request_status(
        in_progress,
        PrivacyRequestStatus.COMPLETED,
        actor=operator,
        resolution_note="Data provided via secure channel.",
    )
    assert completed.status == PrivacyRequestStatus.COMPLETED
    completed.refresh_from_db()
    assert completed.resolved_at is not None
    assert completed.handled_by_id == operator.pk


def test_invalid_privacy_request_transition_is_rejected(person, operator):
    request = file_privacy_request(
        person=person,
        request_type=PrivacyRequestType.DELETION,
        scope_description="Delete my account",
    )
    with pytest.raises(InvalidPrivacyRequestTransitionError):
        transition_privacy_request_status(request, PrivacyRequestStatus.COMPLETED, actor=operator)


def test_privacy_request_transition_requires_permission(person, unauthorized_operator):
    request = file_privacy_request(
        person=person,
        request_type=PrivacyRequestType.ACCESS,
        scope_description="Access request",
    )
    with pytest.raises(PrivacyAuthorizationError):
        transition_privacy_request_status(
            request, PrivacyRequestStatus.IN_PROGRESS, actor=unauthorized_operator
        )


def test_terminal_privacy_request_status_cannot_be_reopened(person, operator):
    request = file_privacy_request(
        person=person, request_type=PrivacyRequestType.ACCESS, scope_description="Access request"
    )
    withdrawn = transition_privacy_request_status(
        request, PrivacyRequestStatus.WITHDRAWN, actor=operator
    )
    with pytest.raises(InvalidPrivacyRequestTransitionError):
        transition_privacy_request_status(
            withdrawn, PrivacyRequestStatus.IN_PROGRESS, actor=operator
        )


# ---------------------------------------------------------------------------
# Retention category (no invented duration)
# ---------------------------------------------------------------------------


def test_retention_category_may_leave_the_period_unset():
    category = RetentionCategory.objects.create(
        code="PENDING_POLICY", description="Awaiting policy approval"
    )
    assert category.retention_period_days is None


# ---------------------------------------------------------------------------
# Legal Hold
# ---------------------------------------------------------------------------


def test_placing_a_legal_hold_requires_a_reason(registration, operator):
    with pytest.raises(LegalHoldError):
        place_legal_hold(registration=registration, reason="  ", placed_by=operator)


def test_placing_a_legal_hold_requires_permission(registration, unauthorized_operator):
    with pytest.raises(PrivacyAuthorizationError):
        place_legal_hold(
            registration=registration,
            reason="Unauthorized hold",
            placed_by=unauthorized_operator,
        )


def test_legal_hold_prevents_routine_deletion_eligibility(registration, operator):
    assert is_under_legal_hold(registration) is False
    place_legal_hold(registration=registration, reason="Litigation hold", placed_by=operator)
    assert is_under_legal_hold(registration) is True


def test_only_one_active_legal_hold_per_registration(registration, operator):
    place_legal_hold(registration=registration, reason="First hold", placed_by=operator)
    with pytest.raises(LegalHoldError):
        place_legal_hold(registration=registration, reason="Second hold", placed_by=operator)


def test_releasing_a_legal_hold_restores_routine_deletion_eligibility(registration, operator):
    hold = place_legal_hold(registration=registration, reason="Temporary hold", placed_by=operator)
    release_legal_hold(legal_hold=hold, released_by=operator)
    assert is_under_legal_hold(registration) is False


def test_releasing_an_already_released_legal_hold_is_rejected(registration, operator):
    hold = place_legal_hold(registration=registration, reason="Temporary hold", placed_by=operator)
    release_legal_hold(legal_hold=hold, released_by=operator)
    with pytest.raises(LegalHoldError):
        release_legal_hold(legal_hold=hold, released_by=operator)


def test_a_new_legal_hold_can_be_placed_after_the_prior_one_is_released(registration, operator):
    hold = place_legal_hold(registration=registration, reason="First hold", placed_by=operator)
    release_legal_hold(legal_hold=hold, released_by=operator)
    second = place_legal_hold(registration=registration, reason="Second hold", placed_by=operator)
    assert LegalHold.objects.filter(registration=registration).count() == 2
    assert is_under_legal_hold(registration) is True
    assert second.released_at is None
