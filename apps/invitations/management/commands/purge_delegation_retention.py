"""Operational entry point for the delegation-CSV retention/purge lifecycle
(Phase 2 Prompt 2 V2 correction pass "wire delegation retention into an
operational entry point").

    uv run --env-file .env python manage.py purge_delegation_retention \
        --settings=config.settings.local

Wraps the existing `apps.invitations.services.purge_expired_delegation_source_objects`
and `purge_expired_delegation_rows` service functions -- this command adds
no new purge logic of its own, only a project-owned, schedulable way to
invoke it (a plain callable command, no Celery: Phase 1/2 has no real
broker integration, ADR-0009 -- a later phase MAY wrap this same command
in a scheduled Celery Beat task without changing its behavior).

Idempotent: every underlying purge only ever touches rows/objects whose
retention window has already passed and that have not already been purged
(`size_bytes=0` / already-blanked `candidate_email_encrypted`), so running
this command twice in a row purges nothing the second time. Reports ONLY
bounded counts -- never an email, storage key, hash, or CSV value.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.invitations.services import (
    purge_expired_delegation_rows,
    purge_expired_delegation_source_objects,
)


class Command(BaseCommand):
    help = (
        "Purge expired delegation-batch source CSV bytes and staged delegation-row "
        "personal values whose retention window has passed. Reports counts only."
    )

    def handle(self, *args, **options) -> None:
        purged_objects = purge_expired_delegation_source_objects()
        purged_rows = purge_expired_delegation_rows()
        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {purged_objects} expired delegation source object(s) and "
                f"{purged_rows} expired delegation row value(s)."
            )
        )
