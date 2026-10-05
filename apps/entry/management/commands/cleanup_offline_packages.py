"""Expire, withdraw and purge Offline Package data (Phase 4 Prompt 2)."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.entry.services.offline_packages import cleanup_offline_packages


class Command(BaseCommand):
    help = (
        "Expire lapsed Offline Packages, withdraw closed events, purge retained "
        "device-encrypted ciphertext past its retention, and prune expired nonces. "
        "Package metadata rows are kept. Idempotent."
    )

    def handle(self, *args, **options):
        result = cleanup_offline_packages()
        self.stdout.write(
            "Offline package cleanup: "
            + ", ".join(f"{key}={value}" for key, value in sorted(result.items()))
        )
