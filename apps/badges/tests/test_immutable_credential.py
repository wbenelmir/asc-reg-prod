"""Immutable issued credentials, key alignment, and snapshot integrity.

Everything here exists because of one class of defect: a credential that is
re-derived at display time is not really *issued*. If any input to that
derivation can move -- a mutable assignment row, the signing provider, the
private key's availability -- then what a participant shows at a gate is not
what was signed for them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accreditation.models import BadgeTypeAssignment
from apps.badges.credentials.verify import VerificationResultCode, verify_pass_token
from apps.badges.models import (
    DigitalEntryPassStatus,
    ParticipantEventPseudonym,
    PassReasonCode,
    VerificationKey,
    VerificationKeyStatus,
)
from apps.badges.services import (
    PassConfigurationError,
    VerificationKeyError,
    activate_pass,
    generate_pass,
    issue_pass_token,
    new_operation_id,
    publish_verification_key,
    reconstruct_pass_token,
    replace_pass,
    require_aligned_signing_key,
)
from apps.core.crypto import signing as signing_module
from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    PublicKeyFormatError,
    canonical_public_key_der,
    public_key_fingerprint,
    set_signing_key_provider_for_testing,
)

pytestmark = pytest.mark.django_db


def _issue(registration, actor):
    return generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential


def _activate(credential, actor):
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


# ---------------------------------------------------------------------------
# 1. The issued artefact is persisted and immutable
# ---------------------------------------------------------------------------


def test_the_signature_is_persisted_at_issuance(eligible_registration, pass_admin, active_key):
    credential = _issue(eligible_registration, pass_admin)
    assert credential.signature_hex
    assert len(credential.signature_hex) == 128  # 64 raw bytes, hex-encoded
    assert credential.payload_hash


def test_repeated_display_returns_identical_bytes(eligible_registration, pass_admin, active_key):
    """The decisive property: displaying a pass twice yields the same token."""
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)

    first = issue_pass_token(credential)
    second = issue_pass_token(credential)
    credential.refresh_from_db()
    third = issue_pass_token(credential)

    assert first == second == third


def test_display_never_calls_the_signing_provider(
    eligible_registration, pass_admin, active_key, signing_provider, monkeypatch
):
    """Display must be key-free, not merely "usually" key-free."""
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)

    def _explode(*args, **kwargs):
        raise AssertionError("display must never invoke the private signing operation")

    monkeypatch.setattr(signing_provider, "sign", _explode)

    token = issue_pass_token(credential)
    assert token.count(".") == 2


def test_a_pass_stays_displayable_after_its_private_key_is_gone(
    eligible_registration, pass_admin, active_key
):
    """Rotation overlap in practice: the private key is removed entirely."""
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    expected = issue_pass_token(credential)

    # The provider no longer holds any key at all.
    set_signing_key_provider_for_testing(InMemorySigningKeyProvider(key_ids=("v9",), current="v9"))
    try:
        credential.refresh_from_db()
        assert issue_pass_token(credential) == expected
        # And it still verifies: the published public key is what matters.
        assert verify_pass_token(expected).code == VerificationResultCode.VALID
    finally:
        set_signing_key_provider_for_testing(None)


def test_replacement_issues_a_different_immutable_token(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    original_token = issue_pass_token(credential)
    original_signature = credential.signature_hex

    replacement = replace_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    ).credential
    replacement = _activate(replacement, pass_admin)

    new_token = issue_pass_token(replacement)
    assert new_token != original_token
    assert replacement.signature_hex != original_signature

    # The replaced credential's signed material is untouched.
    credential.refresh_from_db()
    assert credential.signature_hex == original_signature
    assert reconstruct_pass_token(credential) == original_token


def test_a_credential_without_a_stored_signature_fails_closed(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    credential.signature_hex = ""
    credential.save(update_fields=["signature_hex"])
    with pytest.raises(PassConfigurationError, match="no stored signature"):
        reconstruct_pass_token(credential)


# ---------------------------------------------------------------------------
# 2. Signing-provider / public-key alignment
# ---------------------------------------------------------------------------

RSA_PUBLIC_PEM = None  # built lazily in the test that needs it


def _rsa_public_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def _p384_public_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = ec.generate_private_key(ec.SECP384R1())
    return (
        key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


def test_malformed_pem_is_refused(key_custodian):
    with pytest.raises(VerificationKeyError):
        publish_verification_key(
            key_id="v5", public_key_pem="not a pem at all", actor=key_custodian
        )


def test_an_rsa_public_key_is_refused(key_custodian):
    with pytest.raises(VerificationKeyError):
        publish_verification_key(key_id="v5", public_key_pem=_rsa_public_pem(), actor=key_custodian)


def test_an_ec_key_on_the_wrong_curve_is_refused(key_custodian):
    with pytest.raises(VerificationKeyError):
        publish_verification_key(
            key_id="v5", public_key_pem=_p384_public_pem(), actor=key_custodian
        )


def test_a_private_key_is_refused_by_the_canonical_parser():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode(
        "ascii"
    )
    with pytest.raises(PublicKeyFormatError):
        canonical_public_key_der(private_pem)


def test_canonical_der_is_insensitive_to_pem_formatting(signing_provider):
    """Two encodings of one key must compare equal, and fingerprint equal."""
    pem = signing_provider.public_key_pem("v1").decode("ascii")
    reformatted = pem.replace("\n", "\r\n") + "\n\n   "

    assert canonical_public_key_der(pem) == canonical_public_key_der(reformatted)
    assert public_key_fingerprint(canonical_public_key_der(pem)) == public_key_fingerprint(
        canonical_public_key_der(reformatted)
    )


def test_the_fingerprint_is_computed_over_der_not_pem(signing_provider, key_custodian):
    pem = signing_provider.public_key_pem("v1").decode("ascii")
    key = publish_verification_key(key_id="v3", public_key_pem=pem, actor=key_custodian)
    assert key.public_key_fingerprint == public_key_fingerprint(canonical_public_key_der(pem))
    assert key.public_key_der_b64


def test_promotion_requires_the_provider_to_hold_the_key(signing_provider, key_custodian):
    """A key the provider cannot sign with must never become active."""
    from apps.badges.services import promote_verification_key

    # v2's public half is published, but the provider is still signing with v1.
    key = publish_verification_key(
        key_id="v2",
        public_key_pem=signing_provider.public_key_pem("v2").decode("ascii"),
        actor=key_custodian,
    )
    with pytest.raises(VerificationKeyError, match="does not match the key being promoted"):
        promote_verification_key(key=key, actor=key_custodian)


def test_promotion_rejects_a_public_key_the_provider_does_not_hold(signing_provider, key_custodian):
    from apps.badges.services import promote_verification_key

    stranger = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    key = publish_verification_key(
        key_id="v1",
        public_key_pem=stranger.public_key_pem("v1").decode("ascii"),
        actor=key_custodian,
    )
    with pytest.raises(VerificationKeyError, match="does not match the published key"):
        promote_verification_key(key=key, actor=key_custodian, provider=signing_provider)


def test_generation_requires_provider_and_active_key_to_agree(
    eligible_registration, pass_admin, active_key, signing_provider
):
    signing_provider.set_current_signing_key_id("v2")
    with pytest.raises(PassConfigurationError, match="does not match the active verification key"):
        _issue(eligible_registration, pass_admin)


def test_alignment_rejects_a_key_that_is_not_yet_valid(active_key, signing_provider):
    VerificationKey.objects.filter(pk=active_key.pk).update(
        not_before=timezone.now() + timedelta(days=1)
    )
    with pytest.raises(PassConfigurationError, match="not yet valid"):
        require_aligned_signing_key(signing_provider)


def test_alignment_rejects_an_expired_key(active_key, signing_provider):
    VerificationKey.objects.filter(pk=active_key.pk).update(
        not_before=timezone.now() - timedelta(days=5),
        not_after=timezone.now() - timedelta(days=1),
    )
    with pytest.raises(PassConfigurationError, match="expired"):
        require_aligned_signing_key(signing_provider)


def test_alignment_fails_closed_when_the_provider_errors(active_key):
    from apps.core.crypto.signing import _BrokenSigningKeyProvider

    with pytest.raises(PassConfigurationError):
        require_aligned_signing_key(_BrokenSigningKeyProvider())


def test_alignment_succeeds_for_a_correctly_configured_key(active_key, signing_provider):
    key_id, key = require_aligned_signing_key(signing_provider)
    assert key_id == "v1"
    assert key.pk == active_key.pk
    assert key.status == VerificationKeyStatus.ACTIVE


def test_no_private_key_is_ever_persisted(active_key):
    stored = VerificationKey.objects.get(pk=active_key.pk)
    assert "PRIVATE" not in stored.public_key_pem.upper()
    assert not signing_module.looks_like_private_key_material(stored.public_key_pem)


# ---------------------------------------------------------------------------
# 3. Signed claims are immutable snapshots
# ---------------------------------------------------------------------------


def test_the_snapshot_columns_are_populated_at_issuance(
    eligible_registration, pass_admin, active_key
):
    credential = _issue(eligible_registration, pass_admin)
    assignment = BadgeTypeAssignment.objects.get(pk=credential.badge_assignment_id)
    pseudonym = ParticipantEventPseudonym.objects.get(
        person_id=eligible_registration.person_id,
        event_edition_id=eligible_registration.event_edition_id,
    )
    assert credential.badge_assignment_public_reference == assignment.public_reference
    assert credential.participant_event_pseudonym == pseudonym.pseudonym


def test_changing_the_source_assignment_does_not_alter_an_issued_token(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    before = issue_pass_token(credential)

    BadgeTypeAssignment.objects.filter(pk=credential.badge_assignment_id).update(
        public_reference="ZZZZZZZZZZZZZZZZZZZZZZ"
    )

    credential.refresh_from_db()
    assert issue_pass_token(credential) == before
    assert verify_pass_token(before).code == VerificationResultCode.VALID


def test_changing_the_pseudonym_does_not_alter_an_issued_token(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    before = issue_pass_token(credential)

    ParticipantEventPseudonym.objects.filter(person_id=eligible_registration.person_id).update(
        pseudonym="YYYYYYYYYYYYYYYYYYYYYY"
    )

    credential.refresh_from_db()
    assert issue_pass_token(credential) == before
    assert verify_pass_token(before).code == VerificationResultCode.VALID


def test_a_forged_bai_claim_is_rejected(
    eligible_registration, pass_admin, active_key, signing_provider
):
    from apps.badges.credentials.issue import assemble_compact_jws
    from apps.badges.services import build_pass_claims

    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    claims = build_pass_claims(credential)
    claims["bai"] = "F" * 22
    forged = assemble_compact_jws(claims, key_id="v1", provider=signing_provider).token

    assert verify_pass_token(forged).code == VerificationResultCode.CREDENTIAL_MISMATCH


def test_a_forged_pid_claim_is_rejected(
    eligible_registration, pass_admin, active_key, signing_provider
):
    from apps.badges.credentials.issue import assemble_compact_jws
    from apps.badges.services import build_pass_claims

    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    claims = build_pass_claims(credential)
    claims["pid"] = "G" * 22
    forged = assemble_compact_jws(claims, key_id="v1", provider=signing_provider).token

    assert verify_pass_token(forged).code == VerificationResultCode.CREDENTIAL_MISMATCH


def test_a_payload_hash_mismatch_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    token = issue_pass_token(credential)

    credential.payload_hash = "0" * 64
    credential.save(update_fields=["payload_hash"])

    assert verify_pass_token(token).code == VerificationResultCode.CREDENTIAL_MISMATCH


def test_an_issued_credential_verifies_end_to_end(eligible_registration, pass_admin, active_key):
    credential = _activate(_issue(eligible_registration, pass_admin), pass_admin)
    result = verify_pass_token(issue_pass_token(credential))
    assert result.code == VerificationResultCode.VALID
    assert result.credential.pk == credential.pk


# ---------------------------------------------------------------------------
# 8. ACTIVE-only QR is an invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        DigitalEntryPassStatus.INACTIVE,
        DigitalEntryPassStatus.SUSPENDED,
        DigitalEntryPassStatus.REVOKED,
        DigitalEntryPassStatus.EXPIRED,
        DigitalEntryPassStatus.REPLACED,
    ],
)
def test_no_non_active_status_yields_a_token(eligible_registration, pass_admin, active_key, status):
    from apps.badges.services import PassStateError

    credential = _issue(eligible_registration, pass_admin)
    credential.status = status
    credential.save(update_fields=["status"])
    with pytest.raises(PassStateError, match="only for an active"):
        issue_pass_token(credential)


# ---------------------------------------------------------------------------
# 10. Badge-assignment public references are never blank
# ---------------------------------------------------------------------------


def test_a_blank_public_reference_is_rejected_by_the_database(
    eligible_registration, pass_admin, active_key
):
    from django.db import IntegrityError, transaction

    credential = _issue(eligible_registration, pass_admin)
    with pytest.raises(IntegrityError), transaction.atomic():
        BadgeTypeAssignment.objects.filter(pk=credential.badge_assignment_id).update(
            public_reference=""
        )


def test_public_references_are_unconditionally_unique(
    eligible_registration,
    pass_admin,
    active_key,
    event,
    organization,
    person,
    role,
    badge_type,
    access_profile,
):
    from django.db import IntegrityError, transaction

    from apps.badges.tests.conftest import grant_required_assignments, make_registration

    first = _issue(eligible_registration, pass_admin)
    other = make_registration(event=event, organization=organization, person=person)
    second = grant_required_assignments(
        registration=other,
        event=event,
        actor=pass_admin,
        role=role,
        badge_type=badge_type,
        access_profile=access_profile,
    )
    existing = BadgeTypeAssignment.objects.get(pk=first.badge_assignment_id)
    with pytest.raises(IntegrityError), transaction.atomic():
        BadgeTypeAssignment.objects.filter(pk=second.pk).update(
            public_reference=existing.public_reference
        )


def test_every_new_assignment_receives_a_reference(
    event, organization, person, pass_admin, role, badge_type, access_profile
):
    from apps.badges.tests.conftest import grant_required_assignments, make_registration

    registration = make_registration(event=event, organization=organization, person=person)
    assignment = grant_required_assignments(
        registration=registration,
        event=event,
        actor=pass_admin,
        role=role,
        badge_type=badge_type,
        access_profile=access_profile,
    )
    assert assignment.public_reference
    assert len(assignment.public_reference) == 22
