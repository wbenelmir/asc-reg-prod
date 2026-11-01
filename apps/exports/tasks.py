"""Idempotent export maintenance. Listed in `settings.CELERY_BEAT_SCHEDULE`."""

from __future__ import annotations

from celery import shared_task


@shared_task(name="exports.expire_due_exports")
def expire_due_exports_task() -> int:
    """Expire exports past `expires_at` and delete the files of expired
    verified-identities exports. Returns the number newly expired."""
    from apps.exports.services import expire_due_exports

    return expire_due_exports()
