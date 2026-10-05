"""OTP-domain foundations: generation, delivery, issuance and consumption (Schema §12.4, ADR-0007).

Prompt 4 implements the complete participant access flow (views, forms,
rate-limit-aware error messages); this module is the domain/persistence
layer it will call into. Every issuance/consumption path uses PostgreSQL
advisory locks (`apps.core.concurrency`) and versioned rate-limit HMAC
fingerprints (`apps.core.crypto`) -- never the identity blind-index family.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from importlib import import_module
from uuid import UUID

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.contracts import AuditRecord, AuditRecorder
from apps.audit.services import PersistentAuditRecorder
from apps.core.concurrency import (
    ADVISORY_LOCK_CLASS_NETWORK_ISSUANCE,
    ADVISORY_LOCK_CLASS_RECIPIENT_ISSUANCE,
    ADVISORY_LOCK_CLASS_RECIPIENT_TEMPORARY_LOCK,
    acquire_advisory_locks,
    lock_key,
)
from apps.core.crypto import compute_rate_limit_fingerprints_for_active_versions, get_key_provider
from apps.core.outbox.contracts import OutboxMessage, OutboxPublisher
from apps.core.outbox.persistent import PersistentOutboxPublisher

from .models import (
    AuthenticationChallenge,
    AuthenticationChallengeChannel,
    AuthenticationChallengePurpose,
)

OTP_LENGTH = 6
_OTP_ALPHABET = "0123456789"


class OtpGenerator:
    """OTP-value generation seam."""

    def generate(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class CsprngOtpGenerator(OtpGenerator):
    """Cryptographically secure OTP generation -- the default in every non-test environment."""

    def generate(self) -> str:
        return "".join(secrets.choice(_OTP_ALPHABET) for _ in range(OTP_LENGTH))


class DeterministicTestOtpGenerator(OtpGenerator):
    """Fixed-value OTP generator, selectable ONLY under `config.settings.test` (§5.7)."""

    FIXED_VALUE = "246810"  # noqa: S105 - test-only fixed value

    def __init__(self) -> None:
        # Deliberately checks the OS environment variable, NOT
        # `django.conf.settings.SETTINGS_MODULE`: Django's `override_settings`
        # / `modify_settings` wrap settings in a `UserSettingsHolder` that
        # hardcodes `SETTINGS_MODULE` to `None` regardless of the real
        # settings module -- an unrelated `@override_settings(...)` anywhere
        # in the call stack would otherwise defeat this guard entirely.
        # `DJANGO_SETTINGS_MODULE` is a plain environment variable, read once
        # at process start, and is never altered by settings overrides.
        if os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.test":
            raise RuntimeError(
                "DeterministicTestOtpGenerator may only be selected under "
                "config.settings.test -- it must never generate a predictable "
                "OTP in local, staging, or production (accepted plan §5.7)."
            )

    def generate(self) -> str:
        return self.FIXED_VALUE


def _import_backend(dotted_path: str) -> type[OtpGenerator]:
    module_path, _, class_name = dotted_path.rpartition(".")
    module = import_module(module_path)
    return getattr(module, class_name)


def get_otp_generator() -> OtpGenerator:
    """Instantiate the configured `OTP_GENERATOR_BACKEND`."""
    backend_class = _import_backend(settings.OTP_GENERATOR_BACKEND)
    return backend_class()


def normalize_recipient(channel: str, raw_value: str) -> str:
    value = raw_value.strip()
    if channel == AuthenticationChallengeChannel.EMAIL:
        return value.lower()
    return value


def deliver_otp(*, channel: str, recipient_value: str, otp_value: str) -> None:
    """Send the OTP through the configured delivery adapter.

    Django's configured `EMAIL_BACKEND` decides the transport: the file sink
    (`var/mail/`) locally, the in-memory outbox in tests, and the SMTP service
    configured from the environment in staging and production. The OTP value
    is sent to the recipient here, which is the delivery channel's entire
    purpose; it is never additionally logged or audited anywhere in this
    module.
    """
    if channel == AuthenticationChallengeChannel.EMAIL:
        from django.core.mail import send_mail
        from django.utils.translation import gettext as _

        # Localized in the language of the request that asked for the code, so an
        # Arabic reader sees the full Arabic event name (UX-1, M04, D-03).
        send_mail(
            subject=_("Your ASC 2026 verification code"),
            message=_("Your verification code is %(code)s. It will expire shortly.")
            % {"code": otp_value},
            from_email=None,
            recipient_list=[recipient_value],
        )
        return
    raise NotImplementedError("SMS OTP delivery is not implemented in Phase 1.")


class IssueOutcome:
    ISSUED = "ISSUED"
    THROTTLED = "THROTTLED"
    LOCKED = "LOCKED"


class ConsumeOutcome:
    CONSUMED = "CONSUMED"
    INVALID = "INVALID"
    EXPIRED = "EXPIRED"
    LOCKED = "LOCKED"
    ALREADY_USED = "ALREADY_USED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class IssueResult:
    outcome: str
    challenge_id: UUID | None = None


@dataclass(frozen=True)
class ConsumeResult:
    outcome: str


def _fingerprint_hex_values(fingerprints: dict[int, bytes]) -> list[str]:
    return [digest.hex() for digest in fingerprints.values()]


def latest_pending_challenge_id(*, channel: str, recipient_value: str) -> UUID | None:
    """Return the most recent not-yet-consumed challenge id for `recipient_value`, if any.

    Used by the view layer to know which challenge a submitted OTP code
    should be verified against, without ever exposing the recipient
    fingerprint itself. Returns the latest challenge REGARDLESS of the
    outcome of the issuance call that created it (so a `THROTTLED` resend
    still verifies against the earlier, still-valid code).
    """
    normalized_recipient = normalize_recipient(channel, recipient_value)
    provider = get_key_provider()
    hex_values = _fingerprint_hex_values(
        compute_rate_limit_fingerprints_for_active_versions(normalized_recipient, provider=provider)
    )
    challenge = (
        AuthenticationChallenge.objects.filter(
            recipient_fingerprint__in=hex_values, consumed_at__isnull=True
        )
        .order_by("-issued_at")
        .first()
    )
    return challenge.pk if challenge is not None else None


def issue_challenge(
    *,
    channel: str,
    recipient_value: str,
    client_network_address: str,
    person_id: UUID | None = None,
    contact_point_id: UUID | None = None,
    purpose: str = AuthenticationChallengePurpose.PARTICIPANT_LOGIN,
    correlation_id: str = "",
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
) -> IssueResult:
    """Issue one OTP challenge, or return a generic non-enumerating throttle/lock result.

    Concurrency-safe: acquires every relevant PostgreSQL advisory lock
    (recipient issuance, network issuance, recipient temporary-lock --
    ADR-0007 classes 1, 2, 4) across every active rate-limit key version,
    in the deterministic total order, before reading or writing any row.
    """
    provider = get_key_provider()
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    normalized_recipient = normalize_recipient(channel, recipient_value)

    with transaction.atomic():
        recipient_fingerprints = compute_rate_limit_fingerprints_for_active_versions(
            normalized_recipient, provider=provider
        )
        network_fingerprints = compute_rate_limit_fingerprints_for_active_versions(
            client_network_address, provider=provider
        )

        lock_pairs = (
            [
                lock_key(ADVISORY_LOCK_CLASS_RECIPIENT_ISSUANCE, digest)
                for digest in recipient_fingerprints.values()
            ]
            + [
                lock_key(ADVISORY_LOCK_CLASS_NETWORK_ISSUANCE, digest)
                for digest in network_fingerprints.values()
            ]
            + [
                lock_key(ADVISORY_LOCK_CLASS_RECIPIENT_TEMPORARY_LOCK, digest)
                for digest in recipient_fingerprints.values()
            ]
        )
        acquire_advisory_locks(connection, lock_pairs)

        # Captured AFTER acquiring the advisory locks, deliberately: a
        # thread that had to wait for the lock must judge cooldown/window
        # membership against the moment it actually entered its serialized
        # critical section, not the moment it merely queued up. Using a
        # pre-wait timestamp here would let a fresh concurrent request be
        # wrongly compared against a just-committed sibling row's
        # `resend_available_at`/window using a stale, earlier clock
        # reading, understating how many requests should legitimately
        # succeed.
        now = timezone.now()

        recipient_hex_values = _fingerprint_hex_values(recipient_fingerprints)
        network_hex_values = _fingerprint_hex_values(network_fingerprints)

        latest_for_recipient = (
            AuthenticationChallenge.objects.select_for_update()
            .filter(recipient_fingerprint__in=recipient_hex_values)
            .order_by("-issued_at")
            .first()
        )

        if (
            latest_for_recipient
            and latest_for_recipient.locked_until
            and latest_for_recipient.locked_until > now
        ):
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=person_id,
                    action_code="OTP_ISSUANCE_REJECTED",
                    target_type="AuthenticationChallenge",
                    result="FAILURE",
                    reason_code=IssueOutcome.LOCKED,
                    correlation_id=correlation_id,
                )
            )
            return IssueResult(outcome=IssueOutcome.LOCKED)

        if (
            latest_for_recipient
            and latest_for_recipient.resend_available_at
            and latest_for_recipient.resend_available_at > now
        ):
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=person_id,
                    action_code="OTP_ISSUANCE_REJECTED",
                    target_type="AuthenticationChallenge",
                    result="FAILURE",
                    reason_code=IssueOutcome.THROTTLED,
                    correlation_id=correlation_id,
                )
            )
            return IssueResult(outcome=IssueOutcome.THROTTLED)

        window_start = now - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS)
        recipient_count = AuthenticationChallenge.objects.filter(
            recipient_fingerprint__in=recipient_hex_values, issued_at__gte=window_start
        ).count()
        if recipient_count >= settings.OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW:
            if latest_for_recipient is not None:
                latest_for_recipient.locked_until = now + timedelta(
                    seconds=settings.OTP_TEMPORARY_LOCK_SECONDS
                )
                latest_for_recipient.save(update_fields=["locked_until", "updated_at"])
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=person_id,
                    action_code="OTP_ISSUANCE_REJECTED",
                    target_type="AuthenticationChallenge",
                    result="FAILURE",
                    reason_code=IssueOutcome.THROTTLED,
                    correlation_id=correlation_id,
                )
            )
            return IssueResult(outcome=IssueOutcome.THROTTLED)

        network_count = AuthenticationChallenge.objects.filter(
            network_fingerprint__in=network_hex_values, issued_at__gte=window_start
        ).count()
        if network_count >= settings.OTP_MAX_ISSUANCES_PER_NETWORK_WINDOW:
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=person_id,
                    action_code="OTP_ISSUANCE_REJECTED",
                    target_type="AuthenticationChallenge",
                    result="FAILURE",
                    reason_code=IssueOutcome.THROTTLED,
                    correlation_id=correlation_id,
                )
            )
            return IssueResult(outcome=IssueOutcome.THROTTLED)

        generator = get_otp_generator()
        otp_value = generator.generate()
        otp_hash = make_password(otp_value)

        write_version = provider.write_rate_limit_key_version()
        challenge = AuthenticationChallenge.objects.create(
            channel=channel,
            purpose=purpose,
            person_id=person_id,
            contact_point_id=contact_point_id,
            recipient_fingerprint=recipient_fingerprints[write_version].hex(),
            recipient_fingerprint_key_version=write_version,
            network_fingerprint=network_fingerprints.get(write_version, b"").hex(),
            network_fingerprint_key_version=write_version,
            otp_hash=otp_hash,
            issued_at=now,
            expires_at=now + timedelta(seconds=settings.OTP_LIFETIME_SECONDS),
            resend_available_at=now + timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS),
            request_metadata={"correlation_id": correlation_id} if correlation_id else {},
        )
        audit_recorder.record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=person_id,
                action_code="OTP_ISSUED",
                target_type="AuthenticationChallenge",
                target_uuid=challenge.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="authentication.otp_issued",
                aggregate_type="AuthenticationChallenge",
                aggregate_id=str(challenge.pk),
                payload={"channel": channel, "purpose": purpose},
            )
        )
        # External delivery must never happen for a transaction that later
        # rolls back. The plaintext OTP exists only in this short-lived
        # callback closure and is never stored in audit/outbox/database rows.
        transaction.on_commit(
            lambda: deliver_otp(
                channel=channel,
                recipient_value=normalized_recipient,
                otp_value=otp_value,
            )
        )

    return IssueResult(outcome=IssueOutcome.ISSUED, challenge_id=challenge.id)


def consume_challenge(
    *,
    challenge_id: UUID,
    otp_value: str,
    correlation_id: str = "",
    audit_recorder: AuditRecorder | None = None,
    outbox: OutboxPublisher | None = None,
) -> ConsumeResult:
    """Verify and single-use-consume one challenge.

    Row-level `SELECT ... FOR UPDATE` (ADR-0007 class 3) since the target
    row already exists by definition -- no advisory lock is needed here.
    """
    audit_recorder = audit_recorder or PersistentAuditRecorder()
    outbox = outbox or PersistentOutboxPublisher()
    with transaction.atomic():
        try:
            challenge = AuthenticationChallenge.objects.select_for_update().get(pk=challenge_id)
        except AuthenticationChallenge.DoesNotExist:
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    action_code="OTP_CONSUMPTION_REJECTED",
                    target_type="AuthenticationChallenge",
                    target_uuid=challenge_id,
                    result="FAILURE",
                    reason_code=ConsumeOutcome.NOT_FOUND,
                    correlation_id=correlation_id,
                )
            )
            return ConsumeResult(outcome=ConsumeOutcome.NOT_FOUND)

        def reject(outcome: str) -> ConsumeResult:
            audit_recorder.record(
                AuditRecord(
                    actor_type="PARTICIPANT",
                    actor_person_id=challenge.person_id,
                    action_code="OTP_CONSUMPTION_REJECTED",
                    target_type="AuthenticationChallenge",
                    target_uuid=challenge.pk,
                    result="FAILURE",
                    reason_code=outcome,
                    correlation_id=correlation_id,
                )
            )
            return ConsumeResult(outcome=outcome)

        now = timezone.now()
        if challenge.consumed_at is not None:
            return reject(ConsumeOutcome.ALREADY_USED)
        if challenge.locked_until and challenge.locked_until > now:
            return reject(ConsumeOutcome.LOCKED)
        if challenge.attempt_count >= challenge.max_attempts:
            return reject(ConsumeOutcome.LOCKED)
        if challenge.expires_at <= now:
            return reject(ConsumeOutcome.EXPIRED)

        if not check_password(otp_value, challenge.otp_hash):
            challenge.attempt_count += 1
            update_fields = ["attempt_count", "updated_at"]
            if challenge.attempt_count >= challenge.max_attempts:
                challenge.locked_until = now + timedelta(
                    seconds=settings.OTP_TEMPORARY_LOCK_SECONDS
                )
                update_fields.append("locked_until")
            challenge.save(update_fields=update_fields)
            return reject(ConsumeOutcome.INVALID)

        challenge.consumed_at = now
        challenge.save(update_fields=["consumed_at", "updated_at"])
        audit_recorder.record(
            AuditRecord(
                actor_type="PARTICIPANT",
                actor_person_id=challenge.person_id,
                action_code="OTP_CONSUMED",
                target_type="AuthenticationChallenge",
                target_uuid=challenge.pk,
                result="SUCCESS",
                correlation_id=correlation_id,
            )
        )
        outbox.enqueue(
            OutboxMessage(
                event_type="authentication.otp_consumed",
                aggregate_type="AuthenticationChallenge",
                aggregate_id=str(challenge.pk),
                payload={"purpose": challenge.purpose},
            )
        )
        return ConsumeResult(outcome=ConsumeOutcome.CONSUMED)
