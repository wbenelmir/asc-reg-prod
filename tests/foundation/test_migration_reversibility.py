"""Migration reversibility tests (project rule: "Test migration reversibility ... where
safely practical" / "Never reverse or reset the developer's asc2026_dev database").

`call_command("migrate", ...)` here runs against pytest-django's own
managed test database (Django's test runner repoints `DATABASES["default"]`
at the test-prefixed database for the whole session before any test body
executes) -- this NEVER touches the developer's real `asc2026_dev`.
Terminal (leaf) migrations are chosen specifically so reversing them does
not require also reversing anything else in the graph.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def _table_exists(table_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{table_name}"])
        (exists,) = cursor.fetchone()
    return exists


@pytest.mark.parametrize(
    "app_label,table_name",
    [
        ("communications", "communications_message"),
        ("documents", "documents_document"),
        ("privacy", "privacy_acceptance_record"),
    ],
)
def test_leaf_migration_reverses_and_reapplies_cleanly(app_label: str, table_name: str) -> None:
    assert _table_exists(table_name)

    try:
        call_command("migrate", app_label, "zero", verbosity=0)
        assert not _table_exists(table_name)

        call_command("migrate", app_label, verbosity=0)
        assert _table_exists(table_name)
    finally:
        # IDV-1: `documents` is no longer a leaf (`people.0003` depends on
        # `documents.0002`), so reversing it also unapplies that dependent;
        # restore the complete graph for the tests that follow.
        call_command("migrate", verbosity=0)


def test_staging_provisioning_ownership_migration_reverses_and_reapplies_cleanly() -> None:
    tables = ("core_staging_provisioning_set", "core_staging_provisioned_object")
    assert all(_table_exists(table) for table in tables)
    try:
        call_command("migrate", "core", "0003", verbosity=0)
        assert not any(_table_exists(table) for table in tables)
        call_command("migrate", "core", verbosity=0)
        assert all(_table_exists(table) for table in tables)
    finally:
        call_command("migrate", verbosity=0)


def test_country_catalog_migration_reconciles_an_existing_database_and_reverses_safely() -> None:
    """`core.0005` on a database that already has country rows: missing approved
    entries are created, a stale label and an inactive approved entry are
    corrected, an excluded code is made inactive, and nothing is deleted. Its
    reverse keeps every row (intentionally partial, the `core.0003` convention)."""
    from apps.core.models import Country, selectable_countries
    from apps.core.reference_data import countries_v1

    approved = {code: (name, fr, ar) for code, name, fr, ar in countries_v1.ENTRIES}
    try:
        call_command("migrate", "core", "0004", verbosity=0)
        Country.objects.filter(code__in=["PS", "AQ"]).delete()  # never referenced here
        Country.objects.update_or_create(
            code="TN", defaults={"name": "Tunisia (old)", "name_fr": "", "is_active": False}
        )
        Country.objects.update_or_create(code="IL", defaults={"name": "Israel", "is_active": True})
        rows_before_reverse = Country.objects.count()

        call_command("migrate", "core", verbosity=0)
        for code in ("PS", "AQ", "TN"):
            row = Country.objects.get(code=code)
            assert (row.name, row.name_fr, row.name_ar) == approved[code] and row.is_active
        assert not Country.objects.get(code="IL").is_active
        assert set(selectable_countries().values_list("code", flat=True)) == set(approved)

        call_command("migrate", "core", "0004", verbosity=0)
        assert Country.objects.count() >= rows_before_reverse  # the reverse deletes nothing
        assert Country.objects.get(code="PS").name_ar == "فلسطين"
    finally:
        call_command("migrate", verbosity=0)


def test_official_notices_v3_migration_publishes_retires_reverses_and_guards() -> None:
    """`privacy.0005`: v3 is published and v2 retired (never rewritten); the
    guarded reverse restores v2 while v3 is unreferenced, and refuses, changing
    nothing, once a consent record references v3."""
    from django.utils import timezone

    from apps.people.services import resolve_or_create_participant_for_email
    from apps.privacy.models import ConsentPurpose, ConsentRecord, LegalDocumentVersion
    from apps.privacy.selectors import effective_published_version

    def labels():
        return {
            (code, language): getattr(
                effective_published_version(code, language), "version_label", None
            )
            for code in ("PRIVACY_NOTICE", "TERMS")
            for language in ("en", "fr", "ar")
        }

    try:
        call_command("migrate", "privacy", "0003", verbosity=0)
        call_command("migrate", "privacy", "0004", verbosity=0)
        assert set(labels().values()) == {"v2-draft"}
        v2_hashes = dict(
            LegalDocumentVersion.objects.filter(version_label="v2-draft").values_list(
                "pk", "content_hash"
            )
        )

        call_command("migrate", "privacy", verbosity=0)
        assert set(labels().values()) == {"v3"}
        for version in LegalDocumentVersion.objects.filter(version_label="v2-draft"):
            assert version.status == "RETIRED" and version.content_hash == v2_hashes[version.pk]

        call_command("migrate", "privacy", "0004", verbosity=0)
        assert set(labels().values()) == {"v2-draft"}
        assert not LegalDocumentVersion.objects.filter(version_label="v3").exists()

        call_command("migrate", "privacy", verbosity=0)
        person = resolve_or_create_participant_for_email("v3-migration@example.test")
        ConsentRecord.objects.create(
            person=person,
            purpose=ConsentPurpose.objects.get(code="PERSONAL_DATA_PROCESSING"),
            action="GRANTED",
            source="PUBLIC_WEB",
            occurred_at=timezone.now(),
            legal_document_version=effective_published_version("PRIVACY_NOTICE", "en"),
        )
        with pytest.raises(Exception, match="v3 is referenced"):
            call_command("migrate", "privacy", "0004", verbosity=0)
        assert set(labels().values()) == {"v3"}  # the refused reverse changed nothing
    finally:
        call_command("migrate", verbosity=0)


def test_audit_trigger_reverse_sql_actually_drops_the_trigger_and_forward_recreates_it() -> None:
    assert _table_exists("audit_event")

    call_command("migrate", "audit", "zero", verbosity=0)
    assert not _table_exists("audit_event")
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regprocedure('audit_event_block_update_delete()') IS NOT NULL")
        (function_exists,) = cursor.fetchone()
    assert not function_exists

    call_command("migrate", "audit", verbosity=0)
    assert _table_exists("audit_event")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgenabled FROM pg_trigger WHERE tgrelid = 'audit_event'::regclass "
            "AND tgname = 'audit_event_prevent_update_delete' AND NOT tgisinternal"
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] != "D"
