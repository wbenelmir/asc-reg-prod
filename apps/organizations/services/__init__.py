"""Organization matching for submitted professional affiliation (Schema §5.6),
plus Phase 2 Prompt 2's organization-registry create/duplicate-warning
services (FR-ORG-004/FR-ORG-005)."""

from __future__ import annotations

import re

from apps.organizations.models import Organization, OrganizationType


def normalize_organization_name(raw_name: str) -> str:
    collapsed = re.sub(r"\s+", " ", raw_name.strip()).lower()
    return collapsed


def match_or_create_organization(
    raw_name: str, *, country_code_id: str | None = None
) -> Organization:
    """Match an existing Organization by normalized name, or create a minimal one.

    The submitted snapshot text on `ProfessionalAffiliation.
    submitted_organization_name` is preserved separately by the caller and
    is never rewritten by a later organization rename or merge.
    """
    normalized = normalize_organization_name(raw_name)
    existing = Organization.objects.filter(normalized_name=normalized).first()
    if existing is not None:
        return existing
    return Organization.objects.create(
        official_name=raw_name.strip(),
        normalized_name=normalized,
        organization_type=OrganizationType.OTHER,
        country_code_id=country_code_id,
    )


def find_possible_duplicate_organizations(
    official_name: str, *, exclude_pk=None
) -> list[Organization]:
    """Return organizations whose normalized name matches `official_name` (FR-ORG-005).

    Identifies POSSIBLE duplicates for later authorized human handling
    only -- never merges, modifies, or collapses any record (Phase 2
    Prompt 2 "do not implement a destructive organization merge workflow
    in this prompt").
    """
    normalized = normalize_organization_name(official_name)
    if not normalized:
        return []
    queryset = Organization.objects.filter(normalized_name=normalized)
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return list(queryset)


def create_organization(
    *,
    official_name: str,
    organization_type: str,
    country_code_id: str | None = None,
) -> Organization:
    """Create a new canonical organization record (FR-ORG-002).

    Reuses the same normalization `match_or_create_organization` already
    applies, so a name authored here and a name later submitted through the
    professional-affiliation free-text path normalize identically and can
    be matched by the same `normalized_name` lookup. Does NOT auto-merge a
    possible duplicate -- callers are expected to have already surfaced
    `find_possible_duplicate_organizations` results to the authorized user
    (FR-ORG-004/FR-ORG-005).
    """
    normalized = normalize_organization_name(official_name)
    return Organization.objects.create(
        official_name=official_name.strip(),
        normalized_name=normalized,
        organization_type=organization_type,
        country_code_id=country_code_id,
    )


# ---------------------------------------------------------------------------
# UPD-06: authorized organization management (create, edit, activate,
# deactivate), audited. Never a merge and never a delete: registrations,
# campaigns and batches keep pointing at the same organization row.
# ---------------------------------------------------------------------------


class OrganizationStatusError(Exception):
    """The requested status change is not allowed (a merged organization)."""


def _audit_organization(actor, action_code: str, organization, *, before=None, after=None):
    from apps.audit.contracts import AuditRecord
    from apps.audit.services import PersistentAuditRecorder

    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type="Organization",
            target_uuid=organization.pk,
            result="SUCCESS",
            before_summary=before,
            after_summary=after,
        )
    )


def create_organization_audited(
    *, actor, official_name: str, organization_type: str, country_code_id: str | None = None
) -> Organization:
    """`create_organization`, plus the audit event of who created it."""
    from django.db import transaction

    from apps.audit import action_codes

    with transaction.atomic():
        organization = create_organization(
            official_name=official_name,
            organization_type=organization_type,
            country_code_id=country_code_id,
        )
        _audit_organization(
            actor,
            action_codes.ORGANIZATION_CREATED,
            organization,
            after={"organization_type": organization_type, "country": country_code_id or ""},
        )
    return organization


def update_organization(
    organization: Organization,
    *,
    actor,
    official_name: str,
    organization_type: str,
    country_code_id: str | None = None,
) -> Organization:
    """Edit the master record. The submitted snapshots of registrations
    (`ProfessionalAffiliation.submitted_organization_name`) never change."""
    from django.db import transaction

    from apps.audit import action_codes

    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=organization.pk)
        changed = []
        name = official_name.strip()
        if locked.official_name != name:
            locked.official_name = name
            locked.normalized_name = normalize_organization_name(name)
            changed.append("official_name")
        if locked.organization_type != organization_type:
            locked.organization_type = organization_type
            changed.append("organization_type")
        if (locked.country_code_id or None) != (country_code_id or None):
            locked.country_code_id = country_code_id or None
            changed.append("country_code")
        if changed:
            locked.save()
            _audit_organization(
                actor, action_codes.ORGANIZATION_UPDATED, locked, after={"changed": changed}
            )
    return locked


def set_organization_status(organization: Organization, *, actor, active: bool) -> Organization:
    """Activate or deactivate. A deactivated organization cannot be used for a
    new campaign, delegation list or on-behalf registration; its existing
    campaigns, links and registrations are untouched (close a campaign
    explicitly to stop its link). A merged organization is never changed."""
    from django.db import transaction

    from apps.audit import action_codes
    from apps.organizations.models import OrganizationStatus

    target = OrganizationStatus.ACTIVE if active else OrganizationStatus.INACTIVE
    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=organization.pk)
        if locked.status == OrganizationStatus.MERGED:
            raise OrganizationStatusError("A merged organization cannot change status.")
        if locked.status != target:
            before = locked.status
            locked.status = target
            locked.save(update_fields=["status", "updated_at"])
            _audit_organization(
                actor,
                action_codes.ORGANIZATION_STATUS_CHANGED,
                locked,
                before={"status": before},
                after={"status": target},
            )
    return locked
