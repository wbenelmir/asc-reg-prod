"""Install and reconcile the owner-approved country catalog (decision C-01).

Used by migration `core.0005_approved_country_catalog` (fresh and existing
databases) and by `manage.py reconcile_country_catalog` (a repeatable check or
repair on an existing deployment). Both take the frozen catalog module
(`apps.core.reference_data.countries_v1`) explicitly.

Rules, for every row of `core_country`:

* an approved code that is missing is created, active, with the approved
  English, French and Arabic names;
* an approved code that exists gets the approved names when they differ, and
  is made active when it was inactive (the catalog marks every entry active);
* an excluded code (IL, XK) that exists is made inactive;
* any other code that exists and is outside the catalog is made inactive and
  reported;
* no row is ever deleted and no code is ever changed. Foreign keys to a
  country (nationality, residence, passport issuing country, organization and
  affiliation country, contact region) keep pointing at the same row, so no
  registration or other record is rewritten. An inactive country only leaves
  the lists offered for NEW selections (`apps.core.models.selectable_countries`).

The catalog decides which countries can be selected; it decides nothing about
nationality, residence or identity-document policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count


@dataclass
class CatalogReconciliation:
    """What a reconciliation found and (when applied) changed. Codes only."""

    catalog_version: str
    approved: int
    created: list[str] = field(default_factory=list)
    relabeled: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    excluded_deactivated: list[str] = field(default_factory=list)
    unlisted_deactivated: list[str] = field(default_factory=list)
    #: Codes outside the catalog (excluded or unlisted) that exist, active or not.
    outside_catalog: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def changes(self) -> int:
        return (
            len(self.created)
            + len(self.relabeled)
            + len(self.reactivated)
            + len(self.excluded_deactivated)
            + len(self.unlisted_deactivated)
        )

    @property
    def in_sync(self) -> bool:
        return self.changes == 0


def reconcile_countries(country_model, catalog, *, apply: bool) -> CatalogReconciliation:
    """Compare `core_country` with `catalog` and, when `apply`, converge it.

    `country_model` is the `Country` model, or the historical one inside a
    migration. Inside a transaction the rows are locked first, so two
    concurrent runs serialize and the second finds nothing left to change.
    The caller decides the transaction (a migration runs in its own).
    """
    approved = {code: (name, name_fr, name_ar) for code, name, name_fr, name_ar in catalog.ENTRIES}
    excluded = set(catalog.EXCLUDED_CODES)
    report = CatalogReconciliation(
        catalog_version=catalog.CATALOG_VERSION, approved=len(approved), applied=apply
    )
    rows = country_model.objects.order_by("code")
    if apply:
        rows = rows.select_for_update()
    existing = {row.code: row for row in rows}

    for code in sorted(approved):
        name, name_fr, name_ar = approved[code]
        row = existing.get(code)
        if row is None:
            report.created.append(code)
            if apply:
                country_model.objects.create(
                    code=code, name=name, name_fr=name_fr, name_ar=name_ar, is_active=True
                )
            continue
        fields = []
        if (row.name, row.name_fr, row.name_ar) != (name, name_fr, name_ar):
            report.relabeled.append(code)
            row.name, row.name_fr, row.name_ar = name, name_fr, name_ar
            fields += ["name", "name_fr", "name_ar"]
        if not row.is_active:
            report.reactivated.append(code)
            row.is_active = True
            fields.append("is_active")
        if apply and fields:
            row.save(update_fields=fields)

    for code, row in existing.items():
        if code in approved:
            continue
        report.outside_catalog.append(code)
        if not row.is_active:
            continue
        if code in excluded:
            report.excluded_deactivated.append(code)
        else:
            report.unlisted_deactivated.append(code)
        if apply:
            row.is_active = False
            row.save(update_fields=["is_active"])
    return report


def referencing_row_counts(country_model, codes) -> dict[str, int]:
    """How many rows refer to each code, across every foreign key to Country.

    Report-only, so an operator sees that the history of an inactive country is
    kept. Reads counts, never values.
    """
    counts = {code: 0 for code in codes}
    if not counts:
        return counts
    # Forward foreign keys of every model: most of them hide their reverse
    # accessor (`related_name="+"`), so `_meta.related_objects` misses them.
    for model in country_model._meta.apps.get_models():
        for fk in model._meta.concrete_fields:
            if not fk.many_to_one or fk.related_model is not country_model:
                continue
            for row in (
                model._base_manager.filter(**{f"{fk.attname}__in": list(counts)})
                .values(fk.attname)
                .order_by()
                .annotate(n=Count("pk"))
            ):
                counts[row[fk.attname]] += row["n"]
    return counts
