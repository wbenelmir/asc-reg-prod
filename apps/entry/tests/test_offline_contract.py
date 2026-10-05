"""Offline Package contract: allow-lists, bands, state/action matrix, crypto
compositions, key-family separation, redaction, fail-closed checks
(Phase 4 Prompt 2). No database.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from apps.entry import offline_contract as contract
from apps.entry.services import offline_crypto as oc

# ---------------------------------------------------------------------------
# A minimal valid body, used to prove every level rejects undeclared fields
# ---------------------------------------------------------------------------


def _body():
    entry = {
        "jti": "j" * 22,
        "pid": "p" * 22,
        "bai": "b" * 22,
        "cv": 1,
        "apc": "GENERAL",
        "btc": "STANDARD",
        "valid_from": 1,
        "valid_until": 2,
        "display_name": "Synthetic",
        "badge_label": {"en": "Standard", "fr": "Standard", "ar": "Standard"},
        "assignment_until": None,
        "profile_window": {"from": None, "until": None},
        "reentry": "ALLOW_REENTRY",
        "zone_rules": [
            {"zone": "MAIN", "rules": [{"effect": "ALLOW", "from": None, "until": None}]}
        ],
        "restrictions": [
            {"severity": "MANUAL_REVIEW", "overrideable": False, "from": 1, "until": None}
        ],
        "last_admitted_at": None,
    }
    return {
        "schema_version": 1,
        "package_id": "x" * 22,
        "package_version": 1,
        "device": "d" * 22,
        "event": "ENTTEST",
        "gate": "GA",
        "zones": ["MAIN"],
        "scope_version": 1,
        "sensitivity": "STANDARD",
        "issued_at": 1,
        "data_cutoff_at": 1,
        "aging_at": 2,
        "stale_at": 3,
        "expires_at": 4,
        "entries": [entry],
        "revoked_passes": [{"jti": "r" * 22, "status": "REVOKED"}],
        "qr_keys": [
            {"kid": "v1", "spki": "k", "status": "ACTIVE", "not_before": None, "not_after": None}
        ],
        "override_reasons": [
            {
                "code": "C",
                "names": {"en": "a", "fr": "a", "ar": "a"},
                "overridable_reason_codes": ["PASS_EXPIRED"],
                "requires_note": True,
            }
        ],
    }


def test_valid_body_passes():
    contract.validate_package_body(_body())


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("entries", 0),
        ("entries", 0, "badge_label"),
        ("entries", 0, "profile_window"),
        ("entries", 0, "zone_rules", 0),
        ("entries", 0, "zone_rules", 0, "rules", 0),
        ("entries", 0, "restrictions", 0),
        ("revoked_passes", 0),
        ("qr_keys", 0),
        ("override_reasons", 0),
        ("override_reasons", 0, "names"),
    ],
)
@pytest.mark.parametrize("field", ["nin", "email", "photo", "registration_reference", "category"])
def test_an_undeclared_field_at_any_level_is_refused(path, field):
    body = _body()
    target = body
    for step in path:
        target = target[step]
    target[field] = "sentinel"
    with pytest.raises(contract.OfflineContractError) as exc:
        contract.validate_package_body(body)
    assert "sentinel" not in str(exc.value)  # names only, never values


def test_a_missing_field_or_a_nested_value_is_refused():
    body = _body()
    del body["entries"][0]["pid"]
    with pytest.raises(contract.OfflineContractError):
        contract.validate_package_body(body)
    body = _body()
    body["entries"][0]["display_name"] = {"nested": "value"}
    with pytest.raises(contract.OfflineContractError):
        contract.validate_package_body(body)
    body = _body()
    body["schema_version"] = 2
    with pytest.raises(contract.OfflineContractError):
        contract.validate_package_body(body)


def test_allow_lists_exclude_every_forbidden_concept():
    names = " ".join(
        sorted(
            contract.PACKAGE_BODY
            | contract.PACKAGE_ENTRY
            | contract.RESTRICTION
            | contract.DELTA_BODY
            | contract.PACKAGE_MANIFEST
        )
    ).lower()
    for forbidden in (
        "nin",
        "passport",
        "email",
        "phone",
        "photo",
        "reference",
        "hint",
        "fingerprint",
        "category",
        "role",
        "document",
        "note",
    ):
        assert forbidden not in names.split(), forbidden
        assert not any(forbidden in name for name in names.split() if name != "override_reasons")


def test_delta_body_validation():
    body = {
        "schema_version": 1,
        "package_id": "x",
        "package_version": 1,
        "delta_version": 1,
        "device": "d",
        "scope_version": 1,
        "issued_at": 1,
        "critical_delta_cutoff_at": 1,
        "passes": [{"jti": "j", "status": "REVOKED"}],
        "restrictions": [],
        "access_withdrawn": [{"jti": "j", "reason": "ASSIGNMENT_CHANGED"}],
        "withdrawn_access_profiles": [],
        "revoked_kids": [],
        "rebuild_required": False,
        "rebuild_reasons": [],
    }
    contract.validate_delta_body(body)
    bad = copy.deepcopy(body)
    bad["passes"][0]["status"] = "ACTIVE"
    with pytest.raises(contract.OfflineContractError):
        contract.validate_delta_body(bad)
    bad = copy.deepcopy(body)
    bad["access_withdrawn"][0]["reason"] = "NEW_ACCESS_GRANTED"
    with pytest.raises(contract.OfflineContractError):
        contract.validate_delta_body(bad)


# ---------------------------------------------------------------------------
# Validity bands (binding decision P2-B)
# ---------------------------------------------------------------------------


def test_standard_and_sensitive_bands(settings):
    cutoff = datetime(2026, 11, 30, 8, 0, tzinfo=UTC)
    far = cutoff + timedelta(days=5)
    standard = contract.package_bands(
        data_cutoff_at=cutoff, sensitivity="STANDARD", event_timezone="UTC", device_expires_at=far
    )
    assert standard.aging_at - cutoff == timedelta(minutes=15)
    assert standard.stale_at - cutoff == timedelta(hours=2)
    assert standard.expires_at - cutoff == timedelta(hours=8)
    sensitive = contract.package_bands(
        data_cutoff_at=cutoff, sensitivity="SENSITIVE", event_timezone="UTC", device_expires_at=far
    )
    assert sensitive.aging_at - cutoff == timedelta(minutes=5)
    assert sensitive.stale_at - cutoff == timedelta(minutes=30)
    assert sensitive.expires_at - cutoff == timedelta(hours=2)


def test_hard_expiry_is_capped_by_event_day_end_in_the_event_timezone():
    # 20:00 UTC is 21:00 in Africa/Algiers (UTC+1): the local day ends at
    # 23:00 UTC, before the 8-hour maximum.
    cutoff = datetime(2026, 11, 30, 20, 0, tzinfo=UTC)
    bands = contract.package_bands(
        data_cutoff_at=cutoff,
        sensitivity="STANDARD",
        event_timezone="Africa/Algiers",
        device_expires_at=cutoff + timedelta(days=5),
    )
    assert bands.expires_at == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
    assert bands.stale_at == cutoff + timedelta(hours=2)


def test_hard_expiry_is_capped_by_device_expiry():
    cutoff = datetime(2026, 11, 30, 8, 0, tzinfo=UTC)
    bands = contract.package_bands(
        data_cutoff_at=cutoff,
        sensitivity="STANDARD",
        event_timezone="UTC",
        device_expires_at=cutoff + timedelta(minutes=10),
    )
    assert bands.expires_at == cutoff + timedelta(minutes=10)
    assert bands.aging_at == bands.stale_at == bands.expires_at


def test_a_package_that_would_expire_at_once_is_refused():
    cutoff = datetime(2026, 11, 30, 23, 59, 30, tzinfo=UTC)
    with pytest.raises(contract.ValidityWindowError):
        contract.package_bands(
            data_cutoff_at=cutoff,
            sensitivity="STANDARD",
            event_timezone="UTC",
            device_expires_at=cutoff + timedelta(days=1),
        )


def test_band_at_expired_wins():
    t0 = datetime(2026, 11, 30, 8, 0, tzinfo=UTC)
    kwargs = {"aging_at": t0, "stale_at": t0 + timedelta(1), "expires_at": t0 + timedelta(2)}
    assert contract.band_at(t0 - timedelta(seconds=1), **kwargs) == "FRESH"
    assert contract.band_at(t0, **kwargs) == "AGING"
    assert contract.band_at(t0 + timedelta(1), **kwargs) == "STALE"
    assert contract.band_at(t0 + timedelta(2), **kwargs) == "EXPIRED"


# ---------------------------------------------------------------------------
# The seven states and their permitted actions
# ---------------------------------------------------------------------------


def test_every_required_state_has_an_explicit_action_set():
    assert contract.OPERATIONAL_STATES == (
        "ONLINE",
        "OFFLINE_READY",
        "OFFLINE_ACTIVE",
        "SYNCING",
        "STALE",
        "EXPIRED",
        "BLOCKED",
    )
    assert set(contract.PERMITTED_ACTIONS) == set(contract.OPERATIONAL_STATES)
    for actions in contract.PERMITTED_ACTIONS.values():
        assert actions <= set(contract.ACTIONS)


def test_admission_is_possible_only_in_offline_active():
    for state, actions in contract.PERMITTED_ACTIONS.items():
        allowed = actions & contract.ADMISSION_ACTIONS
        if state == "OFFLINE_ACTIVE":
            assert allowed == contract.ADMISSION_ACTIONS
        else:
            assert not allowed, state


def test_stale_permits_only_manual_review_or_do_not_admit():
    stale = contract.PERMITTED_ACTIONS["STALE"]
    assert "ADMIT_OFFLINE" not in stale and "OVERRIDE_OFFLINE" not in stale
    assert {"MANUAL_REVIEW_OFFLINE", "DO_NOT_ADMIT_OFFLINE"} <= stale


def test_expired_never_verifies_or_admits_and_blocked_permits_nothing_but_procedure():
    expired = contract.PERMITTED_ACTIONS["EXPIRED"]
    assert "VERIFY_OFFLINE_QR" not in expired and "ADMIT_OFFLINE" not in expired
    assert contract.PERMITTED_ACTIONS["BLOCKED"] == {"MANUAL_PROCEDURE"}


def test_syncing_never_admits_offline_while_the_cloud_is_healthy():
    syncing = contract.PERMITTED_ACTIONS["SYNCING"]
    assert not {"VERIFY_OFFLINE_QR", "ADMIT_OFFLINE", "OVERRIDE_OFFLINE"} & syncing
    assert "VERIFY_ONLINE" in syncing


def test_client_config_carries_no_secret(settings):
    text = json.dumps(contract.client_config())
    for word in ("-----BEGIN", "SIGNING_KEY", "SECRET", "PASSWORD"):
        assert word not in text.upper()


# ---------------------------------------------------------------------------
# Crypto compositions (P2-D)
# ---------------------------------------------------------------------------


def test_b64url_and_canonical_json_are_strict():
    assert oc.b64url_decode(oc.b64url_encode(b"\x00\xff")) == b"\x00\xff"
    for bad in ("AA==", "A+B/", "AB", 5):
        with pytest.raises(oc.OfflineCryptoError):
            oc.b64url_decode(bad)
    assert (
        oc.canonical_json({"b": 1, "a": [True, None, "é"]})
        == '{"a":[true,null,"é"],"b":1}'.encode()
    )
    with pytest.raises(oc.OfflineCryptoError):
        oc.canonical_json({"a": 1.5})


def test_device_public_key_parsing_is_strict():
    good = ec.generate_private_key(ec.SECP256R1())
    spki = oc.b64url_encode(oc.public_key_der(good.public_key()))
    oc.load_device_public_key(spki)
    other_curve = oc.b64url_encode(
        oc.public_key_der(ec.generate_private_key(ec.SECP384R1()).public_key())
    )
    for bad in (other_curve, "not-a-key", "", "A" * 400):
        with pytest.raises(oc.OfflineCryptoError):
            oc.load_device_public_key(bad)


def _signer():
    from apps.core.crypto.package_signing import InMemoryPackageSigningKeyProvider

    return InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")


def _pinned(provider):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return {
        kid: oc.public_key_der(load_pem_public_key(provider.public_key_pem(kid)))
        for kid in provider.active_signing_key_ids()
    }


def test_jws_round_trip_with_pinned_keys_and_typ_separation():
    provider = _signer()
    compact, kid = oc.jws_sign({"a": 1}, typ=contract.TYP_PACKAGE_MANIFEST, provider=provider)
    assert kid == "p1"
    assert oc.jws_verify(compact, trusted_keys_der=_pinned(provider), typ="ASC-OPKG") == {"a": 1}
    with pytest.raises(oc.OfflineCryptoError):  # a manifest is not a grant
        oc.jws_verify(compact, trusted_keys_der=_pinned(provider), typ="ASC-OGRANT")


def test_jws_from_an_unpinned_key_is_refused():
    compact, _ = oc.jws_sign({"a": 1}, typ="ASC-OPKG", provider=_signer())
    with pytest.raises(oc.OfflineCryptoError):
        oc.jws_verify(compact, trusted_keys_der=_pinned(_signer()), typ="ASC-OPKG")


def test_jws_tampering_is_detected():
    provider = _signer()
    compact, _ = oc.jws_sign({"a": 1}, typ="ASC-OPKG", provider=provider)
    header, payload, signature = compact.split(".")
    forged_payload = oc.b64url_encode(b'{"a":2}')
    for forged in (
        f"{header}.{forged_payload}.{signature}",
        f"{oc.b64url_encode(b'{"alg":"none","kid":"p1","typ":"ASC-OPKG"}')}.{payload}.{signature}",
        f"{header}.{payload}.{signature[:-2]}AA",
    ):
        with pytest.raises(oc.OfflineCryptoError):
            oc.jws_verify(forged, trusted_keys_der=_pinned(provider), typ="ASC-OPKG")


def _non_p256_der_keys():
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    return {
        "empty": b"",
        "garbage": b"not a DER public key",
        "p384": oc.public_key_der(ec.generate_private_key(ec.SECP384R1()).public_key()),
        "ed25519": oc.public_key_der(ed25519.Ed25519PrivateKey.generate().public_key()),
        "rsa": oc.public_key_der(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
        ),
        "text": "p1-key-as-text",
        "none-bytes": None,
    }


@pytest.mark.parametrize("name", sorted(_non_p256_der_keys()))
def test_jws_refuses_a_pinned_key_that_is_not_a_p256_public_key(name):
    """A malformed or non-P-256 pinned key is a controlled OfflineCryptoError,
    never a library exception and never a verification."""
    provider = _signer()
    compact, _ = oc.jws_sign({"a": 1}, typ="ASC-OPKG", provider=provider)
    pinned = {"p1": _non_p256_der_keys()[name]}
    with pytest.raises(oc.OfflineCryptoError) as exc:
        oc.jws_verify(compact, trusted_keys_der=pinned, typ="ASC-OPKG")
    assert type(exc.value) is oc.OfflineCryptoError
    assert "pinned signing key" in str(exc.value) or "not trusted" in str(exc.value)


def test_encryption_round_trip_and_every_binding():
    device_key = ec.generate_private_key(ec.SECP256R1())
    ciphertext, enc, wrap = oc.encrypt_for_device(
        b"plaintext",
        recipient=device_key.public_key(),
        recipient_fingerprint="f" * 64,
        device_public_id="dev",
        object_id="pkg",
        kind="OPKG",
    )
    kwargs = {
        "enc": enc,
        "wrap": wrap,
        "device_public_id": "dev",
        "object_id": "pkg",
        "kind": "OPKG",
    }
    assert (
        oc.unwrap_and_decrypt_for_tests(ciphertext, recipient_private_key=device_key, **kwargs)
        == b"plaintext"
    )
    assert b"plaintext" not in ciphertext
    failures = [
        {"recipient_private_key": ec.generate_private_key(ec.SECP256R1())},  # another device
        {"object_id": "other-package"},  # binding to the object
        {"device_public_id": "other-device"},  # binding to the device
        {"kind": "ODELTA"},  # a package is not a delta
    ]
    for override in failures:
        arguments = kwargs | {"recipient_private_key": device_key} | override
        with pytest.raises(Exception):  # noqa: B017 - AEAD raises InvalidTag
            oc.unwrap_and_decrypt_for_tests(ciphertext, **arguments)
    tampered = bytearray(ciphertext)
    tampered[0] ^= 1
    with pytest.raises(Exception):  # noqa: B017
        oc.unwrap_and_decrypt_for_tests(bytes(tampered), recipient_private_key=device_key, **kwargs)


def test_every_package_has_a_fresh_data_key_and_ephemeral_key():
    device_key = ec.generate_private_key(ec.SECP256R1())
    args = {
        "recipient": device_key.public_key(),
        "recipient_fingerprint": "f",
        "device_public_id": "d",
        "object_id": "p",
        "kind": "OPKG",
    }
    first = oc.encrypt_for_device(b"same", **args)
    second = oc.encrypt_for_device(b"same", **args)
    assert first[0] != second[0]
    assert first[2]["epk"] != second[2]["epk"] and first[2]["salt"] != second[2]["salt"]


def test_self_test_vector_is_public_and_verifies():
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from django.conf import settings

    vector = json.loads(
        (Path(settings.BASE_DIR) / "tests/assets/offline_vectors/es256_self_test.json").read_text()
    )
    assert set(vector) == {"description", "alg", "spki", "message", "signature"}
    key = serialization.load_der_public_key(oc.b64url_decode(vector["spki"]))
    assert oc.verify_device_signature(
        key, vector["message"].encode(), oc.b64url_decode(vector["signature"])
    )
    assert "-----BEGIN" not in json.dumps(vector)  # no key material other than the public key


# ---------------------------------------------------------------------------
# Key-family separation and redaction (P2-D)
# ---------------------------------------------------------------------------


def test_package_and_qr_key_identifiers_are_disjoint():
    from apps.core.crypto.package_signing import is_valid_package_key_id
    from apps.core.crypto.signing import is_valid_key_id

    for kid in ("p1", "p9999"):
        assert is_valid_package_key_id(kid) and not is_valid_key_id(kid)
    for kid in ("v1", "v2"):
        assert is_valid_key_id(kid) and not is_valid_package_key_id(kid)
    assert not is_valid_package_key_id("p1\n")


def test_env_package_provider_never_reads_qr_keys(settings):
    from apps.core.crypto.package_signing import EnvPackageSigningKeyProvider
    from apps.core.crypto.signing import SigningKeyProviderError

    settings.OFFLINE_PACKAGE_SIGNING_KEYS_BY_VERSION = {1: None}
    settings.OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION = 1
    settings.QR_SIGNING_KEYS_BY_VERSION = {1: "would-be-qr-key"}
    provider = EnvPackageSigningKeyProvider()
    assert provider.current_signing_key_id() == "p1"
    assert provider.is_configured() is False
    with pytest.raises(SigningKeyProviderError) as exc:
        provider.sign("p1", b"message")
    assert "OFFLINE_PACKAGE_SIGNING_KEY_V1" in str(exc.value)
    with pytest.raises(SigningKeyProviderError):
        provider.sign("v1", b"message")


def test_package_signing_keys_are_redacted_in_both_text_forms(monkeypatch):
    import logging

    from apps.core.credential_shapes import is_credential_shaped_name
    from apps.core.redaction import RedactingLogFilter

    # Markers composed at runtime: the repository scanner rejects a literal
    # private-key block anywhere in the tree (the Prompt 8 test idiom).
    begin = "-----BEGIN {} KEY-----".format("PRIVATE")
    end = "-----END {} KEY-----".format("PRIVATE")
    pem_env = f"{begin}\\nSYNTHETICPACKAGEKEYBODY\\n{end}"
    monkeypatch.setenv("OFFLINE_PACKAGE_SIGNING_KEY_V1", pem_env)
    assert is_credential_shaped_name("OFFLINE_PACKAGE_SIGNING_KEY_V1")
    for leaked in (pem_env, pem_env.replace("\\n", "\n")):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, f"value={leaked}", None, None)
        RedactingLogFilter().filter(record)
        assert "SYNTHETICPACKAGEKEYBODY" not in record.getMessage()


# ---------------------------------------------------------------------------
# Fail-closed system checks
# ---------------------------------------------------------------------------


def _check_ids(**overrides):
    from django.test import override_settings

    from apps.entry.checks import entry_offline_settings

    with override_settings(**overrides):
        return {error.id for error in entry_offline_settings()}


def test_default_configuration_is_clean():
    assert _check_ids() == set()


@pytest.mark.parametrize("state", sorted(set(contract.PERMITTED_ACTIONS) - {"OFFLINE_ACTIVE"}))
def test_action_matrix_check_refuses_admission_outside_active(monkeypatch, state):
    monkeypatch.setitem(
        contract.PERMITTED_ACTIONS,
        state,
        contract.PERMITTED_ACTIONS[state] | {"ADMIT_OFFLINE"},
    )
    assert "entry.E012" in _check_ids()


def test_enabling_offline_without_a_package_key_fails_closed():
    from apps.core.crypto.package_signing import set_package_signing_key_provider_for_testing

    set_package_signing_key_provider_for_testing(None)
    assert "entry.E005" in _check_ids(
        ENTRY_OFFLINE_ENABLED=True, OFFLINE_PACKAGE_SIGNING_KEYS_BY_VERSION={1: None}
    )


def test_a_weaker_validity_value_fails_closed(settings):
    weaker = copy.deepcopy(settings.ENTRY_OFFLINE_VALIDITY)
    weaker["STANDARD"]["expires_after_seconds"] = 9 * 60 * 60
    assert "entry.E006" in _check_ids(ENTRY_OFFLINE_VALIDITY=weaker)
    unordered = copy.deepcopy(settings.ENTRY_OFFLINE_VALIDITY)
    unordered["SENSITIVE"]["aging_after_seconds"] = 29 * 60
    unordered["SENSITIVE"]["stale_after_seconds"] = 10 * 60
    assert "entry.E006" in _check_ids(ENTRY_OFFLINE_VALIDITY=unordered)


def test_health_inactivity_and_clear_values_are_pinned(settings):
    assert "entry.E007" in _check_ids(
        ENTRY_OFFLINE_HEALTH={**settings.ENTRY_OFFLINE_HEALTH, "failures_to_offline": 1}
    )
    assert "entry.E007" in _check_ids(ENTRY_OFFLINE_OPERATOR_INACTIVITY_LOCK_SECONDS=3600)
    assert "entry.E007" in _check_ids(ENTRY_RESULT_CLEAR_SECONDS=120)


def test_heartbeat_must_stay_passive():
    assert "entry.E008" in _check_ids(OPERATIONAL_PASSIVE_PATHS=("/entry/status/",))


def test_mfa_test_double_is_refused_outside_test_settings(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    assert "entry.E009" in _check_ids(
        MFA_BACKEND="apps.accounts.tests.mfa_double.AcceptingStepUpBackend"
    )


def test_drf_default_must_deny(settings):
    assert "entry.E010" in _check_ids(
        REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_PERMISSION_CLASSES": []}
    )
