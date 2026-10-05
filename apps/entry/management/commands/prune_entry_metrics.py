"""Delete entry verification telemetry past its retention period (Prompt 5)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.entry.observability import prune_verification_samples


class Command(BaseCommand):
    help = "Delete VerificationSample rows older than ENTRY_METRICS_RETENTION_DAYS. Idempotent."

    def handle(self, *args, **options):
        self.stdout.write(f"Deleted verification samples: {prune_verification_samples()}")
