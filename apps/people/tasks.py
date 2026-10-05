"""Idempotent, retry-safe Celery tasks for apps.people (IDV-2, ADR-0026).

`process_identity_verification_job_task` processes one durable
`IdentityVerificationJob`; a duplicate delivery is a no-op (the job is no longer
due, or its lease is held). Retries are scheduled by the job's own
`next_attempt_at`, never by Celery's retry machinery, so behaviour is the same
in eager mode (local/test, ADR-0009) and with a real broker.

`dispatch_due_identity_verification_jobs` is the beat sweeper: it re-enqueues
every due job, recovering work whose after-commit dispatch failed during a
broker outage and work whose worker died while holding a lease. When the broker
itself is down, `manage.py run_identity_verification_jobs` processes due jobs
inline.
"""

from __future__ import annotations

from celery import shared_task


@shared_task(name="people.process_identity_verification_job")
def process_identity_verification_job_task(job_id: str) -> str:
    from apps.people.services.identity_verification import process_identity_job

    return process_identity_job(job_id)


@shared_task(name="people.dispatch_due_identity_verification_jobs")
def dispatch_due_identity_verification_jobs(limit: int = 100) -> int:
    from apps.people.services.identity_verification import dispatch_due_identity_jobs

    return dispatch_due_identity_jobs(limit)
