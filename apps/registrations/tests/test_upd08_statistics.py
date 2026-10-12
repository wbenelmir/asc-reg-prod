"""UPD-08: registration statistics and the ready-to-export reports.

A small synthetic world with every state the reports distinguish, including
the cases that inflate naive counts: a registration with several decisions
and several identity attempts, a person who withdrew and registered again,
overlapping attendance days. Checked: the overview arithmetic, each figure
equal to the list it opens, unique participants and approved persons, the
workload and exceptions, event trends with their baseline, scope
restrictions, and the XLSX and PDF exports (whole dataset, generation time,
filters, definitions, literal text, right to left in Arabic, the attendee
list only with the export permission). Synthetic data only.
"""

from __future__ import annotations

import datetime
import io
import uuid

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.people.tests.conftest import TEST_OPERATIONAL_PASSWORD, staff_client
from apps.people.tests.identity_fixtures import make_verified_identity_case
from apps.registrations import reporting, statistics
from apps.registrations.models import Registration

pytestmark = pytest.mark.django_db

STATS_URL = reverse("registrations:statistics")
LIST_URL = reverse("registrations:ops-intake-list")


def _user(email, *, superuser=False):
    return OperationalUser.objects.create_user(
        email=email,
        password=TEST_OPERATIONAL_PASSWORD,
        status=OperationalUserStatus.ACTIVE,
        is_superuser=superuser,
    )


@pytest.fixture
def world():
    return build_world()


def build_world():
    """The synthetic statistics world (also used by the review-correction tests)."""
    from apps.accreditation.models import (
        AttendanceEntitlement,
        BadgeType,
        ParticipantRole,
    )
    from apps.accreditation.services import assign
    from apps.events.models import EventEdition
    from apps.organizations.models import Organization
    from apps.people.models import IdentityVerificationAttempt
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import RegistrationProfile
    from apps.reviews.models import RegistrationDecision

    EventEdition.objects.filter(status="REGISTRATION_OPEN").update(status="REGISTRATION_CLOSED")
    event = EventEdition.objects.create(
        code="UPD08",
        name="Statistics test",
        timezone="UTC",
        starts_at=timezone.now() + datetime.timedelta(days=30),
        ends_at=timezone.now() + datetime.timedelta(days=32),
        status="REGISTRATION_OPEN",
    )
    org = Organization.objects.create(
        official_name='=HYPERLINK("http://x.test")',  # formula-shaped on purpose
        normalized_name="formula org",
        organization_type="COMPANY",
    )
    other_org = Organization.objects.create(
        official_name="Other Org", normalized_name="other org", organization_type="NGO"
    )
    decider = _user("upd08-decider@example.test")
    role = ParticipantRole.objects.create(event_edition=event, code="DELEGATE", name="Delegate")
    badge = BadgeType.objects.create(event_edition=event, code="STD", name="Standard")
    now = timezone.now()

    def registration(ref, status, *, person=None, organization=org, nationality="DZ", **extra):
        person = person or resolve_or_create_participant_for_email(
            f"upd08-{uuid.uuid4().hex[:10]}@example.test"
        )
        row = Registration.objects.create(
            public_reference=ref,
            event_edition=event,
            person=person,
            source_kind="OPEN" if organization is None else "ON_BEHALF",
            source_context_key=f"upd08:{uuid.uuid4().hex}",
            source_organization=organization,
            public_status=status,
            submitted_at=None if status == "DRAFT" else now - datetime.timedelta(days=2),
            **extra,
        )
        RegistrationProfile.objects.create(
            registration=row,
            submitted_given_names="Synthetic",
            submitted_family_name=ref,
            nationality_code_id=nationality,
            country_of_residence_id=nationality,
        )
        return row

    def decide(row, outcome, *, current=True, sequence=1):
        RegistrationDecision.objects.create(
            registration=row, sequence=sequence, outcome=outcome, internal_reason_code="X",
            is_current=current, decided_by=decider, decided_at=now,
            event_edition=event, organization=row.source_organization,
        )  # fmt: skip

    def attend(row, category):
        AttendanceEntitlement.objects.create(
            registration=row, event_edition=event, category=category, status="CURRENT",
            origin="APPROVAL", effective_from=now, decided_by=decider,
        )  # fmt: skip

    r1 = registration("ST-APPROVED-1", "APPROVED")
    decide(r1, "NOT_APPROVED", current=False, sequence=1)  # superseded history
    decide(r1, "APPROVED", sequence=2)
    make_verified_identity_case(r1)
    assign(kind="PARTICIPANT_ROLE", registration=r1, reference_obj=role, actor=decider)
    assign(kind="BADGE_TYPE", registration=r1, reference_obj=badge, actor=decider)
    attend(r1, "ALL_CONFERENCE_DAYS")

    r2 = registration("ST-APPROVED-2", "APPROVED", nationality="FR", organization=None)
    decide(r2, "APPROVED")
    make_verified_identity_case(r2)
    attend(r2, "FOLLOWING_TWO_DAYS")

    p3 = resolve_or_create_participant_for_email("upd08-again@example.test")
    registration("ST-WITHDRAWN", "WITHDRAWN", person=p3, withdrawn_at=now, is_current_context=False)
    r3b = registration("ST-AGAIN", "APPROVED", person=p3)
    decide(r3b, "APPROVED")
    make_verified_identity_case(r3b)

    r4 = registration("ST-REJECTED", "NOT_APPROVED", organization=other_org)
    decide(r4, "NOT_APPROVED")

    r5 = registration("ST-PENDING", "SUBMITTED")
    make_verified_identity_case(r5, status="PENDING", source="")

    r6 = registration("ST-MANUAL", "UNDER_REVIEW")
    case6 = make_verified_identity_case(r6, status="MANUAL_REVIEW", source="")
    case6.reason_code = "NOT_FOUND"
    case6.save(update_fields=["reason_code"])
    for _ in range(2):  # several attempts must never multiply the counts
        IdentityVerificationAttempt.objects.create(
            identity_identifier=case6.current_revision.identifier, registration=r6,
            method="EXTERNAL_SERVICE", result="NO_MATCH", attempted_at=now,
        )  # fmt: skip

    registration("ST-CORRECTION", "ADDITIONAL_INFORMATION_REQUIRED")
    r8 = registration("ST-VERIFIED", "UNDER_REVIEW")
    make_verified_identity_case(r8)  # verified, no decision, no review case
    registration("ST-CANCELLED", "WITHDRAWN", cancelled_at=now)
    registration("ST-DRAFT", "DRAFT")
    return {"event": event, "org": org, "other_org": other_org, "role": role, "r1": r1}


@pytest.fixture
def superadmin():
    return _user("upd08-super@example.test", superuser=True)


def test_the_overview_counts_each_population_once(world, superadmin):
    response = staff_client(superadmin).get(STATS_URL, {"event": str(world["event"].pk)})
    assert response.status_code == 200
    overview = response.context["report"].overview
    assert overview["applications"] == 10  # every submitted one, history included
    assert overview["final_approved"] == 3
    assert overview["active"] == 7  # 3 approved + 4 in processing
    assert overview["processing"] == 4
    assert overview["rejected"] == 1
    assert overview["withdrawn"] == 2
    assert overview["cancelled_by_team"] == 1
    assert overview["drafts"] == 1
    assert overview["unique_participants"] == 9  # the person who registered again counts once
    assert overview["approved_persons"] == 3
    assert overview["identity_pending"] == 1
    assert overview["manual_review"] == 1
    assert overview["participant_correction"] == 1
    assert overview["verified_without_review_work"] == 1
    assert overview["percent"]["final_approved"] == 30.0


def test_every_figure_equals_the_list_it_opens(world, superadmin):
    client = staff_client(superadmin)
    filters = {"event": str(world["event"].pk)}
    overview = client.get(STATS_URL, filters).context["report"].overview
    for segment in reporting.SEGMENTS:
        listed = client.get(LIST_URL, {**filters, "segment": segment}).context["matching_total"]
        assert listed == overview[segment], segment


def test_the_final_approved_population_and_overlapping_days(world, superadmin):
    report = (
        staff_client(superadmin).get(STATS_URL, {"event": str(world["event"].pk)}).context["report"]
    )
    population = report.final_approved
    assert population["total"] == 3
    days = {row["day"]: row["count"] for row in population["attendance"]["days"]}
    assert days == {"1": 1, "2": 2, "3": 2}
    categories = {row["value"]: row["count"] for row in population["attendance"]["categories"]}
    assert categories["__missing__"] == 1  # the re-registration has no classified days
    roles = {row["label"]: row["count"] for row in population["roles"]}
    assert roles == {"Delegate": 1, "No current role": 2}
    organizations = {row["label"]: row["count"] for row in population["organizations"]}
    assert organizations["No organization"] == 1
    # The day figure opens the same registrations.
    client = staff_client(superadmin)
    day_two = client.get(
        LIST_URL, {"event": str(world["event"].pk), "segment": "final_approved", "day": "2"}
    )
    assert day_two.context["matching_total"] == 2


def test_workload_and_exceptions(world, superadmin):
    report = (
        staff_client(superadmin).get(STATS_URL, {"event": str(world["event"].pk)}).context["report"]
    )
    stages = {stage["key"]: stage for stage in report.workload}
    assert stages["identity_pending"]["count"] == 1
    assert stages["manual_review"]["count"] == 1
    assert stages["manual_review"]["oldest_items"][0][0] == "ST-MANUAL"
    assert stages["participation_review"]["count"] == 1
    reasons = {row["code"]: row["count"] for row in report.exceptions["manual_review_reasons"]}
    assert reasons == {"NOT_FOUND": 1}
    assert report.exceptions["verified_without_review_work"] == 1


def test_trends_date_events_and_start_cumulative_from_the_baseline(world, superadmin):
    client = staff_client(superadmin)
    today = timezone.now().date()
    response = client.get(
        STATS_URL,
        {
            "event": str(world["event"].pk),
            "submitted_from": (today - datetime.timedelta(days=1)).isoformat(),
            "submitted_to": today.isoformat(),
        },
    )
    trends = response.context["report"].trends
    assert [row["bucket"] for row in trends["rows"]] == [today - datetime.timedelta(days=1), today]
    # Applications were submitted two days ago: before the range (baseline).
    assert trends["baseline_applications"] == 10
    assert sum(row["applications"] for row in trends["rows"]) == 0
    # Decisions are events of today, a superseded one included.
    today_row = trends["rows"][-1]
    assert (today_row["approved"], today_row["not_approved"]) == (3, 2)
    assert today_row["cumulative_applications"] == 10
    # The overview, by contrast, counts the cohort submitted within the range.
    assert response.context["report"].overview["applications"] == 0


def test_scoped_staff_see_only_their_scope(world):
    intake = _user("upd08-intake@example.test")
    ScopedGroupMembership.objects.create(
        user=intake,
        group=Group.objects.get(name="Registration Intake"),
        organization=world["other_org"],
        granted_by=intake,
    )
    response = staff_client(intake).get(STATS_URL)
    assert response.context["report"].overview["applications"] == 1
    assert response.context["report"].overview["final_approved"] == 0
    assert response.context["report"].scope_total == 1


def test_staff_without_the_permission_are_refused(world):
    assert staff_client(_user("upd08-nobody@example.test")).get(STATS_URL).status_code == 403


def test_the_page_uses_a_bounded_number_of_queries(
    world, superadmin, django_assert_max_num_queries
):
    client = staff_client(superadmin)
    client.get(STATS_URL)
    with django_assert_max_num_queries(120):
        client.get(STATS_URL, {"event": str(world["event"].pk)})


def _workbook(response):
    """`{sheet name: [[cell text, ...], ...]}`; fails on any formula cell."""
    from apps.core.xlsx import ReadLimits, read_workbook

    assert response["Content-Type"].startswith("application/vnd.openxmlformats")
    sheets = read_workbook(response.content, limits=ReadLimits(max_bytes=5_000_000))
    result = {}
    for name, sheet in sheets.items():
        assert not any(cell.has_formula for cell in sheet.cells.values()), name
        width = max((column for _row, column in sheet.cells), default=-1) + 1
        result[name] = [sheet.row_values(row, width) for row in range(1, sheet.max_row + 1)]
    return result


def test_the_situation_workbook_covers_the_whole_filtered_dataset(world, superadmin):
    response = staff_client(superadmin).get(
        reverse(
            "registrations:statistics-export",
            kwargs={"report_kind": "situation", "file_format": "xlsx"},
        ),
        {"event": str(world["event"].pk)},
    )
    sheets = _workbook(response)
    text = " ".join(value for row in sheets["Report"] for value in row if value)
    assert "Generated at" in text and "UTC" in text and "UPD08" in text
    overview = {row[0]: row[1] for row in sheets["Overview"][1:]}
    assert overview["All applications (submitted)"] == "10"
    assert overview["Final-approved"] == "3"
    assert "Definitions" in sheets
    sources = [value for row in sheets["Sources"] for value in row]
    assert '=HYPERLINK("http://x.test")' in sources  # literal text, never a formula


def test_the_approved_workbook_lists_attendees_only_with_the_export_permission(world, superadmin):
    url = reverse(
        "registrations:statistics-export", kwargs={"report_kind": "approved", "file_format": "xlsx"}
    )
    params = {"event": str(world["event"].pk)}
    sheets = _workbook(staff_client(superadmin).get(url, params))
    references = sorted(row[0] for row in sheets["Attendees"][1:])
    assert references == ["ST-AGAIN", "ST-APPROVED-1", "ST-APPROVED-2"]
    header = sheets["Attendees"][0]
    assert not any("NIN" in value or "mail" in value.lower() for value in header)

    intake = _user("upd08-intake2@example.test")
    ScopedGroupMembership.objects.create(
        user=intake, group=Group.objects.get(name="Registration Intake"), granted_by=intake
    )
    limited = _workbook(staff_client(intake).get(url, params))
    assert "Attendees" not in limited
    assert "Summary" in limited


def test_an_arabic_workbook_reads_right_to_left(world, superadmin):
    client = staff_client(superadmin)
    url = reverse(
        "registrations:statistics-export", kwargs={"report_kind": "workload", "file_format": "xlsx"}
    )
    with translation.override("ar"):
        from apps.registrations import statistics_exports

        report = statistics.build_report(superadmin, {"event": str(world["event"].pk)})
        content = statistics_exports.build_xlsx(report, "workload")
    import zipfile

    sheet = zipfile.ZipFile(io.BytesIO(content)).read("xl/worksheets/sheet2.xml").decode()
    assert 'rightToLeft="1"' in sheet
    assert client.get(url, {"event": str(world["event"].pk)}).status_code == 200


def test_the_pdf_report_is_a_print_ready_page_for_the_whole_dataset(world, superadmin):
    response = staff_client(superadmin).get(
        reverse(
            "registrations:statistics-export",
            kwargs={"report_kind": "approved", "file_format": "pdf"},
        ),
        {"event": str(world["event"].pk)},
    )
    content = response.content.decode()
    assert response.status_code == 200
    assert "data-auto-print" in content
    assert 'data-stats-attendees="3"' in content
    assert "data-stats-generated" in content
    assert response["Cache-Control"] == "private, no-store"


def test_every_export_is_audited_without_personal_data(world, superadmin):
    from apps.audit.models import AuditEvent

    staff_client(superadmin).get(
        reverse(
            "registrations:statistics-export",
            kwargs={"report_kind": "approved", "file_format": "xlsx"},
        ),
        {"event": str(world["event"].pk)},
    )
    event = AuditEvent.objects.get(action_code="RPT_STATISTICS_REPORT_EXPORTED")
    assert event.after_summary["report"] == "approved"
    assert event.after_summary["attendee_rows"] == 3
    assert "ST-APPROVED-1" not in str(event.after_summary)
