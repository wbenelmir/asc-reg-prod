"""IDV-C1, R-IDV-04: abandoned attempts count against the same bounded budget.

A worker that dies after claiming a job leaves an IN_PROGRESS job whose lease
expires; the sweeper reclaims it. Each claim is an attempt. Once
`max_attempts()` claims are used -- by handled failures or by abandoned
leases -- the job is EXHAUSTED, its lease is cleared and the case goes to
manual review (`PROVIDER_ATTEMPTS_EXHAUSTED`), in one transaction, without
another provider call. A busy provider slot is not an attempt. A late result
from a lost lease never restores the case. Synthetic identities only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.people.identity_contract import LookupKind
from apps.people.models import (
    IdentityJobStatus,
    IdentityReasonCode,
    IdentityStatus,
    IdentityVerificationAttempt,
    IdentityVerificationJob,
)
from apps.people.nin_provider import LocalSimulationNinProvider
from apps.people.services import identity_verification as idv
from apps.people.tests.conftest import case_for, submit_case

pytestmark = pytest.mark.django_db


class CountingProvider:
    PROVIDER_CODE = "COUNTING_TEST_DOUBLE"
    IS_OFFICIAL = False

    def __init__(self) -> None:
        self.calls = 0

    def configuration_problems(self):
        return []

    def lookup(self, nin):
        self.calls += 1
        return LocalSimulationNinProvider().lookup(nin)


def _die_after_claiming(job_id) -> uuid.UUID:
    """A worker claims the job and dies before applying anything: only the
    expired lease is left behind."""
    token = uuid.uuid4()
    job, state = idv._claim(job_id, token, timezone.now())
    assert state == "claimed", state
    IdentityVerificationJob.objects.filter(pk=job_id).update(
        lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    return token


def test_repeated_worker_deaths_exhaust_the_budget_and_stop_calling_the_provider(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    for _ in range(idv.max_attempts()):
        _die_after_claiming(job.pk)
    provider = CountingProvider()

    outcome = idv.process_identity_job(job.pk, provider=provider)

    assert provider.calls == 0
    assert outcome == "exhausted"
    job.refresh_from_db()
    assert job.status == IdentityJobStatus.EXHAUSTED
    assert job.attempts == idv.max_attempts()  # never beyond the budget
    assert job.lease_token is None and job.lease_expires_at is None
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == IdentityReasonCode.PROVIDER_ATTEMPTS_EXHAUSTED
    assert AuditEvent.objects.filter(
        action_code="IDV_PROVIDER_ATTEMPTS_EXHAUSTED", target_uuid=case.pk
    ).exists()
    # The sweeper has nothing left to recover, and a further delivery is inert.
    assert job.pk not in idv.due_job_ids()
    assert idv.process_identity_job(job.pk, provider=provider) == "not_due"
    assert provider.calls == 0


def test_a_recovery_below_the_budget_still_checks_the_identity(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    _die_after_claiming(job.pk)
    provider = CountingProvider()

    assert idv.process_identity_job(job.pk, provider=provider) == IdentityStatus.API_VERIFIED

    assert provider.calls == 1
    job.refresh_from_db()
    assert job.attempts == 2  # the abandoned claim was counted
    assert job.status == IdentityJobStatus.COMPLETED


def test_handled_failures_and_abandoned_claims_share_one_budget(
    idv_event, legal_versions, settings
) -> None:
    settings.IDENTITY_VERIFICATION_RETRY_DELAYS_SECONDS = (0, 0)  # budget: 3 attempts
    registration = submit_case(idv_event, legal_versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    _die_after_claiming(job.pk)  # attempt 1, abandoned

    class Unavailable(CountingProvider):
        def lookup(self, nin):
            self.calls += 1
            from apps.people.nin_provider import LookupOutcome

            return LookupOutcome(LookupKind.UNAVAILABLE, detail="synthetic", retryable=True)

    provider = Unavailable()
    assert idv.process_identity_job(job.pk, provider=provider) == "retry_scheduled"  # attempt 2
    _die_after_claiming(job.pk)  # attempt 3, abandoned
    assert idv.process_identity_job(job.pk, provider=provider) == "exhausted"
    assert provider.calls == 1
    assert case_for(registration).reason_code == IdentityReasonCode.PROVIDER_ATTEMPTS_EXHAUSTED


def test_a_late_result_from_a_lost_lease_cannot_restore_an_exhausted_case(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    tokens = [_die_after_claiming(job.pk) for _ in range(idv.max_attempts())]
    assert idv.process_identity_job(job.pk, provider=CountingProvider()) == "exhausted"
    # The "dead" first worker was only slow: its result arrives now.
    late = LocalSimulationNinProvider().lookup(
        case_for(registration).current_revision.identifier.value_encrypted
    )
    assert late.kind == LookupKind.FOUND

    label = idv._apply_lookup_result(
        job_id=job.pk,
        token=tokens[0],
        outcome=late,
        provider=LocalSimulationNinProvider(),
        attempt_number=1,
        duration_ms=1,
    )

    assert label == "stale"
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == IdentityReasonCode.PROVIDER_ATTEMPTS_EXHAUSTED
    assert not IdentityVerificationAttempt.objects.filter(
        registration=registration, applied=True
    ).exists()


def test_a_busy_provider_slot_is_not_an_attempt(idv_event, legal_versions, settings) -> None:
    import threading

    from django.db import connections

    settings.IDENTITY_PROVIDER_MAX_CONCURRENCY = 1
    registration = submit_case(idv_event, legal_versions)
    job = IdentityVerificationJob.objects.get(verification=case_for(registration))
    held, release = threading.Event(), threading.Event()

    def hold_the_only_slot():
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(%s, %s)",
                    [idv.ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT, 0],
                )
                held.set()
                release.wait(timeout=10)
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [idv.ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT, 0],
                )
        finally:
            other.close()

    thread = threading.Thread(target=hold_the_only_slot)
    thread.start()
    try:
        assert held.wait(timeout=10)
        for _ in range(idv.max_attempts() + 2):
            assert idv.process_identity_job(job.pk, provider=CountingProvider()) == "deferred"
            IdentityVerificationJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now())
    finally:
        release.set()
        thread.join(timeout=10)
    job.refresh_from_db()
    assert job.attempts == 0
    assert job.status == IdentityJobStatus.PENDING
