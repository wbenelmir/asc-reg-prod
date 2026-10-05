"""Idempotent, retry-safe Celery tasks for apps.core.

P4-4 adds two maintenance sweeps. Both only delete rows that have already
expired, carry no participant data, and are safe to run repeatedly and
concurrently. They are listed in `settings.CELERY_BEAT_SCHEDULE`; locally
they run only when called (eager mode, ADR-0009).
"""

from __future__ import annotations

from celery import shared_task

#: Upper bound of replay-store batches removed by one sweep.
_MAX_PURGE_BATCHES = 50


@shared_task(name="core.clear_expired_sessions")
def clear_expired_sessions_task() -> int:
    """Delete server-side session rows past their expiry (the same rows
    `manage.py clearsessions` removes). Returns the number deleted."""
    from django.contrib.sessions.models import Session
    from django.utils import timezone

    deleted, _ = Session.objects.filter(expire_date__lt=timezone.now()).delete()
    return deleted


@shared_task(name="core.purge_human_challenge_uses")
def purge_human_challenge_uses_task() -> int:
    """Delete expired one-time-use records of solved human checks. A record is
    only needed until its challenge expires, since an expired challenge is
    refused anyway."""
    from apps.core.human_check import purge_expired_uses

    total = 0
    for _ in range(_MAX_PURGE_BATCHES):
        deleted = purge_expired_uses()
        total += deleted
        if not deleted:
            break
    return total
