"""Security restrictions (Schema §11.6, BR-ENT-003, FR-ENT-014/018).

A person-level restriction applies to EVERY Registration Context of that
person; a context-level restriction applies to one Registration only. The
free-text reason is encrypted at rest and is never returned to a
checkpoint -- `active_restrictions_for` yields rows, and the entry
evaluator reads only severity, category, and overrideability from them.
Creation and revocation are audited without the reason text.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.policies import has_scoped_permission
from apps.audit import action_codes
from apps.entry.models import (
    RestrictionCategory,
    RestrictionSeverity,
    RestrictionStatus,
    SecurityRestriction,
)
from apps.entry.services import EntryPermissionError, EntryServiceError, EntryStateError, audit

RESTRICTION_REASON_MAX_LENGTH = 1000


class RestrictionConfigurationError(EntryServiceError):
    """Raised for an invalid restriction request."""


def _require_manager(actor, *, event_edition_id) -> None:
    if not has_scoped_permission(
        actor, "entry.manage_securityrestriction", event_edition_id=event_edition_id
    ):
        raise EntryPermissionError("You are not authorized to manage security restrictions.")


def create_restriction(
    *,
    actor,
    severity: str,
    category: str,
    reason: str,
    person=None,
    registration=None,
    event_edition=None,
    is_overrideable: bool = False,
    starts_at=None,
    ends_at=None,
) -> SecurityRestriction:
    if (person is None) == (registration is None):
        raise RestrictionConfigurationError("A restriction targets exactly one person or context.")
    if severity not in RestrictionSeverity.values or category not in RestrictionCategory.values:
        raise RestrictionConfigurationError("Invalid restriction severity or category.")
    reason = (reason or "").strip()[:RESTRICTION_REASON_MAX_LENGTH]
    if not reason:
        raise RestrictionConfigurationError("A restriction requires a reason.")
    if registration is not None:
        event_edition = event_edition or registration.event_edition
        if event_edition.pk != registration.event_edition_id:
            raise RestrictionConfigurationError("The context belongs to another event edition.")
    # The schema permits an all-editions restriction (NULL edition), but
    # this service always scopes one to an edition: an event-scoped manager
    # must never be able to create a restriction reaching beyond their own
    # edition. A person-level restriction still covers EVERY context of that
    # person inside the edition.
    if event_edition is None:
        raise RestrictionConfigurationError("A restriction requires an event edition.")
    _require_manager(actor, event_edition_id=event_edition.pk)
    now = timezone.now()
    starts_at = starts_at or now
    if ends_at is not None and ends_at <= starts_at:
        raise RestrictionConfigurationError("The restriction end must follow its start.")
    with transaction.atomic():
        restriction = SecurityRestriction.objects.create(
            person=person,
            registration=registration,
            event_edition=event_edition,
            severity=severity,
            category=category,
            is_overrideable=bool(is_overrideable),
            starts_at=starts_at,
            ends_at=ends_at,
            reason_encrypted=reason,
            created_by=actor,
        )
        audit(
            action_code=action_codes.SECURITY_RESTRICTION_CREATED,
            actor=actor,
            target_type="SecurityRestriction",
            target_uuid=restriction.pk,
            event_edition_id=event_edition.pk,
            after_summary={
                "target": "PERSON" if person is not None else "REGISTRATION",
                "severity": severity,
                "category": category,
                "overrideable": bool(is_overrideable),
            },
        )
    return restriction


def revoke_restriction(*, restriction: SecurityRestriction, actor, reason_code: str):
    if not reason_code:
        raise RestrictionConfigurationError("A revocation reason is required.")
    _require_manager(actor, event_edition_id=restriction.event_edition_id)
    now = timezone.now()
    with transaction.atomic():
        locked = SecurityRestriction.objects.select_for_update().get(pk=restriction.pk)
        if locked.status != RestrictionStatus.ACTIVE:
            raise EntryStateError("This restriction is no longer active.")
        locked.status = RestrictionStatus.REVOKED
        locked.revoked_at = now
        locked.revoked_by = actor
        locked.revocation_reason_code = reason_code[:32]
        locked.save()
        audit(
            action_code=action_codes.SECURITY_RESTRICTION_REVOKED,
            actor=actor,
            target_type="SecurityRestriction",
            target_uuid=locked.pk,
            event_edition_id=locked.event_edition_id,
            reason_code=reason_code,
        )
    return locked


def active_restrictions_for(registration, *, now=None):
    """Every restriction currently in force for this exact context.

    Includes the person-level restrictions of the context's person, so a
    person-level restriction overrides every context (BR-ENT-003).
    """
    now = now or timezone.now()
    target = Q(registration_id=registration.pk)
    if registration.person_id is not None:
        target |= Q(person_id=registration.person_id)
    return (
        SecurityRestriction.objects.filter(target, status=RestrictionStatus.ACTIVE)
        .filter(Q(event_edition__isnull=True) | Q(event_edition_id=registration.event_edition_id))
        .filter(starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
    )
