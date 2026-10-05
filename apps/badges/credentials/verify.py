"""Digital Entry Pass QR verification.

Phase 3 Prompt 2 / ADR-0019. Fail-closed, deterministic, and deliberately
uninformative to the presenter: the caller receives one stable result code
from a fixed vocabulary, never a parser detail, never an exception message,
and never a hint distinguishing "unknown key" from "bad signature".

This module produces a CREDENTIAL verification result only. It deliberately
does NOT make a gate admission decision and does NOT write an EntryEvent --
both belong to a later Phase 3 prompt.

Verification order (documented precedence, lowest tier wins):

1. malformed structure, unsupported payload version, unknown key, invalid
   or revoked key, invalid signature;
2. wrong event or mismatched credential claims;
3. REVOKED;
4. REPLACED;
5. SUSPENDED;
6. INACTIVE or not yet valid;
7. EXPIRED;
8. valid.

A credential that is both terminal and outside its time window keeps the
more informative terminal result: REVOKED and REPLACED are evaluated before
the validity window, so a revoked credential presented after expiry still
reports REVOKED rather than collapsing into EXPIRED.

Expiry is evaluated from `valid_until` on every call. It never depends on a
sweep, a worker, or a broker having run: if the persisted status still says
ACTIVE but the window has closed, the result is EXPIRED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.badges.credentials.canonical import (
    CanonicalFormatError,
    b64url_decode,
    canonical_header_bytes,
    canonical_payload_bytes,
    loads_strict,
    payload_hash,
    signing_input,
    validate_header,
    validate_payload,
)
from apps.core.crypto.signing import RAW_SIGNATURE_LENGTH_BYTES, is_valid_key_id


class VerificationResultCode:
    """Stable, machine-readable outcomes. Safe to show to an operator."""

    MALFORMED = "MALFORMED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    INVALID_KEY = "INVALID_KEY"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    CREDENTIAL_MISMATCH = "CREDENTIAL_MISMATCH"
    WRONG_EVENT = "WRONG_EVENT"
    REVOKED = "REVOKED"
    REPLACED = "REPLACED"
    SUSPENDED = "SUSPENDED"
    INACTIVE = "INACTIVE"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    VALID = "VALID"


#: Documented precedence tiers. Lower wins. Exposed so tests can assert the
#: ordering rather than trusting the implementation's control flow.
RESULT_PRECEDENCE: dict[str, int] = {
    VerificationResultCode.MALFORMED: 1,
    VerificationResultCode.UNSUPPORTED_VERSION: 1,
    VerificationResultCode.UNKNOWN_KEY: 1,
    VerificationResultCode.INVALID_KEY: 1,
    VerificationResultCode.INVALID_SIGNATURE: 1,
    VerificationResultCode.CREDENTIAL_NOT_FOUND: 2,
    VerificationResultCode.CREDENTIAL_MISMATCH: 2,
    VerificationResultCode.WRONG_EVENT: 2,
    VerificationResultCode.REVOKED: 3,
    VerificationResultCode.REPLACED: 4,
    VerificationResultCode.SUSPENDED: 5,
    VerificationResultCode.INACTIVE: 6,
    VerificationResultCode.NOT_YET_VALID: 6,
    VerificationResultCode.EXPIRED: 7,
    VerificationResultCode.VALID: 8,
}


@dataclass(frozen=True)
class VerificationResult:
    """One credential verification outcome.

    `credential` is populated only once the signature has been verified and
    the credential resolved, so a caller can never read credential state
    from an unverified token.
    """

    code: str
    credential: Any = None
    key_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return self.code == VerificationResultCode.VALID

    @property
    def precedence(self) -> int:
        return RESULT_PRECEDENCE[self.code]


def _fail(code: str, **detail: Any) -> VerificationResult:
    return VerificationResult(code=code, detail=detail)


def _epoch(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp())


def _restricted_registry():
    """A joserfc registry permitting exactly `alg`, `kid`, `typ` and ES256.

    Defence in depth: this module already validates the header against its
    own allowlist before calling joserfc, and joserfc then independently
    rejects anything outside the same three members (including `crit`).
    """
    from joserfc.jws import JWSRegistry
    from joserfc.registry import JWS_HEADER_REGISTRY

    header_registry = {name: JWS_HEADER_REGISTRY[name] for name in ("alg", "kid", "typ")}
    return JWSRegistry(
        header_registry=header_registry,
        algorithms=["ES256"],
        strict_check_header=True,
    )


def _key_is_usable(key, *, now: datetime) -> bool:
    """True if a stored verification key may be used to verify right now.

    ACTIVE and RETIRED keys both verify -- that overlap is what lets
    credentials issued before a rotation keep working. PENDING keys are not
    yet trusted, and REVOKED keys fail immediately for every credential
    bearing their `kid`, regardless of that credential's own expiry.
    """
    from apps.badges.models import VerificationKeyStatus

    if key.status not in (VerificationKeyStatus.ACTIVE, VerificationKeyStatus.RETIRED):
        return False
    if key.not_before is not None and now < key.not_before:
        return False
    if key.not_after is not None and now >= key.not_after:
        return False
    return True


def verify_pass_token(token: object, *, now: datetime | None = None) -> VerificationResult:
    """Verify a presented QR credential and return a single result code.

    Never raises for untrusted input: every failure path returns a
    `VerificationResult`. The raw token is never logged, never echoed, and
    never placed in the returned detail.
    """
    from joserfc.jwk import ECKey
    from joserfc.jws import deserialize_compact

    from apps.badges.models import (
        ES256,
        DigitalEntryPass,
        DigitalEntryPassStatus,
        VerificationKey,
    )

    now = now or timezone.now()
    skew = int(getattr(settings, "QR_CLOCK_SKEW_SECONDS", 60))

    # --- Tier 1: structure -------------------------------------------------
    if not isinstance(token, str):
        return _fail(VerificationResultCode.MALFORMED, reason="NOT_TEXT")
    encoded = token.strip()
    max_bytes = int(getattr(settings, "QR_MAX_TOKEN_BYTES", 1024))
    if len(encoded.encode("utf-8", errors="ignore")) > max_bytes:
        return _fail(VerificationResultCode.MALFORMED, reason="OVERSIZED")

    segments = encoded.split(".")
    if len(segments) != 3:
        return _fail(VerificationResultCode.MALFORMED, reason="SEGMENT_COUNT")
    header_segment, payload_segment, signature_segment = segments

    try:
        header_bytes = b64url_decode(header_segment)
        payload_bytes = b64url_decode(payload_segment)
        signature = b64url_decode(signature_segment)
    except CanonicalFormatError:
        return _fail(VerificationResultCode.MALFORMED, reason="BASE64URL")

    if len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        return _fail(VerificationResultCode.MALFORMED, reason="SIGNATURE_LENGTH")

    try:
        header = validate_header(loads_strict(header_bytes))
    except CanonicalFormatError:
        return _fail(VerificationResultCode.MALFORMED, reason="HEADER")

    key_id = header["kid"]
    if not is_valid_key_id(key_id):
        return _fail(VerificationResultCode.MALFORMED, reason="KEY_ID")

    # --- Tier 1: key resolution, BEFORE any signature verification ---------
    key = VerificationKey.objects.filter(key_id=key_id).first()
    if key is None:
        return _fail(VerificationResultCode.UNKNOWN_KEY)
    # The algorithm is taken from the TRUSTED stored key record, never from
    # the presented header. The header's `alg` was already required to equal
    # ES256 by `validate_header`, so a mismatch here means the stored key is
    # misconfigured, not that the token chose an algorithm.
    if key.algorithm != ES256:
        return _fail(VerificationResultCode.INVALID_KEY, key_id=key_id, reason="ALGORITHM")
    if not _key_is_usable(key, now=now):
        return _fail(VerificationResultCode.INVALID_KEY, key_id=key_id, reason=key.status)

    # --- Tier 1: signature -------------------------------------------------
    try:
        public_key = ECKey.import_key(key.public_key_pem.encode("utf-8"))
        deserialize_compact(
            encoded,
            public_key,
            algorithms=["ES256"],
            registry=_restricted_registry(),
        )
    except Exception:  # noqa: BLE001 - every joserfc failure is one outcome
        return _fail(VerificationResultCode.INVALID_SIGNATURE, key_id=key_id)

    # --- Tier 1: payload structure and version -----------------------------
    try:
        claims = validate_payload(loads_strict(payload_bytes))
    except CanonicalFormatError:
        return _fail(VerificationResultCode.MALFORMED, reason="PAYLOAD", key_id=key_id)

    supported = [int(v) for v in getattr(settings, "QR_SUPPORTED_PAYLOAD_VERSIONS", [1])]
    if claims["v"] not in supported:
        return _fail(VerificationResultCode.UNSUPPORTED_VERSION, key_id=key_id)

    # --- Tier 2: resolve and cross-check the credential --------------------
    credential = (
        DigitalEntryPass.objects.select_related("series", "registration", "event_edition")
        .filter(jti=claims["jti"])
        .first()
    )
    if credential is None:
        return _fail(VerificationResultCode.CREDENTIAL_NOT_FOUND, key_id=key_id)

    if claims["eid"] != credential.event_code:
        return _fail(VerificationResultCode.WRONG_EVENT, key_id=key_id, detail_code="EVENT_CODE")
    if claims["cv"] != credential.credential_version:
        return _fail(
            VerificationResultCode.CREDENTIAL_MISMATCH,
            key_id=key_id,
            detail_code="CREDENTIAL_VERSION",
        )
    # Every one of the eleven claims is compared against the TRUSTED stored
    # snapshot -- including `bai` and `pid`, which a forged token could
    # otherwise carry freely because nothing else in the payload pins them.
    if (
        claims["btc"] != credential.badge_type_code
        or claims["apc"] != credential.access_profile_code
        or claims["n"] != credential.nonce
        or claims["bai"] != credential.badge_assignment_public_reference
        or claims["pid"] != credential.participant_event_pseudonym
        or claims["v"] != credential.payload_version
        or key_id != credential.signing_key_id
    ):
        return _fail(
            VerificationResultCode.CREDENTIAL_MISMATCH, key_id=key_id, detail_code="SNAPSHOT"
        )
    if claims["nbf"] != _epoch(credential.valid_from) or claims["exp"] != _epoch(
        credential.valid_until
    ):
        return _fail(
            VerificationResultCode.CREDENTIAL_MISMATCH, key_id=key_id, detail_code="VALIDITY_WINDOW"
        )

    # Finally, the presented signing input must hash to the value recorded at
    # issuance. This catches any drift between the stored snapshot and the
    # artefact that was actually signed, independently of the claim-by-claim
    # comparison above.
    presented_hash = payload_hash(f"{header_segment}.{payload_segment}".encode("ascii"))
    if credential.payload_hash and presented_hash != credential.payload_hash:
        return _fail(
            VerificationResultCode.CREDENTIAL_MISMATCH, key_id=key_id, detail_code="PAYLOAD_HASH"
        )

    # --- Tiers 3-7: credential status, then the validity window ------------
    # Terminal statuses are evaluated BEFORE the window so a revoked or
    # replaced credential keeps its more informative result instead of being
    # collapsed into EXPIRED.
    status_results = {
        DigitalEntryPassStatus.REVOKED: VerificationResultCode.REVOKED,
        DigitalEntryPassStatus.REPLACED: VerificationResultCode.REPLACED,
        DigitalEntryPassStatus.SUSPENDED: VerificationResultCode.SUSPENDED,
        DigitalEntryPassStatus.INACTIVE: VerificationResultCode.INACTIVE,
        DigitalEntryPassStatus.EXPIRED: VerificationResultCode.EXPIRED,
    }
    mapped = status_results.get(credential.status)
    if mapped is not None:
        return VerificationResult(code=mapped, credential=credential, key_id=key_id)

    # Only ACTIVE reaches here. Expiry is derived from the stored window on
    # every call, so correctness never depends on a sweep having persisted
    # the EXPIRED status.
    if now < credential.valid_from and (credential.valid_from - now).total_seconds() > skew:
        return VerificationResult(
            code=VerificationResultCode.NOT_YET_VALID, credential=credential, key_id=key_id
        )
    if now >= credential.valid_until and (now - credential.valid_until).total_seconds() > skew:
        return VerificationResult(
            code=VerificationResultCode.EXPIRED, credential=credential, key_id=key_id
        )

    return VerificationResult(
        code=VerificationResultCode.VALID, credential=credential, key_id=key_id
    )


def rebuild_signing_input(credential) -> bytes:
    """Recompute a credential's canonical signing input from stored evidence.

    Reads only the credential's own immutable snapshot columns, so this is
    deterministic and requires no private key.
    """
    from apps.badges.services import build_pass_claims

    header_bytes = canonical_header_bytes(key_id=credential.signing_key_id)
    payload_bytes = canonical_payload_bytes(build_pass_claims(credential))
    return signing_input(header_bytes=header_bytes, payload_bytes=payload_bytes)


def stored_payload_hash_matches(credential) -> bool:
    """True if the credential's stored `payload_hash` still reconciles."""
    return payload_hash(rebuild_signing_input(credential)) == credential.payload_hash
