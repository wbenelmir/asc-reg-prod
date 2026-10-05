"""Final closure pass: fingerprint completeness, dual budgets, controlled
reasons, normalized provider failures, and time-expired QR exposure."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.forms import (
    KEY_RETIREMENT_REASON_CODES,
    KEY_REVOCATION_REASON_CODES,
    ReasonedLifecycleForm,
    RevokeVerificationKeyForm,
    credential_reason_choices,
)
from apps.badges.models import (
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PassCredentialSeries,
    PassLifecycleOperation,
    PassReasonCode,
    VerificationKeyReasonCode,
    VerificationKeyStatus,
)
from apps.badges.services import (
    FallbackLookupThrottled,
    OperationConflictError,
    PassConfigurationError,
    PassStateError,
    activate_pass,
    generate_pass,
    issue_pass_token,
    lookup_series_by_fallback_reference,
    new_operation_id,
    normalize_reason_text,
    pass_exposes_usable_qr,
    retire_verification_key,
    revoke_pass,
    suspend_pass,
)
from apps.badges.tests.conftest import (
    make_operational_user_with_membership,
    sign_in_operational,
)
from apps.core.crypto.signing import (
    InMemorySigningKeyProvider,
    SigningKeyProviderError,
    set_signing_key_provider_for_testing,
)
from apps.core.outbox.persistent import OutboxEvent

pytestmark = pytest.mark.django_db


def _issue(registration, actor):
    return generate_pass(
        registration=registration, actor=actor, operation_id=new_operation_id()
    ).credential


def _activated(registration, actor):
    credential = _issue(registration, actor)
    activate_pass(
        credential=credential,
        actor=actor,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    return credential


def _sign_in_participant(client, person):
    from apps.accounts import participant_auth, session_expiry

    now = timezone.now().isoformat()
    session = client.session
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()


# ---------------------------------------------------------------------------
# 1. Complete idempotency command binding
# ---------------------------------------------------------------------------


def test_reason_text_normalization_matches_what_is_persisted():
    assert normalize_reason_text("  padded  ") == "padded"
    assert normalize_reason_text(None) == ""
    assert len(normalize_reason_text("x" * 500)) == 300


def test_same_operation_id_with_different_reason_text_is_rejected(
    eligible_registration, pass_admin, active_key
):
    """Two suspends differing only in justification are different commands."""
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()

    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
        reason_text="Reported lost at gate 3",
    )
    credential.refresh_from_db()

    with pytest.raises(OperationConflictError):
        suspend_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=operation_id,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
            reason_text="Something entirely different",
        )


def test_whitespace_only_differences_in_reason_text_still_replay(
    eligible_registration, pass_admin, active_key
):
    """Normalization is shared with persistence, so padding is not a new command."""
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()
    kwargs = {
        "credential": credential,
        "actor": pass_admin,
        "operation_id": operation_id,
        "expected_lock_version": credential.version,
        "reason_code": PassReasonCode.SECURITY_CONCERN,
    }
    first = suspend_pass(**kwargs, reason_text="Reported lost")
    second = suspend_pass(**kwargs, reason_text="   Reported lost   ")

    assert first.replayed is False
    assert second.replayed is True
    credential.refresh_from_db()
    assert credential.status_reason_text == "Reported lost"


def test_same_operation_id_with_different_reason_code_is_rejected(
    eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()

    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    credential.refresh_from_db()

    with pytest.raises(OperationConflictError):
        suspend_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=operation_id,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.ADMINISTRATIVE_ERROR,
        )


def test_a_legacy_empty_fingerprint_row_cannot_replay_an_unrelated_command(
    eligible_registration, pass_admin, active_key
):
    """An unbound legacy identifier must be a conflict, never a free replay."""
    credential = _activated(eligible_registration, pass_admin)
    operation_id = new_operation_id()

    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    # Simulate a row written before command binding existed.
    PassLifecycleOperation.objects.filter(operation_id=operation_id).update(command_fingerprint="")
    credential.refresh_from_db()

    with pytest.raises(OperationConflictError, match="predates command binding"):
        revoke_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=operation_id,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED


def test_key_retirement_binds_its_reason_text(active_key, key_custodian):
    operation_id = new_operation_id()
    retire_verification_key(
        key=active_key,
        actor=key_custodian,
        reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
        reason_text="Scheduled quarterly rotation",
        operation_id=operation_id,
    )
    with pytest.raises(OperationConflictError):
        retire_verification_key(
            key=active_key,
            actor=key_custodian,
            reason_code=VerificationKeyReasonCode.PLANNED_ROTATION,
            reason_text="A different justification",
            operation_id=operation_id,
        )


# ---------------------------------------------------------------------------
# 2. Both actor and trusted-network budgets
# ---------------------------------------------------------------------------


def _other_admin(email, event, organization):
    return make_operational_user_with_membership(
        email=email,
        group_name="Pass Administrators",
        event_edition=event,
        organization=organization,
    )


def test_one_actor_is_throttled(pass_admin, settings):
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 3
    for _ in range(3):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0", actor=pass_admin, network_identity="203.0.113.10"
        )
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0", actor=pass_admin, network_identity="203.0.113.10"
        )


def test_several_actors_on_one_network_are_collectively_throttled(
    pass_admin, event, organization, settings
):
    """Rotating accounts on one machine must not multiply the budget."""
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 3
    shared_network = "203.0.113.20"
    actors = [pass_admin] + [
        _other_admin(f"net.actor{i}@example.test", event, organization) for i in range(3)
    ]
    for index in range(3):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0",
            actor=actors[index],
            network_identity=shared_network,
        )
    # A fresh actor, same network: the network budget still applies.
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0",
            actor=actors[3],
            network_identity=shared_network,
        )


def test_changing_the_network_does_not_bypass_the_actor_budget(pass_admin, settings):
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 3
    for index in range(3):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0",
            actor=pass_admin,
            network_identity=f"203.0.113.{index + 30}",
        )
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(
            raw_reference="ASC-0000-0000-0",
            actor=pass_admin,
            network_identity="198.51.100.99",
        )


def test_malformed_attempts_consume_both_budgets(pass_admin, event, organization, settings):
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 2
    shared_network = "203.0.113.40"
    for _ in range(2):
        lookup_series_by_fallback_reference(
            raw_reference="!!!not-a-reference!!!",
            actor=pass_admin,
            network_identity=shared_network,
        )
    # Actor budget exhausted by malformed input alone.
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(
            raw_reference="!!!still-bad!!!", actor=pass_admin, network_identity=shared_network
        )
    # And so is the network budget, for a different actor.
    other = _other_admin("malformed.other@example.test", event, organization)
    with pytest.raises(FallbackLookupThrottled):
        lookup_series_by_fallback_reference(
            raw_reference="!!!still-bad!!!", actor=other, network_identity=shared_network
        )


def test_audit_stores_a_keyed_fingerprint_and_never_a_raw_address(pass_admin):
    address = "203.0.113.55"
    lookup_series_by_fallback_reference(
        raw_reference="ASC-0000-0000-0", actor=pass_admin, network_identity=address
    )
    entry = AuditEvent.objects.filter(action_code=action_codes.FALLBACK_REFERENCE_LOOKUP).latest(
        "occurred_at"
    )

    assert entry.network_fingerprint, "a keyed fingerprint must be recorded"
    assert entry.network_fingerprint != address
    assert address not in entry.network_fingerprint
    assert address not in str(entry.after_summary)
    # A hex-encoded HMAC-SHA-256 digest.
    assert len(entry.network_fingerprint) == 64
    int(entry.network_fingerprint, 16)


def test_the_lookup_view_supplies_a_trusted_network_identity(client, pass_admin, settings):
    """The view must not read a client-controlled header directly."""
    settings.FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = 50
    sign_in_operational(client, pass_admin.email_normalized)
    client.post(
        reverse("badges:fallback-reference-lookup"),
        {"reference": "ASC-0000-0000-0"},
        REMOTE_ADDR="203.0.113.77",
        HTTP_X_FORWARDED_FOR="198.51.100.1",
    )
    entry = AuditEvent.objects.filter(action_code=action_codes.FALLBACK_REFERENCE_LOOKUP).latest(
        "occurred_at"
    )
    assert entry.network_fingerprint
    assert "203.0.113.77" not in entry.network_fingerprint
    assert "198.51.100.1" not in entry.network_fingerprint


# ---------------------------------------------------------------------------
# 3. Controlled lifecycle reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["suspend", "resume", "revoke", "replace"])
def test_each_action_offers_only_its_own_reasons(action):
    codes = {code for code, _label in credential_reason_choices(action)}
    assert codes
    assert codes <= set(PassReasonCode.values)


def test_a_reason_outside_the_action_allowlist_is_rejected():
    """A revoke-only reason must not be accepted by the resume endpoint."""
    form = ReasonedLifecycleForm(
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": 1,
            "reason_code": PassReasonCode.LOST_OR_COMPROMISED,
        },
        action="resume",
    )
    assert not form.is_valid()
    assert "reason_code" in form.errors


def test_a_valid_reason_for_the_action_is_accepted():
    form = ReasonedLifecycleForm(
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": 1,
            "reason_code": PassReasonCode.SECURITY_CONCERN,
            "reason_text": "Reported at the desk",
        },
        action="suspend",
    )
    assert form.is_valid(), form.errors


def test_emergency_key_revocation_requires_an_emergency_reason():
    planned = RevokeVerificationKeyForm(
        {"operation_id": new_operation_id(), "reason_code": "PLANNED_ROTATION"}
    )
    assert not planned.is_valid()

    missing = RevokeVerificationKeyForm({"operation_id": new_operation_id()})
    assert not missing.is_valid()

    emergency = RevokeVerificationKeyForm(
        {
            "operation_id": new_operation_id(),
            "reason_code": VerificationKeyReasonCode.CONFIRMED_COMPROMISE,
        }
    )
    assert emergency.is_valid(), emergency.errors


def test_retirement_and_revocation_offer_different_vocabularies():
    """A compromise must not be fileable as a routine rotation."""
    assert VerificationKeyReasonCode.PLANNED_ROTATION in KEY_RETIREMENT_REASON_CODES
    assert VerificationKeyReasonCode.PLANNED_ROTATION not in KEY_REVOCATION_REASON_CODES
    assert VerificationKeyReasonCode.CONFIRMED_COMPROMISE in KEY_REVOCATION_REASON_CODES
    assert VerificationKeyReasonCode.CONFIRMED_COMPROMISE not in KEY_RETIREMENT_REASON_CODES


def test_the_chosen_reason_is_persisted_and_audited(
    client, eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    sign_in_operational(client, pass_admin.email_normalized)

    response = client.post(
        reverse("badges:credential-suspend", kwargs={"pk": credential.pk}),
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": credential.version,
            "reason_code": PassReasonCode.ADMINISTRATIVE_ERROR,
            "reason_text": "Issued against the wrong context",
        },
    )
    assert response.status_code == 302
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED
    assert credential.status_reason_code == PassReasonCode.ADMINISTRATIVE_ERROR
    assert credential.status_reason_text == "Issued against the wrong context"

    entry = AuditEvent.objects.filter(action_code=action_codes.CREDENTIAL_SUSPENDED).latest(
        "occurred_at"
    )
    assert entry.reason_code == PassReasonCode.ADMINISTRATIVE_ERROR


def test_an_invalid_reason_through_the_view_changes_nothing(
    client, eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    sign_in_operational(client, pass_admin.email_normalized)

    response = client.post(
        reverse("badges:credential-suspend", kwargs={"pk": credential.pk}),
        {
            "operation_id": new_operation_id(),
            "expected_lock_version": credential.version,
            "reason_code": "NOT_A_REAL_REASON",
        },
    )
    assert response.status_code == 409
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_the_credential_page_renders_reason_selects_not_hidden_values(
    client, eligible_registration, pass_admin, active_key
):
    _activated(eligible_registration, pass_admin)
    sign_in_operational(client, pass_admin.email_normalized)
    body = client.get(
        reverse("badges:registration-credential", kwargs={"pk": eligible_registration.pk})
    ).content.decode()

    assert 'type="hidden" name="reason_code"' not in body
    assert 'name="reason_code"' in body
    assert "<select" in body


def test_the_key_page_renders_reason_selects_not_hidden_values(client, active_key, key_custodian):
    sign_in_operational(client, key_custodian.email_normalized)
    body = client.get(reverse("badges:verification-keys")).content.decode()

    assert 'type="hidden" name="reason_code"' not in body
    assert "retire-reason-" in body
    assert "revoke-reason-" in body


def test_revoking_a_key_through_the_view_persists_the_chosen_reason(
    client, active_key, key_custodian
):
    sign_in_operational(client, key_custodian.email_normalized)
    response = client.post(
        reverse("badges:verification-key-revoke", kwargs={"pk": active_key.pk}),
        {
            "operation_id": new_operation_id(),
            "reason_code": VerificationKeyReasonCode.CONFIRMED_COMPROMISE,
            "reason_text": "Key material found in a shared drive",
        },
    )
    assert response.status_code == 302
    active_key.refresh_from_db()
    assert active_key.status == VerificationKeyStatus.REVOKED
    assert active_key.revocation_reason_code == VerificationKeyReasonCode.CONFIRMED_COMPROMISE
    assert active_key.revocation_reason_text == "Key material found in a shared drive"


# ---------------------------------------------------------------------------
# 4. Signing-provider failures normalized at the service boundary
# ---------------------------------------------------------------------------


class _AlignsButCannotSign(InMemorySigningKeyProvider):
    """Passes public-key alignment, then fails when asked to sign.

    This is the realistic shape of a KMS outage: the published public key is
    still correct, so alignment succeeds, and only the signing call fails.
    """

    def sign(self, key_id: str, message: bytes) -> bytes:
        raise SigningKeyProviderError("Upstream key service refused the signing request.")


@pytest.fixture
def failing_signer(active_key, signing_provider):
    """Swap in a provider whose public key matches but whose signing fails."""
    broken = _AlignsButCannotSign(key_ids=("v1",), current="v1")
    broken._keys["v1"] = signing_provider._keys["v1"]  # same key pair, broken sign()
    set_signing_key_provider_for_testing(broken)
    yield broken
    set_signing_key_provider_for_testing(signing_provider)


def test_a_signing_failure_becomes_a_domain_error(
    eligible_registration, pass_admin, failing_signer
):
    with pytest.raises(PassConfigurationError, match="could not sign"):
        _issue(eligible_registration, pass_admin)


def test_a_signing_failure_leaves_no_partial_state(
    eligible_registration, pass_admin, failing_signer
):
    """The whole transaction must roll back: no credential, no evidence."""
    before_ops = PassLifecycleOperation.objects.count()
    before_outbox = OutboxEvent.objects.count()
    before_success_audit = AuditEvent.objects.filter(
        action_code=action_codes.CREDENTIAL_GENERATED
    ).count()

    with pytest.raises(PassConfigurationError):
        _issue(eligible_registration, pass_admin)

    assert not DigitalEntryPass.objects.filter(registration=eligible_registration).exists()
    assert not PassCredentialSeries.objects.filter(registration=eligible_registration).exists()
    assert PassLifecycleOperation.objects.count() == before_ops
    assert OutboxEvent.objects.count() == before_outbox
    assert (
        AuditEvent.objects.filter(action_code=action_codes.CREDENTIAL_GENERATED).count()
        == before_success_audit
    )


def test_the_view_shows_a_safe_error_instead_of_a_500(
    client, eligible_registration, pass_admin, failing_signer
):
    sign_in_operational(client, pass_admin.email_normalized)
    response = client.post(
        reverse("badges:credential-generate", kwargs={"pk": eligible_registration.pk}),
        {"operation_id": new_operation_id()},
        follow=True,
    )
    assert response.status_code == 200
    body = response.content.decode()
    # Phase 3 Prompt 8 (P8-01): the safe, translatable configuration message
    # replaces the service's internal wording ("could not sign ...").
    assert "the signing key or the event configuration is not ready" in body
    assert "could not sign" not in body
    # Never the provider's own words or any key material. The check targets
    # the PEM delimiter specifically: the base layout legitimately mentions
    # "private mode" in an unrelated inline script.
    assert "Upstream key service" not in body
    assert "PRIVATE KEY" not in body.upper()
    assert "Traceback" not in body


# ---------------------------------------------------------------------------
# 5. Time-expired QR exposure fails closed
# ---------------------------------------------------------------------------


def _make_window_expired(credential):
    """Move the window into the past WITHOUT touching the stored status."""
    past = timezone.now() - timedelta(days=2)
    DigitalEntryPass.objects.filter(pk=credential.pk).update(
        valid_from=past - timedelta(days=1), valid_until=past
    )
    credential.refresh_from_db()
    return credential


def _make_window_not_started(credential):
    """Move the window into the future WITHOUT touching the stored status."""
    future = timezone.now() + timedelta(days=2)
    DigitalEntryPass.objects.filter(pk=credential.pk).update(
        valid_from=future, valid_until=future + timedelta(days=1)
    )
    credential.refresh_from_db()
    return credential


def test_an_active_row_past_its_window_exposes_no_token(
    eligible_registration, pass_admin, active_key
):
    credential = _make_window_expired(_activated(eligible_registration, pass_admin))
    assert credential.status == DigitalEntryPassStatus.ACTIVE
    assert pass_exposes_usable_qr(credential) is False
    with pytest.raises(PassStateError, match="validity period"):
        issue_pass_token(credential)


def test_the_participant_page_shows_expired_guidance_and_no_qr(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _make_window_expired(_activated(eligible_registration, pass_admin))
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    assert "validity period has ended" in body
    assert "data-pass-qr=" not in body
    assert "/qr.png" not in body
    # The GET must not have mutated anything.
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_an_active_row_before_its_window_is_not_mislabeled_as_expired(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _make_window_not_started(_activated(eligible_registration, pass_admin))
    _sign_in_participant(client, person)
    body = client.get(reverse("badges:participant-passes")).content.decode()

    assert "validity period has not started yet" in body
    assert "validity period has ended" not in body
    assert "data-pass-qr=" not in body
    assert "/qr.png" not in body
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_the_print_page_shows_no_qr_for_an_expired_window(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _make_window_expired(_activated(eligible_registration, pass_admin))
    _sign_in_participant(client, person)
    body = client.get(
        reverse(
            "badges:participant-pass-print",
            kwargs={"public_id": credential.series.public_id},
        )
    ).content.decode()

    assert "data-pass-qr=" not in body
    assert "validity period has ended" in body


def test_the_qr_endpoint_returns_404_for_an_expired_window(
    client, eligible_registration, pass_admin, active_key, person
):
    credential = _make_window_expired(_activated(eligible_registration, pass_admin))
    _sign_in_participant(client, person)
    response = client.get(
        reverse(
            "badges:participant-pass-qr",
            kwargs={"public_id": credential.series.public_id},
        )
    )
    assert response.status_code == 404
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_a_credential_inside_its_window_still_exposes_a_qr(
    eligible_registration, pass_admin, active_key
):
    credential = _activated(eligible_registration, pass_admin)
    assert pass_exposes_usable_qr(credential) is True
    assert issue_pass_token(credential).count(".") == 2


def test_exposure_uses_the_same_clock_skew_as_verification(
    eligible_registration, pass_admin, active_key, settings
):
    """Barely past the window, but inside skew: still exposed, as at a gate."""
    settings.QR_CLOCK_SKEW_SECONDS = 300
    credential = _activated(eligible_registration, pass_admin)
    DigitalEntryPass.objects.filter(pk=credential.pk).update(
        valid_until=timezone.now() - timedelta(seconds=60)
    )
    credential.refresh_from_db()
    assert pass_exposes_usable_qr(credential) is True

    settings.QR_CLOCK_SKEW_SECONDS = 10
    assert pass_exposes_usable_qr(credential) is False
