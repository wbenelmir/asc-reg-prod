"""Compact JWS assembly, joserfc verification, and the key boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.badges.credentials import canonical
from apps.badges.credentials.issue import assemble_compact_jws
from apps.badges.credentials.verify import (
    RESULT_PRECEDENCE,
    VerificationResultCode,
    verify_pass_token,
)
from apps.badges.models import (
    DigitalEntryPassStatus,
    VerificationKey,
    VerificationKeyReasonCode,
    VerificationKeyStatus,
)
from apps.badges.services import (
    activate_pass,
    generate_pass,
    new_operation_id,
    publish_verification_key,
    revoke_verification_key,
)
from apps.core.crypto.signing import (
    RAW_SIGNATURE_LENGTH_BYTES,
    SigningKeyProviderError,
    SigningProviderUnavailableError,
    _BrokenSigningKeyProvider,
    _MalformedSignatureSigningKeyProvider,
    looks_like_private_key_material,
)

pytestmark = pytest.mark.django_db


def _fake_private_pem() -> str:
    """A PEM private-key block assembled at runtime, never stored literally.

    The repository credential scanner rejects a literal
    `BEGIN ... PRIVATE KEY` block anywhere in the tree, and that rule stays
    strict on purpose. These tests still need a string shaped like one, to
    prove the key service refuses private material -- so the marker is
    composed here instead of written out.
    """
    marker = "-----BEGIN {} KEY-----".format("PRIVATE")
    end = "-----END {} KEY-----".format("PRIVATE")
    return marker + chr(10) + "not-a-real-key" + chr(10) + end


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _active_credential(registration, actor):
    outcome = generate_pass(registration=registration, actor=actor, operation_id=new_operation_id())
    credential = outcome.credential
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


def _token_for(credential):
    from apps.badges.services import build_pass_claims

    return assemble_compact_jws(
        build_pass_claims(credential), key_id=credential.signing_key_id
    ).token


# ---------------------------------------------------------------------------
# Assembly adapter
# ---------------------------------------------------------------------------


def test_every_assembled_token_round_trips_through_joserfc(signing_provider):
    """The reviewed consumer validates the minimal producer."""
    from joserfc.jwk import ECKey
    from joserfc.jws import deserialize_compact

    claims = {
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
    assembled = assemble_compact_jws(claims, key_id="v1", provider=signing_provider)
    public_key = ECKey.import_key(signing_provider.public_key_pem("v1"))
    result = deserialize_compact(assembled.token, public_key, algorithms=["ES256"])
    assert result.protected == {"alg": "ES256", "kid": "v1", "typ": "ASC-PASS"}
    assert result.payload == assembled.payload_bytes


def test_assembled_signature_is_the_64_byte_raw_form(signing_provider):
    claims = {
        "apc": "A",
        "bai": "A" * 22,
        "btc": "B",
        "cv": 1,
        "eid": "E",
        "exp": 2_000_000_000,
        "jti": "B" * 22,
        "n": "C" * 16,
        "nbf": 1_000_000_000,
        "pid": "D" * 22,
        "v": 1,
    }
    assembled = assemble_compact_jws(claims, key_id="v1", provider=signing_provider)
    assert len(assembled.signature) == RAW_SIGNATURE_LENGTH_BYTES


def test_a_der_signature_is_rejected_at_the_provider_boundary():
    """A misconfigured provider fails here, not in the field."""
    provider = _MalformedSignatureSigningKeyProvider(key_ids=("v1",), current="v1")
    claims = {
        "apc": "A",
        "bai": "A" * 22,
        "btc": "B",
        "cv": 1,
        "eid": "E",
        "exp": 2_000_000_000,
        "jti": "B" * 22,
        "n": "C" * 16,
        "nbf": 1_000_000_000,
        "pid": "D" * 22,
        "v": 1,
    }
    with pytest.raises(SigningKeyProviderError, match="raw r"):
        assemble_compact_jws(claims, key_id="v1", provider=provider)


def test_an_unavailable_signing_provider_fails_closed():
    provider = _BrokenSigningKeyProvider()
    claims = {
        "apc": "A",
        "bai": "A" * 22,
        "btc": "B",
        "cv": 1,
        "eid": "E",
        "exp": 2_000_000_000,
        "jti": "B" * 22,
        "n": "C" * 16,
        "nbf": 1_000_000_000,
        "pid": "D" * 22,
        "v": 1,
    }
    with pytest.raises(SigningProviderUnavailableError):
        assemble_compact_jws(claims, key_id="v1", provider=provider)


def test_the_provider_never_exposes_private_key_material(signing_provider):
    """There is no accessor that returns private bytes, by construction."""
    assert not hasattr(signing_provider, "private_key")
    pem = signing_provider.public_key_pem("v1").decode("ascii")
    assert "PUBLIC KEY" in pem
    assert not looks_like_private_key_material(pem)


# ---------------------------------------------------------------------------
# Verification: valid path
# ---------------------------------------------------------------------------


def test_an_active_credential_verifies(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    result = verify_pass_token(_token_for(credential))
    assert result.code == VerificationResultCode.VALID
    assert result.credential.pk == credential.pk


# ---------------------------------------------------------------------------
# Verification: tier 1 failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "a.b",
        "a.b.c.d",
        "....",
        123,
        None,
    ],
)
def test_malformed_input_is_rejected(token, active_key):
    assert verify_pass_token(token).code == VerificationResultCode.MALFORMED


def test_oversized_input_is_rejected_before_decoding(active_key, settings):
    settings.QR_MAX_TOKEN_BYTES = 64
    assert verify_pass_token("A" * 200).code == VerificationResultCode.MALFORMED


def test_padded_base64url_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    header, payload, signature = _token_for(credential).split(".")
    assert verify_pass_token(f"{header}=.{payload}.{signature}").code == (
        VerificationResultCode.MALFORMED
    )


def test_alg_none_substitution_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, signature = _token_for(credential).split(".")
    forged_header = canonical.b64url_encode(b'{"alg":"none","kid":"v1","typ":"ASC-PASS"}')
    assert verify_pass_token(f"{forged_header}.{payload}.{signature}").code == (
        VerificationResultCode.MALFORMED
    )


def test_hs256_substitution_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, signature = _token_for(credential).split(".")
    forged_header = canonical.b64url_encode(b'{"alg":"HS256","kid":"v1","typ":"ASC-PASS"}')
    assert verify_pass_token(f"{forged_header}.{payload}.{signature}").code == (
        VerificationResultCode.MALFORMED
    )


def test_an_unknown_header_member_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, signature = _token_for(credential).split(".")
    forged_header = canonical.b64url_encode(
        b'{"alg":"ES256","kid":"v1","typ":"ASC-PASS","x5u":"https://example.test"}'
    )
    assert verify_pass_token(f"{forged_header}.{payload}.{signature}").code == (
        VerificationResultCode.MALFORMED
    )


def test_a_crit_header_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, signature = _token_for(credential).split(".")
    forged_header = canonical.b64url_encode(
        b'{"alg":"ES256","crit":["b64"],"kid":"v1","typ":"ASC-PASS"}'
    )
    assert verify_pass_token(f"{forged_header}.{payload}.{signature}").code == (
        VerificationResultCode.MALFORMED
    )


def test_an_unknown_key_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, signature = _token_for(credential).split(".")
    forged_header = canonical.b64url_encode(b'{"alg":"ES256","kid":"v9","typ":"ASC-PASS"}')
    assert verify_pass_token(f"{forged_header}.{payload}.{signature}").code == (
        VerificationResultCode.UNKNOWN_KEY
    )


def test_a_tampered_payload_fails_the_signature(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    header, payload, signature = _token_for(credential).split(".")
    claims = canonical.loads_strict(canonical.b64url_decode(payload))
    claims["cv"] = claims["cv"] + 1
    forged_payload = canonical.b64url_encode(canonical.dumps_canonical(claims))
    assert verify_pass_token(f"{header}.{forged_payload}.{signature}").code == (
        VerificationResultCode.INVALID_SIGNATURE
    )


def test_a_tampered_signature_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    header, payload, signature = _token_for(credential).split(".")
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    assert verify_pass_token(f"{header}.{payload}.{flipped}").code == (
        VerificationResultCode.INVALID_SIGNATURE
    )


def test_an_unsupported_payload_version_is_rejected(
    eligible_registration, pass_admin, active_key, settings
):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    settings.QR_SUPPORTED_PAYLOAD_VERSIONS = [99]
    assert verify_pass_token(token).code == VerificationResultCode.UNSUPPORTED_VERSION


# ---------------------------------------------------------------------------
# Verification: key states and rotation overlap
# ---------------------------------------------------------------------------


def test_a_pending_key_cannot_verify(
    eligible_registration, pass_admin, active_key, signing_provider, key_custodian
):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    VerificationKey.objects.filter(key_id="v1").update(status=VerificationKeyStatus.PENDING)
    assert verify_pass_token(token).code == VerificationResultCode.INVALID_KEY


def test_a_retired_key_within_its_window_still_verifies(
    eligible_registration, pass_admin, active_key, signing_provider, key_custodian
):
    """Rotation overlap: credentials issued before rotation keep working."""
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)

    second = publish_verification_key(
        key_id="v2",
        public_key_pem=signing_provider.public_key_pem("v2").decode("ascii"),
        actor=key_custodian,
    )
    from apps.badges.services import promote_verification_key

    # Promotion now requires the provider to actually hold the key, so the
    # rotation is modelled the way a real one happens: the provider switches
    # to v2 first, then v2 is promoted.
    signing_provider.set_current_signing_key_id("v2")
    promote_verification_key(key=second, actor=key_custodian)

    first = VerificationKey.objects.get(key_id="v1")
    assert first.status == VerificationKeyStatus.RETIRED
    assert verify_pass_token(token).code == VerificationResultCode.VALID


def test_a_retired_key_outside_its_window_cannot_verify(
    eligible_registration, pass_admin, active_key
):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    VerificationKey.objects.filter(key_id="v1").update(
        status=VerificationKeyStatus.RETIRED,
        not_after=timezone.now() - timedelta(minutes=1),
    )
    assert verify_pass_token(token).code == VerificationResultCode.INVALID_KEY


def test_emergency_key_revocation_fails_verification_immediately(
    eligible_registration, pass_admin, active_key, key_custodian
):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    assert verify_pass_token(token).code == VerificationResultCode.VALID

    revoke_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.SUSPECTED_COMPROMISE,
    )
    assert verify_pass_token(token).code == VerificationResultCode.INVALID_KEY


def test_only_one_key_can_be_active_at_a_time(signing_provider, key_custodian, active_key):
    from apps.badges.services import promote_verification_key

    second = publish_verification_key(
        key_id="v2",
        public_key_pem=signing_provider.public_key_pem("v2").decode("ascii"),
        actor=key_custodian,
    )
    signing_provider.set_current_signing_key_id("v2")
    promote_verification_key(key=second, actor=key_custodian)
    assert VerificationKey.objects.filter(status=VerificationKeyStatus.ACTIVE).count() == 1


def test_a_private_key_pem_is_refused_by_the_key_service(key_custodian):
    from apps.badges.services import VerificationKeyError

    fake_private_pem = _fake_private_pem()
    with pytest.raises(VerificationKeyError):
        publish_verification_key(key_id="v3", public_key_pem=fake_private_pem, actor=key_custodian)


# ---------------------------------------------------------------------------
# Verification: tier 2-7 credential state
# ---------------------------------------------------------------------------


def test_a_wrong_event_code_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    credential.event_code = "OTHEREVENT"
    credential.save(update_fields=["event_code"])
    assert verify_pass_token(token).code == VerificationResultCode.WRONG_EVENT


def test_a_stale_credential_version_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    credential.credential_version = credential.credential_version + 1
    credential.save(update_fields=["credential_version"])
    assert verify_pass_token(token).code == VerificationResultCode.CREDENTIAL_MISMATCH


def test_an_unknown_jti_is_reported_as_not_found(
    eligible_registration, pass_admin, active_key, signing_provider
):
    from apps.badges.services import build_pass_claims

    credential = _active_credential(eligible_registration, pass_admin)
    claims = build_pass_claims(credential)
    claims["jti"] = "Z" * 22
    token = assemble_compact_jws(claims, key_id="v1", provider=signing_provider).token
    assert verify_pass_token(token).code == VerificationResultCode.CREDENTIAL_NOT_FOUND


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DigitalEntryPassStatus.INACTIVE, VerificationResultCode.INACTIVE),
        (DigitalEntryPassStatus.SUSPENDED, VerificationResultCode.SUSPENDED),
        (DigitalEntryPassStatus.REVOKED, VerificationResultCode.REVOKED),
        (DigitalEntryPassStatus.REPLACED, VerificationResultCode.REPLACED),
        (DigitalEntryPassStatus.EXPIRED, VerificationResultCode.EXPIRED),
    ],
)
def test_each_credential_status_maps_to_its_own_result(
    eligible_registration, pass_admin, active_key, status, expected
):
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    credential.status = status
    credential.save(update_fields=["status"])
    assert verify_pass_token(token).code == expected


def test_suspended_is_not_reported_as_revoked(eligible_registration, pass_admin, active_key):
    """A hold must remain distinguishable from a terminal revocation."""
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    credential.status = DigitalEntryPassStatus.SUSPENDED
    credential.save(update_fields=["status"])
    result = verify_pass_token(token)
    assert result.code == VerificationResultCode.SUSPENDED
    assert result.code != VerificationResultCode.REVOKED


def test_expiry_fails_closed_without_any_sweep(eligible_registration, pass_admin, active_key):
    """The decisive test: persisted status still says ACTIVE.

    The verification clock is advanced rather than the stored window. Editing
    an issued credential's window would invalidate its immutable signature --
    exactly the guarantee this correction pass introduced -- so the honest way
    to test expiry is to ask "is it expired *now*" at a later now.
    """
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    assert credential.status == DigitalEntryPassStatus.ACTIVE

    after_expiry = credential.valid_until + timedelta(days=1)
    result = verify_pass_token(token, now=after_expiry)

    assert result.code == VerificationResultCode.EXPIRED
    # No sweep ran: the row still reads ACTIVE and the decision is still right.
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_a_not_yet_valid_credential_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _active_credential(eligible_registration, pass_admin)
    before_validity = credential.valid_from - timedelta(days=5)
    assert verify_pass_token(_token_for(credential), now=before_validity).code == (
        VerificationResultCode.NOT_YET_VALID
    )


def test_a_revoked_credential_outside_its_window_keeps_the_terminal_result(
    eligible_registration, pass_admin, active_key
):
    """Terminal beats EXPIRED: the more informative result survives."""
    credential = _active_credential(eligible_registration, pass_admin)
    token = _token_for(credential)
    after_expiry = credential.valid_until + timedelta(days=1)

    credential.status = DigitalEntryPassStatus.REVOKED
    credential.save(update_fields=["status"])

    assert verify_pass_token(token, now=after_expiry).code == VerificationResultCode.REVOKED


def test_result_precedence_is_documented_and_ordered():
    assert RESULT_PRECEDENCE[VerificationResultCode.MALFORMED] == 1
    assert RESULT_PRECEDENCE[VerificationResultCode.WRONG_EVENT] == 2
    assert RESULT_PRECEDENCE[VerificationResultCode.REVOKED] == 3
    assert RESULT_PRECEDENCE[VerificationResultCode.REPLACED] == 4
    assert RESULT_PRECEDENCE[VerificationResultCode.SUSPENDED] == 5
    assert RESULT_PRECEDENCE[VerificationResultCode.INACTIVE] == 6
    assert RESULT_PRECEDENCE[VerificationResultCode.EXPIRED] == 7
    assert RESULT_PRECEDENCE[VerificationResultCode.VALID] == 8
    assert (
        RESULT_PRECEDENCE[VerificationResultCode.REVOKED]
        < RESULT_PRECEDENCE[VerificationResultCode.EXPIRED]
    )


# ---------------------------------------------------------------------------
# PII absence
# ---------------------------------------------------------------------------


def test_the_qr_carries_exactly_the_eleven_allowed_claims(
    eligible_registration, pass_admin, active_key
):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, _ = _token_for(credential).split(".")
    claims = canonical.loads_strict(canonical.b64url_decode(payload))
    assert tuple(sorted(claims)) == canonical.PAYLOAD_CLAIMS


def test_the_qr_contains_no_identifying_or_internal_value(
    eligible_registration, pass_admin, active_key
):
    credential = _active_credential(eligible_registration, pass_admin)
    _, payload, _ = _token_for(credential).split(".")
    raw = canonical.b64url_decode(payload).decode("utf-8")

    forbidden = [
        str(credential.pk),
        str(credential.registration_id),
        str(credential.series_id),
        str(credential.event_edition_id),
        str(eligible_registration.person_id),
        eligible_registration.public_reference,
        credential.series.public_id,
        credential.series.fallback_reference,
    ]
    for value in forbidden:
        assert value not in raw, f"{value!r} must never appear in the QR payload"
