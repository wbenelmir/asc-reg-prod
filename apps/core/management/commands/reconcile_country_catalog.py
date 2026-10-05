"""`manage.py reconcile_country_catalog`: check or repair the approved country catalog.

    python manage.py reconcile_country_catalog            # preview (writes nothing)
    python manage.py reconcile_country_catalog --apply    # converge, in one transaction

The catalog is installed by migration `core.0005_approved_country_catalog`.
Run this afterwards on an existing deployment to confirm that the countries
still match the approved catalog (for example after a manual edit or an
external script), and with `--apply` to restore it. It follows the same rules
as the migration (`apps.core.services.country_catalog`): approved entries are
created, renamed to the approved names and reactivated; IL, XK and every
other code outside the catalog are made inactive; nothing is deleted.
Repeatable: a second run finds nothing to change.

Exit code 0 when the database matches the catalog (or was converged with
`--apply`); 1 when a preview found differences.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Preview or apply the reconciliation of core_country with the approved catalog."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write (default: preview).")

    def handle(self, *args, **options):
        from apps.core.models import Country
        from apps.core.reference_data import countries_v1
        from apps.core.services.country_catalog import (
            reconcile_countries,
            referencing_row_counts,
        )

        apply = options["apply"]
        with transaction.atomic():
            report = reconcile_countries(Country, countries_v1, apply=apply)
        mode = "APPLY" if apply else "PREVIEW"
        self.stdout.write(
            f"Country catalog {report.catalog_version} ({countries_v1.APPROVED_ON}, "
            f"{report.approved} approved entries) -- {mode}"
        )
        verb = "" if apply else "would be "
        for label, codes in (
            ("created", report.created),
            ("renamed to the approved names", report.relabeled),
            ("reactivated", report.reactivated),
            ("made inactive (excluded: IL, XK)", report.excluded_deactivated),
            ("made inactive (outside the catalog)", report.unlisted_deactivated),
        ):
            listed = f": {_codes(codes)}" if codes else ""
            self.stdout.write(f"  {len(codes):3} {verb}{label}{listed}")
        if report.outside_catalog:
            counts = referencing_row_counts(Country, report.outside_catalog)
            self.stdout.write(
                "  Outside the catalog, kept inactive for history (code: referencing rows): "
                + ", ".join(f"{code}: {counts[code]}" for code in report.outside_catalog)
            )
        if report.in_sync:
            self.stdout.write("In sync: the countries match the approved catalog.")
            return
        if apply:
            self.stdout.write(f"Applied {report.changes} change(s). Nothing was deleted.")
            return
        raise CommandError(
            f"{report.changes} difference(s) found; nothing was written. Add --apply to converge.",
            returncode=1,
        )


def _codes(codes: list[str], limit: int = 40) -> str:
    shown = ", ".join(codes[:limit])
    return shown + (f" (+{len(codes) - limit} more)" if len(codes) > limit else "")
