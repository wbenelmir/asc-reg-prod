"""Phase 3 Prompt 8 (P8-04): whole-string validation of every QR text value.

Python's `re.match` with a `$` anchor also matches immediately before a
trailing newline, so `^[A-Za-z0-9][A-Za-z0-9_.-]*$` accepted "ASC26\\n". The
QR contract (`docs/security/qr_contract.md` §2-§3) rejects any character
outside the declared alphabet, so every validator must check the WHOLE
string. These tests pin that at each layer: the claim validators, the key
identifier, strict base64url, and the public verification boundary.
"""

from __future__ import annotations

import pytest

from apps.badges.credentials import canonical
from apps.badges.credentials.verify import VerificationResultCode, verify_pass_token
from apps.core.crypto.signing import is_valid_key_id

VALID_CLAIMS = {
    "apc": "EXHIBITOR",
    "bai": "A" * 22,
    "btc": "STANDARD",
    "cv": 1,
    "eid": "ASC2026",
    "exp": 2_000_000_000,
    "jti": "B" * 22,
    "n": "C" * 16,
    "nbf": 1_000_000_000,
    "pid": "D" * 22,
    "v": 1,
}

#: A newline (the case `$` silently accepted) plus other trailing control
#: characters that must never be treated as part of an alphabet.
TRAILING_CONTROLS = ["\n", "\r", "\r\n", "\t", "\x00", "\x0b", "\x0c", "\x1f", "\x7f"]

CODE_CLAIMS = ["apc", "btc", "eid"]
OPAQUE_CLAIMS = ["bai", "jti", "pid", "n"]


@pytest.mark.parametrize("suffix", TRAILING_CONTROLS)
@pytest.mark.parametrize("claim", CODE_CLAIMS)
def test_code_claims_reject_a_trailing_control_character(claim, suffix):
    payload = dict(VALID_CLAIMS)
    payload[claim] = payload[claim] + suffix
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(payload)


@pytest.mark.parametrize("suffix", TRAILING_CONTROLS)
@pytest.mark.parametrize("claim", OPAQUE_CLAIMS)
def test_opaque_claims_reject_a_trailing_control_character(claim, suffix):
    payload = dict(VALID_CLAIMS)
    # Same length as a valid value, so ONLY the alphabet check can refuse it.
    original = payload[claim]
    payload[claim] = original[: len(original) - len(suffix)] + suffix
    assert len(payload[claim]) == len(original)
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(payload)
    # And the longer variant is refused as well.
    payload[claim] = original + suffix
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(payload)


@pytest.mark.parametrize("suffix", TRAILING_CONTROLS)
def test_header_kid_rejects_a_trailing_control_character(suffix):
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_header({"alg": "ES256", "kid": "v1" + suffix, "typ": "ASC-PASS"})


@pytest.mark.parametrize("suffix", TRAILING_CONTROLS)
def test_is_valid_key_id_rejects_a_trailing_control_character(suffix):
    assert is_valid_key_id("v1") is True
    assert is_valid_key_id("v1" + suffix) is False
    assert is_valid_key_id("v9999" + suffix) is False


@pytest.mark.parametrize("suffix", TRAILING_CONTROLS)
def test_base64url_rejects_a_trailing_control_character(suffix):
    segment = canonical.b64url_encode(b"abcdef")
    assert canonical.b64url_decode(segment) == b"abcdef"
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode(segment + suffix)


def test_valid_values_are_still_accepted_unchanged():
    """No length or alphabet restriction was loosened -- or tightened."""
    assert canonical.validate_payload(dict(VALID_CLAIMS)) == VALID_CLAIMS
    assert canonical.canonical_header_bytes(key_id="v1") == (
        b'{"alg":"ES256","kid":"v1","typ":"ASC-PASS"}'
    )
    assert is_valid_key_id("v1234") is True
    assert is_valid_key_id("v12345") is False
    assert is_valid_key_id("v0") is False


# ---------------------------------------------------------------------------
# Public verification boundary
# ---------------------------------------------------------------------------


def _raw_token(*, header: dict, payload: dict, signer=None) -> str:
    """Assemble a compact JWS WITHOUT the canonical validators.

    The production assembly adapter now refuses these values, which is the
    point; the verifier must refuse them independently, so the bytes are
    built here by hand.
    """
    header_bytes = canonical.dumps_canonical(header)
    payload_bytes = canonical.dumps_canonical(payload)
    signing_input = canonical.signing_input(header_bytes=header_bytes, payload_bytes=payload_bytes)
    signature = signer(signing_input) if signer is not None else b"\x01" * 64
    return f"{signing_input.decode('ascii')}.{canonical.b64url_encode(signature)}"


@pytest.mark.django_db
@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "\x00"])
def test_verification_refuses_a_kid_with_a_trailing_control_character(suffix, active_key):
    token = _raw_token(
        header={"alg": "ES256", "kid": "v1" + suffix, "typ": "ASC-PASS"},
        payload=dict(VALID_CLAIMS),
    )
    result = verify_pass_token(token)
    # Refused as a structural failure BEFORE any key lookup -- previously it
    # passed header validation and fell through to UNKNOWN_KEY.
    assert result.code == VerificationResultCode.MALFORMED
    assert result.detail.get("reason") == "HEADER"
    assert result.credential is None


@pytest.mark.django_db
@pytest.mark.parametrize("claim", CODE_CLAIMS)
@pytest.mark.parametrize("suffix", ["\n", "\r", "\x00"])
def test_verification_refuses_a_validly_signed_payload_with_a_trailing_control(
    claim, suffix, active_key, signing_provider
):
    """Even a GENUINE signature over such a payload is refused as malformed."""
    payload = dict(VALID_CLAIMS)
    payload[claim] = payload[claim] + suffix
    token = _raw_token(
        header={"alg": "ES256", "kid": "v1", "typ": "ASC-PASS"},
        payload=payload,
        signer=lambda message: signing_provider.sign("v1", message),
    )
    result = verify_pass_token(token)
    assert result.code == VerificationResultCode.MALFORMED
    assert result.detail.get("reason") == "PAYLOAD"
    assert result.credential is None


def test_the_assembly_adapter_refuses_to_sign_such_a_value(signing_provider):
    from apps.badges.credentials.issue import assemble_compact_jws

    payload = dict(VALID_CLAIMS)
    payload["btc"] = "STANDARD\n"
    with pytest.raises(canonical.CanonicalFormatError):
        assemble_compact_jws(payload, key_id="v1", provider=signing_provider)
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.canonical_header_bytes(key_id="v1\n")
