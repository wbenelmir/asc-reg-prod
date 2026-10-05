"""Database-level invariants (Phase 2 Prompt 3 §12)."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.reviews.models import (
    InformationRequest,
    InformationRequestPurpose,
    InformationRequestStatus,
    RegistrationDecision,
    RegistrationDecisionOutcome,
    ReviewAssignment,
    ReviewCase,
    ReviewCaseType,
    ReviewQueueCode,
)

from .conftest import make_operational_user_with_membership

pytestmark = pytest.mark.django_db


def _case(registration, event, organization):
    return ReviewCase.objects.create(
        registration=registration,
        case_type=ReviewCaseType.STANDARD,
        queue_code=ReviewQueueCode.GENERAL,
        event_edition=event,
        organization=organization,
        opened_at=timezone.now(),
    )


def test_priority_outside_bounds_is_rejected(registration, event, organization):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ReviewCase.objects.create(
                registration=registration,
                case_type=ReviewCaseType.STANDARD,
                queue_code=ReviewQueueCode.GENERAL,
                priority=0,
                event_edition=event,
                organization=organization,
                opened_at=timezone.now(),
            )


def test_only_one_current_assignment_per_case(registration, event, organization):
    case = _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="assignee@example.com", group_name="Registration Reviewers"
    )
    ReviewAssignment.objects.create(
        review_case=case,
        assigned_user=user,
        assigned_by=user,
        started_at=timezone.now(),
        is_current=True,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ReviewAssignment.objects.create(
                review_case=case,
                assigned_user=user,
                assigned_by=user,
                started_at=timezone.now(),
                is_current=True,
            )


def test_assignment_requires_exactly_one_target(registration, event, organization):
    from django.contrib.auth.models import Group

    case = _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="ambiguous-assignee@example.com", group_name="Registration Reviewers"
    )
    group = Group.objects.get(name="Registration Reviewers")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ReviewAssignment.objects.create(
                review_case=case,
                assigned_user=user,
                assigned_group=group,
                assigned_by=user,
                started_at=timezone.now(),
            )


def test_only_one_active_information_request_per_registration(registration):
    user = make_operational_user_with_membership(
        email="requester@example.com", group_name="Registration Reviewers"
    )
    InformationRequest.objects.create(
        registration=registration,
        sequence=1,
        status=InformationRequestStatus.SENT,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        created_by=user,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InformationRequest.objects.create(
                registration=registration,
                sequence=2,
                status=InformationRequestStatus.DRAFT,
                purpose=InformationRequestPurpose.CLARIFICATION,
                message_en="Another one.",
                created_by=user,
            )


def test_a_closed_request_never_blocks_a_new_one(registration):
    user = make_operational_user_with_membership(
        email="requester2@example.com", group_name="Registration Reviewers"
    )
    InformationRequest.objects.create(
        registration=registration,
        sequence=1,
        status=InformationRequestStatus.CLOSED,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Done.",
        created_by=user,
    )
    # No IntegrityError -- a CLOSED request is terminal.
    InformationRequest.objects.create(
        registration=registration,
        sequence=2,
        status=InformationRequestStatus.DRAFT,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="New one.",
        created_by=user,
    )


def test_a_submitted_request_still_blocks_a_new_round(registration):
    user = make_operational_user_with_membership(
        email="submitted-requester@example.com", group_name="Registration Reviewers"
    )
    InformationRequest.objects.create(
        registration=registration,
        sequence=1,
        status=InformationRequestStatus.SUBMITTED,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Response awaiting review.",
        created_by=user,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InformationRequest.objects.create(
                registration=registration,
                sequence=2,
                status=InformationRequestStatus.DRAFT,
                purpose=InformationRequestPurpose.CLARIFICATION,
                message_en="Premature second round.",
                created_by=user,
            )


def test_only_one_current_decision_per_registration(registration, event, organization):
    user = make_operational_user_with_membership(
        email="decider@example.com", group_name="Accreditation Managers"
    )
    RegistrationDecision.objects.create(
        registration=registration,
        sequence=1,
        outcome=RegistrationDecisionOutcome.NOT_APPROVED,
        internal_reason_code="INCOMPLETE",
        is_current=True,
        decided_by=user,
        decided_at=timezone.now(),
        event_edition=event,
        organization=organization,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            RegistrationDecision.objects.create(
                registration=registration,
                sequence=2,
                outcome=RegistrationDecisionOutcome.NOT_APPROVED,
                internal_reason_code="INCOMPLETE",
                is_current=True,
                decided_by=user,
                decided_at=timezone.now(),
                event_edition=event,
                organization=organization,
            )
