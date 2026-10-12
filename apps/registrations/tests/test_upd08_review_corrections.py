"""Review corrections R-01 to R-03 of the statistics reports (UPD-08).

* R-01: the attendee list of the final-approved report is limited to the
  registrations the reader may EXPORT (event and organization scope of
  `exports.add_exportrequest`), whatever the reader may VIEW; the withheld
  count is stated in the page, the workbook, the print report and the audit.
* R-02: source breakdowns are complete (no silent truncation): the page shows
  the largest groups plus a disclosed "Other" remainder that keeps the total,
  the reports list every group.
* R-03: weekly bars never widen the requested date range; a reversed range is
  put in order everywhere.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ScopedGroupMembership
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.people.tests.conftest import staff_client
from apps.registrations import ops_list, statistics
from apps.registrations.models import Registration, RegistrationProfile
from apps.registrations.tests.test_upd08_statistics import _user, _workbook, build_world

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    return build_world()


STATS_URL = reverse("registrations:statistics")
NOT_LISTED = "final-approved registrations are not listed"


def _export_url(file_format: str) -> str:
    return reverse(
        "registrations:statistics-export",
        kwargs={"report_kind": "approved", "file_format": file_format},
    )


def _approved_in(world, organization, reference: str) -> Registration:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.identity_fixtures import make_verified_identity_case
    from apps.reviews.models import RegistrationDecision

    row = Registration.objects.create(
        public_reference=reference,
        event_edition=world["event"],
        person=resolve_or_create_participant_for_email(f"r01-{uuid.uuid4().hex[:8]}@example.test"),
        source_kind="ON_BEHALF",
        source_context_key=f"r01:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status="APPROVED",
        submitted_at=timezone.now() - datetime.timedelta(days=1),
    )
    RegistrationProfile.objects.create(
        registration=row,
        submitted_given_names="Synthetic",
        submitted_family_name=reference,
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
    )
    RegistrationDecision.objects.create(
        registration=row,
        sequence=1,
        outcome="APPROVED",
        internal_reason_code="X",
        is_current=True,
        decided_by=_user(f"r01-decider-{uuid.uuid4().hex[:6]}@example.test"),
        decided_at=timezone.now(),
        event_edition=world["event"],
        organization=organization,
    )
    make_verified_identity_case(row)
    return row


@pytest.fixture
def split_scope(world):
    """Event-wide VIEW scope, EXPORT scope for organization A only."""
    _approved_in(world, world["other_org"], "ST-B-APPROVED")
    user = _user("r01-split@example.test")
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name="Registration Intake"),
        event_edition=world["event"],
        granted_by=user,
    )
    ScopedGroupMembership.objects.create(
        user=user,
        group=Group.objects.get(name="Export Administrators"),
        event_edition=world["event"],
        organization=world["org"],
        granted_by=user,
    )
    return user


def _attendee_references(sheets) -> list[str]:
    return sorted(row[0] for row in sheets.get("Attendees", [])[1:])


def _all_text(sheets) -> str:
    return " ".join(str(cell) for rows in sheets.values() for row in rows for cell in row)


# ---------------------------------------------------------------------------
# R-01
# ---------------------------------------------------------------------------


def test_the_attendee_list_never_leaves_the_export_scope(world, split_scope):
    client = staff_client(split_scope)
    event = str(world["event"].pk)
    # The user may VIEW organization B's approved registration...
    listed = client.get(reverse("registrations:ops-intake-list"), {"event": event})
    assert "ST-B-APPROVED" in listed.content.decode()

    # ...but the workbook lists only organization A (no organization filter).
    sheets = _workbook(client.get(_export_url("xlsx"), {"event": event}))
    assert _attendee_references(sheets) == ["ST-AGAIN", "ST-APPROVED-1"]
    assert "ST-B-APPROVED" not in _all_text(sheets)
    assert "ST-APPROVED-2" not in _all_text(sheets)  # no organization: outside an org scope
    assert f"2 of 4 {NOT_LISTED}" in _all_text(sheets)

    # The print report: the same rows and the same statement.
    html = client.get(_export_url("pdf"), {"event": event}).content.decode()
    assert 'data-stats-attendees="2"' in html
    assert "ST-APPROVED-1" in html and "ST-B-APPROVED" not in html
    assert 'data-attendee-scope="partial"' in html

    # Requesting organization B itself lists nobody and says why.
    params = {"event": event, "organization": str(world["other_org"].pk)}
    sheets = _workbook(client.get(_export_url("xlsx"), params))
    assert _attendee_references(sheets) == []
    assert "ST-B-APPROVED" not in _all_text(sheets)
    assert f"1 of 1 {NOT_LISTED}" in _all_text(sheets)
    html = client.get(_export_url("pdf"), params).content.decode()
    assert "ST-B-APPROVED" not in html and 'data-stats-attendees="0"' in html

    # The audit records what was actually listed and withheld (counts only).
    summaries = [
        event_row.after_summary
        for event_row in AuditEvent.objects.filter(
            action_code=action_codes.STATISTICS_REPORT_EXPORTED, actor_user_id=split_scope.pk
        ).order_by("occurred_at")
    ]
    assert summaries[0]["attendee_rows"] == 2
    assert summaries[0]["attendee_rows_withheld"] == 2
    assert summaries[0]["attendee_scope"] == "partial"
    assert summaries[-1]["attendee_rows"] == 0
    assert summaries[-1]["attendee_rows_withheld"] == 1

    # The page states the restriction before any download.
    page = client.get(STATS_URL, {"event": event})
    access = page.context["attendee_access"]
    assert (access.status, access.listed, access.total) == ("partial", 2, 4)
    assert 'data-attendee-scope="partial"' in page.content.decode()


def test_the_superadmin_keeps_the_complete_list(world, split_scope):
    admin = _user("r01-super@example.test", superuser=True)
    client = staff_client(admin)
    params = {"event": str(world["event"].pk)}
    sheets = _workbook(client.get(_export_url("xlsx"), params))
    assert _attendee_references(sheets) == [
        "ST-AGAIN",
        "ST-APPROVED-1",
        "ST-APPROVED-2",
        "ST-B-APPROVED",
    ]
    assert NOT_LISTED not in _all_text(sheets)
    html = client.get(_export_url("pdf"), params).content.decode()
    assert 'data-stats-attendees="4"' in html and "data-attendee-scope" not in html
    assert staff_client(admin).get(STATS_URL, params).context["attendee_access"].status == "full"


def test_staff_without_the_export_permission_get_aggregates_only(world, split_scope):
    intake = _user("r01-intake@example.test")
    ScopedGroupMembership.objects.create(
        user=intake,
        group=Group.objects.get(name="Registration Intake"),
        event_edition=world["event"],
        granted_by=intake,
    )
    client = staff_client(intake)
    params = {"event": str(world["event"].pk)}
    sheets = _workbook(client.get(_export_url("xlsx"), params))
    assert "Attendees" not in sheets and "Summary" in sheets
    html = client.get(_export_url("pdf"), params).content.decode()
    assert "data-stats-attendees" not in html
    for reference in ("ST-APPROVED-1", "ST-B-APPROVED"):
        assert reference not in html
    page = client.get(STATS_URL, params)
    assert page.context["attendee_access"].status == "none"
    assert 'data-attendee-scope="none"' in page.content.decode()


# ---------------------------------------------------------------------------
# R-02
# ---------------------------------------------------------------------------


@pytest.fixture
def many_organizations(world):
    from apps.organizations.models import Organization

    organizations = []
    for index in range(27):
        organization = Organization.objects.create(
            official_name=f"R02 Org {index:02d}",
            normalized_name=f"r02 org {index:02d}",
            organization_type="COMPANY",
        )
        organizations.append(organization)
        _approved_in(world, organization, f"R02-{index:02d}")
    return organizations


def test_source_breakdowns_are_complete_and_the_page_discloses_its_top_groups(
    world, many_organizations
):
    admin = _user("r02-super@example.test", superuser=True)
    filters = {"event": str(world["event"].pk)}
    report = statistics.build_report(admin, filters)
    applications = report.overview["applications"]
    organizations = report.sources["organizations"]
    # Complete: 27 + the world's two organizations + "No organization".
    assert len(organizations) == 30
    assert sum(row["applications"] for row in organizations) == applications
    assert not any(row.get("other") for row in organizations)

    page = staff_client(admin).get(STATS_URL, filters)
    shown = page.context["report"].sources["organizations"]
    named = [row for row in shown if not row.get("other") and not row.get("missing")]
    other = [row for row in shown if row.get("other")]
    assert len(named) == statistics.TOP_ROWS
    assert len(other) == 1 and other[0]["label"] == "Other (4 more)"
    assert sum(row["applications"] for row in shown) == applications
    assert sum(row["approved"] for row in shown) == report.overview["final_approved"]
    assert page.context["breakdowns_compacted"] is True
    assert "data-stats-compacted" in page.content.decode()

    # The reports list every group.
    situation = _workbook(
        staff_client(admin).get(
            reverse(
                "registrations:statistics-export",
                kwargs={"report_kind": "situation", "file_format": "xlsx"},
            ),
            filters,
        )
    )
    text = _all_text(situation)
    for index in range(27):
        assert f"R02 Org {index:02d}" in text
    assert "Other (" not in text
    printed = (
        staff_client(admin)
        .get(
            reverse(
                "registrations:statistics-export",
                kwargs={"report_kind": "situation", "file_format": "pdf"},
            ),
            filters,
        )
        .content.decode()
    )
    assert all(f"R02 Org {index:02d}" in printed for index in range(27))
    assert "data-stats-compacted" not in printed


def test_compact_rows_keep_the_total_and_the_missing_row():
    rows = [
        {"value": str(i), "label": str(i), "applications": 1, "approved": i % 2} for i in range(30)
    ]
    rows.append({"value": "-", "label": "None", "applications": 5, "approved": 0, "missing": True})
    compact, hidden = statistics.compact_rows(rows, limit=25)
    assert hidden == 5 and len(compact) == 27
    assert compact[-1]["missing"] and compact[-2]["other"]
    assert sum(row["applications"] for row in compact) == 35
    assert sum(row["approved"] for row in compact) == sum(row["approved"] for row in rows)
    assert statistics.compact_rows(rows[:10], limit=25) == (rows[:10], 0)


# ---------------------------------------------------------------------------
# R-03
# ---------------------------------------------------------------------------


def test_weekly_bars_never_widen_the_requested_range(world):
    from apps.people.services import resolve_or_create_participant_for_email

    admin = _user("r03-super@example.test", superuser=True)
    Registration.objects.filter(event_edition=world["event"], submitted_at__isnull=False).update(
        submitted_at=datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)
    )
    for reference, day in (("R03-MON", 12), ("R03-WED", 14), ("R03-THU", 15), ("R03-FRI", 16)):
        Registration.objects.create(
            public_reference=reference,
            event_edition=world["event"],
            person=resolve_or_create_participant_for_email(f"{reference.lower()}@example.test"),
            source_kind="OPEN",
            source_context_key=f"r03:{uuid.uuid4().hex}",
            public_status="SUBMITTED",
            submitted_at=datetime.datetime(2026, 10, day, 12, tzinfo=datetime.UTC),
        )
    filters = {
        "event": str(world["event"].pk),
        "submitted_from": "2026-10-14",
        "submitted_to": "2026-10-15",
        "period": "week",
    }
    rng = statistics.trend_range(filters)
    assert rng.start == datetime.date(2026, 10, 14)  # the requested start, not Monday
    assert rng.buckets == [datetime.date(2026, 10, 12)]  # the week label is Monday
    trends = statistics.trends(admin, filters)
    assert [row["applications"] for row in trends["rows"]] == [2]  # Wednesday and Thursday
    # The baseline counts everything before the requested start, Monday included.
    world_submitted = Registration.objects.filter(
        event_edition=world["event"],
        submitted_at__lt=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
    ).count()
    assert trends["baseline_applications"] == world_submitted + 1
    assert trends["rows"][0]["cumulative_applications"] == world_submitted + 3

    daily = statistics.trends(admin, {**filters, "period": "day"})
    assert [row["applications"] for row in daily["rows"]] == [1, 1]


def test_a_reversed_range_is_put_in_order_everywhere(world):
    ordered = ops_list.clean_list_filters(
        {"submitted_from": "2026-10-14", "submitted_to": "2026-10-20"}
    )
    reversed_ = ops_list.clean_list_filters(
        {"submitted_from": "2026-10-20", "submitted_to": "2026-10-14"}
    )
    assert reversed_ == ordered
    only_from = ops_list.clean_list_filters({"submitted_from": "2026-10-20"})
    assert only_from == {"submitted_from": "2026-10-20"}

    admin = _user("r03-order@example.test", superuser=True)
    client = staff_client(admin)
    event = str(world["event"].pk)
    start = (timezone.now() - datetime.timedelta(days=5)).date().isoformat()
    end = timezone.now().date().isoformat()
    forward = client.get(STATS_URL, {"event": event, "submitted_from": start, "submitted_to": end})
    backward = client.get(STATS_URL, {"event": event, "submitted_from": end, "submitted_to": start})
    assert backward.context["filters"]["submitted_from"] == start
    assert (
        backward.context["report"].overview["applications"]
        == forward.context["report"].overview["applications"]
        > 0
    )
