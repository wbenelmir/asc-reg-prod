"""`KeyProvider` abstraction, AES-GCM field encryption, versioned HMAC blind indexes.

ADR-0006: two independent versioned-key families -- identity encryption/
blind-index keys (this module) and the separate rate-limit HMAC key family
(ADR-0007, `apps.core.concurrency`) -- rotating one never disturbs the
other. `EnvKeyProvider` is the local/test/staging development implementation
reading key material from environment variables only (Schema §17.3: no key
ever enters application tables or source control). Production key
management is a later decision; this interface is designed so a real KMS-
backed provider is a drop-in replacement.

Nothing in this module ever includes key material or plaintext in an
exception message, log call, or `repr()`.
"""

from __future__ import annotations

import abc
import base64
import binascii
import hashlib
import hmac
import os


class CryptoError(Exception):
    """Base for crypto-layer failures. Never includes key or plaintext material."""


class KeyProviderError(CryptoError):
    """Raised when required key material is missing or invalid (fail closed)."""


class EncryptionError(CryptoError):
    """Raised when encryption or decryption fails (e.g. tampered ciphertext)."""


class KeyProvider(abc.ABC):
    """Versioned key access for field encryption and blind indexes.

    Every method is version-explicit: callers decide which version to
    write with (`current_*_version`) and which versions to search across
    when reading (`active_*_versions`), so key rotation is a first-class
    operation rather than a single-flag-day cutover.
    """

    @abc.abstractmethod
    def encryption_key(self, version: int) -> bytes: ...

    @abc.abstractmethod
    def current_encryption_key_version(self) -> int: ...

    @abc.abstractmethod
    def active_encryption_key_versions(self) -> list[int]: ...

    @abc.abstractmethod
    def blind_index_key(self, version: int) -> bytes: ...

    @abc.abstractmethod
    def current_blind_index_key_version(self) -> int: ...

    @abc.abstractmethod
    def active_blind_index_key_versions(self) -> list[int]: ...

    @abc.abstractmethod
    def rate_limit_key(self, version: int) -> bytes: ...

    @abc.abstractmethod
    def write_rate_limit_key_version(self) -> int: ...

    @abc.abstractmethod
    def active_rate_limit_key_versions(self) -> list[int]: ...


class EnvKeyProvider(KeyProvider):
    """Environment-backed `KeyProvider` for local development, test, and staging.

    Reads only from `django.conf.settings` (itself populated exclusively
    from `os.environ` via `config/settings/_env.py`) -- never a hard-coded
    default, never a value generated and persisted by this class.
    """

    def _settings(self):
        from django.conf import settings

        return settings

    @staticmethod
    def _require(raw: str | None, *, label: str) -> bytes:
        if not raw:
            raise KeyProviderError(f"{label} is not configured.")
        return raw.encode("utf-8")

    def encryption_key(self, version: int) -> bytes:
        raw = self._settings().IDENTITY_ENCRYPTION_KEYS_BY_VERSION.get(version)
        return self._require(raw, label=f"IDENTITY_ENCRYPTION_KEY_V{version}")

    def current_encryption_key_version(self) -> int:
        return self._settings().IDENTITY_ENCRYPTION_WRITE_VERSION

    def active_encryption_key_versions(self) -> list[int]:
        return list(self._settings().IDENTITY_ENCRYPTION_ACTIVE_VERSIONS)

    def blind_index_key(self, version: int) -> bytes:
        raw = self._settings().IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION.get(version)
        return self._require(raw, label=f"IDENTITY_BLIND_INDEX_HMAC_KEY_V{version}")

    def current_blind_index_key_version(self) -> int:
        return self._settings().IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION

    def active_blind_index_key_versions(self) -> list[int]:
        return list(self._settings().IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS)

    def rate_limit_key(self, version: int) -> bytes:
        raw = self._settings().RATE_LIMIT_HMAC_KEYS_BY_VERSION.get(version)
        return self._require(raw, label=f"RATE_LIMIT_HMAC_KEY_V{version}")

    def write_rate_limit_key_version(self) -> int:
        return self._settings().RATE_LIMIT_HMAC_WRITE_VERSION

    def active_rate_limit_key_versions(self) -> list[int]:
        return list(self._settings().RATE_LIMIT_HMAC_ACTIVE_VERSIONS)


_provider: KeyProvider | None = None


def get_key_provider() -> KeyProvider:
    """Return the process-wide `KeyProvider` (lazily constructed `EnvKeyProvider`)."""
    global _provider
    if _provider is None:
        _provider = EnvKeyProvider()
    return _provider


def set_key_provider_for_testing(provider: KeyProvider | None) -> None:
    """Test-only seam: install a fake/synthetic provider, or reset to the default."""
    global _provider
    _provider = provider


# ---------------------------------------------------------------------------
# AES-GCM authenticated field encryption
# ---------------------------------------------------------------------------

_NONCE_LENGTH_BYTES = 12
_VERSION_TAG_PATTERN_PREFIX = "v"
_VERSION_TAG_SEPARATOR = ":"


def _aes_key_from_key_material(key_material: bytes) -> bytes:
    """Derive a fixed 32-byte AES-256 key from arbitrary-length configured key material."""
    return hashlib.sha256(key_material).digest()


def encrypt(plaintext: str, *, provider: KeyProvider | None = None) -> str:
    """Encrypt `plaintext`, returning `"v<version>:<base64(nonce||ciphertext)>"`.

    A fresh random nonce is drawn for every call, so identical plaintext
    never produces identical ciphertext twice. The key version used is
    embedded in the returned string so decryption is rotation-safe without
    a separate database column.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    provider = provider or get_key_provider()
    version = provider.current_encryption_key_version()
    key = _aes_key_from_key_material(provider.encryption_key(version))
    nonce = os.urandom(_NONCE_LENGTH_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return f"{_VERSION_TAG_PATTERN_PREFIX}{version}{_VERSION_TAG_SEPARATOR}{payload}"


def decrypt(stored: str, *, provider: KeyProvider | None = None) -> str:
    """Decrypt a value produced by `encrypt()`. Raises `EncryptionError` on any failure.

    Failure modes (missing key, malformed tag, tampered ciphertext) are
    deliberately collapsed into one generic exception with no diagnostic
    detail that could leak key material, ciphertext, or plaintext.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    provider = provider or get_key_provider()
    try:
        version_tag, payload = stored.split(_VERSION_TAG_SEPARATOR, 1)
        if not version_tag.startswith(_VERSION_TAG_PATTERN_PREFIX):
            raise ValueError("malformed version tag")
        version = int(version_tag[len(_VERSION_TAG_PATTERN_PREFIX) :])
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        nonce, ciphertext = raw[:_NONCE_LENGTH_BYTES], raw[_NONCE_LENGTH_BYTES:]
        key = _aes_key_from_key_material(provider.encryption_key(version))
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except KeyProviderError:
        raise
    except (InvalidTag, ValueError, IndexError, UnicodeDecodeError, binascii.Error):  # fmt: skip
        raise EncryptionError("Decryption failed.") from None


# ---------------------------------------------------------------------------
# Versioned HMAC-SHA-256 blind indexes (identity family)
# ---------------------------------------------------------------------------


def compute_blind_index(
    normalized_value: str, *, version: int, provider: KeyProvider | None = None
) -> str:
    """Return the hex-encoded HMAC-SHA-256 blind index of `normalized_value`.

    `normalized_value` must already be normalized by the caller (e.g.
    lower-cased/trimmed email, canonical E.164 mobile, normalized national
    identifier) -- this function performs no normalization itself, so two
    callers computing an index for "the same" value under different
    normalization rules would silently fail to match.
    """
    provider = provider or get_key_provider()
    key = provider.blind_index_key(version)
    return hmac.new(key, normalized_value.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_blind_indexes_for_active_versions(
    normalized_value: str, *, provider: KeyProvider | None = None
) -> dict[int, str]:
    """Return `{version: blind_index}` for every currently active blind-index key version.

    Used for rotation-safe lookup: search `WHERE value_hash IN (...)` over
    every value in the returned mapping.
    """
    provider = provider or get_key_provider()
    return {
        version: compute_blind_index(normalized_value, version=version, provider=provider)
        for version in provider.active_blind_index_key_versions()
    }


# ---------------------------------------------------------------------------
# Versioned HMAC-SHA-256 rate-limit fingerprints (separate key family, ADR-0007)
# ---------------------------------------------------------------------------


def compute_rate_limit_fingerprint(
    normalized_identity: str, *, version: int, provider: KeyProvider | None = None
) -> bytes:
    """Return the raw HMAC-SHA-256 digest of `normalized_identity` under rate-limit key `version`.

    Raw bytes (not hex) because the only consumer, `apps.core.concurrency`,
    slices the first 4 bytes to derive a PostgreSQL advisory-lock `objid`
    (ADR-0007) -- callers that need a storable/loggable form must hex-encode
    it themselves, and must still never log the underlying identity.
    """
    provider = provider or get_key_provider()
    key = provider.rate_limit_key(version)
    return hmac.new(key, normalized_identity.encode("utf-8"), hashlib.sha256).digest()


def compute_rate_limit_fingerprints_for_active_versions(
    normalized_identity: str, *, provider: KeyProvider | None = None
) -> dict[int, bytes]:
    """Return `{version: digest}` for every currently active rate-limit key version."""
    provider = provider or get_key_provider()
    return {
        version: compute_rate_limit_fingerprint(
            normalized_identity, version=version, provider=provider
        )
        for version in provider.active_rate_limit_key_versions()
    }
