"""Opaque ES256 signing-key boundary for Offline Packages (Phase 4 Prompt 2).

Binding decision P2-D: Offline Package manifests, critical deltas, operator
grants and emergency-wipe orders are signed with a SEPARATE key family that
never shares material, identifiers, or configuration with the Digital Entry
Pass QR keys (`apps.core.crypto.signing`, ADR-0019):

* keys are read from `OFFLINE_PACKAGE_SIGNING_KEY_V<n>`, never from
  `QR_SIGNING_KEY_V<n>`;
* key identifiers use a distinct alphabet -- `p<n>` here, `v<n>` for QR --
  so a key identifier of one family can never resolve in the other, and a
  device that pinned `p1` can never be tricked into trusting a QR key;
* the provider implements the same `SigningKeyProvider` interface, so it
  exposes signatures and PUBLIC keys only and a future KMS/HSM provider can
  replace it without redesign.

This module implements no cryptographic primitive: signing is delegated to
`cryptography`, and the raw `r || s` encoding reuses
`apps.core.crypto.signing.encode_raw_signature`.
"""

from __future__ import annotations

import re

from apps.core.crypto.signing import (
    SIGNING_ALGORITHM,
    SigningKeyProvider,
    SigningKeyProviderError,
    _normalize_pem,
    encode_raw_signature,
)

#: `p1` .. `p9999`. Deliberately disjoint from the QR family's `v<n>`.
PACKAGE_KEY_ID_PATTERN = re.compile(r"^p[1-9][0-9]{0,3}$")
MAX_PACKAGE_KEY_ID_LENGTH = 8


def package_key_id_for_version(version: int) -> str:
    return f"p{version}"


def is_valid_package_key_id(key_id: object) -> bool:
    if not isinstance(key_id, str) or len(key_id) > MAX_PACKAGE_KEY_ID_LENGTH:
        return False
    return PACKAGE_KEY_ID_PATTERN.fullmatch(key_id) is not None


def package_version_for_key_id(key_id: str) -> int:
    if not is_valid_package_key_id(key_id):
        raise SigningKeyProviderError("Malformed package signing key identifier.")
    return int(key_id[1:])


class EnvPackageSigningKeyProvider(SigningKeyProvider):
    """Environment-backed package-signing provider (local, test, staging).

    Reads only `OFFLINE_PACKAGE_SIGNING_*` settings, which
    `config/settings/base.py` populates exclusively from `os.environ`. It never
    generates, persists, or returns private key material. Production is
    expected to substitute a managed KMS/HSM provider; that substitution is
    deployment-only and not proven by any local test.
    """

    def _settings(self):
        from django.conf import settings

        return settings

    def _private_key_object(self, key_id: str):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        version = package_version_for_key_id(key_id)
        raw = self._settings().OFFLINE_PACKAGE_SIGNING_KEYS_BY_VERSION.get(version)
        if not raw:
            raise SigningKeyProviderError(
                f"OFFLINE_PACKAGE_SIGNING_KEY_V{version} is not configured."
            )
        try:
            key = load_pem_private_key(_normalize_pem(raw), password=None)
        except Exception as exc:  # noqa: BLE001 - never leak the underlying detail
            raise SigningKeyProviderError(
                f"Configured package signing key {key_id} could not be loaded."
            ) from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise SigningKeyProviderError(
                f"Configured package signing key {key_id} is not a P-256 key."
            )
        return key

    def current_signing_key_id(self) -> str:
        return package_key_id_for_version(
            self._settings().OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION
        )

    def active_signing_key_ids(self) -> list[str]:
        return [
            package_key_id_for_version(v)
            for v in self._settings().OFFLINE_PACKAGE_SIGNING_ACTIVE_KEY_VERSIONS
        ]

    def signing_algorithm(self, key_id: str) -> str:
        if not is_valid_package_key_id(key_id):
            raise SigningKeyProviderError("Malformed package signing key identifier.")
        return SIGNING_ALGORITHM

    def sign(self, key_id: str, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        if not isinstance(message, bytes):
            raise SigningKeyProviderError("Signing input must be bytes.")
        der_signature = self._private_key_object(key_id).sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        return encode_raw_signature(r, s)

    def public_key_pem(self, key_id: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        return (
            self._private_key_object(key_id)
            .public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )

    def is_configured(self) -> bool:
        """True if the current write key is present (used by system checks).

        Presence only: the value is never returned, compared, or logged.
        """
        settings = self._settings()
        return bool(
            settings.OFFLINE_PACKAGE_SIGNING_KEYS_BY_VERSION.get(
                settings.OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION
            )
        )


class InMemoryPackageSigningKeyProvider(SigningKeyProvider):
    """TEST-ONLY package-signing provider with ephemeral in-memory P-256 keys.

    Nothing is written to disk, the database, a fixture, or an archive. Only
    `set_package_signing_key_provider_for_testing` installs it.
    """

    def __init__(self, *, key_ids: tuple[str, ...] = ("p1",), current: str = "p1") -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        for key_id in (*key_ids, current):
            if not is_valid_package_key_id(key_id):
                raise SigningKeyProviderError("Malformed package signing key identifier.")
        self._keys = {key_id: ec.generate_private_key(ec.SECP256R1()) for key_id in key_ids}
        if current not in self._keys:
            raise SigningKeyProviderError("Current key identifier is not among the keys.")
        self._current = current

    def current_signing_key_id(self) -> str:
        return self._current

    def set_current_signing_key_id(self, key_id: str) -> None:
        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown package signing key identifier.")
        self._current = key_id

    def active_signing_key_ids(self) -> list[str]:
        return sorted(self._keys)

    def signing_algorithm(self, key_id: str) -> str:
        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown package signing key identifier.")
        return SIGNING_ALGORITHM

    def sign(self, key_id: str, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown package signing key identifier.")
        if not isinstance(message, bytes):
            raise SigningKeyProviderError("Signing input must be bytes.")
        r, s = decode_dss_signature(self._keys[key_id].sign(message, ec.ECDSA(hashes.SHA256())))
        return encode_raw_signature(r, s)

    def public_key_pem(self, key_id: str) -> bytes:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        if key_id not in self._keys:
            raise SigningKeyProviderError("Unknown package signing key identifier.")
        return (
            self._keys[key_id]
            .public_key()
            .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )

    def is_configured(self) -> bool:
        return True


_package_provider: SigningKeyProvider | None = None


def get_package_signing_key_provider() -> SigningKeyProvider:
    """Return the process-wide package-signing provider (lazily constructed)."""
    global _package_provider
    if _package_provider is None:
        _package_provider = EnvPackageSigningKeyProvider()
    return _package_provider


def set_package_signing_key_provider_for_testing(provider: SigningKeyProvider | None) -> None:
    """Test-only seam: install an ephemeral provider, or reset to the default."""
    global _package_provider
    _package_provider = provider
