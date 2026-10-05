"""Persist expiry of lapsed entry devices and temporary security accounts."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.entry.tasks import run_entry_expiry


class Command(BaseCommand):
    help = (
        "Mark lapsed entry devices and temporary external-security accounts EXPIRED, "
        "end their sessions, and record audit events. Idempotent."
    )

    def handle(self, *args, **options):
        counts = run_entry_expiry()
        self.stdout.write(
            f"Expired devices: {counts['devices']}; expired accounts: {counts['accounts']}"
        )
