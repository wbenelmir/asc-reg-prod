"""Scope isolation for review selectors (Phase 2 Prompt 3 §6)."""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.reviews.models import ReviewCase, ReviewCaseType, ReviewQueueCode
from apps.reviews.selectors import review_cases_visible_to

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


def test_user_with_no_membership_sees_no_cases(registration, event, organization):
    from apps.accounts.models import OperationalUser, OperationalUserStatus

    _case(registration, event, organization)
    user = OperationalUser.objects.create_user(
        email="no-membership@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    assert review_cases_visible_to(user).count() == 0


def test_membership_scoped_to_wrong_organization_sees_nothing(
    registration, event, organization, other_organization
):
    _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="wrong-org@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=other_organization,
    )
    assert review_cases_visible_to(user).count() == 0


def test_membership_scoped_to_wrong_event_sees_nothing(
    registration, event, organization, other_event
):
    _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="wrong-event@example.com",
        group_name="Registration Reviewers",
        event_edition=other_event,
        organization=organization,
    )
    assert review_cases_visible_to(user).count() == 0


def test_correctly_scoped_membership_sees_the_case(registration, event, organization):
    case = _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="right-scope@example.com",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    assert review_cases_visible_to(user).filter(pk=case.pk).exists()


def test_permission_from_one_group_never_combines_with_scope_from_another(
    registration, event, organization
):
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import ScopedGroupMembership

    case = _case(registration, event, organization)
    user = make_operational_user_with_membership(
        email="split@example.com", group_name="Registration Reviewers"
    )
    # Remove the correctly-scoped membership; give the permission through
    # an UNSCOPED group instead, and a differently-scoped membership
    # through an UNRELATED group with no permission.
    user.scoped_memberships.all().delete()
    unscoped_group = Group.objects.create(name="Unrelated Unscoped Group")
    permission = Permission.objects.get(
        content_type__app_label="reviews", codename="view_reviewcase"
    )
    unscoped_group.permissions.add(permission)
    user.groups.add(unscoped_group)
    other_group = Group.objects.create(name="Scoped But Different Group")
    ScopedGroupMembership.objects.create(
        user=user,
        group=other_group,
        event_edition=event,
        organization=organization,
        granted_by=user,
    )
    assert review_cases_visible_to(user).filter(pk=case.pk).count() == 0


def test_superuser_sees_every_case(registration, event, organization):
    from apps.accounts.models import OperationalUser

    case = _case(registration, event, organization)
    superuser = OperationalUser.objects.create_superuser(
        email="super-reviews@example.com",
        password="__irrelevant_not_used_for_auth__",  # noqa: S106
    )
    assert review_cases_visible_to(superuser).filter(pk=case.pk).exists()
