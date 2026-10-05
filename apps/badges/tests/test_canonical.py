"""Canonical serialization, strict base64url, and header/payload allowlists.

None of these tests touches the database: they pin the wire format itself,
which is the part a future change is most likely to break silently.
"""

from __future__ import annotations

import json

import pytest

from apps.badges.credentials import canonical

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


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def test_canonical_header_is_byte_exact():
    """The protected header has exactly one permitted byte form."""
    assert canonical.canonical_header_bytes(key_id="v1") == (
        b'{"alg":"ES256","kid":"v1","typ":"ASC-PASS"}'
    )


def test_canonical_header_members_are_sorted_and_exactly_three():
    parsed = json.loads(canonical.canonical_header_bytes(key_id="v7"))
    assert list(parsed) == ["alg", "kid", "typ"]


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": "v1", "typ": "ASC-PASS"},
        {"alg": "HS256", "kid": "v1", "typ": "ASC-PASS"},
        {"alg": "ES384", "kid": "v1", "typ": "ASC-PASS"},
        {"alg": "ES256", "kid": "v1", "typ": "JWT"},
        {"alg": "ES256", "kid": "v1"},
        {"kid": "v1", "typ": "ASC-PASS"},
        {"alg": "ES256", "kid": "v1", "typ": "ASC-PASS", "crit": ["b64"]},
        {"alg": "ES256", "kid": "v1", "typ": "ASC-PASS", "jwk": {}},
        {"alg": "ES256", "kid": "v1", "typ": "ASC-PASS", "b64": False},
        {"alg": "ES256", "kid": "not-a-key-id", "typ": "ASC-PASS"},
        {"alg": "ES256", "kid": "v" * 40, "typ": "ASC-PASS"},
    ],
)
def test_header_allowlist_rejects_everything_outside_the_three_members(header):
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_header(header)


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def test_canonical_payload_is_sorted_and_compact():
    raw = canonical.canonical_payload_bytes(dict(VALID_CLAIMS))
    assert list(json.loads(raw)) == list(canonical.PAYLOAD_CLAIMS)
    assert b", " not in raw
    assert b'": ' not in raw


def test_payload_claim_set_is_exactly_eleven_names():
    """The allowlist is pinned so a claim cannot be added by accident."""
    assert canonical.PAYLOAD_CLAIMS == (
        "apc",
        "bai",
        "btc",
        "cv",
        "eid",
        "exp",
        "jti",
        "n",
        "nbf",
        "pid",
        "v",
    )
    assert len(canonical.PAYLOAD_CLAIMS) == 11


def test_payload_rejects_an_additional_claim():
    claims = dict(VALID_CLAIMS) | {"email": "someone@example.test"}
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(claims)


def test_payload_rejects_a_missing_claim():
    claims = dict(VALID_CLAIMS)
    del claims["jti"]
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(claims)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("cv", "1"),
        ("cv", 1.0),
        ("cv", True),
        ("v", None),
        ("nbf", "2026"),
        ("exp", {"nested": 1}),
        ("jti", ["nested"]),
        ("apc", 5),
        ("pid", {"a": "b"}),
    ],
)
def test_payload_rejects_wrongly_typed_or_nested_values(claim, value):
    claims = dict(VALID_CLAIMS) | {claim: value}
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(claims)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("jti", "short"),
        ("pid", "!" * 22),
        ("n", "C" * 15),
        ("apc", "has space"),
        ("eid", "E" * 64),
        ("btc", "B" * 128),
    ],
)
def test_payload_rejects_out_of_alphabet_or_out_of_length_values(claim, value):
    claims = dict(VALID_CLAIMS) | {claim: value}
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(claims)


def test_payload_rejects_an_inverted_validity_window():
    claims = dict(VALID_CLAIMS) | {"nbf": 2_000_000_000, "exp": 1_000_000_000}
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.validate_payload(claims)


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


def test_duplicate_json_members_are_rejected():
    raw = b'{"alg":"ES256","alg":"none","kid":"v1","typ":"ASC-PASS"}'
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.loads_strict(raw)


def test_non_object_json_is_rejected():
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.loads_strict(b'["alg"]')


def test_invalid_utf8_is_rejected():
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.loads_strict(b"\xff\xfe")


# ---------------------------------------------------------------------------
# Strict base64url
# ---------------------------------------------------------------------------


def test_base64url_round_trips_without_padding():
    raw = b"the quick brown fox"
    encoded = canonical.b64url_encode(raw)
    assert "=" not in encoded
    assert canonical.b64url_decode(encoded) == raw


def test_padded_base64url_is_rejected():
    encoded = canonical.b64url_encode(b"abcde")
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode(encoded + "=")


def test_standard_base64_alphabet_is_rejected():
    # '+' and '/' belong to standard base64, not base64url.
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode("ab+/")


def test_impossible_segment_length_is_rejected():
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode("abcde")


def test_non_canonical_trailing_bits_are_rejected():
    """Two strings must never decode to identical bytes."""
    canonical_form = canonical.b64url_encode(b"\x00")
    # 'AB' and 'AA' both decode to b"\x00" under a lenient decoder; only the
    # canonical spelling is accepted here.
    assert canonical.b64url_decode(canonical_form) == b"\x00"
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode("AB")


def test_empty_segment_is_rejected():
    with pytest.raises(canonical.CanonicalFormatError):
        canonical.b64url_decode("")


# ---------------------------------------------------------------------------
# Signing input
# ---------------------------------------------------------------------------


def test_signing_input_is_the_rfc7515_concatenation():
    header = canonical.canonical_header_bytes(key_id="v1")
    payload = canonical.canonical_payload_bytes(dict(VALID_CLAIMS))
    value = canonical.signing_input(header_bytes=header, payload_bytes=payload)
    assert value.count(b".") == 1
    left, right = value.split(b".")
    assert canonical.b64url_decode(left.decode()) == header
    assert canonical.b64url_decode(right.decode()) == payload


def test_payload_hash_is_deterministic():
    header = canonical.canonical_header_bytes(key_id="v1")
    payload = canonical.canonical_payload_bytes(dict(VALID_CLAIMS))
    value = canonical.signing_input(header_bytes=header, payload_bytes=payload)
    assert canonical.payload_hash(value) == canonical.payload_hash(value)
    assert len(canonical.payload_hash(value)) == 64
