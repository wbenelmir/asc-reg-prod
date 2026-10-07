"""Idempotent, retry-safe Celery tasks for apps.accounts."""

from __future__ import annotations

from celery import shared_task

#: Upper bound of batches removed by one sweep.
_MAX_PURGE_BATCHES = 50


@shared_task(name="accounts.purge_expired_staff_captchas")
def purge_expired_staff_captchas_task() -> int:
    """Delete expired staff sign-in CAPTCHA challenges in bounded batches.
    An expired challenge is refused anyway; this only keeps the table small
    when anonymous sign-in pages are requested and never submitted."""
    from apps.accounts.captcha_guard import purge_expired

    total = 0
    for _ in range(_MAX_PURGE_BATCHES):
        deleted = purge_expired()
        total += deleted
        if not deleted:
            break
    return total
