"""`manage.py release_readiness [--json]` (P4-4-C1, review finding R-03).

Prints the sanitized release-readiness assessment. Exit code 0 means READY,
1 BLOCKED and 2 NOT_ASSESSED, so a script cannot mistake an unassessed or
blocked release for a ready one. Read-only; see `apps.core.release_readiness`.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.release_readiness import BLOCKED, NOT_ASSESSED, READY, assess_release_readiness

EXIT_CODES = {READY: 0, BLOCKED: 1, NOT_ASSESSED: 2}


class Command(BaseCommand):
    help = "Report release readiness as READY, BLOCKED or NOT_ASSESSED (read-only)."
    # The assessment reports schema problems itself; the system checks must not
    # stop it first.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        report = assess_release_readiness()
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Release readiness: {report.overall}")
            for item in report.items:
                self.stdout.write(f"  [{item.status}] {item.key} ({item.reference})")
                for reason in item.reasons:
                    self.stdout.write(f"      - {reason}")
            self.stdout.write("  Facts:")
            for name, value in report.facts.items():
                self.stdout.write(f"      - {name}: {value}")
            self.stdout.write("  Not covered by this assessment:")
            for entry in report.not_covered:
                self.stdout.write(f"      - {entry}")
            self.stdout.write(report.as_dict()["note"])
        code = EXIT_CODES[report.overall]
        if code:
            raise CommandError(f"release readiness is {report.overall}", returncode=code)
