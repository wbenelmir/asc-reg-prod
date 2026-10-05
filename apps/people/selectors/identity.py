"""Read selectors for the identity review queue and review screen (IDV-3).

Scope is enforced at the queryset level (`apps.people.policies`). The queue
uses a bounded set of filters, one paginated query with `select_related`, and
one aggregate for the status counts. Identifiers are masked unless the reader
holds `people.view_identity_evidence` for that case; a NIN search is allowed
only to such readers, by exact blind-index match, never by a plaintext scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Q

from apps.people.models import (
    IdentityDecision,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityRoute,
    IdentityStatus,
    IdentityVerificationAttempt,
    IdentityVerificationMethod,
    VerificationSource,
)
from apps.people.policies import identity_verifications_visible_to

#: The only query-string keys the queue reads, and that "next case" keeps.
#: There is no search text among them: a search is a server-side context
#: referenced by the opaque `ctx` (IDV-C1, R-IDV-05; `apps.people.search_context`).
QUEUE_FILTER_KEYS = ("status", "reason", "route", "nationality", "source", "event", "ctx")
DEFAULT_STATUS_FILTER = IdentityStatus.MANUAL_REVIEW
ALL_STATUSES = "ALL"
#: The most case ids one search keeps.
SEARCH_RESULT_LIMIT = 500


def clean_queue_filters(query) -> dict[str, str]:
    """Bounded, validated filters from a query dict; unknown values are dropped."""
    filters: dict[str, str] = {}
    status = (query.get("status") or DEFAULT_STATUS_FILTER).strip()
    if status == ALL_STATUSES or status in IdentityStatus.values:
        filters["status"] = status
    else:
        filters["status"] = DEFAULT_STATUS_FILTER
    reason = (query.get("reason") or "").strip()
    if reason in IdentityReasonCode.values:
        filters["reason"] = reason
    route = (query.get("route") or "").strip()
    if route in IdentityRoute.values:
        filters["route"] = route
    nationality = (query.get("nationality") or "").strip().upper()
    if len(nationality) == 2 and nationality.isalpha():
        filters["nationality"] = nationality
    source = (query.get("source") or "").strip()
    if source in VerificationSource.values:
        filters["source"] = source
    event = (query.get("event") or "").strip()
    if event:
        import uuid

        try:
            filters["event"] = str(uuid.UUID(event))
        except ValueError:
            pass
    from apps.people.search_context import REFERENCE_PATTERN

    reference = (query.get("ctx") or "").strip()
    if REFERENCE_PATTERN.fullmatch(reference):
        filters["ctx"] = reference
    return filters


def _nin_search_ids(user, search: str):
    """Exact NIN search, only within cases where the reader may see evidence."""
    from apps.core.text_rules import TextRuleError, normalize_nin
    from apps.people.models import IdentifierType
    from apps.people.selectors import identifiers_for_value

    try:
        nin = normalize_nin(search)
    except TextRuleError:
        return None
    identifier_ids = identifiers_for_value(
        identifier_type=IdentifierType.NIN, country_code_id="DZ", raw_value=nin
    ).values_list("pk", flat=True)
    return (
        identity_verifications_visible_to(user, codename="view_identity_evidence")
        .filter(current_revision__identifier_id__in=list(identifier_ids))
        .values_list("pk", flat=True)
    )


def resolve_queue_search(user, text: str) -> tuple[str, list[str], bool]:
    """Run a queue search ONCE: `(kind, case ids, truncated)`.

    A full NIN is an exact blind-index match, only within cases whose evidence
    the reader may see; other text matches a reference or a name prefix
    within the reader's scope. The text itself is never kept (R-IDV-05).
    Returns `("", [], False)` for an empty search.
    """
    text = (text or "").strip()[:100]
    if not text:
        return "", [], False
    nin_ids = _nin_search_ids(user, text) if text.replace(" ", "").isdigit() else None
    if nin_ids is not None:
        kind, ids = "nin", nin_ids
    elif len(text) >= 2:
        kind = "text"
        ids = (
            identity_verifications_visible_to(user)
            .filter(
                Q(registration__public_reference__istartswith=text)
                | Q(registration__profile__submitted_given_names__istartswith=text)
                | Q(registration__profile__submitted_family_name__istartswith=text)
            )
            .values_list("pk", flat=True)
        )
    else:
        return "text", [], False
    found = [str(pk) for pk in ids.order_by("status_changed_at", "pk")[: SEARCH_RESULT_LIMIT + 1]]
    return kind, found[:SEARCH_RESULT_LIMIT], len(found) > SEARCH_RESULT_LIMIT


def queue_queryset(user, filters: dict[str, str], *, search=None, apply_status: bool = True):
    """The filtered queue. `search` (a `SearchContext`) narrows it to the case
    ids of a resolved search; scope -- and, for a NIN search, the evidence
    permission -- is applied again here on every use."""
    queryset = identity_verifications_visible_to(user).select_related(
        "registration",
        "registration__profile",
        "nationality",
        "event_edition",
        "current_revision__identifier",
    )
    if apply_status and filters.get("status") not in (None, ALL_STATUSES):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("reason"):
        queryset = queryset.filter(reason_code=filters["reason"])
    if filters.get("route"):
        queryset = queryset.filter(route=filters["route"])
    if filters.get("nationality"):
        queryset = queryset.filter(nationality_id=filters["nationality"])
    if filters.get("source"):
        queryset = queryset.filter(verification_source=filters["source"])
    if filters.get("event"):
        queryset = queryset.filter(event_edition_id=filters["event"])
    if search is not None:
        queryset = queryset.filter(pk__in=list(search.case_ids))
        if search.kind == "nin":
            queryset = queryset.filter(
                pk__in=identity_verifications_visible_to(
                    user, codename="view_identity_evidence"
                ).values("pk")
            )
    return queryset.order_by("status_changed_at", "pk")


def status_counts(user, filters: dict[str, str], *, search=None) -> dict[str, int]:
    """Counts per status under every filter except the status itself."""
    rows = (
        queue_queryset(user, filters, search=search, apply_status=False)
        .order_by()
        .values("status")
        .annotate(total=Count("pk"))
    )
    counts = {status: 0 for status in IdentityStatus.values}
    for row in rows:
        counts[row["status"]] = row["total"]
    return counts


def filter_choices(user) -> dict[str, list]:
    """Distinct events and nationalities in the reader's scope (bounded)."""
    visible = identity_verifications_visible_to(user)
    events = (
        visible.order_by().values_list("event_edition_id", "event_edition__code").distinct()[:50]
    )
    nationalities = (
        visible.order_by("nationality_id").values_list("nationality_id", flat=True).distinct()[:300]
    )
    return {"events": list(events), "nationalities": list(nationalities)}


def next_case_after(user, filters: dict[str, str], *, after_key, exclude_pk, search=None):
    """The next case in the same filtered order after `after_key`
    (`(status_changed_at, pk)` captured when the reviewer opened the case),
    wrapping to the first; None when the filtered queue is empty."""
    queryset = queue_queryset(user, filters, search=search).exclude(pk=exclude_pk)
    if after_key is not None:
        changed_at, pk = after_key
        following = queryset.filter(
            Q(status_changed_at__gt=changed_at) | Q(status_changed_at=changed_at, pk__gt=pk)
        ).first()
        if following is not None:
            return following
    return queryset.first()


# ---------------------------------------------------------------------------
# Review screen
# ---------------------------------------------------------------------------


@dataclass
class ConflictView:
    visible_references: list = field(default_factory=list)
    hidden_count: int = 0

    @property
    def exists(self) -> bool:
        return bool(self.visible_references or self.hidden_count)


def conflicts_for(user, verification) -> ConflictView:
    """The cross-person conflicts of the case's current identifier, as the
    registration references this reader may see, plus a count of the others.
    Never another person's name, identifier or document."""
    from apps.accounts.selectors import registrations_visible_to
    from apps.people.services.identity_verification import find_cross_person_conflicts

    if verification.current_revision_id is None:
        return ConflictView()
    identifier = verification.current_revision.identifier
    summary = find_cross_person_conflicts(
        identifier_type=identifier.identifier_type,
        country_code_id=identifier.country_code_id,
        raw_value=identifier.value_encrypted,
        person_id=verification.person_id,
    )
    if not summary.exists:
        return ConflictView()
    registration_ids = list(summary.registration_ids)
    visible = list(
        registrations_visible_to(user)
        .filter(pk__in=registration_ids)
        .values_list("public_reference", flat=True)
    )
    hidden = max(len(registration_ids) - len(visible), 0)
    if not registration_ids:
        hidden = 1  # a verified identifier without a case still conflicts
    return ConflictView(visible_references=visible, hidden_count=hidden)


def _checked_revision_id(verification):
    """The revision the service checked: the current one, or, when the current
    one only applied the official name form (IDV-C1, R-IDV-02), the revision it
    was derived from."""
    revision = verification.current_revision
    if revision.source == IdentityRevisionSource.OFFICIAL_NAME_NORMALIZATION:
        previous = (
            verification.revisions.filter(number=revision.number - 1)
            .values_list("pk", flat=True)
            .first()
        )
        if previous is not None:
            return previous
    return revision.pk


def latest_provider_attempt(verification):
    """The latest applied ministry (or simulation) attempt of the checked
    revision, which holds the retained official facts and the comparison."""
    if verification.current_revision_id is None:
        return None
    return (
        IdentityVerificationAttempt.objects.filter(
            revision_id=_checked_revision_id(verification),
            method=IdentityVerificationMethod.EXTERNAL_SERVICE,
            applied=True,
        )
        .order_by("-attempted_at")
        .first()
    )


def names_as_entered(verification) -> dict | None:
    """When the official name form was applied (R-IDV-02), the names as the
    participant last entered them, read from the latest immutable submission
    snapshot; otherwise None."""
    revision = verification.current_revision
    if revision is None or revision.source != IdentityRevisionSource.OFFICIAL_NAME_NORMALIZATION:
        return None
    submission = verification.registration.submissions.order_by("-sequence").first()
    identity = (submission.snapshot_json or {}).get("identity", {}) if submission else {}
    return {
        "given_names": identity.get("given_names", ""),
        "family_name": identity.get("family_name", ""),
    }


def case_history(verification):
    revisions = list(verification.revisions.select_related("created_by_user").order_by("-number"))
    decisions = list(
        IdentityDecision.objects.filter(verification=verification)
        .select_related("actor_user", "evidence_document")
        .order_by("-created_at")
    )
    attempts = list(
        IdentityVerificationAttempt.objects.filter(registration_id=verification.registration_id)
        .select_related("revision", "performed_by_user")
        .order_by("-attempted_at")[:50]
    )
    return revisions, decisions, attempts
