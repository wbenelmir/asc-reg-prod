"""Offline Package cryptography, composed from reviewed primitives only
(Phase 4 Prompt 2, binding decision P2-D, ADR-0023).

No primitive is implemented here. Every operation is a documented
composition of `cryptography` building blocks that WebCrypto implements
identically on the device:

* signatures: ES256 compact JWS, raw `r || s` (RFC 7518 §3.4), produced by
  the opaque package `SigningKeyProvider` -- never the QR provider;
* body encryption: AES-256-GCM, 96-bit random IV, random 256-bit data key
  per package (and per delta);
* key wrapping (ECIES-style, WebCrypto-compatible): an ephemeral P-256 key
  pair, ECDH with the device's registered PACKAGE_UNWRAP public key (the
  32-byte x-coordinate, as WebCrypto `deriveBits` returns it), HKDF-SHA256
  with a random 256-bit salt and a context-binding `info`, then AES-256-GCM
  of the data key under the derived key. The ephemeral private key and the
  data key are discarded when the function returns.

The server can therefore never decrypt a package it has built: it keeps
neither the data key nor any device private key. The `unwrap_*` helpers
below exist ONLY for tests, which hold an in-memory synthetic device key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.core.crypto.signing import RAW_SIGNATURE_LENGTH_BYTES, SigningKeyProvider
from apps.entry.offline_contract import BODY_ENCRYPTION_ALG, KEY_WRAP_ALG

_IV_BYTES = 12
_KEY_BYTES = 32
_SALT_BYTES = 32
_MAX_SPKI_B64_LENGTH = 256


class OfflineCryptoError(ValueError):
    """A cryptographic input is malformed or does not verify.

    Deliberately carries no key material, ciphertext, or plaintext."""


# ---------------------------------------------------------------------------
# Encodings
# ---------------------------------------------------------------------------


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: object) -> bytes:
    """Strict unpadded base64url; rejects padding and foreign characters."""
    if not isinstance(text, str) or "=" in text:
        raise OfflineCryptoError("Invalid base64url value.")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise OfflineCryptoError("Invalid base64url value.") from exc
    if b64url_encode(raw) != text:
        raise OfflineCryptoError("Non-canonical base64url value.")
    return raw


def canonical_json(value) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, no insignificant whitespace, no floats."""

    def _reject_floats(obj):
        if isinstance(obj, float):
            raise OfflineCryptoError("Canonical JSON never contains floating-point numbers.")
        if isinstance(obj, dict):
            for item in obj.values():
                _reject_floats(item)
        elif isinstance(obj, list):
            for item in obj:
                _reject_floats(item)

    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Device public keys (SubjectPublicKeyInfo DER, base64url)
# ---------------------------------------------------------------------------


def load_device_public_key(spki_b64: object) -> ec.EllipticCurvePublicKey:
    """Parse a device-supplied P-256 public key; refuse anything else."""
    if not isinstance(spki_b64, str) or len(spki_b64) > _MAX_SPKI_B64_LENGTH:
        raise OfflineCryptoError("Invalid device public key.")
    der = b64url_decode(spki_b64)
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, TypeError) as exc:
        raise OfflineCryptoError("Invalid device public key.") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise OfflineCryptoError("A device key must be a P-256 public key.")
    if spki_b64 != b64url_encode(public_key_der(key)):
        raise OfflineCryptoError("A device key must be canonical DER SubjectPublicKeyInfo.")
    return key


def public_key_der(key) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def key_fingerprint(spki_b64: str) -> str:
    """SHA-256 over the DER SubjectPublicKeyInfo (never over text)."""
    return sha256_hex(b64url_decode(spki_b64))


def pem_to_spki_b64(pem: bytes) -> str:
    return b64url_encode(public_key_der(serialization.load_pem_public_key(pem)))


def verify_device_signature(
    public_key: ec.EllipticCurvePublicKey, message: bytes, signature: bytes
) -> bool:
    """ECDSA P-256 / SHA-256 over `message`; `signature` is raw `r || s`
    exactly as WebCrypto `sign` returns it."""
    if len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        public_key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


# ---------------------------------------------------------------------------
# Compact JWS (ES256) with the package signing family
# ---------------------------------------------------------------------------


def jws_header(*, key_id: str, typ: str) -> bytes:
    return canonical_json({"alg": "ES256", "kid": key_id, "typ": typ})


def jws_sign(payload: dict, *, typ: str, provider: SigningKeyProvider) -> tuple[str, str]:
    """Return `(compact_jws, key_id)` for `payload` under the CURRENT package key."""
    key_id = provider.current_signing_key_id()
    signing_input = (
        b64url_encode(jws_header(key_id=key_id, typ=typ))
        + "."
        + b64url_encode(canonical_json(payload))
    ).encode("ascii")
    signature = provider.sign(key_id, signing_input)
    if len(signature) != RAW_SIGNATURE_LENGTH_BYTES:
        raise OfflineCryptoError("The signing provider returned a non-JWS signature.")
    return signing_input.decode("ascii") + "." + b64url_encode(signature), key_id


def _pinned_p256_key(der: object) -> ec.EllipticCurvePublicKey:
    """Load one PINNED verification key; anything but a P-256 public key in
    DER SubjectPublicKeyInfo is refused with a controlled error."""
    if not isinstance(der, bytes | bytearray) or not der:
        raise OfflineCryptoError("The pinned signing key is malformed.")
    try:
        key = serialization.load_der_public_key(bytes(der))
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise OfflineCryptoError("The pinned signing key is malformed.") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise OfflineCryptoError("The pinned signing key is not a P-256 public key.")
    return key


def jws_verify(compact: object, *, trusted_keys_der: dict[str, bytes], typ: str) -> dict:
    """Verify a compact JWS against PINNED keys only; return its payload.

    Mirrors the device check: the key is selected from `trusted_keys_der` by
    `kid`, never taken from the token, and the header must be exactly the
    three canonical members with the expected `typ`.
    """
    if not isinstance(compact, str) or compact.count(".") != 2 or len(compact) > 2_000_000:
        raise OfflineCryptoError("Malformed JWS.")
    header_b64, payload_b64, signature_b64 = compact.split(".")
    header_raw = b64url_decode(header_b64)
    try:
        header = json.loads(header_raw)
    except ValueError as exc:
        raise OfflineCryptoError("Malformed JWS header.") from exc
    if not isinstance(header, dict) or set(header) != {"alg", "kid", "typ"}:
        raise OfflineCryptoError("Unexpected JWS header members.")
    if header["alg"] != "ES256" or header["typ"] != typ:
        raise OfflineCryptoError("Unexpected JWS algorithm or type.")
    if header_raw != jws_header(key_id=header["kid"], typ=typ):
        raise OfflineCryptoError("Non-canonical JWS header.")
    der = trusted_keys_der.get(header["kid"]) if isinstance(header["kid"], str) else None
    if der is None:
        raise OfflineCryptoError("The signing key is not trusted.")
    key = _pinned_p256_key(der)
    signature = b64url_decode(signature_b64)
    if not verify_device_signature(key, f"{header_b64}.{payload_b64}".encode("ascii"), signature):
        raise OfflineCryptoError("Invalid JWS signature.")
    try:
        payload = json.loads(b64url_decode(payload_b64))
    except ValueError as exc:
        raise OfflineCryptoError("Malformed JWS payload.") from exc
    if not isinstance(payload, dict):
        raise OfflineCryptoError("Malformed JWS payload.")
    return payload


# ---------------------------------------------------------------------------
# Encryption and ECIES-style key wrapping
# ---------------------------------------------------------------------------


def _context(kind: str, part: str, device_public_id: str, object_id: str) -> bytes:
    return f"ASC-{kind}-{part}-v1|{device_public_id}|{object_id}".encode()


def encrypt_for_device(
    plaintext: bytes,
    *,
    recipient: ec.EllipticCurvePublicKey,
    recipient_fingerprint: str,
    device_public_id: str,
    object_id: str,
    kind: str,
) -> tuple[bytes, dict, dict]:
    """Encrypt `plaintext` so only the device holding `recipient`'s private key
    can read it. Returns `(ciphertext, enc, wrap)`; `enc` and `wrap` go into
    the signed manifest. The data key never leaves this function."""
    data_key = AESGCM.generate_key(bit_length=256)
    body_iv = os.urandom(_IV_BYTES)
    body_aad = _context(kind, "BODY", device_public_id, object_id)
    ciphertext = AESGCM(data_key).encrypt(body_iv, plaintext, body_aad)

    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), recipient)
    salt = os.urandom(_SALT_BYTES)
    wrapping_key = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=salt,
        info=_context(kind, "WRAP", device_public_id, object_id),
    ).derive(shared)
    wrap_iv = os.urandom(_IV_BYTES)
    wrap_aad = _context(kind, "DEK", device_public_id, object_id)
    wrapped_key = AESGCM(wrapping_key).encrypt(wrap_iv, data_key, wrap_aad)
    enc = {
        "alg": BODY_ENCRYPTION_ALG,
        "iv": b64url_encode(body_iv),
        "aad": body_aad.decode("utf-8"),
    }
    wrap = {
        "alg": KEY_WRAP_ALG,
        "epk": b64url_encode(public_key_der(ephemeral.public_key())),
        "salt": b64url_encode(salt),
        "iv": b64url_encode(wrap_iv),
        "wrapped_key": b64url_encode(wrapped_key),
        "recipient": recipient_fingerprint,
    }
    del data_key, wrapping_key, shared, ephemeral
    return ciphertext, enc, wrap


def unwrap_and_decrypt_for_tests(
    ciphertext: bytes,
    *,
    enc: dict,
    wrap: dict,
    recipient_private_key: ec.EllipticCurvePrivateKey,
    device_public_id: str,
    object_id: str,
    kind: str,
) -> bytes:
    """TEST-ONLY inverse of `encrypt_for_device`. Application code never has
    a device private key; this exists to prove the round trip in tests."""
    ephemeral_public = serialization.load_der_public_key(b64url_decode(wrap["epk"]))
    shared = recipient_private_key.exchange(ec.ECDH(), ephemeral_public)
    wrapping_key = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=b64url_decode(wrap["salt"]),
        info=_context(kind, "WRAP", device_public_id, object_id),
    ).derive(shared)
    data_key = AESGCM(wrapping_key).decrypt(
        b64url_decode(wrap["iv"]),
        b64url_decode(wrap["wrapped_key"]),
        _context(kind, "DEK", device_public_id, object_id),
    )
    return AESGCM(data_key).decrypt(
        b64url_decode(enc["iv"]), ciphertext, enc["aad"].encode("utf-8")
    )
