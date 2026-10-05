"""Committed deterministic PUBLIC test vectors.

These vectors are the contract an independent verifier (including a future
Phase 4 offline verifier) can code against without access to this codebase.

They pin the canonical signing input, the payload hash, and the
verification outcome of a COMMITTED signature. They deliberately do not
pin a regenerated signature: ECDSA draws a random nonce, so re-signing the
same input produces different bytes, and claiming otherwise would be false.

No private key is committed, including a test key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.conf import settings

from apps.badges.credentials import canonical
from apps.badges.credentials.verify import VerificationResultCode, verify_pass_token

VECTORS_PATH = Path(settings.BASE_DIR) / "tests" / "assets" / "qr_vectors" / "vectors.json"


def _publish_and_activate_vector_key(vectors, key_custodian):
    """Publish the vector's PUBLIC key and mark it ACTIVE for verification.

    Deliberately bypasses `promote_verification_key`: promotion now requires
    the signing provider to hold the matching PRIVATE key, and these vectors
    ship a public key only -- by design, since no private key may ever be
    committed. These tests exercise verification, not promotion, which has its
    own dedicated alignment tests.
    """
    from apps.badges.models import VerificationKey, VerificationKeyStatus
    from apps.badges.services import publish_verification_key

    key = publish_verification_key(
        key_id=vectors["key_id"],
        public_key_pem=vectors["public_key_pem"],
        actor=key_custodian,
    )
    VerificationKey.objects.filter(pk=key.pk).update(status=VerificationKeyStatus.ACTIVE)
    return VerificationKey.objects.get(pk=key.pk)


@pytest.fixture(scope="module")
def vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_the_vector_file_contains_no_private_key_material(vectors):
    """The single most important assertion in this module."""
    from apps.core.crypto.signing import looks_like_private_key_material

    raw = VECTORS_PATH.read_text(encoding="utf-8")
    assert not looks_like_private_key_material(raw)
    # Match the PEM delimiter form specifically: the file's own prose
    # legitimately says "no private key is present", and a bare substring
    # check would flag that sentence instead of real key material.
    assert "PRIVATE KEY-----" not in raw.upper()
    assert "BEGIN PUBLIC KEY" in vectors["public_key_pem"]


def test_the_vectors_declare_es256_only(vectors):
    assert vectors["algorithm"] == "ES256"


# ---------------------------------------------------------------------------
# Canonical byte forms
# ---------------------------------------------------------------------------


def test_the_canonical_header_bytes_are_reproducible(vectors):
    expected = vectors["canonical"]["protected_header_bytes"].encode("ascii")
    assert canonical.canonical_header_bytes(key_id=vectors["key_id"]) == expected


def test_the_canonical_payload_bytes_are_reproducible(vectors):
    claims = vectors["canonical"]["claims"]
    expected = vectors["canonical"]["payload_bytes"].encode("utf-8")
    assert canonical.canonical_payload_bytes(dict(claims)) == expected


def test_the_signing_input_and_payload_hash_are_reproducible(vectors):
    header = canonical.canonical_header_bytes(key_id=vectors["key_id"])
    payload = canonical.canonical_payload_bytes(dict(vectors["canonical"]["claims"]))
    value = canonical.signing_input(header_bytes=header, payload_bytes=payload)
    assert value.decode("ascii") == vectors["canonical"]["signing_input"]
    assert canonical.payload_hash(value) == vectors["canonical"]["payload_hash_sha256"]


# ---------------------------------------------------------------------------
# Signature verification against the committed public key
# ---------------------------------------------------------------------------


def test_the_committed_signature_verifies_with_the_committed_public_key(vectors):
    from joserfc.jwk import ECKey
    from joserfc.jws import deserialize_compact

    case = next(c for c in vectors["cases"] if c["name"] == "valid")
    public_key = ECKey.import_key(vectors["public_key_pem"].encode("ascii"))
    result = deserialize_compact(case["compact_jws"], public_key, algorithms=["ES256"])
    assert result.protected["alg"] == "ES256"
    assert result.protected["kid"] == vectors["key_id"]
    assert result.protected["typ"] == "ASC-PASS"


def test_every_invalid_vector_fails_signature_verification(vectors):
    from joserfc.jwk import ECKey
    from joserfc.jws import deserialize_compact

    public_key = ECKey.import_key(vectors["public_key_pem"].encode("ascii"))
    for case in vectors["cases"]:
        if case["signature_valid"]:
            continue
        with pytest.raises(Exception):  # noqa: B017 - any failure is the point
            deserialize_compact(case["compact_jws"], public_key, algorithms=["ES256"])


# ---------------------------------------------------------------------------
# End-to-end result codes through the project verifier
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_vector_produces_its_documented_result(vectors, key_custodian):
    """The committed expectations are the contract, not an approximation."""
    _publish_and_activate_vector_key(vectors, key_custodian)

    for case in vectors["cases"]:
        result = verify_pass_token(case["compact_jws"])
        assert result.code == case["expected_structural_result"], (
            f"vector {case['name']!r} produced {result.code}, "
            f"expected {case['expected_structural_result']}"
        )


@pytest.mark.django_db
def test_the_valid_vector_gets_past_signature_verification(vectors, key_custodian):
    """CREDENTIAL_NOT_FOUND proves the signature itself was accepted."""
    _publish_and_activate_vector_key(vectors, key_custodian)

    case = next(c for c in vectors["cases"] if c["name"] == "valid")
    result = verify_pass_token(case["compact_jws"])
    assert result.code == VerificationResultCode.CREDENTIAL_NOT_FOUND
    assert result.code != VerificationResultCode.INVALID_SIGNATURE
