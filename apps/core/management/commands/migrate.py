"""`manage.py migrate`, refused under a deployed RUNTIME settings module.

Django's own command, with one guard: when the settings declare
`DATABASE_PROCESS_ROLE = "runtime"` (`config.settings.staging` and
`config.settings.production`), applying or reversing migrations is refused.
Migrations run with `config.settings.staging_migration` or
`config.settings.production_migration`, which connect with the
migration-owner identity. The read-only forms (`--plan`, `--check`) remain
available everywhere. Local and test settings declare no role and are not
affected.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.commands.migrate import Command as DjangoMigrateCommand


class Command(DjangoMigrateCommand):
    def handle(self, *args, **options):
        if (
            getattr(settings, "DATABASE_PROCESS_ROLE", None) == "runtime"
            and not options.get("plan")
            and not options.get("check_unapplied")
        ):
            raise CommandError(
                "Migrations are not applied with a runtime settings module. Run "
                "`manage.py migrate` with config.settings.staging_migration or "
                "config.settings.production_migration (the migration-owner identity)."
            )
        return super().handle(*args, **options)
