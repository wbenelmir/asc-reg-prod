"""Opaque ES256 signing-key boundary for Digital Entry Pass credentials.

Phase 3 Prompt 2 / ADR-0019. Deliberately mirrors the existing versioned
`KeyProvider` architecture in `apps.core.crypto` (ADR-0006) rather than
introducing a second, differently-shaped key abstraction:

* key material is addressed by an explicit version, so rotation is a
  first-class operation instead of a flag-day cutover;
* every value is read from `django.conf.settings`, itself populated only
  from `os.environ` via `config/settings/_env.py` -- never a hard-coded
  default, never a value this module generates and persists;
* the provider fails closed when key material is missing.

The one property that matters most here, and that the rest of the codebase
depends on: **a caller can ask for a signature but can never obtain private
key bytes.** `sign()` returns a signature; `public_key_pem()` returns the
PUBLIC half only. There is deliberately no `private_key()` accessor, and
none may be added -- a future KMS/HSM-backed provider physically cannot
implement one, and the interface must stay implementable by such a
provider without redesign.

This module is also the reason the Compact JWS *signing* path uses a
minimal project-owned assembly adapter instead of handing a key to the JOSE
library: `joserfc.jws.serialize_compact()` requires a private key object,
which would drag private material across this boundary. Verification -- the
security-critical parsing side -- is fully delegated to `joserfc`.
"""

from __future__ import annotations

import abc
import binascii
import re

from apps.core.crypto import CryptoError

#: The single permitted signing algorithm. There is no negotiation, no
#: fallback, and no configuration switch: an allowlist of exactly one.
SIGNING_ALGORITHM = "ES256"

#: ES256 (RFC 7518 s3.4) signatures are the fixed-length concatenation
#: `r || s`, each 32 bytes for the P-256 curve. DER is explicitly NOT the
#: JWS wire format and is rejected at this boundary.
RAW_SIGNATURE_LENGTH_BYTES = 64
_COORDINATE_LENGTH_BYTES = 32

#: A key identifier is short, bounded, and drawn from a restricted alphabet
#: so that an attacker-supplied `kid` can never become an unbounded lookup
#: term, a path fragment, or a log-injection vector.
KEY_ID_PATTERN = re.compile(r"^v[1-9][0-9]{0,3}$")
MAX_KEY_ID_LENGTH = 8


class SigningKeyProviderError(CryptoError):
    """Raised when signing key material is missing, invalid, or unusable.

    Never carries key material, a PEM fragment, or a plaintext message in
    its string representation.
    """


class SigningProviderUnavailableError(SigningKeyProviderError):
    """Raised when the configured provider cannot sign at all right now."""


def key_id_for_version(version: int) -> str:
    """Return the public `kid` for a configured key version (`1` -> `"v1"`)."""
    return f"v{version}"


def version_for_key_id(key_id: str) -> int:
    """Inverse of :func:`key_id_for_version`, with strict validation."""
    if not is_valid_key_id(key_id):
        raise SigningKeyProviderError("Malformed signing key identifier.")
    return int(key_id[1:])


def is_valid_key_id(key_id: object) -> bool:
    """True only for a syntactically valid, length-bounded key identifier."""
    if not isinstance(key_id, str):
        return False
    if len(key_id) > MAX_KEY_ID_LENGTH:
        return False
    # `fullmatch`: a `$`-anchored `match` would accept "v1\n" (P8-04).
    return KEY_ID_PATTERN.fullmatch(key_id) is not None


def encode_raw_signature(r: int, s: int) -> bytes:
    """Encode an ECDSA `(r, s)` pair as the JWS fixed-length `r || s` form."""
    return r.to_bytes(_COORDINATE_LENGTH_BYTES, "big") + s.to_bytes(_COORDINATE_LENGTH_BYTES, "big")


class SigningKeyProvider(abc.ABC):
    """Opaque signing capability.

    Implementations expose the minimum needed to issue and verify pass
    credentials. No method returns private key material, and none may be
    added that does.
    """

    @abc.abstractmethod
    def current_signing_key_id(self) -> str:
        """The `kid` new credentials must be signed with."""

    @abc.abstractmethod
    def active_signing_key_ids(self) -> list[str]:
        """Every `kid` this provider can still sign with or resolve."""

    @abc.abstractmethod
    def signing_algorithm(self, key_id: str) -> str:
        """The algorithm bound to `key_id`. Always `ES256` in Phase 3."""

    @abc.abstractmethod
    def sign(self, key_id: str, message: bytes) -> bytes:
        """Sign exactly `message`, returning a 64-byte raw `r || s` signature.

        The caller supplies the precise bytes to be signed and receives only
        the signature. Implementations MUST NOT return, log, or otherwise
        surface private key material.
        """

    @abc.abstractmethod
    def public_key_pem(self, key_id: str) -> bytes:
        """The PUBLIC key for `key_id`, PEM-encoded (SubjectPublicKeyInfo)."""


def _normalize_pem(raw: str) -> bytes:
    r"""Accept a PEM supplied through an environment variable.

    `.env` values cannot span lines, so a literal two-character `\n`
    sequence is accepted and folded to a real newline. The value is
    otherwise passed through untouched. The rule itself lives in
    `apps.core.credential_shapes.normalize_pem_env_value`, shared with the
    log-redaction layer so both handle the identical text (P8-03).
    """
    from apps.core.credential_shapes import normalize_pem_env_value

    return normalize_pem_env_value(raw).encode("utf-8")


class EnvSigningKeyProvider(SigningKeyProvider):
    """Environment-backed provider for local development, test, and staging.

    Reads only from `django.conf.settings`, which `config/settings/_env.py`
    populates exclusively from `os.environ`. This class never generates a
    key, never writes one anywhere, and never returns private bytes.

    Production is expected to substitute a managed KMS/HSM-backed provider
    implementing the same interface. That substitution is deployment-only
    and is NOT proven by any local test.
    """

    def _settings(self):
        from django.conf import settings

        return settings

    def _private_key_object(self, key_id: str):
        """Load the private key for `key_id`. Private to this class.

        Returns a `cryptography` key OBJECT, never bytes, and is never
        exposed through the public interface.
        """
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        version = version_for_key_id(key_id)
        raw = self._settings().QR_SIGNING_KEYS_BY_VERSION.get(version)
        if not raw:
            raise SigningKeyProviderError(f"QR_SIGNING_KEY_V{version} is not configured.")
        try:
            key = load_pem_private_key(_normalize_pem(raw), password=None)
        except Exception as exc:  # noqa: BLE001 - never leak the underlying detail
            raise SigningKeyProviderError(
                f"Configured signing key {key_id} could not be loaded."
            ) from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise SigningKeyProviderError(
                f"Configured signing key {key_id} is not a P-256 key required by ES256."
            )
        return key

    def current_signing_key_id(self) -> str:
        return key_id_for_version(self._settings().QR_SIGNING_WRITE_KEY_VERSION)

    def active_signing_key_ids(self) -> list[str]:
        return [key_id_for_version(v) for v in self._settings().QR_SIGNING_ACTIVE_KEY_VERSIONS]

    def signing_algorithm(self, key_id: str) -> str:
        if not is_valid_key_id(key_id):
            raise SigningKeyProviderError("Malformed signing key identifier.")
        return SIGNING_ALGORITHM

    def sign(self, key_id: str, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        if not isinstance(message, bytes):
            raise SigningKeyProviderError("Signing input must be bytes.")
        key = self._private_key_object(key_id)
        der_signature = key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        return encode_raw_signature(r, s)

    def public_key_pem(self, key_id: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return (
            self._private_key_object(key_id)
            .public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )


class InMemorySigningKeyProvider(SigningKeyProvider):
    """TEST-ONLY provider holding ephemeral, in-memory P-256 keys.

    Keys are generated in the running process and discarded when it exits.
    Nothing is written to disk, to the database, to a fixture, or to a
    review archive -- which is exactly why the committed public test
    vectors carry a public key and a precomputed signature but never a
    private key.

    Never installed by application code; only `set_signing_key_provider_for_testing`
    puts it in place.
    """

    def __init__(self, *, key_ids: tuple[str, ...] = ("v1",), current: str = "v1") -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        for key_id in (*key_ids, current):
            if not is_valid_key_id(key_id):
                raise SigningKeyProviderError("Malformed signing key identifier.")
        self._keys = {key_id: ec.generate_private_key(ec.SECP256R1()) for key_id in key_ids}
        if current not in self._keys:
            raise SigningKeyProviderError("Current key identifier is not among the generated keys.")
        self._current = current

    def current_signing_key_id(self) -> str:
        return self._current

    def set_current_signing_key_id(self, key_id: str) -> None:
        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown signing key identifier.")
        self._current = key_id

    def active_signing_key_ids(self) -> list[str]:
        return sorted(self._keys)

    def signing_algorithm(self, key_id: str) -> str:
        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown signing key identifier.")
        return SIGNING_ALGORITHM

    def sign(self, key_id: str, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown signing key identifier.")
        if not isinstance(message, bytes):
            raise SigningKeyProviderError("Signing input must be bytes.")
        der_signature = self._keys[key_id].sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        return encode_raw_signature(r, s)

    def public_key_pem(self, key_id: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown signing key identifier.")
        return (
            self._keys[key_id]
            .public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )


class _BrokenSigningKeyProvider(SigningKeyProvider):
    """TEST-ONLY provider that always fails, for unavailable-provider tests."""

    def current_signing_key_id(self) -> str:
        return "v1"

    def active_signing_key_ids(self) -> list[str]:
        return ["v1"]

    def signing_algorithm(self, key_id: str) -> str:
        return SIGNING_ALGORITHM

    def sign(self, key_id: str, message: bytes) -> bytes:
        raise SigningProviderUnavailableError("Signing provider is unavailable.")

    def public_key_pem(self, key_id: str) -> bytes:
        raise SigningProviderUnavailableError("Signing provider is unavailable.")


class _MalformedSignatureSigningKeyProvider(InMemorySigningKeyProvider):
    """TEST-ONLY provider returning a DER signature instead of raw `r || s`.

    Exists solely to prove the assembly adapter rejects a misconfigured
    provider at the boundary rather than emitting a non-compliant token.
    """

    def sign(self, key_id: str, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        return self._keys[key_id].sign(message, ec.ECDSA(hashes.SHA256()))


_signing_provider: SigningKeyProvider | None = None


def get_signing_key_provider() -> SigningKeyProvider:
    """Return the process-wide signing provider (lazily constructed)."""
    global _signing_provider
    if _signing_provider is None:
        _signing_provider = EnvSigningKeyProvider()
    return _signing_provider


def set_signing_key_provider_for_testing(provider: SigningKeyProvider | None) -> None:
    """Test-only seam: install an ephemeral provider, or reset to the default."""
    global _signing_provider
    _signing_provider = provider


class PublicKeyFormatError(CryptoError):
    """Raised when supplied public key material is not an ES256 public key.

    Never echoes the supplied material, and never distinguishes "this was
    actually a private key" from "this was malformed" to an end user -- the
    caller maps it to one safe message.
    """


def canonical_public_key_der(public_key_pem: str | bytes) -> bytes:
    """Parse a PEM public key and return canonical DER SubjectPublicKeyInfo.

    This is the single gate every published verification key passes through.
    It accepts **only** an EC public key on NIST P-256 (secp256r1), which is
    the one curve ES256 is defined over. A private key, an RSA key, an EC key
    on another curve, or malformed PEM is rejected.

    Canonical DER matters for correctness, not tidiness: two PEM strings that
    differ only in line wrapping, trailing newline, or CRLF endings describe
    the same key but are different bytes. Comparing or fingerprinting the PEM
    text would let a formatting difference read as a key mismatch -- or, worse,
    let two encodings of one key be published as if they were distinct.
    """
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )

    if isinstance(public_key_pem, str):
        material = public_key_pem.encode("utf-8")
    elif isinstance(public_key_pem, bytes):
        material = public_key_pem
    else:
        raise PublicKeyFormatError("Public key material must be text or bytes.")

    if looks_like_private_key_material(material.decode("utf-8", errors="ignore")):
        raise PublicKeyFormatError("A verification key must be public key material only.")

    try:
        key = load_pem_public_key(material)
    except Exception as exc:  # noqa: BLE001 - never leak the parser's detail
        raise PublicKeyFormatError("Public key material could not be parsed.") from exc

    if isinstance(key, rsa.RSAPublicKey):
        raise PublicKeyFormatError("ES256 requires an EC public key, not an RSA key.")
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise PublicKeyFormatError("ES256 requires an EC public key.")
    if not isinstance(key.curve, ec.SECP256R1):
        raise PublicKeyFormatError("ES256 requires the NIST P-256 (secp256r1) curve.")

    return key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def public_key_fingerprint(public_key_der: bytes) -> str:
    """SHA-256 over canonical DER bytes, for audit summaries and comparison.

    Takes DER, never PEM: a fingerprint computed over PEM text would change
    when the formatting changed, which defeats the entire purpose.
    """
    import hashlib

    return hashlib.sha256(public_key_der).hexdigest()


def public_key_der_matches(left_pem: str | bytes, right_pem: str | bytes) -> bool:
    """True if two PEM public keys canonicalize to identical DER."""
    return canonical_public_key_der(left_pem) == canonical_public_key_der(right_pem)


def looks_like_private_key_material(pem_text: str) -> bool:
    """True if `pem_text` contains any PEM private-key marker.

    Used to fail closed before a public-key record is stored, so private
    material can never be written into `badges.VerificationKey` even by
    mistake.
    """
    haystack = pem_text.upper()
    return any(
        marker in haystack
        for marker in ("PRIVATE KEY-----", "BEGIN PRIVATE", "BEGIN EC PRIVATE", "BEGIN RSA PRIVATE")
    )


def decode_raw_signature(signature: bytes) -> tuple[int, int]:
    """Split a 64-byte raw `r || s` signature, validating its length."""
    if len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        raise SigningKeyProviderError(
            "ES256 signature must be the 64-byte raw r||s form required by RFC 7518."
        )
    r = int.from_bytes(signature[:_COORDINATE_LENGTH_BYTES], "big")
    s = int.from_bytes(signature[_COORDINATE_LENGTH_BYTES:], "big")
    return r, s


def hex_to_raw_signature(value: str) -> bytes:
    """Decode a hex-encoded raw signature from a committed public test vector."""
    try:
        signature = binascii.unhexlify(value)
    except (binascii.Error, ValueError) as exc:
        raise SigningKeyProviderError("Malformed test-vector signature encoding.") from exc
    decode_raw_signature(signature)
    return signature
