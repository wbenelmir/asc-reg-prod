"""Install the owner-approved country catalog, version 1 (decision C-01, 2026-10-04).

On a fresh database this creates the 248 approved countries and territories
with their English, French and Arabic names. On an existing database it
reconciles `core_country` with the catalog: missing approved codes are
created, approved codes get the approved names and become active, IL and XK
and any other code outside the catalog become inactive (no longer offered for
new selections). Nothing is deleted and no code changes, so every existing
registration, identity record and organization keeps its country reference.
See `apps.core.services.country_catalog` for the exact rules; the frozen data
is `apps.core.reference_data.countries_v1`.

`manage.py reconcile_country_catalog` repeats the same reconciliation later
(preview by default) on an existing deployment.
"""

from __future__ import annotations

from django.db import migrations


def forward(apps, schema_editor):
    from apps.core.reference_data import countries_v1
    from apps.core.services.country_catalog import reconcile_countries

    reconcile_countries(apps.get_model("core", "Country"), countries_v1, apply=True)


def backward(apps, schema_editor):
    """A guarded fail-safe reverse that keeps every row (the `core.0003`
    convention). Countries are referenced by protected foreign keys and the
    forward keeps no record of a row's earlier names or active flag, so the
    reverse changes nothing. This is an intentionally partial reversal; a
    later catalog is a new, reviewed roll-forward migration."""


class Migration(migrations.Migration):
    dependencies = [("core", "0004_staging_provisioning_ownership")]

    operations = [migrations.RunPython(forward, backward)]
