"""The complete operations registrations list (UPD-05, `/ops/registrations/`).

Every registration the reader may see (`registrations_visible_to`, the same
scope rule as before), drafts, cancelled and historical ones included, with
server-side filters and database pagination:

* filters and the search apply to the whole authorized set BEFORE pagination
  (a reference on page 9 is found from page 1), and the matching count is the
  paginator's count of that same queryset;
* ordering is stable: newest first, ties broken by id;
* nothing is decrypted or loaded beyond the current page (50 rows, one query
  with `select_related`, plus two counts);
* the reference search is an exact or prefix match on the public reference,
  safe in the address (`reference=`). Names and e-mail addresses are personal
  data: they are searched through a CSRF-protected POST resolved once into a
  bounded list of ids kept in the reader's session (`ctx`, the identity
  queue's search-context mechanism), never put in a URL. A name matches the
  start of the given or family name; an e-mail matches exactly through its
  blind index, never by decrypting contact data.

`segment` accepts the population keys of `apps.registrations.reporting`, so
a statistics figure opens exactly the registrations it counts.
"""

from __future__ import annotations

import datetime
import uuid
import zoneinfo
from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q

from apps.registrations import reporting

PAGE_SIZE = 50
#: The query-string keys the list reads (and keeps across pages). No personal
#: search text is among them: a private search is the opaque `ctx` reference.
LIST_FILTER_KEYS = (
    "reference",
    "status",
    "segment",
    "event",
    "source",
    "organization",
    "campaign",
    "link",
    "batch",
    "nationality",
    "residence",
    "role",
    "attendance",
    "day",
    "submitted_from",
    "submitted_to",
    "ctx",
)
#: Session namespace of the private searches of this list.
SEARCH_NAMESPACE = "ops_registration_search"
SEARCH_RESULT_LIMIT = 500
MISSING = "__missing__"

#: Extra status filters besides the stored public statuses.
STATUS_CANCELLED_BY_TEAM = "CANCELLED_BY_TEAM"
STATUS_WITHDRAWN_BY_PARTICIPANT = "WITHDRAWN_BY_PARTICIPANT"


def _uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except ValueError:
        return ""


def _country(value) -> str:
    code = (value or "").strip().upper()
    if code == MISSING.upper():
        return MISSING
    return code if len(code) == 2 and code.isalpha() else ""


def _date(value) -> str:
    try:
        return datetime.date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        return ""


def clean_list_filters(query) -> dict[str, str]:
    """Bounded, validated filters; unknown or malformed values are dropped."""
    from apps.accreditation.models import AttendanceCategory
    from apps.people.search_context import REFERENCE_PATTERN
    from apps.registrations.models import RegistrationPublicStatus, RegistrationSourceKind

    filters: dict[str, str] = {}
    reference = "".join((query.get("reference") or "").split())[:32]
    if reference and all(char.isalnum() or char in "-_" for char in reference):
        filters["reference"] = reference
    status = (query.get("status") or "").strip()
    if status in RegistrationPublicStatus.values or status in (
        STATUS_CANCELLED_BY_TEAM,
        STATUS_WITHDRAWN_BY_PARTICIPANT,
    ):
        filters["status"] = status
    segment = (query.get("segment") or "").strip()
    if segment in reporting.SEGMENTS:
        filters["segment"] = segment
    for key in ("event", "organization", "campaign", "link", "batch", "role"):
        value = (query.get(key) or "").strip()
        if key in ("organization", "campaign", "role") and value == MISSING:
            filters[key] = MISSING
        elif _uuid(value):
            filters[key] = _uuid(value)
    source = (query.get("source") or "").strip()
    if source in RegistrationSourceKind.values:
        filters["source"] = source
    for key in ("nationality", "residence"):
        code = _country(query.get(key))
        if code:
            filters[key] = code
    attendance = (query.get("attendance") or "").strip()
    if attendance in AttendanceCategory.values or attendance == MISSING:
        filters["attendance"] = attendance
    day = (query.get("day") or "").strip()
    if day in ("1", "2", "3"):
        filters["day"] = day
    for key in ("submitted_from", "submitted_to"):
        value = _date(query.get(key))
        if value:
            filters[key] = value
    # R-03: one policy for a reversed range everywhere (list, statistics,
    # charts, exports): it is put in order, never read as an empty range.
    if filters.get("submitted_from", "") > filters.get("submitted_to", "9999-12-31"):
        filters["submitted_from"], filters["submitted_to"] = (
            filters["submitted_to"],
            filters["submitted_from"],
        )
    reference_ctx = (query.get("ctx") or "").strip()
    if REFERENCE_PATTERN.fullmatch(reference_ctx):
        filters["ctx"] = reference_ctx
    return filters


def filters_timezone(filters: dict[str, str]):
    """The timezone of the date filters: the selected event's own timezone
    (its local calendar days), otherwise UTC."""
    if filters.get("event"):
        from apps.events.models import EventEdition

        name = (
            EventEdition.objects.filter(pk=filters["event"])
            .values_list("timezone", flat=True)
            .first()
        )
        if name:
            try:
                return zoneinfo.ZoneInfo(name)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):  # fmt: skip
                pass
    return datetime.UTC


def _day_start(day: str, tz) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time.min, tz)


def apply_filters(queryset, filters: dict[str, str], *, search=None):
    """Narrow a `Registration` queryset by `filters` (and a resolved private
    search). Used by the list, its counts and the statistics drill-down."""
    from apps.accreditation.models import AttendanceEntitlement, ParticipantRoleAssignment

    if filters.get("reference"):
        queryset = queryset.filter(public_reference__istartswith=filters["reference"])
    status = filters.get("status")
    if status == STATUS_CANCELLED_BY_TEAM:
        queryset = queryset.filter(reporting.cancelled_by_team_q())
    elif status == STATUS_WITHDRAWN_BY_PARTICIPANT:
        queryset = queryset.filter(reporting.withdrawn_by_participant_q())
    elif status:
        queryset = queryset.filter(public_status=status)
    if filters.get("segment"):
        queryset = queryset.filter(reporting.segment_q(filters["segment"]))
    if filters.get("event"):
        queryset = queryset.filter(event_edition_id=filters["event"])
    if filters.get("source"):
        queryset = queryset.filter(source_kind=filters["source"])
    for key, field in (
        ("organization", "source_organization_id"),
        ("campaign", "invitation_campaign_id"),
    ):
        value = filters.get(key)
        if value == MISSING:
            queryset = queryset.filter(**{f"{field}__isnull": True})
        elif value:
            queryset = queryset.filter(**{field: value})
    if filters.get("link"):
        from apps.invitations.models import InvitationUse

        queryset = queryset.filter(
            Exists(
                InvitationUse.objects.filter(
                    registration_id=OuterRef("pk"), link_id=filters["link"]
                )
            )
        )
    if filters.get("batch"):
        from apps.invitations.models import DelegationRow

        queryset = queryset.filter(
            Exists(
                DelegationRow.objects.filter(
                    registration_id=OuterRef("pk"), batch_id=filters["batch"]
                )
            )
        )
    for key, field in (
        ("nationality", "profile__nationality_code_id"),
        ("residence", "profile__country_of_residence_id"),
    ):
        value = filters.get(key)
        if value == MISSING:
            queryset = queryset.filter(**{f"{field}__isnull": True})
        elif value:
            queryset = queryset.filter(**{field: value})
    role = filters.get("role")
    if role:
        current_roles = ParticipantRoleAssignment.objects.filter(
            registration_id=OuterRef("pk"), status="CURRENT"
        )
        if role == MISSING:
            queryset = queryset.exclude(Exists(current_roles))
        else:
            queryset = queryset.filter(Exists(current_roles.filter(role_id=role)))
    attendance = filters.get("attendance")
    if attendance:
        current = AttendanceEntitlement.objects.filter(
            registration_id=OuterRef("pk"), status="CURRENT"
        )
        if attendance == MISSING:
            queryset = queryset.exclude(Exists(current))
        else:
            queryset = queryset.filter(Exists(current.filter(category=attendance)))
    if filters.get("day"):
        # A conference day: the opening day is covered only by "all three
        # days"; days 2 and 3 by either category (a registration covering
        # several days is listed under each of them).
        current = AttendanceEntitlement.objects.filter(
            registration_id=OuterRef("pk"), status="CURRENT"
        )
        if filters["day"] == "1":
            current = current.filter(category="ALL_CONFERENCE_DAYS")
        queryset = queryset.filter(Exists(current))
    tz = (
        filters_timezone(filters)
        if (filters.get("submitted_from") or filters.get("submitted_to"))
        else None
    )
    if filters.get("submitted_from"):
        queryset = queryset.filter(submitted_at__gte=_day_start(filters["submitted_from"], tz))
    if filters.get("submitted_to"):
        end = _day_start(filters["submitted_to"], tz) + datetime.timedelta(days=1)
        queryset = queryset.filter(submitted_at__lt=end)
    if search is not None:
        queryset = queryset.filter(pk__in=list(search.case_ids))
    return queryset


def list_queryset(user, filters: dict[str, str], *, search=None):
    """The page queryset: authorized scope, filters, stable order."""
    from apps.accounts.selectors import registrations_visible_to

    queryset = registrations_visible_to(user).select_related(
        "event_edition",
        "source_organization",
        "invitation_campaign",
        "profile",
        "profile__nationality_code",
    )
    return apply_filters(queryset, filters, search=search).order_by("-created_at", "-pk")


def scope_total(user) -> int:
    """Every registration the reader may see, before any filter."""
    from apps.accounts.selectors import registrations_visible_to

    return registrations_visible_to(user).order_by().count()


def resolve_private_search(user, text: str) -> tuple[str, list[str], bool]:
    """Resolve a name or e-mail search ONCE into registration ids within the
    reader's scope: `(kind, ids, truncated)`. The text itself is never kept.

    An e-mail is matched exactly through the contact blind index (every
    active key version); anything else matches the start of the given or the
    family name (at least 2 characters). A text that looks like a reference
    is matched as a reference prefix."""
    from apps.accounts.selectors import registrations_visible_to

    text = (text or "").strip()[:100]
    if len(text) < 2:
        return "", [], False
    visible = registrations_visible_to(user)
    if "@" in text:
        from apps.people.models import ContactPointType
        from apps.people.selectors import contact_points_for_value

        try:
            person_ids = contact_points_for_value(
                contact_type=ContactPointType.EMAIL, raw_value=text
            ).values("person_id")
        except Exception:  # noqa: BLE001 - an unusable address matches nothing
            return "email", [], False
        kind, matching = "email", visible.filter(person_id__in=person_ids)
    else:
        kind = "text"
        matching = visible.filter(
            Q(public_reference__istartswith=text)
            | Q(profile__submitted_given_names__istartswith=text)
            | Q(profile__submitted_family_name__istartswith=text)
        )
    found = [
        str(pk)
        for pk in matching.order_by("-created_at", "-pk").values_list("pk", flat=True)[
            : SEARCH_RESULT_LIMIT + 1
        ]
    ]
    return kind, found[:SEARCH_RESULT_LIMIT], len(found) > SEARCH_RESULT_LIMIT


@dataclass(frozen=True)
class FilterChoices:
    events: list
    organizations: list
    campaigns: list
    roles: list
    nationalities: list


def filter_choices(user) -> FilterChoices:
    """Bounded choices drawn from the reader's own scope."""
    from apps.accounts.selectors import registrations_visible_to
    from apps.accreditation.models import ParticipantRole
    from apps.core.models import Country
    from apps.events.models import EventEdition
    from apps.invitations.models import InvitationCampaign
    from apps.organizations.models import Organization

    visible = registrations_visible_to(user).order_by()
    event_ids = visible.values("event_edition_id").distinct()
    organization_ids = visible.filter(source_organization__isnull=False).values(
        "source_organization_id"
    )
    campaign_ids = visible.filter(invitation_campaign__isnull=False).values(
        "invitation_campaign_id"
    )
    nationality_ids = visible.filter(profile__nationality_code__isnull=False).values(
        "profile__nationality_code_id"
    )
    return FilterChoices(
        events=list(EventEdition.objects.filter(pk__in=event_ids).order_by("-starts_at")[:50]),
        organizations=list(
            Organization.objects.filter(pk__in=organization_ids).order_by("official_name")[:300]
        ),
        campaigns=list(
            InvitationCampaign.objects.filter(pk__in=campaign_ids)
            .select_related("organization")
            .order_by("name")[:300]
        ),
        roles=list(
            ParticipantRole.objects.filter(event_edition_id__in=event_ids).order_by("code")[:100]
        ),
        nationalities=sorted(
            Country.objects.filter(pk__in=nationality_ids)[:300], key=lambda c: c.localized_name
        ),
    )
