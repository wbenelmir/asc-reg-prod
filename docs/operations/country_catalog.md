# Country and territory catalog: install and reconcile

Audience: operators and reviewers. The selectable countries for nationality,
country of residence, passport issuing country, organization headquarters and
the phone calling code all come from one shared reference table,
`core_country` (`apps.core.models.Country`).

## 1. The approved catalog

| Item | Value |
| --- | --- |
| Version | `countries-v1`, owner-approved 2026-10-04 (decision C-01) |
| Source | Owner-supplied file `ASC2026_STAGING_COUNTRIES (1).sql`, SHA-256 `3c82fc8c4e5061fa8441f465738ceb2b4b1c8e9b1274f00f5fbb9cc1ef2728ca` |
| Codes and names, as stated by the source | ISO 3166-1 alpha-2 codes from the pycountry 24.6.1 snapshot; English, French and Arabic names from Babel 2.17.0 / Unicode CLDR |
| Entries | 248 countries and territories (territories are included on purpose) |
| Palestine | `PS`, labels "Palestine", "Palestine", "فلسطين" |
| Excluded | `IL` and `XK`: never selectable; a row that exists is kept, inactive |
| Repository data | `apps/core/reference_data/countries_v1.py` (frozen, never edited; a later catalog is a new module and a new migration) |

The rows were copied from the supplied file unchanged; the module records the
file's hash and a hash of the rows, and a test pins both. The SQL script itself
is **not** used and must not be run: it only inserted missing rows
(`ON CONFLICT DO NOTHING`), so it never corrected existing names or reactivated
an approved entry.

The catalog decides which countries a participant can choose. It decides
nothing about nationality, residence or identity documents: the identity route
still follows the nationality only (Algerian nationals on the NIN route,
including those living abroad; every other nationality on the passport route).

## 2. The rules (migration and command alike)

`apps.core.services.country_catalog.reconcile_countries` applies them:

| Situation in `core_country` | Result |
| --- | --- |
| Approved code missing | Created, active, with the approved English, French and Arabic names |
| Approved code present, other names | Names replaced by the approved names |
| Approved code present, inactive | Made active |
| `IL` or `XK` present and active | Made inactive |
| Any other code outside the catalog, active | Made inactive and reported |
| Any row referenced by a registration, an identity record, an organization or a contact | Never deleted; the reference is never changed |

An inactive country only leaves the lists offered for new choices
(`selectable_countries()`); existing registrations keep it and still display
its name. Nothing is deleted and no code ever changes.

The phone calling-code list offers the catalog minus the seven territories that
have no telephone calling code (`AQ`, `BV`, `GS`, `HM`, `PN`, `TF`, `UM`): no
number can be validated for them. A pasted international number still decides
its own region.

## 3. Install on a fresh database

Nothing to do beyond the normal migration (deployment guide §7):

```bash
python manage.py migrate --plan
python manage.py migrate
```

Migration `core.0005_approved_country_catalog` creates the 248 entries.

## 4. Existing deployment (staging today)

The same migration reconciles an existing table: it creates the missing
entries, renames and reactivates approved ones and makes `IL`, `XK` and any
unlisted code inactive. Before migrating, keep the pre-migration backup the
deployment guide asks for. After migrating, confirm with the runtime settings:

```bash
python manage.py reconcile_country_catalog            # preview: exit 0 and "In sync"
```

Run the same command later whenever a manual edit, an import or a restored
backup may have changed `core_country`:

```bash
python manage.py reconcile_country_catalog            # preview, writes nothing; exit 1 if it differs
python manage.py reconcile_country_catalog --apply    # converges in one transaction; repeatable
```

The output lists codes only: what would be (or was) created, renamed,
reactivated or made inactive, and, for every code outside the catalog, how many
rows still refer to it (kept for history).

## 5. Provisioning and later changes

* `provision_staging_uat` creates no country. It checks that Algeria and France
  are selectable and stops with a conflict otherwise; it can never replace the
  catalog with a synthetic subset.
* `seed_phase1_local_data` (local only) uses `get_or_create`: on a migrated
  database it finds the catalog rows and creates nothing.
* A later catalog is a new frozen module and a new reviewed migration. Never
  edit `countries_v1.py` or this migration.

## 6. Rollback

The reverse of `core.0005` keeps every row (an intentionally partial reverse,
as for the sector migration `core.0003`): it cannot know earlier names or
active flags, and countries are referenced by protected foreign keys. To undo
the catalog, restore the pre-migration backup or roll forward with a reviewed
migration.

## 7. Still open

The catalog does not settle the assistance path for a stateless participant or
one whose country is not listed (part of C-01 in the decision gate); no such
path exists in the application.
