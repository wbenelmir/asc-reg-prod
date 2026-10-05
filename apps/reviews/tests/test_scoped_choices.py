"""UI/UX Completion Gate F8: review controls use permission-scoped choices,
never typed database ids.

* The assignment control offers exactly the people who may open the case
  (`eligible_review_assignees`), and the assignment view accepts only them:
  an out-of-scope, non-existent, malformed or inactive user id is refused
  with the same response, so ids cannot be enumerated.
* The duplicate-resolution control offers only the case's own candidate
  records, labelled with masked values, and refuses any other identifier.
* The checklist control offers the active checklist's own items.
"""

from __future__ import annotations

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.reviews.models import ReviewCaseType
from apps.reviews.selectors import eligible_review_assignees
from apps.reviews.services import open_review_case

from .conftest import make_operational_user_with_membership, make_registration, sign_in_operational

pytestmark = pytest.mark.django_db


@pytest.fixture
def case(event, organization):
    registration = make_registration(event=event, organization=organization)
    return open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )


@pytest.fixture
def manager(event, organization):
    user = make_operational_user_with_membership(
        email="f8-manager@example.test",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    user.display_name = "Synthetic Manager"
    user.save(update_fields=["display_name"])
    return user


def _client(user) -> Client:
    client = Client()
    sign_in_operational(client, user.email_normalized)
    return client


def _assign(client, case, user_id) -> object:
    return client.post(
        reverse("reviews:case-assign", kwargs={"pk": case.pk}),
        {"assigned_user_id": user_id, "expected_version": case.version},
    )


# ---------------------------------------------------------------------------
# Assignees
# ---------------------------------------------------------------------------


def test_eligible_assignees_follow_the_case_scope(event, organization, other_organization, case):
    in_scope = make_operational_user_with_membership(
        email="f8-reviewer@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    other_org = make_operational_user_with_membership(
        email="f8-other-org@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=other_organization,
    )
    broad = make_operational_user_with_membership(
        email="f8-broad@example.test", group_name="Registration Reviewers"
    )
    no_review_permission = make_operational_user_with_membership(
        email="f8-exporter@example.test",
        group_name="Export Administrators",
        event_edition=event,
        organization=organization,
    )
    eligible = set(eligible_review_assignees(case))
    assert in_scope in eligible
    assert broad in eligible
    assert other_org not in eligible
    assert no_review_permission not in eligible


def test_inactive_accounts_and_memberships_are_not_eligible(event, organization, case):
    from apps.accounts.models import (
        OperationalUserStatus,
        ScopedGroupMembership,
        ScopedGroupMembershipStatus,
    )

    suspended = make_operational_user_with_membership(
        email="f8-suspended@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    suspended.status = OperationalUserStatus.SUSPENDED
    suspended.save(update_fields=["status"])
    ended = make_operational_user_with_membership(
        email="f8-ended@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=organization,
    )
    ScopedGroupMembership.objects.filter(user=ended).update(
        status=ScopedGroupMembershipStatus.SUSPENDED
    )
    eligible = set(eligible_review_assignees(case))
    assert suspended not in eligible
    assert ended not in eligible


def test_case_page_offers_named_choices_and_no_raw_id_field(case, manager):
    response = _client(manager).get(reverse("reviews:case-detail", kwargs={"pk": case.pk}))
    content = response.content.decode()
    assert response.status_code == 200
    assert "Operational user id" not in content
    assert 'type="text" id="id_assign_assigned_user_id"' not in content
    assert '<select name="assigned_user_id"' in content
    assert ">Synthetic Manager<" in content


def test_case_page_never_lists_an_out_of_scope_person(case, manager, event, other_organization):
    outsider = make_operational_user_with_membership(
        email="f8-outsider@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=other_organization,
    )
    outsider.display_name = "Out Of Scope Reviewer"
    outsider.save(update_fields=["display_name"])
    content = (
        _client(manager).get(reverse("reviews:case-detail", kwargs={"pk": case.pk})).content
    ).decode()
    assert "Out Of Scope Reviewer" not in content
    assert str(outsider.pk) not in content
    assert outsider.email_normalized not in content


def test_eligible_assignee_is_accepted(case, manager):
    response = _assign(_client(manager), case, str(manager.pk))
    assert response.status_code == 302
    assert case.assignments.filter(is_current=True, assigned_user=manager).exists()


@pytest.mark.parametrize("kind", ["out_of_scope", "unknown", "malformed", "empty"])
def test_forged_assignee_ids_are_refused_identically(
    kind, case, manager, event, other_organization
):
    outsider = make_operational_user_with_membership(
        email=f"f8-forged-{kind}@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=other_organization,
    )
    value = {
        "out_of_scope": str(outsider.pk),
        "unknown": str(uuid.uuid4()),
        "malformed": "not-a-uuid'; --",
        "empty": "",
    }[kind]
    client = _client(manager)
    response = _assign(client, case, value)
    # Same redirect back to the case, same generic message, nothing assigned:
    # the answer never reveals whether the id exists.
    assert response.status_code == 302
    assert response["Location"] == reverse("reviews:case-detail", kwargs={"pk": case.pk})
    assert case.assignments.count() == 0
    page = client.get(response["Location"]).content.decode()
    assert "Select a valid user to assign." in page


# ---------------------------------------------------------------------------
# Duplicate candidates and checklist items
# ---------------------------------------------------------------------------


def test_duplicate_resolution_refuses_an_identifier_that_is_not_a_candidate(event, organization):
    from apps.people.services import (
        create_identity_identifier,
        resolve_or_create_participant_for_email,
    )

    registration = make_registration(event=event, organization=organization)
    duplicate_case = open_review_case(
        registration=registration, case_type=ReviewCaseType.DUPLICATE, queue_code="GENERAL"
    )
    manager = make_operational_user_with_membership(
        email="f8-dup-manager@example.test",
        group_name="Accreditation Managers",
        event_edition=event,
        organization=organization,
    )
    # A real identifier that belongs to someone else and is NOT a candidate.
    other_person = resolve_or_create_participant_for_email("f8-unrelated@example.test")
    unrelated = create_identity_identifier(
        person=other_person,
        identifier_type="NIN",
        country_code_id="DZ",
        raw_value="109990000000000077",
    )
    client = _client(manager)
    for value in (str(unrelated.pk), str(uuid.uuid4()), "garbage"):
        response = client.post(
            reverse("reviews:case-resolve-duplicate", kwargs={"pk": duplicate_case.pk}),
            {"outcome": "DIFFERENT_PERSON", "candidate_identifier_id": value},
        )
        assert response.status_code == 302
        assert duplicate_case.duplicate_resolutions.count() == 0
    page = client.get(reverse("reviews:case-detail", kwargs={"pk": duplicate_case.pk}))
    content = page.content.decode()
    assert "Candidate identifier id" not in content
    assert str(unrelated.pk) not in content


def test_checklist_control_offers_the_active_checklist_items(case, event):
    from apps.reviews.services import create_checklist_definition

    create_checklist_definition(
        event_edition=event, case_type=ReviewCaseType.STANDARD, version_label="f8"
    )
    reviewer = make_operational_user_with_membership(
        email="f8-checklist@example.test",
        group_name="Registration Reviewers",
        event_edition=event,
        organization=case.organization,
    )
    content = (
        _client(reviewer).get(reverse("reviews:case-detail", kwargs={"pk": case.pk})).content
    ).decode()
    assert '<select id="id_item_code" name="item_code"' in content
    assert 'value="IDENTITY_PLAUSIBLE">Identity details are plausible<' in content
    assert "Item code" not in content
