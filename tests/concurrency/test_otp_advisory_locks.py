"""Real multi-connection PostgreSQL concurrency tests for the OTP domain (ADR-0007).

Every test here spawns genuinely separate threads, each opening its OWN
PostgreSQL connection (Django's connection handling is thread-local, so
touching the ORM from a new thread transparently opens a new `psycopg`
connection) -- never a sequential mock. This is what actually exercises
`pg_advisory_xact_lock` serialization: two ordinary `READ COMMITTED`
transactions racing on the SAME row (or no row at all yet) is exactly the
scenario ADR-0007 says a plain row count cannot defend against.

Marked `concurrency`, matching `pyproject.toml`'s definition: "real
multi-connection PostgreSQL concurrency tests (not Redis integration)".
"""

from __future__ import annotations

import threading

import pytest
from django.db import connection
from django.test import override_settings

from apps.accounts.models import AuthenticationChallenge, AuthenticationChallengeChannel
from apps.accounts.otp import ConsumeOutcome, IssueOutcome, consume_challenge, issue_challenge

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def test_concurrent_reference_allocation_never_duplicates_or_skips() -> None:
    """Schema §3.1/§20: event-scoped reference generation must be concurrency-safe.

    N threads race to allocate a reference for the SAME EventEdition with
    no synchronization of their own -- `allocate_registration_reference`'s
    `SELECT ... FOR UPDATE` row lock must serialize them so every sequence
    number 1..N is produced exactly once.
    """
    from django.db import transaction
    from django.utils import timezone

    from apps.events.models import EventEdition
    from apps.registrations.services import allocate_registration_reference

    now = timezone.now()
    event = EventEdition.objects.create(
        code="CONCURRENT", name="Concurrent Ref Test", timezone="UTC", starts_at=now, ends_at=now
    )
    thread_count = 12

    def _allocate():
        with transaction.atomic():
            return allocate_registration_reference(event)

    references = _run_in_threads(_allocate, thread_count)

    assert len(references) == len(set(references)), f"duplicate references: {references}"
    expected = {f"CONCURRENT-R-{i:06d}" for i in range(1, thread_count + 1)}
    assert set(references) == expected


def _run_in_threads(target, count: int) -> list:
    """Run `target(barrier) -> result` in `count` real threads; return the results.

    A `threading.Barrier` holds every thread at the starting line so the
    calls genuinely overlap in time, maximizing the chance of the exact
    race ADR-0007 describes -- concurrent callers who each observe zero
    prior rows before any of them commits.
    """
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    if errors:
        raise errors[0]
    return results


@override_settings(OTP_RESEND_COOLDOWN_SECONDS=0, OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW=3)
def test_concurrent_first_time_issuance_never_exceeds_the_recipient_limit() -> None:
    """The exact ADR-0007 race: N simultaneous first-ever requests, no pre-existing row.

    Without the advisory lock, every thread could observe a zero count and
    all would proceed, exceeding the configured ceiling. With it, exactly
    `OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW` succeed.
    """
    recipient = "concurrent-recipient@example.com"
    thread_count = 8

    def _issue():
        return issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value=recipient,
            client_network_address=f"203.0.113.{threading.get_ident() % 200}",
        )

    results = _run_in_threads(_issue, thread_count)

    issued = [r for r in results if r.outcome == IssueOutcome.ISSUED]
    assert len(issued) == 3

    from apps.core.crypto import (
        compute_rate_limit_fingerprints_for_active_versions,
        get_key_provider,
    )

    candidates = [
        digest.hex()
        for digest in compute_rate_limit_fingerprints_for_active_versions(
            recipient, provider=get_key_provider()
        ).values()
    ]
    actual_row_count = AuthenticationChallenge.objects.filter(
        recipient_fingerprint__in=candidates
    ).count()
    # No over-issuance: exactly one persisted row per successful ISSUED
    # outcome, never more than the configured ceiling.
    assert actual_row_count == 3


@override_settings(OTP_RESEND_COOLDOWN_SECONDS=0, OTP_MAX_ISSUANCES_PER_NETWORK_WINDOW=3)
def test_concurrent_first_time_issuance_never_exceeds_the_network_limit() -> None:
    network = "198.51.100.77"
    thread_count = 8

    def _issue():
        import uuid

        return issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value=f"{uuid.uuid4()}@example.com",
            client_network_address=network,
        )

    results = _run_in_threads(_issue, thread_count)
    issued = [r for r in results if r.outcome == IssueOutcome.ISSUED]
    assert len(issued) == 3


def test_concurrent_issuance_for_different_recipients_does_not_deadlock() -> None:
    """Deterministic lock ordering (ADR-0007) must prevent deadlock across unrelated keys."""
    thread_count = 6

    def _make_issuer(index: int):
        def _issue():
            return issue_challenge(
                channel=AuthenticationChallengeChannel.EMAIL,
                recipient_value=f"distinct-{index}@example.com",
                client_network_address=f"203.0.113.{50 + index}",
            )

        return _issue

    barrier = threading.Barrier(thread_count)
    results: list = [None] * thread_count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = _make_issuer(index)()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"unexpected error(s) (deadlock or otherwise): {errors}"
    assert all(r.outcome == IssueOutcome.ISSUED for r in results)


def test_concurrent_consumption_of_one_challenge_succeeds_exactly_once() -> None:
    """Row-level `SELECT ... FOR UPDATE` (ADR-0007 class 3) must prevent replay races."""
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        from apps.accounts.otp import DeterministicTestOtpGenerator

        issued = issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value="single-consume@example.com",
            client_network_address="203.0.113.200",
        )
        correct_otp = DeterministicTestOtpGenerator.FIXED_VALUE

    thread_count = 10

    def _consume():
        return consume_challenge(challenge_id=issued.challenge_id, otp_value=correct_otp)

    results = _run_in_threads(_consume, thread_count)

    consumed = [r for r in results if r.outcome == ConsumeOutcome.CONSUMED]
    already_used = [r for r in results if r.outcome == ConsumeOutcome.ALREADY_USED]
    assert len(consumed) == 1
    assert len(already_used) == thread_count - 1
