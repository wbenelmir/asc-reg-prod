# ADR-0028: Approved country catalog: frozen data, reconciling migration, operator command

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-10-04 |
| Related | Decision C-01 (UX remarks decision gate §17.12); ADR-0027 (staging provisioning); [operator guide](../operations/country_catalog.md) |

## Context

Every country list (nationality, residence, passport issuing country,
organization headquarters, phone calling code) reads the shared `core.Country`
table through `selectable_countries()`. Until now the table held only what a
seed or the staging provisioning created (Algeria and France), because the
catalog decision C-01 was open. On 2026-10-04 the owner approved a catalog of
248 entries, supplied as an SQL script that inserts missing rows and ignores
existing ones. Deployed databases already hold country rows that registrations
reference through protected foreign keys.

## Decisions

1. **Frozen data in the repository.** The approved rows are copied unchanged
   into `apps/core/reference_data/countries_v1.py`, with the source file's
   SHA-256 and a hash of the rows, both pinned by a test. The module is never
   edited; a later catalog is a new module and a new migration.
2. **One reconciliation, two entry points.**
   `apps.core.services.country_catalog.reconcile_countries(model, catalog,
   apply=...)` creates missing approved entries, sets the approved names,
   reactivates approved entries, and makes `IL`, `XK` and every other code
   outside the catalog inactive. It never deletes a row and never changes a
   code. Migration `core.0005_approved_country_catalog` runs it once (fresh and
   existing databases); `manage.py reconcile_country_catalog` previews it (exit
   1 on a difference) or applies it again (`--apply`) on an existing
   deployment.
3. **History is preserved by deactivation.** An inactive country leaves only
   the lists for new choices; every existing reference stays valid and is
   displayed. The supplied SQL is not used, because it would neither correct
   existing names nor reactivate approved entries.
4. **Nothing else creates countries.** Staging provisioning only checks that
   the countries its scenarios use are selectable; the local seed command uses
   `get_or_create` and finds the catalog rows.
5. **No policy is inferred.** The catalog decides selectability only; the
   identity route still follows the nationality. The calling-code list leaves
   out the seven territories that have no telephone calling code, a technical
   constraint of phone validation.

## Consequences

* Fresh and existing staging databases converge to the same 248 selectable
  entries with English, French and Arabic names.
* The reverse of `core.0005` keeps every row (no record of earlier names or
  flags exists); recovery is a backup restore or a reviewed roll-forward.
* Tests that assumed an empty country table now install or narrow the catalog
  explicitly inside their transaction.
