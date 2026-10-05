"""Digital Entry Pass lifecycle and verification-key services.

Phase 3 Prompt 2. Every state change lives here, inside an explicit
transaction, never in a view, a form, a template, a model `save()`, or a
signal (TRD `BE-002`, project architecture rules).

Three invariants this module is built around:

1. **Eligibility is never reimplemented.** Generation and replacement call
   `apps.accreditation.services.evaluate_eligibility` -- the one reviewed,
   deny-by-default boundary Phase 2 exposed for exactly this purpose.
2. **Correctness never depends on a worker.** Expiry is derived from
   `valid_until` at verification time. The sweep in this module only
   persists a status for reporting; removing it changes no decision.
3. **Idempotency is explicit.** Every lifecycle command takes a
   cryptographically random `operation_id`, recorded in the same
   transaction as the change. A retry returns the original outcome.

Audit records and durable outbox rows are written INSIDE the state-change
transaction, never deferred to `transaction.on_commit`. Only delivery of
already-committed outbox work happens after commit.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accreditation.models import (
    AccessProfileAssignment,
    AssignmentStatus,
    BadgeTypeAssignment,
    ParticipantRoleAssignment,
)
from apps.audit import action_codes
from apps.audit.contracts import AuditRecord
from apps.audit.services import PersistentAuditRecorder
from apps.badges import references
from apps.badges.credentials.canonical import (
    NONCE_LENGTH,
    OPAQUE_ID_LENGTH,
    b64url_encode,
    canonical_header_bytes,
    canonical_payload_bytes,
    payload_hash,
    signing_input,
)
from apps.badges.credentials.issue import assemble_compact_jws
from apps.badges.models import (
    ES256,
    NON_TERMINAL_STATUSES,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    ParticipantEventPseudonym,
    PassCredentialSeries,
    PassLifecycleOperation,
    PassLifecycleOperationType,
    PassReasonCode,
    VerificationKey,
    VerificationKeyReasonCode,
    VerificationKeyStatus,
)
from apps.core.crypto.signing import (
    RAW_SIGNATURE_LENGTH_BYTES,
    SIGNING_ALGORITHM,
    PublicKeyFormatError,
    SigningKeyProviderError,
    canonical_public_key_der,
    get_signing_key_provider,
    is_valid_key_id,
    public_key_fingerprint,
)
from apps.core.outbox.contracts import OutboxMessage
from apps.core.outbox.persistent import PersistentOutboxPublisher
from apps.registrations.models import Registration

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PassServiceError(Exception):
    """Base for every Digital Entry Pass domain failure.

    The message is developer-facing English and is never rendered to a user.
    `code` is a stable, machine-readable refinement of the class that the
    presentation layer (`apps.badges.presentation`) maps to a localized
    message (Phase 3 Prompt 8, P8-01); services never translate.
    """

    code = ""

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PassNotEligibleError(PassServiceError):
    """Raised when `evaluate_eligibility` denies issue or replacement.

    Carries the reviewed boundary's own reason code verbatim; this module
    never invents or reinterprets an eligibility reason.
    """

    def __init__(self, reason: str) -> None:
        # The eligibility reason doubles as the stable presentation `code`.
        super().__init__(
            f"Registration is not eligible for a Digital Entry Pass: {reason}", code=reason
        )
        self.reason = reason


class PassStateError(PassServiceError):
    """Raised for a transition that the lifecycle does not permit."""


class PassConcurrencyError(PassServiceError):
    """Raised when the caller's expected credential or lock version is stale."""


class PassConfigurationError(PassServiceError):
    """Raised when event or key configuration cannot produce a credential."""


class VerificationKeyError(PassServiceError):
    """Raised for an invalid verification-key lifecycle operation."""


class FallbackLookupThrottled(PassServiceError):
    """Raised when controlled fallback-reference lookup exceeds its budget."""


# ---------------------------------------------------------------------------
# Opaque identifier generation
# ---------------------------------------------------------------------------


def _opaque_id(length: int = OPAQUE_ID_LENGTH) -> str:
    """A random base64url identifier of exactly `length` characters.

    Drawn from `secrets`. Never derived from a primary key, a UUIDv7, a
    registration reference, or any participant value.
    """
    import base64

    raw = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return encoded[:length]


def _new_nonce() -> str:
    return _opaque_id(NONCE_LENGTH)


def new_operation_id() -> str:
    """A fresh, cryptographically random lifecycle operation identifier."""
    return secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Lifecycle transition table -- the single source of truth
# ---------------------------------------------------------------------------

#: Exactly the permitted transitions. Anything absent here is rejected, and
#: a table-driven test walks the full status matrix so a transition cannot
#: be added without a deliberate change to this map.
PERMITTED_TRANSITIONS: dict[str, frozenset[str]] = {
    DigitalEntryPassStatus.INACTIVE: frozenset(
        {
            DigitalEntryPassStatus.ACTIVE,
            DigitalEntryPassStatus.REVOKED,
            DigitalEntryPassStatus.REPLACED,
            DigitalEntryPassStatus.EXPIRED,
        }
    ),
    DigitalEntryPassStatus.ACTIVE: frozenset(
        {
            DigitalEntryPassStatus.SUSPENDED,
            DigitalEntryPassStatus.REVOKED,
            DigitalEntryPassStatus.REPLACED,
            DigitalEntryPassStatus.EXPIRED,
        }
    ),
    DigitalEntryPassStatus.SUSPENDED: frozenset(
        {
            DigitalEntryPassStatus.ACTIVE,
            DigitalEntryPassStatus.REVOKED,
            DigitalEntryPassStatus.REPLACED,
            DigitalEntryPassStatus.EXPIRED,
        }
    ),
    DigitalEntryPassStatus.REVOKED: frozenset(),
    DigitalEntryPassStatus.REPLACED: frozenset(),
    DigitalEntryPassStatus.EXPIRED: frozenset(),
}


def transition_is_permitted(current: str, target: str) -> bool:
    return target in PERMITTED_TRANSITIONS.get(current, frozenset())


def _require_transition(current: str, target: str) -> None:
    if not transition_is_permitted(current, target):
        raise PassStateError(f"A Digital Entry Pass cannot move from {current} to {target}.")


# ---------------------------------------------------------------------------
# Claim construction
# ---------------------------------------------------------------------------


def _epoch(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())


def build_pass_claims(credential: DigitalEntryPass) -> dict[str, Any]:
    """Build the exact eleven-claim payload from the credential's OWN snapshot.

    Every claim is read from an immutable column on this row. Nothing is
    resolved through `badge_assignment`, `registration`, or
    `ParticipantEventPseudonym` at this point -- those rows are mutable, and
    reading them here would mean a later assignment change silently altered
    the bytes an already-issued credential reproduces.
    """
    return {
        "apc": credential.access_profile_code,
        "bai": credential.badge_assignment_public_reference,
        "btc": credential.badge_type_code,
        "cv": credential.credential_version,
        "eid": credential.event_code,
        "exp": _epoch(credential.valid_until),
        "jti": credential.jti,
        "n": credential.nonce,
        "nbf": _epoch(credential.valid_from),
        "pid": credential.participant_event_pseudonym,
        "v": credential.payload_version,
    }


def reconstruct_pass_token(credential: DigitalEntryPass) -> str:
    """Rebuild the originally issued compact JWS, byte-for-byte.

    Deterministic and key-free: the header follows from `signing_key_id`, the
    payload follows from this row's immutable claim snapshot, and the
    signature was persisted at issuance. Nothing here calls the signing
    provider, so display keeps working after the private key is gone.

    The returned value is credential material. Callers must never log it,
    place it in a URL, or put it in an audit summary.
    """
    if not credential.signature_hex:
        raise PassConfigurationError(
            "This Digital Entry Pass has no stored signature and cannot be reconstructed."
        )
    try:
        signature = bytes.fromhex(credential.signature_hex)
    except ValueError as exc:
        raise PassConfigurationError("Stored signature material is malformed.") from exc
    if len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        raise PassConfigurationError("Stored signature material has the wrong length.")

    header_bytes = canonical_header_bytes(key_id=credential.signing_key_id)
    payload_bytes = canonical_payload_bytes(build_pass_claims(credential))
    signing_input_bytes = signing_input(header_bytes=header_bytes, payload_bytes=payload_bytes)
    return f"{signing_input_bytes.decode('ascii')}.{b64url_encode(signature)}"


def payload_hash_of(compact_jws: str) -> str:
    """SHA-256 over a compact JWS's signing input (its first two segments)."""
    header_segment, payload_segment, _ = compact_jws.split(".")
    return payload_hash(f"{header_segment}.{payload_segment}".encode("ascii"))


def require_aligned_signing_key(provider=None) -> tuple[str, VerificationKey]:
    """Return the current `(kid, VerificationKey)` only if everything aligns.

    Fails closed on every mismatch. A credential signed by a key whose public
    half is not the published active one would verify nowhere, so this runs
    before issuance rather than leaving the discovery to a gate operator.
    """
    provider = provider or get_signing_key_provider()
    try:
        key_id = provider.current_signing_key_id()
    except Exception as exc:  # noqa: BLE001 - provider failure is fatal
        raise PassConfigurationError("The signing provider is unavailable.") from exc
    if not is_valid_key_id(key_id):
        raise PassConfigurationError("The configured signing key identifier is malformed.")

    active_key = VerificationKey.objects.filter(status=VerificationKeyStatus.ACTIVE).first()
    if active_key is None:
        raise PassConfigurationError("No ACTIVE verification key is published.")
    if active_key.key_id != key_id:
        raise PassConfigurationError(
            "The signing provider's current key does not match the active verification key."
        )
    if active_key.algorithm != ES256:
        raise PassConfigurationError("The active verification key is not an ES256 key.")

    now = timezone.now()
    if active_key.not_before is not None and now < active_key.not_before:
        raise PassConfigurationError("The active verification key is not yet valid.")
    if active_key.not_after is not None and now >= active_key.not_after:
        raise PassConfigurationError("The active verification key has expired.")

    try:
        provider_der = canonical_public_key_der(provider.public_key_pem(key_id))
    except PublicKeyFormatError as exc:
        raise PassConfigurationError(
            "The signing provider's public key is not a valid ES256 public key."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - provider failure is fatal
        raise PassConfigurationError("The signing provider's public key is unavailable.") from exc

    if provider_der != stored_public_key_der(active_key):
        raise PassConfigurationError(
            "The signing provider's public key does not match the active verification key."
        )
    return key_id, active_key


def stored_public_key_der(key: VerificationKey) -> bytes:
    """Canonical DER for a stored key, recomputed if the column is empty."""
    import base64

    if key.public_key_der_b64:
        return base64.b64decode(key.public_key_der_b64)
    return canonical_public_key_der(key.public_key_pem)


def _get_or_create_pseudonym(registration: Registration) -> ParticipantEventPseudonym:
    if registration.person_id is None:
        raise PassConfigurationError(
            "A Digital Entry Pass requires a registration linked to a person."
        )
    existing = ParticipantEventPseudonym.objects.filter(
        person_id=registration.person_id, event_edition_id=registration.event_edition_id
    ).first()
    if existing is not None:
        return existing
    for _attempt in range(5):
        try:
            with transaction.atomic():
                return ParticipantEventPseudonym.objects.create(
                    person_id=registration.person_id,
                    event_edition_id=registration.event_edition_id,
                    pseudonym=_opaque_id(),
                )
        except IntegrityError:
            existing = ParticipantEventPseudonym.objects.filter(
                person_id=registration.person_id,
                event_edition_id=registration.event_edition_id,
            ).first()
            if existing is not None:
                return existing
    raise PassConfigurationError("Could not allocate an event pseudonym.")


# ---------------------------------------------------------------------------
# Validity window
# ---------------------------------------------------------------------------


def compute_validity_window(event_edition) -> tuple[datetime, datetime]:
    """Derive `valid_from`/`valid_until` from the EventEdition window.

    Margins default to zero (the approved Phase 3 default): the credential
    is valid exactly for the configured event window unless an operator
    configures a margin. Per-Badge-Type validity policy is deliberately not
    implemented in Phase 3.
    """
    before = int(getattr(settings, "PASS_VALIDITY_MARGIN_BEFORE_SECONDS", 0))
    after = int(getattr(settings, "PASS_VALIDITY_MARGIN_AFTER_SECONDS", 0))
    valid_from = event_edition.starts_at - timedelta(seconds=before)
    valid_until = event_edition.ends_at + timedelta(seconds=after)
    if valid_until <= valid_from:
        raise PassConfigurationError(
            "The configured event window and margins produce an empty validity interval."
        )
    return valid_from, valid_until


# ---------------------------------------------------------------------------
# Series allocation
# ---------------------------------------------------------------------------


def _get_or_create_series(registration: Registration, *, actor) -> PassCredentialSeries:
    """Return the registration's series, creating it on first issue.

    The fallback reference relies on the unique database constraint plus a
    bounded retry -- a 40-bit reference has a small but finite collision
    chance, and it is handled as an ordinary event rather than assumed away.
    """
    existing = PassCredentialSeries.objects.filter(registration=registration).first()
    if existing is not None:
        return existing
    last_error: Exception | None = None
    for _attempt in range(5):
        try:
            with transaction.atomic():
                return PassCredentialSeries.objects.create(
                    registration=registration,
                    public_id=_opaque_id(),
                    fallback_reference=references.generate_fallback_reference(),
                    created_by=actor if getattr(actor, "pk", None) else None,
                )
        except IntegrityError as exc:
            last_error = exc
            existing = PassCredentialSeries.objects.filter(registration=registration).first()
            if existing is not None:
                return existing
    raise PassConfigurationError(
        "Could not allocate a unique credential series reference."
    ) from last_error


def _allocate_credential_version(series: PassCredentialSeries) -> int:
    """Allocate the next credential version under a row lock.

    Mirrors the accepted concurrency-safe sequence pattern used by
    `apps.registrations.services.allocate_registration_reference`: lock the
    owning row, read, increment, save -- so two concurrent callers can never
    observe or consume the same number.
    """
    locked = PassCredentialSeries.objects.select_for_update().get(pk=series.pk)
    allocated = locked.next_credential_version
    locked.next_credential_version = allocated + 1
    locked.save(update_fields=["next_credential_version", "updated_at"])
    return allocated


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleOutcome:
    """The result of a lifecycle command, and whether it was a replay."""

    credential: DigitalEntryPass | None
    replayed: bool = False


class OperationConflictError(PassServiceError):
    """Raised when an `operation_id` is reused for a DIFFERENT command.

    Deliberately not a replay: returning the first command's outcome for a
    second, different command would report a suspend as if it were a revoke.
    """


#: Advisory-lock class id for lifecycle idempotency. Distinct from every
#: other class id in the project so a pass command can never contend with an
#: unrelated advisory lock (ADR-0007's namespacing convention).
_IDEMPOTENCY_LOCK_CLASSID = 0x42444745  # "BDGE"


#: Bounded reason text is persisted in a `CharField(max_length=300)`. The
#: fingerprint must hash the value that will ACTUALLY be stored, not the raw
#: input -- otherwise two requests that persist identical text could hash
#: differently (trailing whitespace) or two that persist different text could
#: hash identically (truncation).
REASON_TEXT_MAX_LENGTH = 300


def normalize_reason_text(value: object) -> str:
    """Return the exact bounded reason text that will be persisted.

    Used for BOTH the stored column and the command fingerprint, from one
    place, so the two can never diverge.
    """
    if value is None:
        return ""
    return str(value).strip()[:REASON_TEXT_MAX_LENGTH]


def command_fingerprint(
    *,
    operation_type: str,
    actor,
    target_type: str,
    target_id: str,
    parameters: dict[str, Any] | None = None,
) -> str:
    """Canonical SHA-256 binding an `operation_id` to one exact command.

    Covers the operation type, the acting user, the exact target, and every
    **material** request value -- reason codes, reason text, expected lock
    versions, and the expected `jti`. Two requests are the same command only
    if all of them agree.

    Reason text is included deliberately. Two suspend commands that differ
    only in their recorded justification are different commands: replaying
    one as the other would attribute the wrong reason to an operator's
    action in the audit trail.

    Text values are normalized through `normalize_reason_text` before
    hashing, using the exact value that will be persisted.
    """
    import hashlib
    import json

    actor_id = str(getattr(actor, "pk", "") or "")
    normalized_parameters = {
        key: (normalize_reason_text(value) if key.endswith("_text") else value)
        for key, value in (parameters or {}).items()
    }
    canonical = json.dumps(
        {
            "type": operation_type,
            "actor": actor_id,
            "target_type": target_type,
            "target_id": target_id,
            "parameters": normalized_parameters,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _acquire_idempotency_lock(operation_id: str) -> None:
    """Serialize concurrent attempts that share one `operation_id`.

    Without this, two simultaneous requests both miss the existence check and
    then race to insert, so one wins and the other surfaces a raw
    `IntegrityError` instead of a clean replay. The lock is transaction
    scoped, so it releases on commit or rollback with no cleanup path.
    """
    import hashlib

    from django.db import connection

    from apps.core.concurrency import lock_key, order_lock_keys

    digest = hashlib.sha256(operation_id.encode("utf-8")).digest()
    pairs = order_lock_keys([lock_key(_IDEMPOTENCY_LOCK_CLASSID, digest)])
    with connection.cursor() as cursor:
        for classid, objid in pairs:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [classid, objid])


def _existing_operation(operation_id: str) -> PassLifecycleOperation | None:
    return PassLifecycleOperation.objects.filter(operation_id=operation_id).first()


def _match_existing_operation(operation_id: str, fingerprint: str) -> PassLifecycleOperation | None:
    """Return the prior operation only if it is the SAME command.

    A row whose fingerprint differs means the identifier was reused across
    commands; that is a conflict, and returning its outcome would be
    reporting the wrong thing.
    """
    existing = _existing_operation(operation_id)
    if existing is None:
        return None
    if not existing.command_fingerprint:
        # A legacy row written before command binding existed. Its original
        # command cannot be reconstructed, so it must NOT be matched against
        # an arbitrary new one -- an unrestricted replay is exactly the hole
        # fingerprinting closes. Reuse of such an identifier is a conflict.
        raise OperationConflictError(
            "This operation identifier predates command binding and cannot be replayed."
        )
    if existing.command_fingerprint != fingerprint:
        raise OperationConflictError(
            "This operation identifier was already used for a different command."
        )
    return existing


def _require_operation_id(operation_id: object) -> str:
    if not isinstance(operation_id, str) or not 16 <= len(operation_id) <= 64:
        raise PassServiceError("A lifecycle operation identifier is required.")
    return operation_id


def _record_operation(
    *,
    operation_id: str,
    operation_type: str,
    series: PassCredentialSeries | None = None,
    previous_pass: DigitalEntryPass | None = None,
    resulting_pass: DigitalEntryPass | None = None,
    verification_key: VerificationKey | None = None,
    actor,
    fingerprint: str = "",
    target_type: str = "",
    target_id: str = "",
) -> None:
    PassLifecycleOperation.objects.create(
        operation_id=operation_id,
        operation_type=operation_type,
        command_fingerprint=fingerprint,
        target_type=target_type,
        target_id=target_id,
        series=series,
        previous_pass=previous_pass,
        resulting_pass=resulting_pass,
        verification_key=verification_key,
        performed_by=actor if getattr(actor, "pk", None) else None,
    )


def _audit(
    *,
    action_code: str,
    credential: DigitalEntryPass | None,
    actor,
    result: str = "SUCCESS",
    reason_code: str | None = None,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    target_type: str = "DigitalEntryPass",
    target_uuid=None,
    event_edition_id=None,
    network_fingerprint: str = "",
) -> None:
    """Write one audit record inside the caller's transaction.

    Summaries carry credential state only -- never a raw QR value, a signing
    input, a signature, key material, a clear identity value, or a full
    fallback reference.
    """
    PersistentAuditRecorder().record(
        AuditRecord(
            actor_type="OPERATIONAL_USER" if getattr(actor, "pk", None) else "SYSTEM",
            actor_user_id=getattr(actor, "pk", None),
            action_code=action_code,
            target_type=target_type,
            target_uuid=target_uuid if target_uuid is not None else getattr(credential, "pk", None),
            event_edition_id=event_edition_id or getattr(credential, "event_edition_id", None),
            result=result,
            reason_code=reason_code,
            before_summary=before,
            after_summary=after,
            network_fingerprint=network_fingerprint,
        )
    )


def _credential_summary(credential: DigitalEntryPass) -> dict[str, Any]:
    """A bounded, non-sensitive summary safe for an audit payload."""
    return {
        "jti": credential.jti,
        "credential_version": credential.credential_version,
        "status": credential.status,
        "signing_key_id": credential.signing_key_id,
        "payload_version": credential.payload_version,
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _current_assignments(registration: Registration):
    role = (
        ParticipantRoleAssignment.objects.filter(
            registration=registration, status=AssignmentStatus.CURRENT
        )
        .order_by("-effective_from")
        .first()
    )
    badge = BadgeTypeAssignment.objects.filter(
        registration=registration, status=AssignmentStatus.CURRENT
    ).first()
    access = (
        AccessProfileAssignment.objects.filter(
            registration=registration, status=AssignmentStatus.CURRENT
        )
        .order_by("-effective_from")
        .first()
    )
    return role, badge, access


def _issue_credential_row(
    *,
    series: PassCredentialSeries,
    registration: Registration,
    actor,
    reason_code: str,
    reason_text: str,
) -> DigitalEntryPass:
    """Create one INACTIVE credential version. Caller holds the series lock."""
    from apps.accreditation.services import evaluate_eligibility

    verdict = evaluate_eligibility(registration)
    if not verdict["eligible"]:
        raise PassNotEligibleError(verdict["reason"])

    role, badge, access = _current_assignments(registration)
    if role is None or badge is None or access is None:
        # Defensive: `evaluate_eligibility` already requires all three, so
        # reaching here means the state changed underneath us.
        raise PassNotEligibleError("NO_CURRENT_ASSIGNMENTS")
    if not badge.public_reference:
        raise PassConfigurationError("The current badge assignment has no public reference.")

    pseudonym = _get_or_create_pseudonym(registration)
    valid_from, valid_until = compute_validity_window(registration.event_edition)

    provider = get_signing_key_provider()
    key_id, active_key = require_aligned_signing_key(provider)

    credential_version = _allocate_credential_version(series)
    credential = DigitalEntryPass.objects.create(
        series=series,
        registration=registration,
        event_edition=registration.event_edition,
        credential_version=credential_version,
        jti=_opaque_id(),
        status=DigitalEntryPassStatus.INACTIVE,
        role_assignment=role,
        badge_assignment=badge,
        access_assignment=access,
        event_code=registration.event_edition.code,
        badge_type_code=badge.badge_type.code,
        access_profile_code=access.access_profile.code,
        badge_assignment_public_reference=badge.public_reference,
        participant_event_pseudonym=pseudonym.pseudonym,
        payload_version=int(getattr(settings, "QR_PAYLOAD_VERSION", 1)),
        signing_key_id=key_id,
        payload_hash="",
        signature_hex="",
        nonce=_new_nonce(),
        valid_from=valid_from,
        valid_until=valid_until,
        reason_code=reason_code,
        reason_text=reason_text,
        issued_by=actor if getattr(actor, "pk", None) else None,
    )

    # Sign EXACTLY ONCE, here, and persist the signature. Display later
    # reconstructs the identical compact JWS from this row without the
    # private key, so an issued pass survives key retirement and the removal
    # of the private material.
    #
    # Alignment passing does not guarantee signing will succeed: a provider
    # can hold the right public key and still fail on `signing_algorithm()`,
    # on `sign()`, or by returning a badly shaped signature. Those are the
    # provider's own safe exception family, and they are normalized here into
    # a domain error so the caller sees a safe message instead of a 500 --
    # and so the whole transaction rolls back, leaving no half-issued
    # credential, no lifecycle row, no success audit, and no outbox event.
    try:
        assembled = assemble_compact_jws(build_pass_claims(credential), key_id=key_id)
    except SigningKeyProviderError as exc:
        raise PassConfigurationError(
            "The signing provider could not sign this Digital Entry Pass. No credential was issued."
        ) from exc
    credential.payload_hash = assembled.payload_hash
    credential.signature_hex = assembled.signature.hex()
    credential.save(update_fields=["payload_hash", "signature_hex", "updated_at"])

    # Verify what we just produced against the PUBLISHED public key before
    # the transaction commits. A credential that cannot be verified must
    # never reach the database: the failure surfaces here, at issue time,
    # rather than at a gate.
    _assert_issued_credential_verifies(credential, active_key)
    return credential


def _assert_issued_credential_verifies(
    credential: DigitalEntryPass, verification_key: VerificationKey
) -> None:
    """Fail closed unless the freshly issued token verifies with the public key."""
    from joserfc.jwk import ECKey
    from joserfc.jws import deserialize_compact

    token = reconstruct_pass_token(credential)
    try:
        public_key = ECKey.import_key(verification_key.public_key_pem.encode("utf-8"))
        deserialize_compact(token, public_key, algorithms=[SIGNING_ALGORITHM])
    except Exception as exc:  # noqa: BLE001 - any failure is fatal here
        raise PassConfigurationError(
            "The issued Digital Entry Pass did not verify against the published "
            "verification key and was not stored."
        ) from exc

    expected_hash = payload_hash_of(token)
    if expected_hash != credential.payload_hash:
        raise PassConfigurationError(
            "The issued Digital Entry Pass payload hash did not reconcile."
        )


def generate_pass(
    *,
    registration: Registration,
    actor,
    operation_id: str,
    reason_code: str = PassReasonCode.INITIAL_ISSUE,
    reason_text: str = "",
) -> LifecycleOutcome:
    """Create the first credential for a registration context.

    Idempotent on `operation_id`. Fails closed when the registration is not
    eligible, when no ACTIVE verification key is published, or when the
    event window cannot produce a valid interval.
    """
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=PassLifecycleOperationType.GENERATE,
        actor=actor,
        target_type="Registration",
        target_id=str(registration.pk),
        parameters={"reason_code": reason_code, "reason_text": reason_text},
    )

    try:
        with transaction.atomic():
            # Serialize same-identifier attempts BEFORE the existence check,
            # so two simultaneous requests resolve as one original plus one
            # clean replay instead of racing into an IntegrityError.
            _acquire_idempotency_lock(operation_id)
            replay = _match_existing_operation(operation_id, fingerprint)
            if replay is not None:
                return LifecycleOutcome(credential=replay.resulting_pass, replayed=True)

            locked_registration = Registration.objects.select_for_update().get(pk=registration.pk)
            series = _get_or_create_series(locked_registration, actor=actor)
            current = (
                DigitalEntryPass.objects.select_for_update()
                .filter(registration=locked_registration, status__in=NON_TERMINAL_STATUSES)
                .first()
            )
            if current is not None:
                raise PassStateError(
                    "This registration already has a current Digital Entry Pass.",
                    code="ALREADY_CURRENT",
                )
            credential = _issue_credential_row(
                series=series,
                registration=locked_registration,
                actor=actor,
                reason_code=reason_code,
                reason_text=reason_text,
            )
            _record_generation_evidence(
                operation_id=operation_id,
                series=series,
                credential=credential,
                registration=locked_registration,
                actor=actor,
                reason_code=reason_code,
                fingerprint=fingerprint,
            )
    except PassNotEligibleError as exc:
        # The denial audit is written in its OWN transaction, deliberately.
        # Recording it inside the failed one would roll it back with
        # everything else, and a refusal that leaves no evidence is worse
        # than no refusal at all.
        _record_generation_denial(registration=registration, actor=actor, reason=exc.reason)
        raise
    return LifecycleOutcome(credential=credential)


def _record_generation_denial(*, registration: Registration, actor, reason: str) -> None:
    with transaction.atomic():
        _audit(
            action_code=action_codes.CREDENTIAL_GENERATION_BLOCKED,
            credential=None,
            actor=actor,
            result="DENIED",
            reason_code=reason,
            target_type="Registration",
            target_uuid=registration.pk,
            event_edition_id=registration.event_edition_id,
            after={"reason": reason},
        )


def _record_generation_evidence(
    *,
    operation_id: str,
    series: PassCredentialSeries,
    credential: DigitalEntryPass,
    registration: Registration,
    actor,
    reason_code: str,
    fingerprint: str = "",
) -> None:
    """Idempotency row, audit record, and outbox row -- all in the caller's transaction."""
    _record_operation(
        operation_id=operation_id,
        operation_type=PassLifecycleOperationType.GENERATE,
        series=series,
        previous_pass=None,
        resulting_pass=credential,
        actor=actor,
        fingerprint=fingerprint,
        target_type="Registration",
        target_id=str(registration.pk),
    )
    _audit(
        action_code=action_codes.CREDENTIAL_GENERATED,
        credential=credential,
        actor=actor,
        reason_code=reason_code,
        after=_credential_summary(credential),
    )
    PersistentOutboxPublisher().enqueue(
        OutboxMessage(
            event_type="badges.pass.generated",
            aggregate_type="DigitalEntryPass",
            aggregate_id=str(credential.pk),
            payload={
                "registration_id": str(registration.pk),
                "credential_version": credential.credential_version,
                "status": credential.status,
            },
        )
    )


# ---------------------------------------------------------------------------
# Simple status transitions
# ---------------------------------------------------------------------------

#: Eligibility reason reported when a Registration Context is no longer an
#: active approved context (withdrawn, operationally cancelled, superseded,
#: or not approved). The reviewed eligibility boundary's own code.
CONTEXT_NOT_ACTIVE_REASON = "NOT_APPROVED"


def _lock_active_context(registration_id) -> Registration:
    """Lock the Registration row and require an active approved context.

    Raises `PassNotEligibleError` before any credential row is locked or
    changed, so a refusal leaves no lifecycle row, audit record, or outbox
    event behind. Uses the shared definition in `apps.registrations.selectors`.
    """
    from apps.registrations.selectors import is_active_approved_context

    registration = Registration.objects.select_for_update(of=("self",)).get(pk=registration_id)
    if not is_active_approved_context(registration):
        raise PassNotEligibleError(CONTEXT_NOT_ACTIVE_REASON)
    return registration


def _transition(
    *,
    credential: DigitalEntryPass,
    target_status: str,
    actor,
    operation_id: str,
    operation_type: str,
    action_code: str,
    expected_lock_version: int,
    reason_code: str = "",
    reason_text: str = "",
    outbox_event: str | None = None,
    require_active_context: bool = False,
) -> LifecycleOutcome:
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=operation_type,
        actor=actor,
        target_type="DigitalEntryPass",
        target_id=str(credential.pk),
        parameters={
            "target_status": target_status,
            "expected_lock_version": expected_lock_version,
            "reason_code": reason_code,
            "reason_text": reason_text,
        },
    )

    with transaction.atomic():
        _acquire_idempotency_lock(operation_id)
        replay = _match_existing_operation(operation_id, fingerprint)
        if replay is not None:
            return LifecycleOutcome(credential=replay.resulting_pass, replayed=True)

        if require_active_context:
            # Re-checked HERE, under the Registration row lock, never from the
            # possibly stale object the view loaded (P8-06). The Registration
            # is locked BEFORE the credential -- the order `generate_pass` and
            # `replace_pass` use and the one participant withdrawal and
            # operational cancellation serialize on -- so a concurrent
            # withdrawal either commits first and is seen here, or waits
            # until this transaction ends.
            _lock_active_context(credential.registration_id)

        locked = DigitalEntryPass.objects.select_for_update().get(pk=credential.pk)
        if locked.version != expected_lock_version:
            raise PassConcurrencyError("This Digital Entry Pass changed since it was loaded.")
        _require_transition(locked.status, target_status)

        before = _credential_summary(locked)
        now = timezone.now()
        fields = ["status", "version", "status_reason_code", "status_reason_text", "updated_at"]
        locked.status = target_status
        locked.version = locked.version + 1
        locked.status_reason_code = reason_code
        locked.status_reason_text = reason_text

        if target_status == DigitalEntryPassStatus.ACTIVE:
            if before["status"] == DigitalEntryPassStatus.SUSPENDED:
                locked.resumed_at = now
                locked.resumed_by = actor if getattr(actor, "pk", None) else None
                fields += ["resumed_at", "resumed_by"]
            else:
                locked.activated_at = now
                locked.activated_by = actor if getattr(actor, "pk", None) else None
                fields += ["activated_at", "activated_by"]
        elif target_status == DigitalEntryPassStatus.SUSPENDED:
            locked.suspended_at = now
            locked.suspended_by = actor if getattr(actor, "pk", None) else None
            fields += ["suspended_at", "suspended_by"]
        elif target_status == DigitalEntryPassStatus.REVOKED:
            locked.revoked_at = now
            locked.revoked_by = actor if getattr(actor, "pk", None) else None
            fields += ["revoked_at", "revoked_by"]
        elif target_status == DigitalEntryPassStatus.EXPIRED:
            locked.expired_at = now
            fields += ["expired_at"]

        locked.save(update_fields=sorted(set(fields)))

        _record_operation(
            operation_id=operation_id,
            operation_type=operation_type,
            series=locked.series,
            previous_pass=None,
            resulting_pass=locked,
            actor=actor,
            fingerprint=fingerprint,
            target_type="DigitalEntryPass",
            target_id=str(locked.pk),
        )
        _audit(
            action_code=action_code,
            credential=locked,
            actor=actor,
            reason_code=reason_code or None,
            before=before,
            after=_credential_summary(locked),
        )
        if outbox_event:
            PersistentOutboxPublisher().enqueue(
                OutboxMessage(
                    event_type=outbox_event,
                    aggregate_type="DigitalEntryPass",
                    aggregate_id=str(locked.pk),
                    payload={
                        "registration_id": str(locked.registration_id),
                        "credential_version": locked.credential_version,
                        "status": locked.status,
                    },
                )
            )
    return LifecycleOutcome(credential=locked)


def activate_pass(
    *, credential: DigitalEntryPass, actor, operation_id: str, expected_lock_version: int
) -> LifecycleOutcome:
    """INACTIVE -> ACTIVE. Always explicit; there is no automatic activation.

    Refuses to activate a credential whose validity window has already
    closed, so a stale INACTIVE row can never be activated into usefulness.
    """
    if credential.valid_until <= timezone.now():
        raise PassStateError(
            "This Digital Entry Pass cannot be activated after its validity period has ended.",
            code="VALIDITY_ENDED",
        )
    return _transition(
        credential=credential,
        target_status=DigitalEntryPassStatus.ACTIVE,
        actor=actor,
        operation_id=operation_id,
        operation_type=PassLifecycleOperationType.ACTIVATE,
        action_code=action_codes.CREDENTIAL_ACTIVATED,
        expected_lock_version=expected_lock_version,
        outbox_event="badges.pass.activated",
        # A withdrawn or cancelled context can never gain a usable pass (P8-06).
        require_active_context=True,
    )


def suspend_pass(
    *,
    credential: DigitalEntryPass,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str = "",
) -> LifecycleOutcome:
    """ACTIVE -> SUSPENDED. A reversible hold, never a revocation."""
    return _transition(
        credential=credential,
        target_status=DigitalEntryPassStatus.SUSPENDED,
        actor=actor,
        operation_id=operation_id,
        operation_type=PassLifecycleOperationType.SUSPEND,
        action_code=action_codes.CREDENTIAL_SUSPENDED,
        expected_lock_version=expected_lock_version,
        reason_code=reason_code,
        reason_text=reason_text,
    )


def resume_pass(
    *,
    credential: DigitalEntryPass,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str = "",
    reason_text: str = "",
) -> LifecycleOutcome:
    """SUSPENDED -> ACTIVE. An explicit authorized action, never automatic."""
    if credential.status != DigitalEntryPassStatus.SUSPENDED:
        raise PassStateError("Only a suspended Digital Entry Pass can be resumed.")
    if credential.valid_until <= timezone.now():
        raise PassStateError(
            "This Digital Entry Pass cannot be resumed after its validity period has ended.",
            code="VALIDITY_ENDED",
        )
    return _transition(
        credential=credential,
        target_status=DigitalEntryPassStatus.ACTIVE,
        actor=actor,
        operation_id=operation_id,
        operation_type=PassLifecycleOperationType.RESUME,
        action_code=action_codes.CREDENTIAL_RESUMED,
        expected_lock_version=expected_lock_version,
        reason_code=reason_code,
        reason_text=reason_text,
        outbox_event="badges.pass.resumed",
        require_active_context=True,
    )


def revoke_pass(
    *,
    credential: DigitalEntryPass,
    actor,
    operation_id: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str = "",
) -> LifecycleOutcome:
    """Any non-terminal status -> REVOKED. Terminal and irreversible."""
    return _transition(
        credential=credential,
        target_status=DigitalEntryPassStatus.REVOKED,
        actor=actor,
        operation_id=operation_id,
        operation_type=PassLifecycleOperationType.REVOKE,
        action_code=action_codes.CREDENTIAL_REVOKED,
        expected_lock_version=expected_lock_version,
        reason_code=reason_code,
        reason_text=reason_text,
        outbox_event="badges.pass.revoked",
    )


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


def replace_pass(
    *,
    credential: DigitalEntryPass,
    actor,
    operation_id: str,
    expected_jti: str,
    expected_lock_version: int,
    reason_code: str,
    reason_text: str = "",
) -> LifecycleOutcome:
    """Supersede the current credential with a fresh INACTIVE one.

    Three independent mechanisms prevent a double replacement, and the
    `expected_jti` check is essential rather than decorative: a newly
    created credential starts at lock version 1, so a stale
    `expected_lock_version` alone would match the replacement row that a
    competing request just created. Requiring the caller to name the exact
    credential it intends to supersede closes that window.
    """
    operation_id = _require_operation_id(operation_id)
    reason_text = normalize_reason_text(reason_text)
    fingerprint = command_fingerprint(
        operation_type=PassLifecycleOperationType.REPLACE,
        actor=actor,
        target_type="DigitalEntryPass",
        target_id=str(credential.pk),
        parameters={
            "expected_jti": expected_jti,
            "expected_lock_version": expected_lock_version,
            "reason_code": reason_code,
            "reason_text": reason_text,
        },
    )

    with transaction.atomic():
        _acquire_idempotency_lock(operation_id)
        replay = _match_existing_operation(operation_id, fingerprint)
        if replay is not None:
            return LifecycleOutcome(credential=replay.resulting_pass, replayed=True)

        # Lock order: Registration, then series, then credential -- the
        # Registration row FIRST, exactly as `generate_pass` and the
        # activation/resumption path do (P8-06), so no two pass-lifecycle
        # commands for one context can take these rows in opposite orders.
        # A credential's registration never changes, so reading it from the
        # caller's object is safe.
        registration = Registration.objects.select_for_update().get(pk=credential.registration_id)
        series = PassCredentialSeries.objects.select_for_update().get(pk=credential.series_id)
        current = (
            DigitalEntryPass.objects.select_for_update()
            .filter(series=series, status__in=NON_TERMINAL_STATUSES)
            .first()
        )
        if current is None:
            raise PassStateError(
                "This registration has no current Digital Entry Pass to replace.",
                code="NO_CURRENT_PASS",
            )
        if current.jti != expected_jti:
            raise PassConcurrencyError(
                "The Digital Entry Pass you are replacing is no longer the current one."
            )
        if current.version != expected_lock_version:
            raise PassConcurrencyError("This Digital Entry Pass changed since it was loaded.")
        _require_transition(current.status, DigitalEntryPassStatus.REPLACED)

        before = _credential_summary(current)

        # Order matters and is not cosmetic. `bdg_pass_one_current_uq`
        # permits exactly one non-terminal credential per registration, so
        # the outgoing credential must leave that set BEFORE the replacement
        # enters it. Issuing first would collide with the constraint.
        now = timezone.now()
        current.status = DigitalEntryPassStatus.REPLACED
        current.version = current.version + 1
        current.replaced_at = now
        current.replaced_by_user = actor if getattr(actor, "pk", None) else None
        current.status_reason_code = reason_code
        current.status_reason_text = reason_text
        current.save(
            update_fields=[
                "status",
                "version",
                "replaced_at",
                "replaced_by_user",
                "status_reason_code",
                "status_reason_text",
                "updated_at",
            ]
        )

        replacement = _issue_credential_row(
            series=series,
            registration=registration,
            actor=actor,
            reason_code=PassReasonCode.REPLACEMENT_ISSUE,
            reason_text=reason_text,
        )

        # Link the chain only once the replacement exists. If issuing fails
        # -- for example because eligibility lapsed -- the whole transaction
        # rolls back and the outgoing credential stays current and usable.
        current.replaced_by = replacement
        current.save(update_fields=["replaced_by", "updated_at"])

        _record_operation(
            operation_id=operation_id,
            operation_type=PassLifecycleOperationType.REPLACE,
            series=series,
            previous_pass=current,
            resulting_pass=replacement,
            actor=actor,
            fingerprint=fingerprint,
            target_type="DigitalEntryPass",
            target_id=str(credential.pk),
        )
        _audit(
            action_code=action_codes.CREDENTIAL_REPLACED,
            credential=replacement,
            actor=actor,
            reason_code=reason_code,
            before=before,
            after=_credential_summary(replacement),
        )
        PersistentOutboxPublisher().enqueue(
            OutboxMessage(
                event_type="badges.pass.replaced",
                aggregate_type="DigitalEntryPass",
                aggregate_id=str(replacement.pk),
                payload={
                    "registration_id": str(registration.pk),
                    "credential_version": replacement.credential_version,
                    "status": replacement.status,
                },
            )
        )
    return LifecycleOutcome(credential=replacement)


# ---------------------------------------------------------------------------
# Optional expiry persistence (reporting only -- never load-bearing)
# ---------------------------------------------------------------------------


def expire_due_passes(*, now: datetime | None = None, limit: int = 500) -> int:
    """Persist EXPIRED for credentials whose window has closed.

    Purely for reporting and queue hygiene. Verification already derives
    expiry from `valid_until` on every call, so if this never runs, no
    admission decision changes. Idempotent and safely re-runnable.
    """
    now = now or timezone.now()
    due = list(
        DigitalEntryPass.objects.filter(
            status__in=NON_TERMINAL_STATUSES, valid_until__lte=now
        ).order_by("valid_until")[:limit]
    )
    persisted = 0
    for credential in due:
        try:
            with transaction.atomic():
                locked = DigitalEntryPass.objects.select_for_update().get(pk=credential.pk)
                if locked.status not in NON_TERMINAL_STATUSES or locked.valid_until > now:
                    continue
                before = _credential_summary(locked)
                locked.status = DigitalEntryPassStatus.EXPIRED
                locked.version = locked.version + 1
                locked.expired_at = now
                locked.status_reason_code = PassReasonCode.VALIDITY_ENDED
                locked.save(
                    update_fields=[
                        "status",
                        "version",
                        "expired_at",
                        "status_reason_code",
                        "updated_at",
                    ]
                )
                _audit(
                    action_code=action_codes.CREDENTIAL_EXPIRY_PERSISTED,
                    credential=locked,
                    actor=None,
                    reason_code=PassReasonCode.VALIDITY_ENDED,
                    before=before,
                    after=_credential_summary(locked),
                )
                persisted += 1
        except DigitalEntryPass.DoesNotExist:  # pragma: no cover - defensive
            continue
    return persisted


# ---------------------------------------------------------------------------
# Token issue for a participant
# ---------------------------------------------------------------------------


def credential_is_time_valid(credential: DigitalEntryPass, *, now: datetime | None = None) -> bool:
    """True if `now` falls inside the credential's own validity window.

    Uses the SAME bounded clock-skew tolerance as verification
    (`QR_CLOCK_SKEW_SECONDS`), so the participant surface and the gate can
    never disagree about whether a pass is currently within its window.
    """
    now = now or timezone.now()
    skew = int(getattr(settings, "QR_CLOCK_SKEW_SECONDS", 60))
    if now < credential.valid_from and (credential.valid_from - now).total_seconds() > skew:
        return False
    if now >= credential.valid_until and (now - credential.valid_until).total_seconds() > skew:
        return False
    return True


def pass_exposes_usable_qr(credential: DigitalEntryPass, *, now: datetime | None = None) -> bool:
    """True only when a usable QR may be exposed for this credential.

    Two conditions, both required: the stored status is ACTIVE, **and** the
    validity window is currently open. The second is not redundant -- the
    expiry sweep is deliberately not load-bearing, so a row can legitimately
    still read ACTIVE after its window closed. Verification already fails
    closed on that; the participant surface must too, or a participant would
    be shown a QR that every gate rejects.

    A third condition (P8-06): the linked Registration Context must still be
    an active approved context. Withdrawal and operational cancellation do
    not revoke the credential -- its history is preserved and verification
    keeps denying it -- but a QR that every gate would refuse must never be
    presented as usable.
    """
    from apps.registrations.selectors import is_active_approved_context

    return (
        credential is not None
        and credential.status == DigitalEntryPassStatus.ACTIVE
        and credential_is_time_valid(credential, now=now)
        and is_active_approved_context(credential.registration)
    )


def issue_pass_token(credential: DigitalEntryPass, *, now: datetime | None = None) -> str:
    """Return the originally issued compact JWS for a usable ACTIVE credential.

    ACTIVE-only is a hard invariant with no configuration escape: there is
    deliberately no pre-activation flag. INACTIVE, SUSPENDED, REVOKED,
    REPLACED, and EXPIRED never yield a usable token, whatever the settings
    say -- and neither does an ACTIVE row whose validity window has ended.

    This reconstructs the stored artefact; it never signs, and it never
    mutates state. The returned value is credential material and is never
    logged, audited, or placed in a URL by any caller in this codebase.
    """
    if credential.status != DigitalEntryPassStatus.ACTIVE:
        raise PassStateError(
            "A usable QR credential is exposed only for an active Digital Entry Pass."
        )
    if not credential_is_time_valid(credential, now=now):
        raise PassStateError(
            "A usable QR credential is exposed only inside the credential's validity period."
        )
    # Enforced independently of `pass_exposes_usable_qr` (P8-06), and read
    # from the database rather than trusting a Registration the caller may
    # have loaded before a withdrawal or cancellation.
    from apps.registrations.selectors import is_active_approved_context

    if not is_active_approved_context(Registration.objects.get(pk=credential.registration_id)):
        raise PassStateError(
            "A usable QR credential is exposed only for an active approved registration."
        )
    return reconstruct_pass_token(credential)


# ---------------------------------------------------------------------------
# Verification-key lifecycle (PUBLIC key material only)
# ---------------------------------------------------------------------------


def publish_verification_key(
    *,
    key_id: str,
    public_key_pem: str,
    actor,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> VerificationKey:
    """Publish a PUBLIC verification key in PENDING state.

    Every key is parsed with `cryptography` before storage: only an EC public
    key on NIST P-256 is accepted, and the canonical DER SubjectPublicKeyInfo
    is stored alongside the PEM. A private key, an RSA key, an EC key on a
    different curve, or malformed PEM is refused here rather than discovered
    later at a gate.
    """
    if not is_valid_key_id(key_id):
        raise VerificationKeyError("Malformed verification key identifier.", code="INVALID_KEY_ID")
    try:
        der = canonical_public_key_der(public_key_pem)
    except PublicKeyFormatError as exc:
        # One safe message for every rejection reason: an operator learns the
        # key was refused, not which parser branch refused it.
        raise VerificationKeyError(
            "A verification key must be a PEM-encoded EC public key on the "
            "NIST P-256 curve, containing public key material only.",
            code="INVALID_PUBLIC_KEY",
        ) from exc
    if VerificationKey.objects.filter(key_id=key_id).exists():
        raise VerificationKeyError(
            "This verification key identifier already exists.", code="DUPLICATE_KEY_ID"
        )

    import base64

    with transaction.atomic():
        key = VerificationKey.objects.create(
            key_id=key_id,
            algorithm=ES256,
            public_key_pem=public_key_pem,
            public_key_der_b64=base64.b64encode(der).decode("ascii"),
            public_key_fingerprint=public_key_fingerprint(der),
            status=VerificationKeyStatus.PENDING,
            not_before=not_before,
            not_after=not_after,
            published_at=timezone.now(),
            published_by=actor,
        )
        _audit(
            action_code=action_codes.VERIFICATION_KEY_PUBLISHED,
            credential=None,
            actor=actor,
            target_type="VerificationKey",
            target_uuid=key.pk,
            after={
                "key_id": key.key_id,
                "status": key.status,
                "algorithm": key.algorithm,
                "fingerprint": key.public_key_fingerprint,
            },
        )
    return key


def promote_verification_key(
    *, key: VerificationKey, actor, operation_id: str | None = None, provider=None
) -> VerificationKey:
    """PENDING -> ACTIVE, retiring whichever key was previously ACTIVE.

    Promotion requires the signing provider to already hold the matching
    private key: its current `kid` must equal this key's `kid`, and its public
    half must canonicalize to the same DER. Promoting a key the provider
    cannot sign with would mint credentials that verify nowhere.

    The previous key is RETIRED, not REVOKED -- that overlap is exactly what
    lets credentials issued before the rotation keep verifying.
    """
    provider = provider or get_signing_key_provider()
    operation_id = operation_id or new_operation_id()
    fingerprint = command_fingerprint(
        operation_type=PassLifecycleOperationType.KEY_PROMOTE,
        actor=actor,
        target_type="VerificationKey",
        target_id=str(key.pk),
        parameters={},
    )

    with transaction.atomic():
        _acquire_idempotency_lock(operation_id)
        replay = _match_existing_operation(operation_id, fingerprint)
        if replay is not None:
            return VerificationKey.objects.get(pk=key.pk)

        locked = VerificationKey.objects.select_for_update().get(pk=key.pk)
        if locked.status == VerificationKeyStatus.ACTIVE:
            # Idempotent: an identical repeat of a completed promotion is a
            # no-op rather than an error.
            return locked
        if locked.status != VerificationKeyStatus.PENDING:
            raise VerificationKeyError(
                "Only a pending verification key can be promoted.", code="WRONG_STATUS"
            )

        _require_provider_holds_key(locked, provider)

        now = timezone.now()
        previous = (
            VerificationKey.objects.select_for_update()
            .filter(status=VerificationKeyStatus.ACTIVE)
            .first()
        )
        if previous is not None:
            previous.status = VerificationKeyStatus.RETIRED
            previous.retired_at = now
            previous.retired_by = actor
            previous.save(update_fields=["status", "retired_at", "retired_by", "updated_at"])
            _audit(
                action_code=action_codes.VERIFICATION_KEY_RETIRED,
                credential=None,
                actor=actor,
                reason_code=VerificationKeyReasonCode.ROTATION_SUPERSEDED,
                target_type="VerificationKey",
                target_uuid=previous.pk,
                after={"key_id": previous.key_id, "status": previous.status},
            )
        locked.status = VerificationKeyStatus.ACTIVE
        locked.promoted_at = now
        locked.promoted_by = actor
        locked.save(update_fields=["status", "promoted_at", "promoted_by", "updated_at"])
        _record_operation(
            operation_id=operation_id,
            operation_type=PassLifecycleOperationType.KEY_PROMOTE,
            verification_key=locked,
            actor=actor,
            fingerprint=fingerprint,
            target_type="VerificationKey",
            target_id=str(locked.pk),
        )
        _audit(
            action_code=action_codes.VERIFICATION_KEY_PROMOTED,
            credential=None,
            actor=actor,
            target_type="VerificationKey",
            target_uuid=locked.pk,
            after={"key_id": locked.key_id, "status": locked.status},
        )
    return locked


def _require_provider_holds_key(key: VerificationKey, provider) -> None:
    """Fail closed unless the provider can actually sign with this key."""
    try:
        current_kid = provider.current_signing_key_id()
    except Exception as exc:  # noqa: BLE001 - provider failure is fatal
        raise VerificationKeyError(
            "The signing provider is unavailable.", code="PROVIDER_MISMATCH"
        ) from exc
    if current_kid != key.key_id:
        raise VerificationKeyError(
            "The signing provider's current key does not match the key being promoted.",
            code="PROVIDER_MISMATCH",
        )
    try:
        provider_der = canonical_public_key_der(provider.public_key_pem(key.key_id))
    except PublicKeyFormatError as exc:
        raise VerificationKeyError(
            "The signing provider's public key is not a valid ES256 public key.",
            code="PROVIDER_MISMATCH",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - provider failure is fatal
        raise VerificationKeyError(
            "The signing provider's public key is unavailable.", code="PROVIDER_MISMATCH"
        ) from exc
    if provider_der != stored_public_key_der(key):
        raise VerificationKeyError(
            "The signing provider's public key does not match the published key.",
            code="PROVIDER_MISMATCH",
        )


def retire_verification_key(
    *,
    key: VerificationKey,
    actor,
    reason_code: str = "",
    reason_text: str = "",
    operation_id: str | None = None,
) -> VerificationKey:
    """ACTIVE -> RETIRED without promoting a successor.

    A retired key keeps verifying credentials already issued under it while
    they remain within their own validity window. Use revocation, not
    retirement, when a key must stop verifying immediately.
    """
    reason_code = reason_code or VerificationKeyReasonCode.PLANNED_ROTATION
    _require_key_reason_code(reason_code)
    reason_text = normalize_reason_text(reason_text)
    operation_id = operation_id or new_operation_id()
    fingerprint = command_fingerprint(
        operation_type=PassLifecycleOperationType.KEY_RETIRE,
        actor=actor,
        target_type="VerificationKey",
        target_id=str(key.pk),
        parameters={"reason_code": reason_code, "reason_text": reason_text},
    )

    with transaction.atomic():
        _acquire_idempotency_lock(operation_id)
        replay = _match_existing_operation(operation_id, fingerprint)
        if replay is not None:
            return VerificationKey.objects.get(pk=key.pk)

        locked = VerificationKey.objects.select_for_update().get(pk=key.pk)
        if locked.status == VerificationKeyStatus.RETIRED:
            return locked
        if locked.status != VerificationKeyStatus.ACTIVE:
            raise VerificationKeyError(
                "Only an active verification key can be retired.", code="WRONG_STATUS"
            )

        before = {"key_id": locked.key_id, "status": locked.status}
        locked.status = VerificationKeyStatus.RETIRED
        locked.retired_at = timezone.now()
        locked.retired_by = actor
        locked.save(update_fields=["status", "retired_at", "retired_by", "updated_at"])
        _record_operation(
            operation_id=operation_id,
            operation_type=PassLifecycleOperationType.KEY_RETIRE,
            verification_key=locked,
            actor=actor,
            fingerprint=fingerprint,
            target_type="VerificationKey",
            target_id=str(locked.pk),
        )
        _audit(
            action_code=action_codes.VERIFICATION_KEY_RETIRED,
            credential=None,
            actor=actor,
            reason_code=reason_code,
            target_type="VerificationKey",
            target_uuid=locked.pk,
            before=before,
            after={"key_id": locked.key_id, "status": locked.status},
        )
    return locked


def revoke_verification_key(
    *,
    key: VerificationKey,
    actor,
    reason_code: str,
    reason_text: str = "",
    operation_id: str | None = None,
) -> VerificationKey:
    """Emergency revocation.

    Every credential bearing this `kid` fails online verification
    immediately, regardless of its own `exp`. Phase 3 makes no claim about
    offline devices: offline packages, offline verification, and offline
    revocation propagation are Phase 4 and are not implemented here.
    """
    _require_key_reason_code(reason_code)
    reason_text = normalize_reason_text(reason_text)
    operation_id = operation_id or new_operation_id()
    fingerprint = command_fingerprint(
        operation_type=PassLifecycleOperationType.KEY_REVOKE,
        actor=actor,
        target_type="VerificationKey",
        target_id=str(key.pk),
        parameters={"reason_code": reason_code, "reason_text": reason_text},
    )

    with transaction.atomic():
        _acquire_idempotency_lock(operation_id)
        replay = _match_existing_operation(operation_id, fingerprint)
        if replay is not None:
            return VerificationKey.objects.get(pk=key.pk)

        locked = VerificationKey.objects.select_for_update().get(pk=key.pk)
        if locked.status == VerificationKeyStatus.REVOKED:
            return locked

        before = {"key_id": locked.key_id, "status": locked.status}
        locked.status = VerificationKeyStatus.REVOKED
        locked.revoked_at = timezone.now()
        locked.revoked_by = actor
        locked.revocation_reason_code = reason_code
        locked.revocation_reason_text = reason_text
        locked.save(
            update_fields=[
                "status",
                "revoked_at",
                "revoked_by",
                "revocation_reason_code",
                "revocation_reason_text",
                "updated_at",
            ]
        )
        _record_operation(
            operation_id=operation_id,
            operation_type=PassLifecycleOperationType.KEY_REVOKE,
            verification_key=locked,
            actor=actor,
            fingerprint=fingerprint,
            target_type="VerificationKey",
            target_id=str(locked.pk),
        )
        _audit(
            action_code=action_codes.VERIFICATION_KEY_REVOKED,
            credential=None,
            actor=actor,
            reason_code=reason_code,
            target_type="VerificationKey",
            target_uuid=locked.pk,
            before=before,
            after={"key_id": locked.key_id, "status": locked.status},
        )
    return locked


def _require_key_reason_code(reason_code: str) -> None:
    if reason_code not in VerificationKeyReasonCode.values:
        raise VerificationKeyError(
            "Unrecognized verification-key reason code.", code="INVALID_REASON"
        )


def published_verification_key_set() -> list[dict[str, Any]]:
    """The approved public representation of the verification key set.

    Public key material only. Never includes a private key, and never
    includes a credential or participant value.
    """
    keys = VerificationKey.objects.exclude(status=VerificationKeyStatus.PENDING).order_by("key_id")
    return [
        {
            "kid": key.key_id,
            "alg": key.algorithm,
            "status": key.status,
            "public_key_pem": key.public_key_pem,
            "fingerprint": key.public_key_fingerprint,
            "not_before": key.not_before.isoformat() if key.not_before else None,
            "not_after": key.not_after.isoformat() if key.not_after else None,
        }
        for key in keys
    ]


# ---------------------------------------------------------------------------
# Controlled fallback-reference lookup
# ---------------------------------------------------------------------------


def lookup_series_by_fallback_reference(
    *, raw_reference: object, actor, network_identity: str = ""
) -> PassCredentialSeries | None:
    """Resolve a fallback reference to a credential series the actor may see.

    Four properties matter here, and each one closes a real hole:

    1. **Both applicable budgets are charged, independently.** An actor
       budget alone lets one person rotate through accounts on a single
       machine; a network budget alone lets a distributed caller grind from
       many addresses. Neither substitutes for the other, so this is not an
       `if actor ... elif network` -- both are checked whenever they apply.
    2. **Budgets are charged FIRST**, before the check symbol is evaluated.
       Rejecting malformed input for free would leave an unthrottled oracle.
    3. **Scope filtering uses the same primitive as the credential detail
       page.** A match outside the caller's event or organization scope is
       returned as `None` -- indistinguishable from an unknown reference.
    4. **Every attempt is audited** with a MASKED reference and a KEYED
       network fingerprint, never a raw address.

    `network_identity` is the already-normalized, trusted-proxy-resolved
    client address from `apps.core.concurrency`. It is used to derive a
    keyed fingerprint and is never itself stored.

    Returns the series only. It grants nothing: the caller must still run the
    full server-side verification decision, and this writes no entry event and
    makes no admission decision.
    """
    network_fingerprint = _network_fingerprint_for(network_identity)

    # Charged before any validation, so malformed attempts consume both
    # applicable budgets too.
    _enforce_fallback_lookup_budget(actor=actor, network_fingerprint=network_fingerprint)

    masked = "****"
    try:
        normalized = references.parse_fallback_reference(raw_reference)
        masked = references.mask_fallback_reference(normalized)
    except references.InvalidFallbackReferenceError:
        _audit(
            action_code=action_codes.FALLBACK_REFERENCE_LOOKUP,
            credential=None,
            actor=actor,
            result="FAILURE",
            reason_code="INVALID_REFERENCE",
            target_type="PassCredentialSeries",
            after={"reference": masked},
            network_fingerprint=network_fingerprint,
        )
        return None

    series = _scoped_series_for_reference(actor=actor, normalized=normalized)

    _audit(
        action_code=action_codes.FALLBACK_REFERENCE_LOOKUP,
        credential=None,
        actor=actor,
        result="SUCCESS" if series is not None else "FAILURE",
        reason_code=None if series is not None else "NOT_FOUND",
        target_type="PassCredentialSeries",
        target_uuid=getattr(series, "pk", None),
        after={"reference": masked},
        network_fingerprint=network_fingerprint,
    )
    return series


def _network_fingerprint_for(network_identity: str) -> str:
    """Derive the project-approved KEYED fingerprint for a network identity.

    Reuses the existing rate-limit HMAC key family (ADR-0007) exactly as
    `apps.accounts.otp` does, so a raw IP address is never stored anywhere.
    Returns `""` when no identity was resolved, which simply means the
    network budget does not apply to this request.
    """
    if not network_identity:
        return ""
    from apps.core.crypto import (
        compute_rate_limit_fingerprints_for_active_versions,
        get_key_provider,
    )

    provider = get_key_provider()
    fingerprints = compute_rate_limit_fingerprints_for_active_versions(
        network_identity, provider=provider
    )
    write_version = provider.write_rate_limit_key_version()
    return fingerprints.get(write_version, b"").hex()


def _scoped_series_for_reference(*, actor, normalized: str) -> PassCredentialSeries | None:
    """Match a reference only within the actor's authorized scope.

    Routes through `apps.accounts.selectors.scope_filtered_queryset` -- the
    same shared primitive the protected credential detail page uses -- so the
    two surfaces cannot drift apart on what a given operator may see.
    """
    from apps.accounts.selectors import scope_filtered_queryset
    from apps.registrations.models import Registration

    visible_registrations = scope_filtered_queryset(
        actor,
        Registration.objects,
        app_label="badges",
        codename="view_digitalentrypass",
        organization_field="source_organization_id",
    )
    return (
        PassCredentialSeries.objects.select_related("registration")
        .filter(
            fallback_reference=normalized,
            registration__in=visible_registrations.values("pk"),
        )
        .first()
    )


def _enforce_fallback_lookup_budget(*, actor, network_fingerprint: str) -> None:
    """Charge every applicable budget, independently.

    Uses the audit trail itself as the counter, so the limit holds without a
    cache dependency that local runs never exercise. Both budgets are
    evaluated: an actor may not escape the network budget by signing in as
    someone else, and a network may not escape the actor budget by moving.
    """
    from apps.audit.models import AuditEvent

    window = int(getattr(settings, "FALLBACK_REFERENCE_LOOKUP_WINDOW_SECONDS", 300))
    maximum = int(getattr(settings, "FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW", 30))
    since = timezone.now() - timedelta(seconds=window)
    recent = AuditEvent.objects.filter(
        action_code=action_codes.FALLBACK_REFERENCE_LOOKUP,
        occurred_at__gte=since,
    )

    actor_id = getattr(actor, "pk", None)
    if actor_id and recent.filter(actor_user_id=actor_id).count() >= maximum:
        raise FallbackLookupThrottled(
            "Too many fallback reference lookups. Please wait before trying again."
        )
    if (
        network_fingerprint
        and recent.filter(network_fingerprint=network_fingerprint).count() >= maximum
    ):
        raise FallbackLookupThrottled(
            "Too many fallback reference lookups. Please wait before trying again."
        )


# ---------------------------------------------------------------------------
# Generic physical badge stock (Phase 3 Prompt 3, ADR-0020)
#
# Imported at the BOTTOM of this module, deliberately: `apps.badges.services
# .stock` reuses `command_fingerprint`/`normalize_reason_text`/
# `OperationConflictError` from this module (ADR-0020 §4, "reuse existing
# shared primitives instead of creating parallel ... implementations")
# rather than duplicating them, and every name it imports is already bound
# above by the time this line runs.
# ---------------------------------------------------------------------------

from apps.badges.services.stock import (  # noqa: E402, F401 -- re-exported for `apps.badges.services.*`
    CrossEventScopeError,
    DuplicateStockLocationError,
    InsufficientStockError,
    RegistrationNotEligibleError,
    StockConcurrencyError,
    StockServiceError,
    StockStateError,
    WrongBadgeTypeError,
    allocate_stock,
    available_balance,
    change_batch_status,
    create_print_batch,
    create_stock_location,
    current_balance,
    issue_badge,
    mark_issuance_lost,
    receive_print_batch,
    reconstruct_balance_from_ledger,
    record_adjustment,
    record_reconciliation,
    release_allocation,
    replace_issuance,
    require_same_event,
    reserved_balance,
    return_issuance,
    transfer_stock,
    void_issuance,
)
