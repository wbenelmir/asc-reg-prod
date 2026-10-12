"""Registration statistics page and its exports (UPD-08).

`/ops/statistics/` needs `registrations.view_registration` (the registrations
list's permission) and shows only the reader's authorized scope. The page is
computed when it opens or the filters change (no cache) and states its
generation time. Every figure links to the registrations list filtered to the
same population.

Exports (`/ops/statistics/export/<report>.<format>`) cover the whole filtered
scope. Aggregate reports need the same permission. The attendee list inside
the final-approved report lists only registrations the reader may also
EXPORT (`exports.add_exportrequest`, event and organization scope, through
`registrations_visible_for_export`); without that permission the report is
produced without the list, and registrations outside the export scope are
counted as not listed. Every export is audited (report, format, filter keys,
listed and withheld counts), and never cached.
"""

from __future__ import annotations

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils.http import urlencode
from django.views.decorators.http import require_GET

from apps.accounts.policies import operational_permission_required

from . import ops_list, reporting, statistics, statistics_exports

LOGIN = "accounts:operational-sign-in"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _share(rows, total, key="count"):
    for row in rows:
        row["share"] = round(100.0 * row[key] / total, 1) if total else 0
    return rows


def _decorate(report, filters, *, compact: bool = True):
    """Links, shares and chart geometry for the template (presentation only).
    `compact` (the page) shows each long source/composition breakdown as its
    largest groups plus a disclosed "Other" remainder; the print report keeps
    every group (R-02)."""
    overview = report.overview
    applications = overview["applications"]
    cards = []
    for key in ("applications", "active", "final_approved", "processing", "rejected", "withdrawn"):
        cards.append(
            {
                "key": key,
                "label": reporting.segment_label(key),
                "count": overview[key],
                "percent": overview["percent"].get(key),
                "url": statistics.list_url(filters, segment=key),
            }
        )
    for stage in report.workload:
        stage["url"] = statistics.list_url(filters, segment=stage["key"])
    population = report.final_approved
    total = population["total"]
    for group in ("roles", "badge_types", "organizations"):
        _share(population[group], total)
    _share(population["attendance"]["categories"], total)
    for row in population["roles"]:
        row["url"] = (
            statistics.list_url(filters, segment="final_approved", role=row["value"])
            if row["value"]
            else ""
        )
    for row in population["organizations"]:
        row["url"] = (
            statistics.list_url(filters, segment="final_approved", organization=row["value"])
            if row["value"]
            else ""
        )
    for row in population["attendance"]["categories"]:
        row["url"] = statistics.list_url(filters, segment="final_approved", attendance=row["value"])
    for row in population["attendance"]["days"]:
        row["url"] = statistics.list_url(filters, segment="final_approved", day=row["day"])
    for group, key in (
        ("kinds", "source"),
        ("campaigns", "campaign"),
        ("organizations", "organization"),
        ("batches", "batch"),
    ):
        for row in report.sources[group]:
            row["url"] = (
                statistics.list_url(filters, segment="applications", **{key: row["value"]})
                if row["value"]
                else ""
            )
            row["approved_url"] = (
                statistics.list_url(filters, segment="final_approved", **{key: row["value"]})
                if row["value"]
                else ""
            )
    for group, key in (("nationality", "nationality"), ("residence", "residence")):
        for row in report.composition[group]:
            if row["value"]:
                row["url"] = statistics.list_url(
                    filters, segment="applications", **{key: row["value"]}
                )
                row["approved_url"] = statistics.list_url(
                    filters, segment="final_approved", **{key: row["value"]}
                )
    for group in ("organization_type", "sector"):
        _share(report.composition[group], applications, key="applications")
    compacted = 0
    if compact:
        for container, groups in (
            (report.sources, ("campaigns", "organizations", "batches")),
            (report.composition, ("nationality", "residence", "organization_type", "sector")),
        ):
            for group in groups:
                container[group], hidden = statistics.compact_rows(container[group])
                compacted += hidden
    trend_rows = report.trends["rows"]
    peak = max([row["applications"] for row in trend_rows] + [1])
    peak_decisions = max([row["approved"] + row["not_approved"] for row in trend_rows] + [1])
    count = max(len(trend_rows), 1)
    for index, row in enumerate(trend_rows):
        row["x"] = index * 10 + 1
        row["height"] = round(100.0 * row["applications"] / peak, 2)
        row["y"] = round(100 - row["height"], 2)
        row["decision_height"] = round(100.0 * row["approved"] / peak_decisions, 2)
        row["decision_y"] = round(100 - row["decision_height"], 2)
    return {
        "cards": cards,
        "chart_width": count * 10,
        "peak": peak,
        "peak_decisions": peak_decisions,
        "final_approved_url": statistics.list_url(filters, segment="final_approved"),
        "verified_without_review_url": statistics.list_url(
            filters, segment="verified_without_review_work"
        ),
        "breakdowns_compacted": bool(compacted),
        "breakdown_limit": statistics.TOP_ROWS,
    }


def _choices(user):
    from apps.accreditation.models import AttendanceCategory
    from apps.registrations.models import RegistrationPublicStatus, RegistrationSourceKind

    return {
        "list_choices": ops_list.filter_choices(user),
        "status_choices": [
            *RegistrationPublicStatus.choices,
            (
                ops_list.STATUS_WITHDRAWN_BY_PARTICIPANT,
                reporting.segment_label("withdrawn_by_participant"),
            ),
            (ops_list.STATUS_CANCELLED_BY_TEAM, reporting.segment_label("cancelled_by_team")),
        ],
        "source_choices": RegistrationSourceKind.choices,
        "attendance_choices": AttendanceCategory.choices,
    }


def _querystring(filters) -> str:
    return urlencode({key: value for key, value in filters.items() if value})


@operational_permission_required("registrations.view_registration", login_url=LOGIN)
@require_GET
def statistics_page(request):
    filters = statistics.clean_stats_filters(request.GET, request.user)
    report = statistics.build_report(request.user, filters)
    context = {
        "report": report,
        "filters": filters,
        "querystring": _querystring(filters),
        "generated_label": statistics_exports.generated_label(report),
        "filter_summary": statistics_exports.filter_summary(filters),
        "definitions": statistics.metric_rows(),
        "attendee_access": statistics_exports.attendee_access(request.user, filters),
        "missing": ops_list.MISSING,
        "export_kinds": [
            (kind, statistics_exports.report_title(kind))
            for kind in statistics_exports.REPORT_KINDS
        ],
        **_decorate(report, filters),
        **_choices(request.user),
    }
    response = render(request, "registrations/statistics.html", context)
    response["Cache-Control"] = "private, no-store"
    return response


@operational_permission_required("registrations.view_registration", login_url=LOGIN)
@require_GET
def statistics_export(request, report_kind: str, file_format: str):
    from apps.audit import action_codes
    from apps.audit.contracts import AuditRecord
    from apps.audit.services import PersistentAuditRecorder

    if report_kind not in statistics_exports.REPORT_KINDS or file_format not in ("xlsx", "pdf"):
        raise Http404
    filters = statistics.clean_stats_filters(request.GET, request.user)
    report = statistics.build_report(request.user, filters)
    attendees = None
    access = None
    if report_kind == "approved":
        access = statistics_exports.attendee_access(request.user, filters)
        if access.permitted:
            attendees = statistics_exports.attendee_rows(request.user, filters)
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=request.user.pk,
            action_code=action_codes.STATISTICS_REPORT_EXPORTED,
            target_type="RegistrationStatistics",
            event_edition_id=filters.get("event") or None,
            result="SUCCESS",
            after_summary={
                "report": report_kind,
                "format": file_format,
                "filters": sorted(filters),
                "attendee_rows": len(attendees) if attendees is not None else None,
                "attendee_scope": access.status if access is not None else None,
                "attendee_rows_withheld": access.withheld if access is not None else None,
            },
        )
    )
    if file_format == "xlsx":
        content = statistics_exports.build_xlsx(
            report, report_kind, attendees=attendees, access=access
        )
        response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
        response["Content-Disposition"] = (
            f'attachment; filename="{statistics_exports.export_filename(report_kind, "xlsx")}"'
        )
    else:
        context = {
            "report": report,
            "report_kind": report_kind,
            "report_title": statistics_exports.report_title(report_kind),
            "filters": filters,
            "generated_label": statistics_exports.generated_label(report),
            "filter_summary": statistics_exports.filter_summary(filters),
            "definitions": statistics.metric_rows(),
            "attendees": attendees,
            "attendees_withheld": report_kind == "approved" and attendees is None,
            "attendee_access": access,
            "querystring": _querystring(filters),
            **_decorate(report, filters, compact=False),
        }
        response = render(request, "registrations/statistics_print.html", context)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
