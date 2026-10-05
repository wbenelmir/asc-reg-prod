"""Field encryption and versioned HMAC blind-index tests (ADR-0006).

No database access required -- pure crypto-layer unit tests against the
`EnvKeyProvider`, which itself only reads Django settings (already
populated with test-only synthetic key material by `config/settings/test.py`).
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.core.crypto import (
    EncryptionError,
    EnvKeyProvider,
    KeyProviderError,
    compute_blind_index,
    compute_blind_indexes_for_active_versions,
    decrypt,
    encrypt,
)


def test_encrypt_decrypt_round_trip() -> None:
    plaintext = "participant@example.com"
    ciphertext = encrypt(plaintext)
    assert decrypt(ciphertext) == plaintext


def test_ciphertext_is_randomized_for_identical_plaintext() -> None:
    plaintext = "same-value@example.com"
    first = encrypt(plaintext)
    second = encrypt(plaintext)
    assert first != second
    assert decrypt(first) == decrypt(second) == plaintext


def test_ciphertext_embeds_key_version_tag() -> None:
    ciphertext = encrypt("value")
    assert ciphertext.startswith("v1:")


def test_tampered_ciphertext_fails_authentication() -> None:
    ciphertext = encrypt("value")
    tag, payload = ciphertext.split(":", 1)
    tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
    tampered = f"{tag}:{tampered_payload}"
    with pytest.raises(EncryptionError):
        decrypt(tampered)


def test_malformed_stored_value_fails_closed() -> None:
    with pytest.raises(EncryptionError):
        decrypt("not-a-valid-stored-value")


def test_decrypt_reads_across_key_rotation() -> None:
    # Encrypted under the default test key-version-1 configuration.
    old_ciphertext = encrypt("value-encrypted-under-v1")
    assert old_ciphertext.startswith("v1:")

    # Simulate rotation: version 2 becomes the write version, but version 1
    # remains an active (readable) version.
    with override_settings(
        IDENTITY_ENCRYPTION_ACTIVE_VERSIONS=[1, 2],
        IDENTITY_ENCRYPTION_WRITE_VERSION=2,
        IDENTITY_ENCRYPTION_KEYS_BY_VERSION={
            1: "test-only-identity-encryption-key-v1-not-real",
            2: "test-only-identity-encryption-key-v2-not-real",
        },
    ):
        new_ciphertext = encrypt("value-encrypted-under-v2")
        assert new_ciphertext.startswith("v2:")

        # Both remain decryptable regardless of which version is currently
        # the write version -- decryption always follows the tag in the
        # ciphertext, not the currently configured write version.
        assert decrypt(old_ciphertext) == "value-encrypted-under-v1"
        assert decrypt(new_ciphertext) == "value-encrypted-under-v2"


@override_settings(IDENTITY_ENCRYPTION_KEYS_BY_VERSION={})
def test_encrypt_fails_closed_when_key_missing() -> None:
    with pytest.raises(KeyProviderError):
        encrypt("value")


def test_blind_index_is_stable_for_a_fixed_key_version() -> None:
    first = compute_blind_index("normalized@example.com", version=1)
    second = compute_blind_index("normalized@example.com", version=1)
    assert first == second
    assert len(first) == 64  # SHA-256 hex digest


def test_blind_index_differs_across_normalized_values() -> None:
    a = compute_blind_index("a@example.com", version=1)
    b = compute_blind_index("b@example.com", version=1)
    assert a != b


@override_settings(
    IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS=[1, 2],
    IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION={
        1: "test-only-blind-index-key-v1-not-real",
        2: "test-only-blind-index-key-v2-not-real",
    },
)
def test_lookup_across_active_blind_index_versions_finds_old_and_new() -> None:
    normalized = "rotating@example.com"
    v1_index = compute_blind_index(normalized, version=1)
    v2_index = compute_blind_index(normalized, version=2)
    assert v1_index != v2_index

    candidates = compute_blind_indexes_for_active_versions(normalized)
    assert set(candidates.values()) == {v1_index, v2_index}


@override_settings(IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION={})
def test_blind_index_fails_closed_when_key_missing() -> None:
    with pytest.raises(KeyProviderError):
        compute_blind_index("value", version=1)


def test_encrypted_value_never_appears_in_provider_repr_or_error() -> None:
    provider = EnvKeyProvider()
    plaintext = "super-secret-national-id-123456"
    ciphertext = encrypt(plaintext, provider=provider)
    assert plaintext not in repr(provider)
    assert plaintext not in ciphertext

    try:
        decrypt("garbage", provider=provider)
    except EncryptionError as exc:
        assert plaintext not in str(exc)
