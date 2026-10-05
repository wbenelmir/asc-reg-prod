"""Read models for the entry interfaces (Phase 3 Prompt 4, ADR-0021).

`result_projection` is the ONLY path by which participant data reaches a
checkpoint template. It enforces the minimum-display rules:

* PRD FR-ENT-009 / TRD ENTRY-001: display name, optional photograph,
  masked identity hint, Badge Type, access result, status, concise reason;
* ENTRY-002: never full identity values, documents, notes, exports, or
  unrelated registrations;
* FR-ENT-018: a restriction's category is shown only with the separate
  `entry.view_restriction_reason` permission -- otherwise the operator sees
  only "follow the restricted procedure", and an external security user
  sees a neutral escalation message with no restriction wording at all;
* external security accounts additionally never see the Badge Type, the
  organization, or the masked identity hint (minimum-data external view);
* when the context is not in this edition, not approved, or unresolved,
  NO participant field is projected at all.

The encrypted restriction reason, identity values, contact data, and every
other registration field are simply never read here.
"""

from __future__ import annotations

from django.conf import settings

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.entry.models import (
    EntryDevice,
    EntryEvent,
    EntryReasonCode,
    EntryResult,
    VerificationMethod,
)
from apps.entry.policies import checkpoint_permission, is_external_security_user

#: Reasons for which no participant detail is projected at all: the context
#: either belongs to another edition or must not be admitted on any basis.
_NO_DETAIL_REASONS = frozenset(
    {
        EntryReasonCode.WRONG_EVENT,
        EntryReasonCode.REGISTRATION_NOT_APPROVED,
        EntryReasonCode.INVALID_CREDENTIAL,
        EntryReasonCode.NO_MATCH,
        EntryReasonCode.UNSUPPORTED_CREDENTIAL,
        EntryReasonCode.TECHNICAL_ERROR,
    }
)

_RESTRICTION_REASONS = frozenset(
    {EntryReasonCode.SECURITY_RESTRICTION, EntryReasonCode.RESTRICTION_REVIEW}
)


def viewer_flags(user, checkpoint) -> dict:
    external = is_external_security_user(user)
    return {
        "is_external": external,
        "show_restriction_category": checkpoint_permission(
            user, "view_restriction_reason", checkpoint
        ),
        "may_override": checkpoint_permission(user, "override_entry", checkpoint),
        "may_monitor": checkpoint_permission(user, "view_entryevent", checkpoint),
    }


def _masked_identity_hint(registration, method: str) -> str:
    if method not in (VerificationMethod.NIN, VerificationMethod.PASSPORT):
        return ""
    from apps.people.models import IdentifierStatus, IdentifierType, IdentityIdentifier

    identifier_type = IdentifierType.NIN if method == VerificationMethod.NIN else "PASSPORT"
    row = (
        IdentityIdentifier.objects.filter(
            person_id=registration.person_id, identifier_type=identifier_type
        )
        .exclude(status=IdentifierStatus.REPLACED)
        .order_by("-created_at")
        .values_list("masked_value", flat=True)
        .first()
    )
    return row or ""


def result_projection(*, outcome, user, checkpoint) -> dict:
    """Everything a result screen may render, and nothing more."""
    from apps.entry.models import EntryReasonCode as Reason

    flags = viewer_flags(user, checkpoint)
    assessment = outcome.assessment
    reason = outcome.reason_code
    restriction_related = reason in _RESTRICTION_REASONS or (
        assessment is not None and set(assessment.blocker_codes) & _RESTRICTION_REASONS
    )
    # Concise reason, reduced for external viewers on restriction results.
    if restriction_related and flags["is_external"] and not flags["show_restriction_category"]:
        shown_reason = ""
        escalate = True
    else:
        shown_reason = reason
        escalate = restriction_related
    projection = {
        "result": outcome.result,
        "reason_code": shown_reason,
        # An empty code (an allowed result) has no reason to show at all --
        # never the catalogue's "No reason" label.
        "reason_label": (
            Reason(shown_reason).label if shown_reason and shown_reason in Reason.values else ""
        ),
        "method": outcome.method,
        "escalate_restricted": escalate,
        "restriction_categories": (),
        "participant": None,
        "prior_entry": None,
        "advisories": (),
        **flags,
    }
    if assessment is None or reason in _NO_DETAIL_REASONS:
        return projection
    registration = assessment.registration
    person = registration.person
    participant = {
        "display_name": person.display_name if person is not None else "",
        "registration_reference": registration.public_reference,
        "has_photo": _has_photo(registration),
    }
    if not flags["is_external"]:
        participant["badge_type"] = (
            assessment.badge_assignment.badge_type.localized_name
            if assessment.badge_assignment is not None
            else ""
        )
        participant["identity_hint"] = _masked_identity_hint(registration, outcome.method)
    projection["participant"] = participant
    projection["advisories"] = tuple(
        Reason(code).label for code in assessment.advisory_codes if code in Reason.values
    )
    if assessment.prior_entry is not None:
        projection["prior_entry"] = {
            "occurred_at": assessment.prior_entry.occurred_at,
            "gate": assessment.prior_entry.gate.localized_name,
        }
    if flags["show_restriction_category"]:
        from apps.entry.models import RestrictionCategory

        projection["restriction_categories"] = tuple(
            RestrictionCategory(code).label for code in assessment.restriction_categories
        )
    return projection


def _has_photo(registration) -> bool:
    from apps.documents.services import active_profile_photo

    return active_profile_photo(registration) is not None


def candidate_projection(registration) -> dict:
    """The minimum needed to distinguish several contexts (Flow §10.4)."""
    person = registration.person
    reference = registration.public_reference or ""
    return {
        "display_name": person.display_name if person is not None else "",
        "reference_hint": f"…{reference[-4:]}" if reference else "",
        "nationality": getattr(person, "nationality_id", "") or "",
    }


def recent_entry_events(checkpoint, *, limit: int = 25):
    """This gate's recent Entry Events, for the supervisor monitor."""
    return (
        EntryEvent.objects.select_related("zone", "operator_user")
        .filter(event_edition=checkpoint.event_edition, gate=checkpoint.gate)
        .order_by("-occurred_at")[:limit]
    )


def recent_denied_attempts(checkpoint, *, limit: int = 25):
    """Denied or unresolved verification attempts at this gate (ENTRY-005).

    Read from the audit trail, which records the outcome and counts only --
    never a token, identity value, or search string.

    Filtered on the exact Gate identity (`gate_id`), never on the gate code:
    a code is unique only within a venue, so two venues of one event may
    both have a `G1` (P8-05). Historical rows written before `gate_id`
    existed simply do not match -- fail closed, never a code-only fallback
    that could cross venues.
    """
    return AuditEvent.objects.filter(
        event_edition=checkpoint.event_edition,
        action_code__in=[
            action_codes.ENTRY_VERIFICATION_PERFORMED,
            action_codes.ENTRY_IDENTITY_LOOKUP,
            action_codes.ENTRY_REFERENCE_LOOKUP,
            action_codes.ENTRY_LOOKUP_DENIED,
            # Phase 4 Prompt 3: unresolved scans recorded offline (codes only).
            action_codes.OFFLINE_VERIFICATION_ATTEMPT,
        ],
        after_summary__gate_id=str(checkpoint.gate.pk),
        after_summary__result__in=[
            EntryResult.DENIED,
            EntryResult.UNSUPPORTED,
            EntryResult.TECHNICAL_ERROR,
            EntryResult.STALE,
        ],
    ).order_by("-occurred_at")[:limit]


def labelled_attempts(attempts) -> list[dict]:
    """Denied/unresolved attempts with localized method, result and reason
    labels for display. Codes only ever come from the audit summary, which
    never holds a token, identity value, or search text."""
    methods = dict(VerificationMethod.choices)
    results = dict(EntryResult.choices)
    reasons = dict(EntryReasonCode.choices)
    rows = []
    for attempt in attempts:
        summary = attempt.after_summary or {}
        result = summary.get("result", "")
        reason = summary.get("reason", "")
        rows.append(
            {
                "occurred_at": attempt.occurred_at,
                "method": methods.get(summary.get("method", ""), summary.get("method", "")),
                "result": result,
                "result_label": results.get(result, result),
                "reason_label": reasons.get(reason, reason) if reason else "",
            }
        )
    return rows


def recent_anomaly_signals(checkpoint, *, limit: int = 10) -> list[dict]:
    """Anomaly signals and throttled lookups raised at THIS gate (Prompt 5).

    Kind, count, and time only -- the operator concerned is recorded in the
    audit trail, not shown on the checkpoint monitor. Isolated by exact Gate
    identity, failing closed for rows without it (P8-05).
    """
    rows = AuditEvent.objects.filter(
        event_edition=checkpoint.event_edition,
        action_code__in=[action_codes.ENTRY_ANOMALY_SIGNAL, action_codes.ENTRY_LOOKUP_THROTTLED],
        after_summary__gate_id=str(checkpoint.gate.pk),
    ).order_by("-occurred_at")[:limit]
    return [
        {
            "occurred_at": row.occurred_at,
            "throttled": row.action_code == action_codes.ENTRY_LOOKUP_THROTTLED,
            "kind": row.reason_code,
            "count": (row.after_summary or {}).get("count"),
        }
        for row in rows
    ]


def devices_visible_to(user):
    """Devices of the event editions where `user` holds `entry.view_entrydevice`.

    Devices belong to an edition, not to an organization, so only an
    organization-UNscoped membership contributes (an organization-scoped
    one is a misconfiguration and grants nothing here). Venue/gate-narrowed
    memberships are already excluded by `effective_scoped_memberships`.
    """
    from apps.accounts.policies import effective_scoped_memberships

    queryset = EntryDevice.objects.select_related("event_edition")
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    if user.is_superuser:
        return queryset
    event_ids = set(
        effective_scoped_memberships(user)
        .filter(
            organization__isnull=True,
            group__permissions__content_type__app_label="entry",
            group__permissions__codename="view_entrydevice",
        )
        .values_list("event_edition_id", flat=True)
    )
    if None in event_ids:
        return queryset
    return queryset.filter(event_edition_id__in=event_ids)


def result_clear_seconds() -> int:
    return int(settings.ENTRY_RESULT_CLEAR_SECONDS)
