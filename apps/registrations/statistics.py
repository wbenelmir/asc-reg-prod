"""Registration statistics, computed on the server from stored facts (UPD-08).

Every figure is an aggregate query over the reader's authorized registrations
(`registrations_visible_to`), narrowed by the same filters as the operations
list (`apps.registrations.ops_list.apply_filters`), so a figure and the list
it opens agree. No participant record is loaded to count, nothing is
decrypted, and every population is defined once in
`apps.registrations.reporting` with `Exists` subqueries (a registration is
counted once whatever its number of decisions, identity attempts, review
cases, imports or attendance days).

Two time bases, kept apart (`METRIC_DEFINITIONS` says which applies):

* registration cohort -- the overview, workload, final-approved population,
  sources and composition count the registrations FIRST SUBMITTED within the
  date range (all dates when it is empty), in their CURRENT state;
* events -- "progress over time" counts submissions, decisions and
  withdrawals DATED within the range, whatever the submission date. Decision
  events come from the append-only decision history (a superseded decision is
  still an event of its day). Nothing earlier than the stored history is
  reconstructed; a current-state count is never presented as a trend.

Dates are calendar days in the selected event's time zone (UTC without an
event).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from django.db.models import (
    Avg,
    Count,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Min,
    OuterRef,
    Q,
    Value,
)
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.registrations import ops_list, reporting

#: Query-string keys of the statistics page: the list filters (the date
#: range is `submitted_from`/`submitted_to`) plus the chart period.
STATS_FILTER_KEYS = tuple(key for key in ops_list.LIST_FILTER_KEYS if key not in ("ctx",)) + (
    "period",
)
TOP_ROWS = 25
DEFAULT_TREND_DAYS = 30
MAX_DAILY_BUCKETS = 62


# ---------------------------------------------------------------------------
# Metric definitions (shown on the page and exported with every report)
# ---------------------------------------------------------------------------

#: key -> (label, population, source, denominator, time basis)
METRIC_DEFINITIONS = {
    "applications": (
        _("Total applications"),
        _("Every submitted registration, including withdrawn, cancelled and replaced ones."),
        _("Registration: first submission time"),
        _("—"),
        _("Registration cohort"),
    ),
    "active": (
        _("Active applications"),
        _("Submitted and not withdrawn, cancelled or not approved."),
        _("Registration status"),
        _("Total applications"),
        _("Registration cohort, current state"),
    ),
    "final_approved": (
        _("Final-approved"),
        _(
            "Current decision Approved, registration approved, current, not withdrawn or "
            "cancelled. A person who registered again is counted once."
        ),
        _("Current participation decision and registration status"),
        _("Total applications"),
        _("Registration cohort, current state"),
    ),
    "processing": (
        _("In processing"),
        _("Submitted, under review or waiting for the participant; no final outcome."),
        _("Registration status"),
        _("Total applications"),
        _("Registration cohort, current state"),
    ),
    "rejected": (
        _("Not approved"),
        _("Current decision Not approved (an identity rejection included)."),
        _("Current participation decision"),
        _("Total applications"),
        _("Registration cohort, current state"),
    ),
    "withdrawn": (
        _("Withdrawn or cancelled"),
        _("Withdrawn by the participant or cancelled by the team; kept as history."),
        _("Registration status and withdrawal or cancellation time"),
        _("Total applications"),
        _("Registration cohort, current state"),
    ),
    "unique_participants": (
        _("Unique participants"),
        _("Distinct persons among the applications (several applications, one person)."),
        _("Registration person"),
        _("—"),
        _("Registration cohort"),
    ),
    "approved_persons": (
        _("Approved persons"),
        _("Distinct persons with a final-approved registration."),
        _("Registration person"),
        _("Final-approved"),
        _("Registration cohort, current state"),
    ),
    "identity_pending": (
        _("Identity check pending"),
        _("Active registrations whose Ministry check is queued or running."),
        _("Identity verification status"),
        _("Active applications"),
        _("Current state; waiting since the last identity status change"),
    ),
    "manual_review": (
        _("Identity manual review"),
        _("Active registrations whose identity waits for a reviewer."),
        _("Identity verification status"),
        _("Active applications"),
        _("Current state; waiting since the last identity status change"),
    ),
    "participant_correction": (
        _("Waiting for the participant"),
        _("An identity correction or an information request is open."),
        _("Registration status"),
        _("Active applications"),
        _("Current state; waiting since the request was sent"),
    ),
    "participation_review": (
        _("Waiting for the participation decision"),
        _("Identity verified, no current decision yet."),
        _("Identity status and decision history"),
        _("Active applications"),
        _("Current state; waiting since the review case opened"),
    ),
    "verified_without_review_work": (
        _("Verified without review work"),
        _("Identity verified and undecided, but no open participation-review case."),
        _("Identity status, review cases and decisions"),
        _("Waiting for the participation decision"),
        _("Current state"),
    ),
}


def metric_rows() -> list[dict]:
    return [
        {
            "key": key,
            "label": str(label),
            "population": str(population),
            "source": str(source),
            "denominator": str(denominator),
            "time_basis": str(basis),
        }
        for key, (label, population, source, denominator, basis) in METRIC_DEFINITIONS.items()
    ]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def clean_stats_filters(query, user) -> dict[str, str]:
    """The list filters, the chart period and the default event (the open
    event when it is in the reader's scope and no event was chosen)."""
    filters = ops_list.clean_list_filters(query)
    filters.pop("ctx", None)
    period = (query.get("period") or "").strip()
    if period in ("day", "week"):
        filters["period"] = period
    if "event" not in filters and query.get("event") is None:
        from apps.accounts.selectors import registrations_visible_to
        from apps.events.selectors import NoOpenEventEdition, current_event_edition

        try:
            event = current_event_edition()
        except NoOpenEventEdition:
            event = None
        if (
            event is not None
            and registrations_visible_to(user).filter(event_edition_id=event.pk).exists()
        ):
            filters["event"] = str(event.pk)
    return filters


def base_queryset(user, filters: dict[str, str], *, cohort: bool = True):
    """The authorized, filtered registrations. `cohort=False` drops the
    submission-date range (event trends date the events themselves)."""
    from apps.accounts.selectors import registrations_visible_to

    effective = dict(filters)
    if not cohort:
        effective.pop("submitted_from", None)
        effective.pop("submitted_to", None)
    from apps.registrations.models import Registration

    scoped = ops_list.apply_filters(registrations_visible_to(user), effective)
    # A plain queryset over the scoped ids: a scoped reader's queryset is
    # DISTINCT, which conditional aggregates with correlated subqueries cannot
    # wrap. The ids stay a subquery (nothing is loaded in memory).
    return Registration.objects.filter(pk__in=scoped.values("pk")).order_by()


def list_url(filters: dict[str, str], **extra) -> str:
    """The operations list opened on exactly this population."""
    from django.urls import reverse
    from django.utils.http import urlencode

    params = {
        key: value
        for key, value in {**filters, **extra}.items()
        if key in ops_list.LIST_FILTER_KEYS and key not in ("period",) and value
    }
    url = reverse("registrations:ops-intake-list")
    return f"{url}?{urlencode(params)}" if params else url


# ---------------------------------------------------------------------------
# Computations
# ---------------------------------------------------------------------------


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def overview(base) -> dict:
    segments = (
        "applications",
        "active",
        "final_approved",
        "processing",
        "rejected",
        "withdrawn",
        "cancelled_by_team",
        "withdrawn_by_participant",
        "drafts",
        "identity_pending",
        "manual_review",
        "participant_correction",
        "participation_review",
        "verified_without_review_work",
    )
    counts = base.aggregate(
        **{key: Count("pk", filter=reporting.segment_q(key)) for key in segments}
    )
    counts["unique_participants"] = (
        base.filter(reporting.applications_q(), person__isnull=False)
        .values("person_id")
        .distinct()
        .count()
    )
    counts["approved_persons"] = (
        base.filter(reporting.final_approved_q(), person__isnull=False)
        .values("person_id")
        .distinct()
        .count()
    )
    applications = counts["applications"]
    counts["percent"] = {
        key: _pct(counts[key], applications)
        for key in ("active", "final_approved", "processing", "rejected", "withdrawn")
    }
    return counts


def _waiting(queryset, field_name: str, now) -> dict:
    data = queryset.aggregate(
        oldest=Min(field_name),
        average=Avg(ExpressionWrapper(Value(now) - F(field_name), output_field=DurationField())),
    )
    average = data["average"]
    return {
        "oldest": data["oldest"],
        "average_days": round(average.total_seconds() / 86400, 1) if average else None,
    }


def workload(base) -> list[dict]:
    """Per processing stage: how many, the oldest waiting item and the
    average waiting time (only where a timestamp records the start), and the
    five oldest references."""
    from apps.people.models import IdentityVerification
    from apps.reviews.models import (
        InformationRequest,
        InformationRequestStatus,
        ReviewCase,
        ReviewCaseStatus,
    )

    now = timezone.now()
    stages = []
    for key, status in (("identity_pending", "PENDING"), ("manual_review", "MANUAL_REVIEW")):
        items = IdentityVerification.objects.filter(
            registration__in=base.filter(reporting.segment_q(key)).values("pk"), status=status
        )
        stages.append(
            {
                "key": key,
                "count": items.count(),
                **_waiting(items, "status_changed_at", now),
                "oldest_items": list(
                    items.order_by("status_changed_at").values_list(
                        "registration__public_reference", "status_changed_at"
                    )[:5]
                ),
                "basis": _("since the identity status changed"),
            }
        )
    correction_registrations = base.filter(reporting.participant_correction_q()).values("pk")
    identity_returns = IdentityVerification.objects.filter(
        registration__in=correction_registrations, status="RETURNED_FOR_CORRECTION"
    )
    requests = InformationRequest.objects.filter(
        registration__in=correction_registrations,
        status__in=InformationRequestStatus.participant_action_required_statuses(),
        sent_at__isnull=False,
    )
    oldest_candidates = [
        *identity_returns.order_by("status_changed_at").values_list(
            "registration__public_reference", "status_changed_at"
        )[:5],
        *requests.order_by("sent_at").values_list("registration__public_reference", "sent_at")[:5],
    ]
    oldest_candidates.sort(key=lambda row: row[1])
    returns_wait = _waiting(identity_returns, "status_changed_at", now)
    requests_wait = _waiting(requests, "sent_at", now)
    oldest = [value for value in (returns_wait["oldest"], requests_wait["oldest"]) if value]
    stages.append(
        {
            "key": "participant_correction",
            "count": base.filter(reporting.participant_correction_q()).count(),
            "oldest": min(oldest) if oldest else None,
            "average_days": None,
            "oldest_items": oldest_candidates[:5],
            "basis": _("since the correction or information request was sent"),
        }
    )
    review_registrations = base.filter(reporting.participation_review_q()).values("pk")
    cases = ReviewCase.objects.filter(
        registration__in=review_registrations, status__in=ReviewCaseStatus.open_statuses()
    )
    stages.append(
        {
            "key": "participation_review",
            "count": base.filter(reporting.participation_review_q()).count(),
            **_waiting(cases, "opened_at", now),
            "oldest_items": list(
                cases.order_by("opened_at").values_list(
                    "registration__public_reference", "opened_at"
                )[:5]
            ),
            "basis": _("since the participation-review case opened"),
        }
    )
    for stage in stages:
        stage["label"] = reporting.segment_label(stage["key"])
    return stages


def exceptions(base) -> dict:
    from apps.people.models import IdentityDecision, IdentityReasonCode, IdentityVerification
    from apps.reviews.models import InformationRequest

    reason_labels = dict(IdentityReasonCode.choices)
    reasons = [
        {
            "code": row["reason_code"] or "",
            "label": str(reason_labels.get(row["reason_code"], _("No reason recorded"))),
            "count": row["total"],
        }
        for row in IdentityVerification.objects.filter(
            registration__in=base.filter(reporting.manual_review_q()).values("pk"),
            status="MANUAL_REVIEW",
        )
        .values("reason_code")
        .annotate(total=Count("pk"))
        .order_by("-total", "reason_code")
    ]
    applications = base.filter(reporting.applications_q()).values("pk")
    returns_per_case = (
        IdentityDecision.objects.filter(
            action="RETURN_FOR_CORRECTION", verification__registration__in=applications
        )
        .values("verification_id")
        .annotate(total=Count("pk"))
        .values_list("total", flat=True)
    )
    identity_buckets = {"1": 0, "2": 0, "3+": 0}
    for total in returns_per_case:
        identity_buckets["1" if total == 1 else "2" if total == 2 else "3+"] += 1
    requests_per_registration = (
        InformationRequest.objects.filter(registration__in=applications, sent_at__isnull=False)
        .values("registration_id")
        .annotate(total=Count("pk"))
        .values_list("total", flat=True)
    )
    request_buckets = {"1": 0, "2": 0, "3+": 0}
    for total in requests_per_registration:
        request_buckets["1" if total == 1 else "2" if total == 2 else "3+"] += 1
    return {
        "manual_review_reasons": reasons,
        "identity_corrections": identity_buckets,
        "repeated_identity_corrections": identity_buckets["2"] + identity_buckets["3+"],
        "information_requests": request_buckets,
        "repeated_information_requests": request_buckets["2"] + request_buckets["3+"],
        "bucket_rows": [
            (bucket, identity_buckets[bucket], request_buckets[bucket])
            for bucket in ("1", "2", "3+")
        ],
        "verified_without_review_work": base.filter(
            reporting.verified_without_review_work_q()
        ).count(),
    }


def _grouped(queryset, value_field: str, label_of, *, missing_label, limit=TOP_ROWS):
    rows = list(
        queryset.values(value_field).annotate(total=Count("pk", distinct=True)).order_by("-total")
    )
    result, missing = [], 0
    for row in rows:
        if row[value_field] in (None, ""):
            missing += row["total"]
            continue
        result.append({"value": str(row[value_field]), "label": label_of(row[value_field]),
                       "count": row["total"]})  # fmt: skip
    shown = result[:limit]
    other = sum(row["count"] for row in result[limit:])
    if other:
        shown.append({"value": "", "label": str(_("Other")), "count": other, "other": True})
    if missing:
        shown.append(
            {"value": ops_list.MISSING, "label": str(missing_label), "count": missing,
             "missing": True}
        )  # fmt: skip
    return shown


def final_approved_population(base) -> dict:
    """The final-approved population and its readiness. A registration holds
    at most one CURRENT role and one CURRENT badge type (exclusive
    assignments), so those rows add up to the total with the "none" row.
    Attendance DAYS overlap: a registration approved for all three days
    counts on each of them."""
    from apps.accreditation.models import (
        AccessProfileAssignment,
        AttendanceEntitlement,
        BadgeType,
        BadgeTypeAssignment,
        ParticipantRole,
        ParticipantRoleAssignment,
    )
    from apps.badges.models import DigitalEntryPass
    from apps.badges.models.stock import BadgeIssuance
    from apps.organizations.models import Organization

    approved = base.filter(reporting.final_approved_q())
    total = approved.count()
    ids = approved.values("pk")

    role_names = {}

    def role_label(role_id):
        if role_id not in role_names:
            role = ParticipantRole.objects.filter(pk=role_id).first()
            role_names[role_id] = role.localized_name if role else str(role_id)
        return role_names[role_id]

    roles = _grouped(
        ParticipantRoleAssignment.objects.filter(registration_id__in=ids, status="CURRENT"),
        "role_id",
        role_label,
        missing_label="",
    )
    without_role = approved.exclude(
        Exists(
            ParticipantRoleAssignment.objects.filter(
                registration_id=OuterRef("pk"), status="CURRENT"
            )
        )
    ).count()
    if without_role:
        roles.append(
            {"value": ops_list.MISSING, "label": str(_("No current role")), "count": without_role,
             "missing": True}
        )  # fmt: skip

    badge_names = {
        str(badge.pk): badge.localized_name
        for badge in BadgeType.objects.filter(
            pk__in=BadgeTypeAssignment.objects.filter(
                registration_id__in=ids, status="CURRENT"
            ).values("badge_type_id")
        )
    }
    badge_types = _grouped(
        BadgeTypeAssignment.objects.filter(registration_id__in=ids, status="CURRENT"),
        "badge_type_id",
        lambda value: badge_names.get(str(value), str(value)),
        missing_label="",
    )
    without_badge = approved.exclude(
        Exists(BadgeTypeAssignment.objects.filter(registration_id=OuterRef("pk"), status="CURRENT"))
    ).count()
    if without_badge:
        badge_types.append(
            {"value": "", "label": str(_("No current badge type")), "count": without_badge,
             "missing": True}
        )  # fmt: skip

    organization_names = dict(
        Organization.objects.filter(
            pk__in=approved.exclude(source_organization__isnull=True).values(
                "source_organization_id"
            )
        ).values_list("pk", "official_name")
    )
    organizations = _grouped(
        approved,
        "source_organization_id",
        lambda value: organization_names.get(value, str(value)),
        missing_label=_("No organization"),
    )

    current_attendance = AttendanceEntitlement.objects.filter(
        registration_id=OuterRef("pk"), status="CURRENT"
    )
    all_days = approved.filter(
        Exists(current_attendance.filter(category="ALL_CONFERENCE_DAYS"))
    ).count()
    following = approved.filter(
        Exists(current_attendance.filter(category="FOLLOWING_TWO_DAYS"))
    ).count()
    unclassified = approved.exclude(Exists(current_attendance)).count()
    attendance = {
        "categories": [
            {
                "value": "ALL_CONFERENCE_DAYS",
                "label": str(_("All three conference days")),
                "count": all_days,
            },
            {
                "value": "FOLLOWING_TWO_DAYS",
                "label": str(_("The two days after the opening day")),
                "count": following,
            },
            {
                "value": ops_list.MISSING,
                "label": str(_("Not classified yet")),
                "count": unclassified,
                "missing": True,
            },
        ],  # fmt: skip
        # Overlapping: a registration with all three days counts on each day.
        "days": [
            {"day": "1", "count": all_days},
            {"day": "2", "count": all_days + following},
            {"day": "3", "count": all_days + following},
        ],
    }

    with_profile = approved.filter(
        Exists(
            AccessProfileAssignment.objects.filter(registration_id=OuterRef("pk"), status="CURRENT")
        )
    ).count()
    passes = DigitalEntryPass.objects.filter(registration_id=OuterRef("pk"))
    pass_active = approved.filter(Exists(passes.filter(status="ACTIVE"))).count()
    pass_generated = (
        approved.filter(Exists(passes.filter(status__in=("INACTIVE", "SUSPENDED"))))
        .exclude(Exists(passes.filter(status="ACTIVE")))
        .count()
    )
    badge_issued = approved.filter(
        Exists(BadgeIssuance.objects.filter(registration_id=OuterRef("pk"), status="ISSUED"))
    ).count()
    readiness = [
        {"key": "access_profile", "label": str(_("Current access profile")), "count": with_profile},
        {"key": "pass_active", "label": str(_("Digital entry pass active")), "count": pass_active},
        {"key": "pass_generated", "label": str(_("Pass generated, not active")),
         "count": pass_generated},
        {"key": "pass_none", "label": str(_("No digital entry pass yet")),
         "count": total - pass_active - pass_generated},
        {"key": "badge_issued", "label": str(_("Physical badge handed over")),
         "count": badge_issued},
    ]  # fmt: skip
    for row in readiness:
        row["percent"] = _pct(row["count"], total)
    return {
        "total": total,
        "roles": roles,
        "badge_types": badge_types,
        "organizations": organizations,
        "attendance": attendance,
        "readiness": readiness,
    }


def sources(base) -> dict:
    from apps.invitations.models import DelegationRow, InvitationCampaign
    from apps.organizations.models import Organization
    from apps.registrations.models import RegistrationSourceKind

    applications = base.filter(reporting.applications_q())
    approved = reporting.final_approved_q()
    kind_labels = dict(RegistrationSourceKind.choices)
    by_kind = [
        {
            "value": row["source_kind"],
            "label": str(kind_labels.get(row["source_kind"], row["source_kind"])),
            "applications": row["applications"],
            "approved": row["approved"],
        }
        for row in applications.values("source_kind")
        .annotate(applications=Count("pk"), approved=Count("pk", filter=approved))
        .order_by("-applications")
    ]

    def grouped(field_name, names, missing_label):
        rows, missing = [], None
        for row in (
            applications.values(field_name)
            .annotate(applications=Count("pk"), approved=Count("pk", filter=approved))
            .order_by("-applications", field_name)
        ):
            item = {
                "value": str(row[field_name] or ""),
                "label": names.get(row[field_name], str(row[field_name])),
                "applications": row["applications"],
                "approved": row["approved"],
            }
            if row[field_name] is None:
                item.update(value=ops_list.MISSING, label=str(missing_label), missing=True)
                missing = item
            else:
                rows.append(item)
        # R-02: every group; the page shows a disclosed top-N (`compact_rows`),
        # the reports list them all.
        if missing:
            rows.append(missing)
        return rows

    campaign_names = {
        campaign.pk: f"{campaign.name} ({campaign.public_reference})"
        for campaign in InvitationCampaign.objects.filter(
            pk__in=applications.exclude(invitation_campaign__isnull=True).values(
                "invitation_campaign_id"
            )
        )
    }
    organization_names = dict(
        Organization.objects.filter(
            pk__in=applications.exclude(source_organization__isnull=True).values(
                "source_organization_id"
            )
        ).values_list("pk", "official_name")
    )
    batches = [
        {
            "value": str(row["batch_id"]),
            "label": f"{row['batch__organization__official_name']} · "
            f"{timezone.localtime(row['batch__created_at']):%Y-%m-%d}",
            "applications": row["applications"],
            "approved": row["approved"],
        }
        for row in DelegationRow.objects.filter(registration__in=applications.values("pk"))
        .values("batch_id", "batch__organization__official_name", "batch__created_at")
        .annotate(
            applications=Count("registration", distinct=True),
            approved=Count(
                "registration",
                distinct=True,
                filter=Q(registration__in=applications.filter(approved).values("pk")),
            ),
        )
        .order_by("-applications", "batch_id")
    ]
    return {
        "kinds": by_kind,
        "campaigns": grouped("invitation_campaign_id", campaign_names, _("No campaign")),
        "organizations": grouped(
            "source_organization_id", organization_names, _("No organization")
        ),
        "batches": batches,
    }


def compact_rows(rows: list[dict], limit: int = TOP_ROWS) -> tuple[list[dict], int]:
    """The page's compact form of a complete applications/final-approved
    breakdown (R-02): the `limit` largest groups, then ONE "Other" row that
    adds up every remaining group (so the rows still sum to the population),
    then the "missing" row. Returns the rows and the number of groups folded
    into "Other". Reports use the complete breakdown instead."""
    named = [row for row in rows if not row.get("missing")]
    missing = [row for row in rows if row.get("missing")]
    if len(named) <= limit:
        return rows, 0
    hidden = named[limit:]
    other = {
        "value": "",
        "label": str(_("Other (%(groups)s more)") % {"groups": len(hidden)}),
        "applications": sum(row["applications"] for row in hidden),
        "approved": sum(row["approved"] for row in hidden),
        "other": True,
    }
    return [*named[:limit], other, *missing], len(hidden)


def composition(base) -> dict:
    """Stored categories of the applications and of the final-approved, side
    by side. Missing values are their own row ("Not provided")."""
    from apps.core.models import Country, Sector
    from apps.organizations.models import OrganizationType

    applications = base.filter(reporting.applications_q())
    approved = reporting.final_approved_q()

    def grouped(field_name, label_of):
        rows, missing = [], None
        for row in (
            applications.values(field_name)
            .annotate(applications=Count("pk"), approved=Count("pk", filter=approved))
            .order_by("-applications", field_name)
        ):
            value = row[field_name]
            item = {
                "value": str(value or ""),
                "label": label_of(value) if value else str(_("Not provided")),
                "applications": row["applications"],
                "approved": row["approved"],
            }
            if not value:
                item.update(value=ops_list.MISSING, missing=True)
                missing = (
                    item
                    if missing is None
                    else {
                        **missing,
                        "applications": missing["applications"] + item["applications"],
                        "approved": missing["approved"] + item["approved"],
                    }
                )
            else:
                rows.append(item)
        if missing:
            rows.append(missing)
        return rows

    countries = {country.pk: country.localized_name for country in Country.objects.all()}
    sectors = {sector.pk: sector.localized_name for sector in Sector.objects.all()}
    organization_types = dict(OrganizationType.choices)
    return {
        "nationality": grouped(
            "profile__nationality_code_id", lambda value: countries.get(value, value)
        ),
        "residence": grouped(
            "profile__country_of_residence_id", lambda value: countries.get(value, value)
        ),
        "organization_type": grouped(
            "professional_affiliation__organization_type",
            lambda value: str(organization_types.get(value, value)),
        ),
        "sector": grouped(
            "professional_affiliation__sector_id", lambda value: sectors.get(value, value)
        ),
    }


@dataclass
class TrendRange:
    """`start`/`end`: the exact requested dates, which select the events and
    the cumulative baseline. `buckets`: the bar labels (each day, or the
    Monday of each week); a week bar counts only its dates inside the range."""

    start: datetime.date
    end: datetime.date
    period: str
    tz: object
    buckets: list = field(default_factory=list)


def trend_range(filters: dict[str, str]) -> TrendRange:
    tz = ops_list.filters_timezone(filters)
    today = timezone.now().astimezone(tz).date()
    end = (
        datetime.date.fromisoformat(filters["submitted_to"])
        if filters.get("submitted_to")
        else today
    )
    start = (
        datetime.date.fromisoformat(filters["submitted_from"])
        if filters.get("submitted_from")
        else end - datetime.timedelta(days=DEFAULT_TREND_DAYS - 1)
    )
    if start > end:
        start, end = end, start
    span = (end - start).days + 1
    period = filters.get("period") or ("week" if span > MAX_DAILY_BUCKETS else "day")
    if period == "week":
        # R-03: only the labels align to Monday; the range itself is kept.
        first = start - datetime.timedelta(days=start.weekday())
        step = datetime.timedelta(days=7)
    else:
        first = start
        step = datetime.timedelta(days=1)
    buckets, current = [], first
    while current <= end:
        buckets.append(current)
        current += step
    return TrendRange(start=start, end=end, period=period, tz=tz, buckets=buckets)


def _bucket(day: datetime.date, rng: TrendRange) -> datetime.date:
    if rng.period == "week":
        return day - datetime.timedelta(days=day.weekday())
    return day


def _daily(queryset, field_name: str, rng: TrendRange, *, extra=None) -> dict:
    start = datetime.datetime.combine(rng.start, datetime.time.min, rng.tz)
    end = datetime.datetime.combine(rng.end, datetime.time.min, rng.tz) + datetime.timedelta(days=1)
    rows = (
        queryset.filter(**{f"{field_name}__gte": start, f"{field_name}__lt": end})
        .annotate(day=TruncDate(field_name, tzinfo=rng.tz))
        .values("day")
        .annotate(total=Count("pk"))
    )
    counts: dict = {}
    for row in rows:
        key = _bucket(row["day"], rng)
        counts[key] = counts.get(key, 0) + row["total"]
    return counts


def trends(user, filters: dict[str, str]) -> dict:
    """Events dated within the range (module docstring), per day or week,
    with cumulative series that start from the count before the range."""
    from apps.reviews.models import RegistrationDecision

    rng = trend_range(filters)
    events = base_queryset(user, filters, cohort=False)
    start_at = datetime.datetime.combine(rng.start, datetime.time.min, rng.tz)
    submitted = _daily(events, "submitted_at", rng)
    decisions = RegistrationDecision.objects.filter(registration__in=events.values("pk"))
    approved = _daily(decisions.filter(outcome="APPROVED"), "decided_at", rng)
    not_approved = _daily(decisions.filter(outcome="NOT_APPROVED"), "decided_at", rng)
    withdrawn = _daily(events.filter(cancelled_at__isnull=True), "withdrawn_at", rng)
    cancelled = _daily(events, "cancelled_at", rng)
    baseline_applications = events.filter(submitted_at__lt=start_at).count()
    baseline_approvals = decisions.filter(outcome="APPROVED", decided_at__lt=start_at).count()
    rows, total_apps, total_approved = [], baseline_applications, baseline_approvals
    for bucket in rng.buckets:
        total_apps += submitted.get(bucket, 0)
        total_approved += approved.get(bucket, 0)
        rows.append(
            {
                "bucket": bucket,
                "applications": submitted.get(bucket, 0),
                "approved": approved.get(bucket, 0),
                "not_approved": not_approved.get(bucket, 0),
                "withdrawn": withdrawn.get(bucket, 0) + cancelled.get(bucket, 0),
                "cumulative_applications": total_apps,
                "cumulative_approval_decisions": total_approved,
            }
        )
    peak = max([row["applications"] for row in rows] + [row["approved"] for row in rows] + [1])
    return {
        "range": rng,
        "rows": rows,
        "peak": peak,
        "baseline_applications": baseline_applications,
        "baseline_approvals": baseline_approvals,
    }


@dataclass
class Report:
    """Everything one page or export shows, computed together for one moment."""

    generated_at: datetime.datetime
    filters: dict
    overview: dict
    workload: list
    exceptions: dict
    final_approved: dict
    sources: dict
    composition: dict
    trends: dict
    scope_total: int


def build_report(user, filters: dict[str, str]) -> Report:
    base = base_queryset(user, filters)
    return Report(
        generated_at=timezone.now(),
        filters=filters,
        overview=overview(base),
        workload=workload(base),
        exceptions=exceptions(base),
        final_approved=final_approved_population(base),
        sources=sources(base),
        composition=composition(base),
        trends=trends(user, filters),
        scope_total=ops_list.scope_total(user),
    )
