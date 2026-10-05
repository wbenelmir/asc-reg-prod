"""AuthenticationChallenge / OTP-domain tests (Schema §12.4, ADR-0007)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import AuthenticationChallenge, AuthenticationChallengeChannel
from apps.accounts.otp import (
    ConsumeOutcome,
    CsprngOtpGenerator,
    DeterministicTestOtpGenerator,
    IssueOutcome,
    consume_challenge,
    issue_challenge,
)
from apps.audit.models import AuditEvent
from apps.core.models import OutboxEvent

pytestmark = pytest.mark.django_db(transaction=True)


def test_csprng_generator_produces_six_numeric_digits() -> None:
    generator = CsprngOtpGenerator()
    value = generator.generate()
    assert len(value) == 6
    assert value.isdigit()


def test_csprng_generator_is_not_constant() -> None:
    generator = CsprngOtpGenerator()
    values = {generator.generate() for _ in range(20)}
    assert len(values) > 1


def test_deterministic_generator_works_under_test_settings() -> None:
    generator = DeterministicTestOtpGenerator()
    assert generator.generate() == DeterministicTestOtpGenerator.FIXED_VALUE


def test_deterministic_generator_refuses_outside_test_settings(monkeypatch) -> None:
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.local")
    with pytest.raises(RuntimeError, match="config.settings.test"):
        DeterministicTestOtpGenerator()


def test_issue_sends_email_and_persists_a_challenge() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="participant@example.com",
        client_network_address="203.0.113.10",
    )
    assert result.outcome == IssueOutcome.ISSUED
    assert AuthenticationChallenge.objects.filter(pk=result.challenge_id).exists()
    assert len(mail.outbox) == 1
    assert AuditEvent.objects.filter(
        target_uuid=result.challenge_id, action_code="OTP_ISSUED"
    ).exists()
    assert OutboxEvent.objects.filter(
        aggregate_id=str(result.challenge_id), event_type="authentication.otp_issued"
    ).exists()


def test_otp_delivery_is_scheduled_only_after_commit(monkeypatch) -> None:
    delivered: list[dict[str, str]] = []
    callbacks: list[object] = []
    monkeypatch.setattr("apps.accounts.otp.deliver_otp", lambda **kwargs: delivered.append(kwargs))
    monkeypatch.setattr("apps.accounts.otp.transaction.on_commit", callbacks.append)

    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="after-commit@example.com",
        client_network_address="203.0.113.19",
    )

    assert result.outcome == IssueOutcome.ISSUED
    assert delivered == []
    assert len(callbacks) == 1
    callbacks[0]()
    assert delivered[0]["recipient_value"] == "after-commit@example.com"


def test_otp_is_not_delivered_when_the_domain_transaction_rolls_back() -> None:
    class FailingOutbox:
        def enqueue(self, message) -> None:
            raise RuntimeError("synthetic outbox failure")

    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value="rollback@example.com",
            client_network_address="203.0.113.20",
            outbox=FailingOutbox(),
        )

    assert mail.outbox == []
    assert not AuthenticationChallenge.objects.filter(recipient_fingerprint__isnull=False).exists()


def test_issued_otp_is_never_stored_in_plaintext() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="plaintext-check@example.com",
        client_network_address="203.0.113.11",
    )
    challenge = AuthenticationChallenge.objects.get(pk=result.challenge_id)
    otp_value = mail.outbox[-1].body.split()[-1].rstrip(".")
    assert otp_value not in challenge.otp_hash
    assert challenge.otp_hash != otp_value


@override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator")
def test_consume_with_correct_otp_succeeds_once() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="consume-once@example.com",
        client_network_address="203.0.113.12",
    )
    outcome = consume_challenge(
        challenge_id=result.challenge_id, otp_value=DeterministicTestOtpGenerator.FIXED_VALUE
    )
    assert outcome.outcome == ConsumeOutcome.CONSUMED
    assert AuditEvent.objects.filter(
        target_uuid=result.challenge_id, action_code="OTP_CONSUMED"
    ).exists()
    assert OutboxEvent.objects.filter(
        aggregate_id=str(result.challenge_id), event_type="authentication.otp_consumed"
    ).exists()

    replay = consume_challenge(
        challenge_id=result.challenge_id, otp_value=DeterministicTestOtpGenerator.FIXED_VALUE
    )
    assert replay.outcome == ConsumeOutcome.ALREADY_USED


def test_consume_with_wrong_otp_is_invalid_and_counts_as_an_attempt() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="wrong-otp@example.com",
        client_network_address="203.0.113.13",
    )
    outcome = consume_challenge(challenge_id=result.challenge_id, otp_value="000000")
    assert outcome.outcome == ConsumeOutcome.INVALID
    challenge = AuthenticationChallenge.objects.get(pk=result.challenge_id)
    assert challenge.attempt_count == 1


def test_max_attempts_locks_the_challenge() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="max-attempts@example.com",
        client_network_address="203.0.113.14",
    )
    challenge = AuthenticationChallenge.objects.get(pk=result.challenge_id)
    for _ in range(challenge.max_attempts):
        consume_challenge(challenge_id=result.challenge_id, otp_value="000000")
    final = consume_challenge(challenge_id=result.challenge_id, otp_value="000000")
    assert final.outcome == ConsumeOutcome.LOCKED


def test_expired_challenge_cannot_be_consumed() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="expired@example.com",
        client_network_address="203.0.113.15",
    )
    challenge = AuthenticationChallenge.objects.get(pk=result.challenge_id)
    challenge.expires_at = timezone.now() - timedelta(seconds=1)
    challenge.save(update_fields=["expires_at"])
    outcome = consume_challenge(challenge_id=result.challenge_id, otp_value="000000")
    assert outcome.outcome == ConsumeOutcome.EXPIRED


def test_consuming_an_unknown_challenge_is_generic_not_found() -> None:
    import uuid

    outcome = consume_challenge(challenge_id=uuid.uuid4(), otp_value="000000")
    assert outcome.outcome == ConsumeOutcome.NOT_FOUND


def test_resend_before_cooldown_elapses_is_throttled() -> None:
    first = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="cooldown@example.com",
        client_network_address="203.0.113.16",
    )
    assert first.outcome == IssueOutcome.ISSUED
    second = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="cooldown@example.com",
        client_network_address="203.0.113.16",
    )
    assert second.outcome == IssueOutcome.THROTTLED


@override_settings(OTP_RESEND_COOLDOWN_SECONDS=0, OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW=2)
def test_recipient_issuance_rate_limit_locks_after_the_configured_ceiling() -> None:
    recipient = "rate-limited@example.com"
    for _ in range(2):
        outcome = issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value=recipient,
            client_network_address="203.0.113.17",
        )
        assert outcome.outcome == IssueOutcome.ISSUED
    third = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value=recipient,
        client_network_address="203.0.113.17",
    )
    assert third.outcome == IssueOutcome.THROTTLED

    # And now locked -- even after any further attempt.
    fourth = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value=recipient,
        client_network_address="203.0.113.17",
    )
    assert fourth.outcome == IssueOutcome.LOCKED


@override_settings(OTP_RESEND_COOLDOWN_SECONDS=0, OTP_MAX_ISSUANCES_PER_NETWORK_WINDOW=2)
def test_network_issuance_rate_limit_applies_across_different_recipients() -> None:
    network = "198.51.100.42"
    for index in range(2):
        outcome = issue_challenge(
            channel=AuthenticationChallengeChannel.EMAIL,
            recipient_value=f"network-limit-{index}@example.com",
            client_network_address=network,
        )
        assert outcome.outcome == IssueOutcome.ISSUED
    third = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="network-limit-third@example.com",
        client_network_address=network,
    )
    assert third.outcome == IssueOutcome.THROTTLED


def test_recipient_fingerprint_normalizes_email_case() -> None:
    from apps.accounts.otp import normalize_recipient

    assert normalize_recipient(AuthenticationChallengeChannel.EMAIL, "  Person@Example.COM ") == (
        "person@example.com"
    )


def test_request_metadata_never_contains_the_recipient_or_otp() -> None:
    result = issue_challenge(
        channel=AuthenticationChallengeChannel.EMAIL,
        recipient_value="metadata-check@example.com",
        client_network_address="203.0.113.18",
        correlation_id="corr-xyz",
    )
    challenge = AuthenticationChallenge.objects.get(pk=result.challenge_id)
    serialized = str(challenge.request_metadata)
    assert "metadata-check@example.com" not in serialized
    otp_value = mail.outbox[-1].body.split()[-1].rstrip(".")
    assert otp_value not in serialized
