"""The supervisor reconciliation queue (Phase 4 Prompt 3, ADR-0024, Schema
§11.7): permission-scoped by event, venue and gate; append-only, audited
actions; immutable evidence; localized pages. PostgreSQL; synthetic data.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import PassReasonCode
from apps.badges.services import new_operation_id, revoke_pass
from apps.entry.models import (
    EntryEvent,
    ReconciliationAction,
    ReconciliationCase,
)
from apps.entry.services import EntryConcurrencyError, EntryPermissionError, EntryStateError
from apps.entry.services.reconciliation import (
    cases_visible_to,
    may_open_queue,
    record_action,
)
from apps.entry.tests import factories, offline_factories

pytestmark = pytest.mark.django_db


@pytest.fixture
def conflict_case(offline_world, staff):
    """A PASS_REVOKED case at gate A (the pass was revoked after the
    device's last data, then admitted offline)."""
    credential = offline_world.credential
    revoke_pass(
        credential=credential,
        actor=staff,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    op = offline_world.store.next(
        offline_factories.base_operation(
            device=offline_world.device,
            package=offline_world.package,
            grant=offline_world.grant,
            credential=credential,
            occurred_at=timezone.now() + timedelta(seconds=2),
        )
    )
    offline_factories.sync(
        offline_world.synthetic, offline_world.store, [offline_world.store.envelope(op)]
    )
    return ReconciliationCase.objects.get(case_type="PASS_REVOKED")


def _signed_in(user) -> Client:
    from apps.accounts import session_expiry

    client = Client()
    client.force_login(user)
    session = client.session
    now = timezone.now().isoformat()
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return client


# ---------------------------------------------------------------------------
# Scoping: event, venue, gate; organization-scoped memberships grant nothing
# ---------------------------------------------------------------------------


def test_a_gate_scoped_supervisor_sees_only_the_cases_of_that_gate(
    conflict_case, supervisor, event, layout, other_event
):
    assert list(cases_visible_to(supervisor)) == [conflict_case]
    other_gate = factories.make_user(
        "gate.b.supervisor@example.test",
        group_name="Entry Supervisors",
        event=event,
        gate=layout.gate_b,
    )
    assert list(cases_visible_to(other_gate)) == []
    other_event_supervisor = factories.make_user(
        "other.event.supervisor@example.test", group_name="Entry Supervisors", event=other_event
    )
    assert list(cases_visible_to(other_event_supervisor)) == []
    event_wide = factories.make_user(
        "event.supervisor@example.test", group_name="Entry Supervisors", event=event
    )
    assert list(cases_visible_to(event_wide)) == [conflict_case]
    other_venue = factories.VenueLayout(event, code="V9")
    venue_scoped = factories.make_user("venue.supervisor@example.test")
    factories.grant(venue_scoped, "Entry Supervisors", event=event, venue=other_venue.venue)
    assert list(cases_visible_to(venue_scoped)) == []


def test_an_organization_scoped_membership_grants_no_reconciliation(conflict_case, event):
    from apps.organizations.models import Organization

    organization = Organization.objects.create(
        official_name="Synthetic Org", organization_type="COMPANY"
    )
    user = factories.make_user("org.supervisor@example.test")
    factories.grant(user, "Entry Supervisors", event=event, organization=organization)
    assert list(cases_visible_to(user)) == []
    assert not may_open_queue(user, event_edition=event)


def test_roles_without_the_permission_see_nothing(conflict_case, device_admin, operator, event):
    for user in (device_admin, operator):
        assert list(cases_visible_to(user)) == []
        assert not may_open_queue(user, event_edition=event)


# ---------------------------------------------------------------------------
# HTTP: queue, case, action
# ---------------------------------------------------------------------------


def test_the_queue_lists_scoped_cases_with_recovery_counts(conflict_case, supervisor, event):
    response = _signed_in(supervisor).get(
        reverse("entry:reconciliation-queue", kwargs={"event_pk": event.pk})
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert conflict_case.public_id in body
    assert response.context["counts"]["conflict"] == 1
    assert response.context["counts"]["uploaded"] == 1
    assert "no-store" in response["Cache-Control"] and "private" in response["Cache-Control"]


def test_the_queue_refuses_users_without_the_permission(conflict_case, device_admin, event):
    url = reverse("entry:reconciliation-queue", kwargs={"event_pk": event.pk})
    assert _signed_in(device_admin).get(url).status_code == 403
    anonymous = Client().get(url)
    assert anonymous.status_code == 302 and "sign-in" in anonymous["Location"]


def test_a_case_outside_the_scope_is_not_found_and_the_attempt_audited(
    conflict_case, event, layout
):
    other_gate = factories.make_user(
        "gate.b.supervisor@example.test",
        group_name="Entry Supervisors",
        event=event,
        gate=layout.gate_b,
    )
    client = _signed_in(other_gate)
    url = reverse("entry:reconciliation-case", kwargs={"public_id": conflict_case.public_id})
    assert client.get(url).status_code == 404
    action = reverse("entry:reconciliation-action", kwargs={"public_id": conflict_case.public_id})
    response = client.post(action, {"action": "CLOSE", "note": "not mine", "expected_version": 1})
    assert response.status_code == 404
    denied = AuditEvent.objects.filter(action_code=action_codes.RECONCILIATION_ACCESS_DENIED)
    assert denied.count() == 2
    conflict_case.refresh_from_db()
    assert conflict_case.status == "OPEN"


def test_the_case_page_shows_what_the_device_and_the_server_knew(conflict_case, supervisor):
    response = _signed_in(supervisor).get(
        reverse("entry:reconciliation-case", kwargs={"public_id": conflict_case.public_id})
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "Pass revoked before the offline admission" in body
    labels = [row[0] for row in response.context["device_rows"]]
    assert "Result" in labels and "Freshness" in labels
    server = dict((row[0], row[1]) for row in response.context["server_rows"])
    assert server["Pass status at the admission"] == "REVOKED"
    assert response.context["participant"]["registration_reference"]


def test_closing_a_case_requires_a_note_and_is_audited_and_final(conflict_case, supervisor):
    client = _signed_in(supervisor)
    url = reverse("entry:reconciliation-action", kwargs={"public_id": conflict_case.public_id})
    response = client.post(url, {"action": "CLOSE", "note": "", "expected_version": 1})
    assert response.status_code == 302
    conflict_case.refresh_from_db()
    assert conflict_case.status == "OPEN"
    client.post(url, {"action": "NOTE", "note": "Checked the gate log.", "expected_version": 1})
    client.post(
        url, {"action": "CLOSE", "note": "Participant was entitled.", "expected_version": 2}
    )
    conflict_case.refresh_from_db()
    assert conflict_case.status == "CLOSED" and conflict_case.closed_by == supervisor
    actions = list(conflict_case.actions.order_by("created_at").values_list("action", flat=True))
    assert actions == ["NOTE", "CLOSE"]
    audits = AuditEvent.objects.filter(target_uuid=conflict_case.pk)
    assert {a.action_code for a in audits} >= {
        action_codes.RECONCILIATION_NOTE_ADDED,
        action_codes.RECONCILIATION_CASE_CLOSED,
    }
    for audit in audits:
        assert "entitled" not in str(audit.after_summary)  # the note is never audited
    # The original evidence is never rewritten by reconciliation.
    assert EntryEvent.objects.get().offline_conflict is True


def test_record_action_enforces_version_state_and_scope(conflict_case, supervisor, event, layout):
    with pytest.raises(EntryConcurrencyError):
        record_action(
            case=conflict_case, actor=supervisor, action="NOTE", note="n", expected_version=9
        )
    other_gate = factories.make_user(
        "gate.b.supervisor@example.test",
        group_name="Entry Supervisors",
        event=event,
        gate=layout.gate_b,
    )
    with pytest.raises(EntryPermissionError):
        record_action(
            case=conflict_case, actor=other_gate, action="CLOSE", note="n", expected_version=1
        )
    record_action(
        case=conflict_case, actor=supervisor, action="CLOSE", note="done", expected_version=1
    )
    with pytest.raises(EntryStateError):
        record_action(
            case=conflict_case, actor=supervisor, action="NOTE", note="again", expected_version=2
        )


def test_reconciliation_evidence_is_immutable_in_the_database(conflict_case, supervisor):
    record_action(
        case=conflict_case, actor=supervisor, action="CLOSE", note="done", expected_version=1
    )
    for change in (
        {"case_type": "WRONG_EVENT"},
        {"server_known_state": {}},
        {"status": "OPEN", "closed_at": None, "closed_by": None},
    ):
        with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
            ReconciliationCase.objects.filter(pk=conflict_case.pk).update(**change)
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        ReconciliationCase.objects.filter(pk=conflict_case.pk).delete()
    action = ReconciliationAction.objects.get()
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        ReconciliationAction.objects.filter(pk=action.pk).update(action="NOTE")
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        ReconciliationAction.objects.filter(pk=action.pk).delete()
    with pytest.raises(Exception), transaction.atomic():  # noqa: B017, PT011
        conflict_case.links.all().delete()


# ---------------------------------------------------------------------------
# English, French, Arabic (RTL)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "direction", "heading"),
    [
        ("en", "ltr", "Offline reconciliation"),
        ("fr", "ltr", "Rapprochement hors ligne"),
        ("ar", "rtl", "المطابقة دون اتصال"),
    ],
)
def test_the_queue_and_case_are_localized(
    conflict_case, supervisor, event, settings, language, direction, heading
):
    client = _signed_in(supervisor)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    queue = client.get(reverse("entry:reconciliation-queue", kwargs={"event_pk": event.pk}))
    body = queue.content.decode()
    assert f'lang="{language}"' in body and f'dir="{direction}"' in body
    assert heading in body
    case = client.get(
        reverse("entry:reconciliation-case", kwargs={"public_id": conflict_case.public_id})
    )
    assert case.status_code == 200 and f'dir="{direction}"' in case.content.decode()
