"""UPD-05: the complete operations registrations list (`/ops/registrations/`).

Synthetic registrations only. Covers: no truncation (every page reachable,
50 per page, stable order), filters and the reference search applied before
pagination, the scope and matching counts, cancelled and withdrawn
registrations visible with clear statuses, the private name / e-mail search
kept out of the address, empty scope versus zero matches, and the scope of
non-superadmin staff.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.people.tests.conftest import TEST_OPERATIONAL_PASSWORD, staff_client
from apps.registrations.models import Registration, RegistrationProfile

pytestmark = pytest.mark.django_db

URL = reverse("registrations:ops-intake-list")
SEARCH_URL = reverse("registrations:ops-search")


def _event(code: str):
    from apps.events.models import EventEdition

    return EventEdition.objects.create(
        code=code,
        name=f"List test {code}",
        timezone="Africa/Algiers",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        ends_at=timezone.now() + datetime.timedelta(days=32),
        status="REGISTRATION_OPEN",
    )


def _organization(name: str):
    from apps.organizations.models import Organization

    return Organization.objects.create(
        official_name=name, normalized_name=name.lower(), organization_type="COMPANY"
    )


def _registrations(event, count: int, *, prefix: str, organization=None, status="SUBMITTED"):
    base = timezone.now() - datetime.timedelta(days=5)
    rows = [
        Registration(
            public_reference=f"{prefix}{index:05d}",
            event_edition=event,
            source_kind="OPEN" if organization is None else "ON_BEHALF",
            source_context_key=f"list-test:{uuid.uuid4().hex}",
            source_organization=organization,
            public_status=status,
            submitted_at=None if status == "DRAFT" else base + datetime.timedelta(minutes=index),
        )
        for index in range(count)
    ]
    created = Registration.objects.bulk_create(rows)
    # Distinct creation times, oldest first, so the order is deterministic.
    for index, registration in enumerate(created):
        Registration.objects.filter(pk=registration.pk).update(
            created_at=base + datetime.timedelta(minutes=index)
        )
    return created


def _staff(email: str, *, superuser: bool = False, event=None, organization=None):
    user = OperationalUser.objects.create_user(
        email=email,
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=superuser,
    )
    if not superuser:
        ScopedGroupMembership.objects.create(
            user=user,
            group=Group.objects.get(name="Registration Intake"),
            event_edition=event,
            organization=organization,
            granted_by=user,
        )
    return user


@pytest.fixture
def world():
    event = _event("UPD05A")
    other_event = _event("UPD05B")
    org = _organization("Upd05 Org")
    main = _registrations(event, 120, prefix="LSTA")
    on_behalf = _registrations(event, 7, prefix="LSTO", organization=org)
    other = _registrations(other_event, 5, prefix="LSTB")
    withdrawn = _registrations(event, 3, prefix="LSTW", status="WITHDRAWN")
    Registration.objects.filter(pk=withdrawn[0].pk).update(cancelled_at=timezone.now())
    Registration.objects.filter(pk__in=[r.pk for r in withdrawn[1:]]).update(
        withdrawn_at=timezone.now()
    )
    drafts = _registrations(event, 2, prefix="LSTD", status="DRAFT")
    return {
        "event": event,
        "other_event": other_event,
        "org": org,
        "main": main,
        "on_behalf": on_behalf,
        "other": other,
        "withdrawn": withdrawn,
        "drafts": drafts,
        "total": 120 + 7 + 5 + 3 + 2,
    }


def _references(response) -> list[str]:
    return [registration.public_reference for registration in response.context["registrations"]]


def test_the_superadmin_sees_every_registration_on_paginated_pages(world) -> None:
    client = staff_client(_staff("upd05-super@example.test", superuser=True))
    response = client.get(URL)
    assert response.status_code == 200
    assert response.context["scope_total"] == world["total"]
    assert response.context["page"].paginator.count == world["total"]
    assert len(_references(response)) == 50
    seen = []
    for number in range(1, response.context["page"].paginator.num_pages + 1):
        seen += _references(client.get(URL, {"page": number}))
    assert len(seen) == len(set(seen)) == world["total"]  # nothing truncated, nothing twice
    assert "Only the 200 most recent" not in response.content.decode()


def test_a_reference_outside_the_first_page_is_found(world) -> None:
    client = staff_client(_staff("upd05-ref@example.test", superuser=True))
    first_page = _references(client.get(URL))
    oldest = world["main"][0].public_reference
    assert oldest not in first_page
    response = client.get(URL, {"reference": oldest.lower()})
    assert _references(response) == [oldest]
    assert response.context["matching_total"] == 1
    prefix = client.get(URL, {"reference": "LSTO"})
    assert sorted(_references(prefix)) == sorted(r.public_reference for r in world["on_behalf"])


def test_filters_apply_before_pagination_and_counts_agree(world) -> None:
    client = staff_client(_staff("upd05-filter@example.test", superuser=True))
    response = client.get(URL, {"status": "WITHDRAWN"})
    assert response.context["matching_total"] == 3
    assert response.context["scope_total"] == world["total"]
    cancelled = client.get(URL, {"status": "CANCELLED_BY_TEAM"})
    assert _references(cancelled) == [world["withdrawn"][0].public_reference]
    assert "Cancelled by the team" in cancelled.content.decode()
    withdrawn = client.get(URL, {"status": "WITHDRAWN_BY_PARTICIPANT"})
    assert len(_references(withdrawn)) == 2
    by_org = client.get(URL, {"organization": str(world["org"].pk)})
    assert by_org.context["matching_total"] == 7
    no_org = client.get(URL, {"organization": "__missing__", "event": str(world["event"].pk)})
    assert no_org.context["matching_total"] == 120 + 3 + 2
    page_two = client.get(URL, {"event": str(world["event"].pk), "page": 2})
    assert page_two.context["page"].paginator.count == world["total"] - 5
    assert "event=" in page_two.context["querystring"]  # kept across pages
    drafts = client.get(URL, {"segment": "drafts"})
    assert drafts.context["matching_total"] == 2


def test_the_order_is_stable_newest_first(world) -> None:
    client = staff_client(_staff("upd05-order@example.test", superuser=True))
    references = _references(client.get(URL, {"event": str(world["event"].pk)}))
    created = list(
        Registration.objects.filter(public_reference__in=references).values_list(
            "public_reference", "created_at"
        )
    )
    by_reference = dict(created)
    times = [by_reference[reference] for reference in references]
    assert times == sorted(times, reverse=True)


def test_event_and_organization_scoped_staff_see_only_their_scope(world) -> None:
    event_staff = staff_client(_staff("upd05-event@example.test", event=world["other_event"]))
    response = event_staff.get(URL)
    assert sorted(_references(response)) == sorted(r.public_reference for r in world["other"])
    assert response.context["scope_total"] == 5
    outside = event_staff.get(URL, {"reference": world["main"][0].public_reference})
    assert _references(outside) == []
    assert outside.context["scope_total"] == 5
    assert 'data-intake-empty="no-match"' in outside.content.decode()

    org_staff = staff_client(_staff("upd05-org@example.test", organization=world["org"]))
    org_response = org_staff.get(URL)
    assert sorted(_references(org_response)) == sorted(
        r.public_reference for r in world["on_behalf"]
    )


def test_an_empty_scope_is_not_reported_as_zero_matches(world) -> None:
    lonely_event = _event("UPD05C")
    client = staff_client(_staff("upd05-empty@example.test", event=lonely_event))
    response = client.get(URL)
    assert response.context["scope_total"] == 0
    assert 'data-intake-empty="empty-scope"' in response.content.decode()


def test_staff_without_the_permission_are_refused(world) -> None:
    user = OperationalUser.objects.create_user(
        email="upd05-none@example.test",
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
    )
    assert staff_client(user).get(URL).status_code == 403


def test_a_name_search_is_private_and_scoped(world) -> None:
    target = world["main"][3]
    RegistrationProfile.objects.create(
        registration=target, submitted_given_names="Zelda", submitted_family_name="Quarkson"
    )
    user = _staff("upd05-search@example.test", superuser=True)
    client = staff_client(user)
    response = client.post(SEARCH_URL, {"q": "Quark"})
    assert response.status_code == 302
    assert "Quark" not in response.url and "ctx=" in response.url
    listed = client.get(response.url)
    assert _references(listed) == [target.public_reference]
    # The search context belongs to this reader only.
    other = staff_client(_staff("upd05-other@example.test", superuser=True))
    foreign = other.get(response.url)
    assert foreign.context["search_expired"] is True
    assert foreign.context["matching_total"] == world["total"]
    # A scoped reader never finds a registration outside the scope by name.
    scoped = staff_client(_staff("upd05-scoped@example.test", event=world["other_event"]))
    scoped_search = scoped.post(SEARCH_URL, {"q": "Quark"})
    assert _references(scoped.get(scoped_search.url)) == []


def test_the_page_uses_a_bounded_number_of_queries(world, django_assert_max_num_queries) -> None:
    client = staff_client(_staff("upd05-queries@example.test", superuser=True))
    client.get(URL)  # warm the session and the permission caches
    with django_assert_max_num_queries(25):
        response = client.get(URL, {"event": str(world["event"].pk), "page": 2})
    assert len(_references(response)) == 50
