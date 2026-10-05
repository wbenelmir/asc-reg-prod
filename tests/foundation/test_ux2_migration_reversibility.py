"""UX-2 migrations reverse and re-apply cleanly on real PostgreSQL."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def _columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {col.name for col in connection.introspection.get_table_description(cursor, table)}


@pytest.mark.parametrize(
    ("app_label", "previous", "table", "column"),
    [
        ("events", "0005", "events_event_edition", "minimum_participant_age"),
        ("organizations", "0003", "organizations_professional_affiliation", "operating_scope"),
        ("registrations", "0008", "registrations_interest_topic", "group_code"),
    ],
)
def test_schema_migrations_reverse_and_reapply(app_label, previous, table, column) -> None:
    try:
        call_command("migrate", app_label, previous, verbosity=0)
        assert column not in _columns(table)
    finally:
        call_command("migrate", verbosity=0)
    assert column in _columns(table)


def test_data_migrations_reverse_and_reapply() -> None:
    from apps.core.models import Sector

    # A transactional test database may have been flushed: seed the row first.
    Sector.objects.get_or_create(code="AGRITECH", defaults={"name": "Agriculture and agritech"})
    try:
        call_command("migrate", "core", "0002", verbosity=0)
        # UX-C2 (UX-F04, R1): the reverse keeps every sector row; nothing is deleted.
        assert Sector.objects.filter(code="AGRITECH").exists()
        call_command("migrate", "communications", "0005", verbosity=0)
    finally:
        call_command("migrate", verbosity=0)
    assert Sector.objects.filter(code="AGRITECH").exists()
