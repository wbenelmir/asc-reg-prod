"""`manage.py run_identity_verification_jobs [--limit N]` (IDV-2, ADR-0026).

Processes the due identity verification jobs inline, without the broker: the
local development path (where after-commit dispatch is off) and the recovery
path when the broker is down. Each job is claimed with a lease, so running this
next to a worker never processes a job twice. Prints counts only, never an
identity value.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.people.services.identity_verification import run_due_identity_jobs


class Command(BaseCommand):
    help = "Process due identity verification jobs inline (local development and recovery)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="At most this many jobs.")

    def handle(self, *args, **options):
        counts = run_due_identity_jobs(limit=options["limit"])
        total = sum(counts.values())
        self.stdout.write(f"Processed {total} identity verification job(s).")
        for label, count in sorted(counts.items()):
            self.stdout.write(f"  {label}: {count}")
