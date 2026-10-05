"""Minimal Compact JWS assembly adapter.

Phase 3 Prompt 2 / ADR-0019. This module exists for exactly one reason:
`joserfc.jws.serialize_compact()` requires a private key OBJECT, and handing
one to it would drag private key material across the
`SigningKeyProvider` boundary -- which a future KMS/HSM-backed provider
physically cannot do.

So the signing side assembles the compact serialization itself. Critically,
**this adapter implements no cryptographic primitive**: it canonicalizes,
base64url-encodes, concatenates, and asks the provider for a signature.
Every cryptographic operation happens inside `cryptography` (behind the
provider) or inside `joserfc` (on the verification side).

The assurance that this minimal producer stays RFC-compliant is not a
narrative claim: every token it produces is round-tripped through
`joserfc` with the corresponding public key in the test suite, so the
reviewed consumer validates the minimal producer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.badges.credentials.canonical import (
    b64url_encode,
    canonical_header_bytes,
    canonical_payload_bytes,
    payload_hash,
    signing_input,
)
from apps.core.crypto.signing import (
    RAW_SIGNATURE_LENGTH_BYTES,
    SIGNING_ALGORITHM,
    SigningKeyProvider,
    SigningKeyProviderError,
    get_signing_key_provider,
    is_valid_key_id,
)


@dataclass(frozen=True)
class AssembledCredential:
    """The signed credential plus the evidence persisted alongside it."""

    token: str
    key_id: str
    header_bytes: bytes
    payload_bytes: bytes
    signing_input_bytes: bytes
    signature: bytes
    payload_hash: str


def assemble_compact_jws(
    payload: dict[str, Any],
    *,
    key_id: str | None = None,
    provider: SigningKeyProvider | None = None,
) -> AssembledCredential:
    """Canonicalize, sign through the provider, and assemble a compact JWS.

    Raises `SigningKeyProviderError` if the provider declares an algorithm
    other than ES256 or returns anything but a 64-byte raw `r || s`
    signature -- so a misconfigured provider (for example one returning a
    DER signature) fails at this boundary instead of emitting a
    non-compliant token that a scanner would later reject in the field.
    """
    provider = provider or get_signing_key_provider()
    key_id = key_id or provider.current_signing_key_id()
    if not is_valid_key_id(key_id):
        raise SigningKeyProviderError("Malformed signing key identifier.")

    algorithm = provider.signing_algorithm(key_id)
    if algorithm != SIGNING_ALGORITHM:
        raise SigningKeyProviderError(
            "Signing provider declared an algorithm other than the permitted ES256."
        )

    header_bytes = canonical_header_bytes(key_id=key_id)
    payload_bytes = canonical_payload_bytes(payload)
    signing_input_bytes = signing_input(header_bytes=header_bytes, payload_bytes=payload_bytes)

    signature = provider.sign(key_id, signing_input_bytes)
    if not isinstance(signature, bytes) or len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        raise SigningKeyProviderError(
            "Signing provider returned a signature that is not the 64-byte raw r||s "
            "form required by RFC 7518 for ES256."
        )

    token = f"{signing_input_bytes.decode('ascii')}.{b64url_encode(signature)}"
    return AssembledCredential(
        token=token,
        key_id=key_id,
        header_bytes=header_bytes,
        payload_bytes=payload_bytes,
        signing_input_bytes=signing_input_bytes,
        signature=signature,
        payload_hash=payload_hash(signing_input_bytes),
    )
