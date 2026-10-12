"""Ready-to-export statistics reports (UPD-08).

Three reports, each for the WHOLE filtered, authorized dataset (never the
current page), computed once for one moment (`statistics.build_report`) and
labelled with the event, the generation time and its time zone, the active
filters and the metric definitions:

* `situation` -- general registration situation;
* `approved` -- the final-approved attendees: summary, breakdowns and, for
  holders of the export permission in that scope (`exports.add_exportrequest`,
  the existing participant-level export rule), the attendee list. It never
  holds an identity number, a document, a contact detail or an internal note;
* `workload` -- processing workload and exceptions.

Formats: XLSX, written by the project's standard-library writer
(`apps.core.xlsx`: text is always literal inline text with a quote prefix,
never a formula; numbers are numeric cells; Arabic sheets read right to
left), and PDF through the printable report page, which the browser prints or
saves as PDF with the platform fonts and right-to-left layout (the project
has no server-side PDF dependency; the participant pass print view uses the
same approach).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext as _

from apps.core.xlsx import STYLE_TEXT, STYLE_TITLE, STYLE_WRAP, Column, Sheet, write_workbook
from apps.registrations import ops_list, reporting

REPORT_KINDS = ("situation", "approved", "workload")


def report_title(kind: str) -> str:
    return {
        "situation": _("General registration situation"),
        "approved": _("Final-approved attendees"),
        "workload": _("Processing workload and exceptions"),
    }[kind]


def report_timezone(filters):
    return ops_list.filters_timezone(filters)


def generated_label(report) -> str:
    tz = report_timezone(report.filters)
    moment = report.generated_at.astimezone(tz)
    return f"{moment:%Y-%m-%d %H:%M} ({getattr(tz, 'key', 'UTC')})"


def filter_summary(filters: dict) -> list[tuple[str, str]]:
    """Human-readable active filters (labels and values; ids are resolved)."""
    from apps.accreditation.models import AttendanceCategory, ParticipantRole
    from apps.core.models import Country
    from apps.events.models import EventEdition
    from apps.invitations.models import InvitationCampaign
    from apps.organizations.models import Organization
    from apps.registrations.models import RegistrationPublicStatus, RegistrationSourceKind

    def missing_or(value, resolver):
        return _("None (not provided)") if value == ops_list.MISSING else resolver(value)

    def label_of(model, value, attr):
        obj = model.objects.filter(pk=value).first()
        return str(getattr(obj, attr)) if obj else str(value)

    statuses = dict(RegistrationPublicStatus.choices)
    statuses.update(
        {
            ops_list.STATUS_CANCELLED_BY_TEAM: _("Cancelled by the team"),
            ops_list.STATUS_WITHDRAWN_BY_PARTICIPANT: _("Withdrawn by the participant"),
        }
    )
    resolvers = {
        "event": (_("Event"), lambda v: label_of(EventEdition, v, "code")),
        "submitted_from": (_("Submitted from"), str),
        "submitted_to": (_("Submitted until"), str),
        "status": (_("Applicant status"), lambda v: str(statuses.get(v, v))),
        "segment": (_("Population"), reporting.segment_label),
        "source": (
            _("Source"),
            lambda v: str(dict(RegistrationSourceKind.choices).get(v, v)),
        ),
        "organization": (
            _("Organization"),
            lambda v: missing_or(v, lambda x: label_of(Organization, x, "official_name")),
        ),
        "campaign": (
            _("Invitation campaign"),
            lambda v: missing_or(v, lambda x: label_of(InvitationCampaign, x, "name")),
        ),
        "role": (
            _("Participation category"),
            lambda v: missing_or(v, lambda x: label_of(ParticipantRole, x, "localized_name")),
        ),
        "nationality": (
            _("Nationality"),
            lambda v: missing_or(v, lambda x: label_of(Country, x, "localized_name")),
        ),
        "residence": (
            _("Country of residence"),
            lambda v: missing_or(v, lambda x: label_of(Country, x, "localized_name")),
        ),
        "attendance": (
            _("Approved attendance days"),
            lambda v: missing_or(v, lambda x: str(dict(AttendanceCategory.choices).get(x, x))),
        ),
        "day": (_("Conference day"), lambda v: _("Day %(day)s") % {"day": v}),
    }
    summary = []
    for key, (label, resolve) in resolvers.items():
        if filters.get(key):
            summary.append((str(label), str(resolve(filters[key]))))
    return summary


def _header_sheet(report, kind: str, *, rtl: bool, extra_notes=()) -> Sheet:
    rows = [
        [report_title(kind)],
        [_("Generated at") + ": " + generated_label(report)],
        [
            _("Dataset")
            + ": "
            + _("every registration in the authorized scope that matches the filters below")
        ],
        [
            _("Date basis")
            + ": "
            + _(
                "counts are registrations first submitted within the date range, in their "
                "current state; trends count events dated within the range"
            )
        ],
    ]
    filters = filter_summary(report.filters)
    rows.append([_("Filters") + ": " + (", ".join(f"{a}: {b}" for a, b in filters) or _("none"))])
    for note in extra_notes:
        rows.append([str(note)])
    return Sheet(
        name=_("Report"),
        columns=[Column("", width=110, style=STYLE_WRAP)],
        rows=rows,
        header=False,
        freeze_header=False,
        auto_filter=False,
        row_styles={0: STYLE_TITLE},
        right_to_left=rtl,
    )


def _definitions_sheet(rtl: bool) -> Sheet:
    from apps.registrations.statistics import metric_rows

    return Sheet(
        name=_("Definitions"),
        columns=[
            Column(_("Metric"), width=30),
            Column(_("Population"), width=60, style=STYLE_WRAP),
            Column(_("Source"), width=40, style=STYLE_WRAP),
            Column(_("Denominator"), width=24),
            Column(_("Time basis"), width=36, style=STYLE_WRAP),
        ],
        rows=[
            [row["label"], row["population"], row["source"], row["denominator"], row["time_basis"]]
            for row in metric_rows()
        ],
        right_to_left=rtl,
    )


def _overview_rows(report):
    overview = report.overview
    rows = []
    for key in (
        "applications",
        "active",
        "final_approved",
        "processing",
        "rejected",
        "withdrawn",
        "cancelled_by_team",
        "withdrawn_by_participant",
        "unique_participants",
        "approved_persons",
        "drafts",
    ):
        percent = overview["percent"].get(key) if key in overview["percent"] else None
        label = (
            reporting.segment_label(key)
            if key in reporting.SEGMENTS
            else {
                "unique_participants": _("Unique participants"),
                "approved_persons": _("Approved persons"),
            }[key]
        )
        rows.append([str(label), overview[key], percent if percent is not None else ""])
    return rows


def _breakdown(rows, *, value_key="count"):
    return [[row["label"], row[value_key]] for row in rows]


def _number_columns(*headers, first_width=44):
    columns = [Column(str(headers[0]), width=first_width)]
    columns += [Column(str(header), width=18) for header in headers[1:]]
    return columns


def build_situation(report, *, rtl: bool) -> list[Sheet]:
    trend_rows = [
        [
            row["bucket"].isoformat(),
            row["applications"],
            row["approved"],
            row["not_approved"],
            row["withdrawn"],
            row["cumulative_applications"],
            row["cumulative_approval_decisions"],
        ]
        for row in report.trends["rows"]
    ]
    source_rows = []
    for group, label in (
        ("kinds", _("Source")),
        ("campaigns", _("Invitation campaign")),
        ("organizations", _("Organization")),
        ("batches", _("Delegation list")),
    ):
        for row in report.sources[group]:
            source_rows.append([str(label), row["label"], row["applications"], row["approved"]])
    composition_rows = []
    for group, label in (
        ("nationality", _("Nationality")),
        ("residence", _("Country of residence")),
        ("organization_type", _("Organization type")),
        ("sector", _("Sector")),
    ):
        for row in report.composition[group]:
            composition_rows.append(
                [str(label), row["label"], row["applications"], row["approved"]]
            )
    stage_rows = [[str(stage["label"]), stage["count"]] for stage in report.workload]
    return [
        _header_sheet(report, "situation", rtl=rtl),
        Sheet(
            name=_("Overview"),
            columns=_number_columns(_("Metric"), _("Count"), _("Percentage of applications")),
            rows=_overview_rows(report),
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Processing stages"),
            columns=_number_columns(_("Stage"), _("Count")),
            rows=stage_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Progress over time"),
            columns=_number_columns(
                _("Period start"),
                _("New applications"),
                _("Approval decisions"),
                _("Not-approved decisions"),
                _("Withdrawals and cancellations"),
                _("Cumulative applications"),
                _("Cumulative approval decisions"),
                first_width=16,
            ),
            rows=trend_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Sources"),
            columns=[
                Column(_("Dimension"), width=24),
                Column(_("Value"), width=44),
                Column(_("Applications"), width=16),
                Column(_("Final-approved"), width=16),
            ],
            rows=source_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Composition"),
            columns=[
                Column(_("Dimension"), width=24),
                Column(_("Value"), width=44),
                Column(_("Applications"), width=16),
                Column(_("Final-approved"), width=16),
            ],
            rows=composition_rows,
            right_to_left=rtl,
        ),
        _definitions_sheet(rtl),
    ]


@dataclass(frozen=True)
class AttendeeAccess:
    """Which final-approved registrations of a report may be listed by name.

    Viewing registrations (the report scope) never confers bulk export: the
    attendee list is limited to the registrations the reader may EXPORT
    (`apps.exports.selectors.registrations_visible_for_export`, the exact
    `exports.add_exportrequest` action scope, event AND organization)."""

    permitted: bool  # holds the export permission somewhere in the report's event
    total: int  # final-approved registrations in the report scope
    listed: int  # ... of which inside the reader's export scope

    @property
    def withheld(self) -> int:
        return self.total - self.listed

    @property
    def status(self) -> str:
        if not self.permitted:
            return "none"
        return "full" if self.listed == self.total else "partial"


def _final_approved_in_scope(user, filters):
    from apps.registrations.statistics import base_queryset

    return base_queryset(user, filters).filter(reporting.final_approved_q())


def _exportable_final_approved(user, filters):
    from apps.exports.selectors import registrations_visible_for_export

    return _final_approved_in_scope(user, filters).filter(
        pk__in=registrations_visible_for_export(user).values("pk")
    )


def attendee_access(user, filters) -> AttendeeAccess:
    from apps.accounts.policies import has_scoped_permission

    total = _final_approved_in_scope(user, filters).count()
    permitted = has_scoped_permission(
        user, "exports.add_exportrequest", event_edition_id=filters.get("event") or None
    )
    listed = _exportable_final_approved(user, filters).count() if permitted else 0
    return AttendeeAccess(permitted=permitted, total=total, listed=listed)


def attendee_rows(user, filters) -> list[list]:
    """The final-approved attendees of the filtered scope that `user` may
    export, one row each (R-01: report scope AND export scope; a registration
    outside the reader's export scope is never listed). Bounded columns:
    reference, names, nationality, organization, role, badge type, attendance
    category, approval date, pass and badge status. Never an identity number,
    a document, a contact detail or a note."""
    from apps.accreditation.models import (
        AttendanceCategory,
        AttendanceEntitlement,
        BadgeType,
        BadgeTypeAssignment,
        ParticipantRole,
        ParticipantRoleAssignment,
    )
    from apps.badges.models import DigitalEntryPass
    from apps.badges.models.stock import BadgeIssuance
    from apps.reviews.models import RegistrationDecision

    current_role = ParticipantRoleAssignment.objects.filter(
        registration_id=OuterRef("pk"), status="CURRENT"
    ).values("role_id")[:1]
    current_badge = BadgeTypeAssignment.objects.filter(
        registration_id=OuterRef("pk"), status="CURRENT"
    ).values("badge_type_id")[:1]
    current_attendance = AttendanceEntitlement.objects.filter(
        registration_id=OuterRef("pk"), status="CURRENT"
    ).values("category")[:1]
    approved_at = RegistrationDecision.objects.filter(
        registration_id=OuterRef("pk"), is_current=True
    ).values("decided_at")[:1]
    passes = DigitalEntryPass.objects.filter(registration_id=OuterRef("pk"))
    rows_qs = (
        _exportable_final_approved(user, filters)
        .select_related("profile", "profile__nationality_code", "source_organization")
        .annotate(
            role_id=Subquery(current_role),
            badge_type_id=Subquery(current_badge),
            attendance_category=Subquery(current_attendance),
            approved_at=Subquery(approved_at),
            pass_active=Exists(passes.filter(status="ACTIVE")),
            pass_generated=Exists(passes.filter(status__in=("INACTIVE", "SUSPENDED"))),
            badge_issued=Exists(
                BadgeIssuance.objects.filter(registration_id=OuterRef("pk"), status="ISSUED")
            ),
        )
        .order_by("public_reference")
    )
    roles = {str(role.pk): role.localized_name for role in ParticipantRole.objects.all()}
    badges = {str(badge.pk): badge.localized_name for badge in BadgeType.objects.all()}
    categories = dict(AttendanceCategory.choices)
    tz = report_timezone(filters)
    rows = []
    for registration in rows_qs.iterator(chunk_size=500):
        profile = getattr(registration, "profile", None)
        rows.append(
            [
                registration.public_reference,
                profile.submitted_given_names if profile else "",
                profile.submitted_family_name if profile else "",
                profile.nationality_code.localized_name
                if profile and profile.nationality_code
                else _("Not provided"),
                registration.source_organization.official_name
                if registration.source_organization
                else _("No organization"),
                roles.get(str(registration.role_id), _("No current role")),
                badges.get(str(registration.badge_type_id), _("No current badge type")),
                str(categories.get(registration.attendance_category, _("Not classified yet"))),
                registration.approved_at.astimezone(tz).strftime("%Y-%m-%d")
                if registration.approved_at
                else "",
                _("Active")
                if registration.pass_active
                else (_("Generated, not active") if registration.pass_generated else _("None")),
                _("Yes") if registration.badge_issued else _("No"),
            ]
        )
    return rows


def build_approved(
    report, *, rtl: bool, attendees: list | None, access: AttendeeAccess | None = None
) -> list[Sheet]:
    population = report.final_approved
    summary_rows = [[_("Final-approved registrations"), population["total"]]]
    summary_rows += [[row["label"], row["count"]] for row in population["readiness"]]
    attendance_rows = [
        [row["label"], row["count"]] for row in population["attendance"]["categories"]
    ]
    attendance_rows += [
        [_("Day %(day)s (overlapping)") % {"day": row["day"]}, row["count"]]
        for row in population["attendance"]["days"]
    ]
    notes = []
    if attendees is None:
        notes.append(
            _(
                "The attendee list is included only for holders of the export permission in "
                "this scope."
            )
        )
    elif access is not None and access.withheld:
        notes.append(withheld_note(access))
    sheets = [
        _header_sheet(report, "approved", rtl=rtl, extra_notes=notes),
        Sheet(
            name=_("Summary"),
            columns=_number_columns(_("Item"), _("Count")),
            rows=summary_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Attendance days"),
            columns=_number_columns(_("Attendance"), _("Count")),
            rows=attendance_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Participation categories"),
            columns=_number_columns(_("Participant role"), _("Count")),
            rows=_breakdown(population["roles"]),
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Badge types"),
            columns=_number_columns(_("Badge type"), _("Count")),
            rows=_breakdown(population["badge_types"]),
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Organizations"),
            columns=_number_columns(_("Organization"), _("Count")),
            rows=_breakdown(population["organizations"]),
            right_to_left=rtl,
        ),
    ]
    if attendees is not None:
        sheets.append(
            Sheet(
                name=_("Attendees"),
                columns=[
                    Column(_("Registration Reference"), width=20),
                    Column(_("Given name(s)"), width=22),
                    Column(_("Family name"), width=22),
                    Column(_("Nationality"), width=22),
                    Column(_("Organization"), width=34),
                    Column(_("Participant role"), width=22),
                    Column(_("Badge type"), width=22),
                    Column(_("Attendance days"), width=30),
                    Column(_("Approved on"), width=14),
                    Column(_("Digital entry pass"), width=20),
                    Column(_("Physical badge handed over"), width=16),
                ],
                rows=attendees,
                right_to_left=rtl,
            )
        )
    sheets.append(_definitions_sheet(rtl))
    return sheets


def build_workload(report, *, rtl: bool) -> list[Sheet]:
    tz = report_timezone(report.filters)

    def when(value):
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""

    stage_rows = [
        [
            str(stage["label"]),
            stage["count"],
            when(stage["oldest"]),
            stage["average_days"] if stage["average_days"] is not None else "",
            str(stage["basis"]),
        ]
        for stage in report.workload
    ]
    oldest_rows = [
        [str(stage["label"]), reference, when(since)]
        for stage in report.workload
        for reference, since in stage["oldest_items"]
    ]
    exceptions = report.exceptions
    exception_rows = [
        [_("Identity manual review reason"), row["label"], row["count"]]
        for row in exceptions["manual_review_reasons"]
    ]
    for bucket, count in exceptions["identity_corrections"].items():
        exception_rows.append([_("Identity correction requests per registration"), bucket, count])
    for bucket, count in exceptions["information_requests"].items():
        exception_rows.append([_("Information requests per registration"), bucket, count])
    exception_rows.append(
        [
            _("Identity verified, no participation-review work"),
            "",
            exceptions["verified_without_review_work"],
        ]
    )
    return [
        _header_sheet(report, "workload", rtl=rtl),
        Sheet(
            name=_("Workload"),
            columns=[
                Column(_("Stage"), width=40),
                Column(_("Count"), width=12),
                Column(_("Oldest waiting since"), width=20),
                Column(_("Average wait (days)"), width=18),
                Column(_("Waiting time basis"), width=46, style=STYLE_WRAP),
            ],
            rows=stage_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Oldest items"),
            columns=[
                Column(_("Stage"), width=40),
                Column(_("Registration Reference"), width=22, style=STYLE_TEXT),
                Column(_("Waiting since"), width=20),
            ],
            rows=oldest_rows,
            right_to_left=rtl,
        ),
        Sheet(
            name=_("Exceptions"),
            columns=[
                Column(_("Exception"), width=44),
                Column(_("Value"), width=44),
                Column(_("Count"), width=12),
            ],
            rows=exception_rows,
            right_to_left=rtl,
        ),
        _definitions_sheet(rtl),
    ]


def withheld_note(access: AttendeeAccess) -> str:
    return _(
        "The attendee list covers only the registrations inside your export scope: "
        "%(withheld)s of %(total)s final-approved registrations are not listed."
    ) % {"withheld": access.withheld, "total": access.total}


def build_xlsx(
    report, kind: str, *, attendees: list | None = None, access: AttendeeAccess | None = None
) -> bytes:
    rtl = (get_language() or "en").startswith("ar")
    if kind == "situation":
        sheets = build_situation(report, rtl=rtl)
    elif kind == "approved":
        sheets = build_approved(report, rtl=rtl, attendees=attendees, access=access)
    else:
        sheets = build_workload(report, rtl=rtl)
    return write_workbook(sheets)


def export_filename(kind: str, extension: str) -> str:
    stamp = timezone.now().strftime("%Y%m%d-%H%M")
    return f"asc-registrations-{kind}-{stamp}.{extension}"
