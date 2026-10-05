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
