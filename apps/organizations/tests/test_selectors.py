"""Bounded organization search tests (Phase 2 Prompt 2, FR-ORG-004)."""

from __future__ import annotations

import pytest

from apps.organizations.models import (
    Organization,
    OrganizationAlias,
    OrganizationStatus,
    OrganizationType,
)
from apps.organizations.selectors import search_organizations
from apps.organizations.services import (
    create_organization,
    find_possible_duplicate_organizations,
)

pytestmark = pytest.mark.django_db


def test_blank_query_returns_no_results() -> None:
    Organization.objects.create(
        official_name="Ministry of Example",
        normalized_name="ministry of example",
        organization_type=OrganizationType.MINISTRY,
    )
    assert search_organizations("").count() == 0
    assert search_organizations("   ").count() == 0


def test_search_matches_official_name() -> None:
    org = Organization.objects.create(
        official_name="Acme Corporation",
        normalized_name="acme corporation",
        organization_type=OrganizationType.COMPANY,
    )
    results = list(search_organizations("Acme"))
    assert org in results


def test_search_matches_an_alias_by_prefix() -> None:
    # Prefix-anchored (Phase 2 Prompt 2 V2 correction pass "remove the
    # remaining leading-wildcard alias search") -- a query matching the
    # START of the alias matches; a mid-string substring, which would
    # require an unindexed leading-wildcard scan, deliberately does not.
    org = Organization.objects.create(
        official_name="Official Name Only",
        normalized_name="official name only",
        organization_type=OrganizationType.OTHER,
    )
    OrganizationAlias.objects.create(organization=org, alias_name="Common Nickname")
    results = list(search_organizations("Common"))
    assert org in results


def test_search_does_not_match_an_alias_by_mid_string_substring() -> None:
    org = Organization.objects.create(
        official_name="Official Name Only Two",
        normalized_name="official name only two",
        organization_type=OrganizationType.OTHER,
    )
    OrganizationAlias.objects.create(organization=org, alias_name="Common Nickname")
    results = list(search_organizations("Nickname"))
    assert org not in results


def test_search_excludes_merged_organizations() -> None:
    org = Organization.objects.create(
        official_name="Merged Away Inc",
        normalized_name="merged away inc",
        organization_type=OrganizationType.COMPANY,
        status=OrganizationStatus.MERGED,
    )
    results = list(search_organizations("Merged Away"))
    assert org not in results


def test_search_results_are_bounded() -> None:
    for index in range(30):
        Organization.objects.create(
            official_name=f"Bounded Test Org {index}",
            normalized_name=f"bounded test org {index}",
            organization_type=OrganizationType.OTHER,
        )
    results = list(search_organizations("Bounded Test Org", limit=20))
    assert len(results) == 20


def test_find_possible_duplicate_organizations_never_merges() -> None:
    first = create_organization(
        official_name="Duplicate Name Co", organization_type=OrganizationType.COMPANY
    )
    second = create_organization(
        official_name="Duplicate Name Co", organization_type=OrganizationType.COMPANY
    )
    duplicates = find_possible_duplicate_organizations("Duplicate Name Co", exclude_pk=first.pk)
    assert [d.pk for d in duplicates] == [second.pk]
    # Both records still exist independently -- no automatic merge.
    assert Organization.objects.filter(pk__in=[first.pk, second.pk]).count() == 2
