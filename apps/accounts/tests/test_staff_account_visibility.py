"""Which staff accounts a scoped account administrator may READ (list,
search, filters, counts, pages, account pages and command URLs).

An administrator for one event or organization never sees another event's
or organization's accounts and roles; a shared account shows only the roles
inside the reader's scope. Synthetic data, PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import administration as admin
from apps.accounts.models import (
    CredentialSetupToken,
    OperationalUser,
    OperationalUserStatus,
    ScopedGroupMembership,
)
from apps.accounts.tests.sign_in import staff_sign_in
from apps.audit.models import AuditEvent
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType

pytestmark = pytest.mark.django_db

GOOD = "visibility-tests-correct-horse-42"


def _event(code):
    now = timezone.now()
    return EventEdition.objects.create(
        code=code, name=code, timezone="UTC", starts_at=now, ends_at=now + timedelta(days=3)
    )


def _organization(name):
    return Organization.objects.create(
        official_name=name, normalized_name=name.lower(), organization_type=OrganizationType.OTHER
    )


def _staff(email, **extra):
    return OperationalUser.objects.create_user(
        email=email,
        password=GOOD,
        status=OperationalUserStatus.ACTIVE,
        display_name=email.split("@")[0],
        **extra,
    )


def _grant(user, group_name, *, event=None, organization=None, ended=False):
    now = timezone.now()
    return ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name=group_name),
        event_edition=event,
        organization=organization,
        granted_by=user,
        active_from=now - timedelta(days=2),
        active_until=now - timedelta(days=1) if ended else None,
    )


@pytest.fixture
def world():
    e1, e2 = _event("VIS1"), _event("VIS2")
    o1, o2 = _organization("Vis Org One"), _organization("Vis Org Two")
    root = _staff("root-admin@example.test")
    _grant(root, "Account Administrators")
    admin_e1 = _staff("admin-e1@example.test")
    _grant(admin_e1, "Account Administrators", event=e1)
    admin_e2 = _staff("admin-e2@example.test")
    _grant(admin_e2, "Account Administrators", event=e2)
    admin_o1 = _staff("admin-e1-o1@example.test")
    _grant(admin_o1, "Account Administrators", event=e1, organization=o1)

    only_e1 = _staff("only-e1@example.test")
    _grant(only_e1, "Accreditation Managers", event=e1)
    only_e2 = _staff("only-e2@example.test")
    _grant(only_e2, "Accreditation Managers", event=e2)
    shared = _staff("shared@example.test")
    m_shared_e1 = _grant(shared, "Accreditation Managers", event=e1)
    m_shared_e2 = _grant(shared, "Accreditation Coordinators", event=e2)
    every_event = _staff("every-event@example.test")
    _grant(every_event, "Accreditation Managers")
    ended_e1 = _staff("ended-e1@example.test")
    _grant(ended_e1, "Accreditation Managers", event=e1, ended=True)
    e1_o1 = _staff("e1-o1@example.test")
    _grant(e1_o1, "Accreditation Managers", event=e1, organization=o1)
    e1_o2 = _staff("e1-o2@example.test")
    _grant(e1_o2, "Accreditation Managers", event=e1, organization=o2)
    unassigned_root = admin.create_account(
        actor=root,
        email="new-by-root@example.test",
        display_name="New by root",
        account_type="INTERNAL",
    )
    unassigned_e1 = admin.create_account(
        actor=admin_e1,
        email="new-by-e1@example.test",
        display_name="New by e1",
        account_type="INTERNAL",
    )
    return {
        "e1": e1,
        "e2": e2,
        "o1": o1,
        "root": root,
        "admin_e1": admin_e1,
        "admin_e2": admin_e2,
        "admin_o1": admin_o1,
        "only_e1": only_e1,
        "only_e2": only_e2,
        "shared": shared,
        "m_shared_e1": m_shared_e1,
        "m_shared_e2": m_shared_e2,
        "every_event": every_event,
        "ended_e1": ended_e1,
        "e1_o1": e1_o1,
        "e1_o2": e1_o2,
        "unassigned_root": unassigned_root,
        "unassigned_e1": unassigned_e1,
    }


def _client(user) -> Client:
    client = Client()
    assert staff_sign_in(client, user.email_normalized, GOOD).status_code == 302
    return client


def _listed(client, **params) -> tuple[set[str], int]:
    response = client.get(reverse("staff_accounts:list"), params)
    assert response.status_code == 200
    page = response.context["page"]
    emails = {account.email_normalized for account in page.object_list}
    return emails, page.paginator.count


def _emails(*users) -> set[str]:
    return {user.email_normalized for user in users}


# ---------------------------------------------------------------------------
# The rule itself (service)
# ---------------------------------------------------------------------------


def test_an_event_administrator_reads_only_accounts_with_a_role_in_that_event(world):
    w = world
    visible = set(admin.visible_accounts(w["admin_e1"]).values_list("email_normalized", flat=True))
    assert visible == _emails(
        w["admin_e1"],
        w["admin_o1"],
        w["only_e1"],
        w["shared"],
        w["ended_e1"],
        w["e1_o1"],
        w["e1_o2"],
        w["unassigned_e1"],
    )


def test_an_organization_administrator_reads_only_that_organization(world):
    w = world
    visible = set(admin.visible_accounts(w["admin_o1"]).values_list("email_normalized", flat=True))
    assert visible == _emails(w["admin_o1"], w["e1_o1"])


def test_an_unrestricted_administrator_reads_every_account(world):
    assert admin.visible_accounts(world["root"]).count() == OperationalUser.objects.count()


def test_a_new_account_stays_with_its_creator_until_it_has_a_role(world):
    w = world
    assert w["unassigned_e1"].created_by_id == w["admin_e1"].pk
    assert not admin.visible_accounts(w["admin_e2"]).filter(pk=w["unassigned_e1"].pk).exists()
    assert not admin.visible_accounts(w["admin_e1"]).filter(pk=w["unassigned_root"].pk).exists()
    # Given a role in another event only, it leaves its creator's view.
    _grant(w["unassigned_e1"], "Accreditation Managers", event=w["e2"])
    assert not admin.visible_accounts(w["admin_e1"]).filter(pk=w["unassigned_e1"].pk).exists()
    assert admin.visible_accounts(w["admin_e2"]).filter(pk=w["unassigned_e1"].pk).exists()


def test_visible_memberships_excludes_every_event_roles_for_an_event_administrator(world):
    w = world
    scope = admin.administration_scope(w["admin_e1"])
    roles = admin.visible_memberships(
        scope, ScopedGroupMembership.objects.filter(user__in=[w["shared"], w["every_event"]])
    )
    assert set(roles) == {w["m_shared_e1"]}


# ---------------------------------------------------------------------------
# List, search, filters, counts
# ---------------------------------------------------------------------------


def test_the_list_count_and_page_hold_only_readable_accounts(world):
    w = world
    emails, count = _listed(_client(w["admin_e1"]))
    assert emails == set(
        admin.visible_accounts(w["admin_e1"]).values_list("email_normalized", flat=True)
    )
    assert count == len(emails) == 8
    for hidden in ("only_e2", "every_event", "unassigned_root", "admin_e2", "root"):
        assert w[hidden].email_normalized not in emails


def test_search_never_finds_an_account_outside_the_scope(world):
    client = _client(world["admin_e1"])
    assert _listed(client, q="only-e2") == (set(), 0)
    assert _listed(client, q="root-admin") == (set(), 0)
    assert _listed(client, q="only-e1") == (_emails(world["only_e1"]), 1)


def test_the_role_filter_uses_only_roles_inside_the_scope(world):
    w = world
    client = _client(w["admin_e1"])
    # `shared` holds Accreditation Coordinators only in event 2.
    emails, count = _listed(client, role="accreditation-coordinator")
    assert (emails, count) == (set(), 0)
    emails, _count = _listed(client, role="accreditation-manager")
    assert w["shared"].email_normalized in emails
    assert w["only_e2"].email_normalized not in emails
    assert w["every_event"].email_normalized not in emails


def test_the_list_shows_only_roles_inside_the_scope(world):
    w = world
    response = _client(w["admin_e1"]).get(reverse("staff_accounts:list"), {"q": "shared"})
    (shared,) = response.context["page"].object_list
    assert [str(role) for role in shared.current_roles] == ["Accreditation manager"]
    assert shared.has_hidden_roles is True
    records = response.content.decode().split('class="asc-records"', 1)[1]
    assert "Accreditation coordinator" not in records.split("</ul>", 1)[0]


def test_pages_never_reach_outside_the_scope(world):
    w = world
    for number in range(30):
        _grant(
            _staff(f"bulk-e2-{number:02d}@example.test"), "Accreditation Managers", event=w["e2"]
        )
    client = _client(w["admin_e1"])
    for page in ("1", "2", "99"):
        emails, count = _listed(client, page=page)
        assert count == 8
        assert not any(email.startswith("bulk-e2-") for email in emails)
    emails, count = _listed(_client(w["admin_e2"]))
    assert count == 33  # 30 bulk + only_e2 + shared + admin_e2


# ---------------------------------------------------------------------------
# Account page and guessed command URLs
# ---------------------------------------------------------------------------


def _command_posts(target, membership_id):
    return [
        ("staff_accounts:status", {"command": "suspend", "reason": "Visibility test"}),
        ("staff_accounts:update", {"display_name": "Renamed", "reason": "Visibility test"}),
        ("staff_accounts:credential-link", {"purpose": "RESET"}),
        ("staff_accounts:grant", {"role": "accreditation-manager", "reason": "Visibility test"}),
        ("staff_accounts:revoke", {"membership_id": membership_id, "reason": "Visibility test"}),
    ]


def test_a_guessed_account_url_outside_the_scope_is_not_found(world):
    w = world
    client = _client(w["admin_e1"])
    target = w["only_e2"]
    membership = target.scoped_memberships.get()
    audit_before = AuditEvent.objects.count()
    tokens_before = CredentialSetupToken.objects.count()
    assert client.get(reverse("staff_accounts:detail", kwargs={"pk": target.pk})).status_code == 404
    for name, data in _command_posts(target, membership.pk):
        response = client.post(reverse(name, kwargs={"pk": target.pk}), data)
        assert response.status_code == 404, name
    target.refresh_from_db()
    membership.refresh_from_db()
    assert target.status == OperationalUserStatus.ACTIVE
    assert target.display_name == "only-e2"
    assert membership.active_until is None
    assert CredentialSetupToken.objects.count() == tokens_before
    assert AuditEvent.objects.count() == audit_before
    # The same answer as for an unknown id.
    unknown = reverse(
        "staff_accounts:detail", kwargs={"pk": "01a10000-0000-7000-8000-000000000000"}
    )
    assert client.get(unknown).status_code == 404


def test_a_shared_account_shows_only_roles_inside_the_scope(world):
    w = world
    response = _client(w["admin_e1"]).get(
        reverse("staff_accounts:detail", kwargs={"pk": w["shared"].pk})
    )
    assert response.status_code == 200
    shown = {membership.pk for membership in response.context["memberships"]}
    assert shown == {w["m_shared_e1"].pk}
    assert response.context["hidden_roles"] is True
    assert response.context["can_administer"] is False
    assert response.context["open_link"] is None
    html = response.content.decode()
    assert "VIS2" not in html
    assert str(w["m_shared_e2"].pk) not in html
    roles_section = html.split('id="roles-heading"', 1)[1].split("</section>", 1)[0]
    assert roles_section.count("data-membership=") == 1
    assert "Accreditation coordinator" not in roles_section
    assert "Roles outside your administration scope are not shown." in html
    assert "Setup link" not in html


def test_setup_link_metadata_is_shown_only_to_an_administrator_who_may_send_links(world):
    w = world
    admin.issue_credential_setup(actor=w["root"], target=w["shared"], purpose="RESET")
    admin.issue_credential_setup(actor=w["root"], target=w["only_e1"], purpose="RESET")
    client = _client(w["admin_e1"])
    shared = client.get(reverse("staff_accounts:detail", kwargs={"pk": w["shared"].pk}))
    assert shared.context["open_link"] is None
    own_scope = client.get(reverse("staff_accounts:detail", kwargs={"pk": w["only_e1"].pk}))
    assert own_scope.context["open_link"] is not None
    assert "Setup link" in own_scope.content.decode()


def test_an_unrestricted_administrator_reads_every_role(world):
    w = world
    response = _client(w["root"]).get(
        reverse("staff_accounts:detail", kwargs={"pk": w["shared"].pk})
    )
    shown = {membership.pk for membership in response.context["memberships"]}
    assert shown == {w["m_shared_e1"].pk, w["m_shared_e2"].pk}
    assert response.context["hidden_roles"] is False
    emails, count = _listed(_client(w["root"]))
    assert count == OperationalUser.objects.count()


def test_an_organization_administrator_cannot_open_another_organization(world):
    w = world
    client = _client(w["admin_o1"])
    assert (
        client.get(reverse("staff_accounts:detail", kwargs={"pk": w["e1_o2"].pk})).status_code
        == 404
    )
    assert (
        client.get(reverse("staff_accounts:detail", kwargs={"pk": w["e1_o1"].pk})).status_code
        == 200
    )
    assert _listed(client)[0] == _emails(w["admin_o1"], w["e1_o1"])


def test_a_new_account_page_opens_for_its_creator_only(world):
    w = world
    target = w["unassigned_e1"]
    url = reverse("staff_accounts:detail", kwargs={"pk": target.pk})
    assert _client(w["admin_e1"]).get(url).status_code == 200
    assert _client(w["admin_e2"]).get(url).status_code == 404
    assert _client(w["root"]).get(url).status_code == 200
    root_new = reverse("staff_accounts:detail", kwargs={"pk": w["unassigned_root"].pk})
    assert _client(w["admin_e1"]).get(root_new).status_code == 404
