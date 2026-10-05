"""IDV migrations reverse and reapply cleanly (ADR-0026 §Consequences).

Runs against pytest-django's own test database, never `asc2026_dev`.
`people.0003_identity_verification` is a leaf: reversing it drops the four
identity tables and the added attempt columns, and reapplying restores them.
`documents.0002` only changes choices.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

IDV_TABLES = (
    "people_identity_verification",
    "people_identity_revision",
    "people_identity_verification_job",
    "people_identity_decision",
)


def _table_exists(table_name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{table_name}"])
        (exists,) = cursor.fetchone()
    return exists


def _attempt_columns() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'people_identity_verification_attempt'"
        )
        return {row[0] for row in cursor.fetchall()}


def test_the_identity_migration_reverses_and_reapplies_cleanly() -> None:
    # Another reversibility test may have left a dependent migration unapplied
    # (migrating an app to zero also unapplies its dependents); start complete.
    call_command("migrate", verbosity=0)
    assert all(_table_exists(table) for table in IDV_TABLES)
    assert {"revision_id", "reason_code", "official_family_name_latin"} <= _attempt_columns()

    call_command("migrate", "people", "0002", verbosity=0)
    assert not any(_table_exists(table) for table in IDV_TABLES)
    assert "revision_id" not in _attempt_columns()
    assert _table_exists("people_identity_verification_attempt")  # the original table stays

    call_command("migrate", "documents", "0001", verbosity=0)
    call_command("migrate", "documents", verbosity=0)
    call_command("migrate", "people", verbosity=0)
    assert all(_table_exists(table) for table in IDV_TABLES)
    assert {"revision_id", "reason_code", "official_family_name_latin"} <= _attempt_columns()
