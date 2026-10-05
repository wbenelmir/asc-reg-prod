"""UX-4 migrations reverse and re-apply cleanly on real PostgreSQL.

`events.0005_registration_channels` adds one column with a behaviour-preserving
default and one CHECK constraint; `core.0002_human_challenge_use` adds the
replay-store table. Both are rolled back to their predecessor and re-applied
here, against pytest-django's isolated test database.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def _columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {col.name for col in connection.introspection.get_table_description(cursor, table)}


def _constraints(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.get_constraints(cursor, table))


def _tables() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_events_0005_reverses_and_reapplies() -> None:
    try:
        call_command("migrate", "events", "0004", verbosity=0)
        assert "public_registration_mode" not in _columns("events_event_edition")
        assert "events_edition_registration_window_order" not in _constraints(
            "events_event_edition"
        )
    finally:
        call_command("migrate", "events", verbosity=0)
    assert "public_registration_mode" in _columns("events_event_edition")
    assert "events_edition_registration_window_order" in _constraints("events_event_edition")


def test_core_0002_reverses_and_reapplies() -> None:
    try:
        call_command("migrate", "core", "0001", verbosity=0)
        assert "core_human_challenge_use" not in _tables()
    finally:
        call_command("migrate", "core", verbosity=0)
    assert "core_human_challenge_use" in _tables()
