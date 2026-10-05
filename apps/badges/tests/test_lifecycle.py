"""Credential lifecycle: transitions, eligibility, idempotency, replacement."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accreditation.models import AssignmentStatus, BadgeTypeAssignment
from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.badges.models import (
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    DigitalEntryPass,
    DigitalEntryPassStatus,
    PassCredentialSeries,
    PassLifecycleOperation,
    PassLifecycleOperationType,
    PassReasonCode,
)
from apps.badges.services import (
    PERMITTED_TRANSITIONS,
    PassConcurrencyError,
    PassConfigurationError,
    PassNotEligibleError,
    PassStateError,
    activate_pass,
    expire_due_passes,
    generate_pass,
    issue_pass_token,
    new_operation_id,
    replace_pass,
    resume_pass,
    revoke_pass,
    suspend_pass,
    transition_is_permitted,
)
from apps.core.outbox.persistent import OutboxEvent
from apps.registrations.models import RegistrationPublicStatus

pytestmark = pytest.mark.django_db


def _generate(registration, actor):
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
# Status vocabulary
# ---------------------------------------------------------------------------


def test_the_status_vocabulary_is_exactly_the_six_approved_members():
    assert set(DigitalEntryPassStatus.values) == {
        "INACTIVE",
        "ACTIVE",
        "SUSPENDED",
        "REVOKED",
        "EXPIRED",
        "REPLACED",
    }


def test_not_generated_is_not_a_persisted_status():
    """Absence of a row is 'not generated'; it is never stored."""
    assert "NOT_GENERATED" not in DigitalEntryPassStatus.values
    assert "READY" not in DigitalEntryPassStatus.values


def test_current_and_terminal_partitions_are_complete_and_disjoint():
    assert set(NON_TERMINAL_STATUSES) | set(TERMINAL_STATUSES) == set(DigitalEntryPassStatus.values)
    assert not set(NON_TERMINAL_STATUSES) & set(TERMINAL_STATUSES)


# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------

ALLOWED_PAIRS = {
    ("INACTIVE", "ACTIVE"),
    ("INACTIVE", "REVOKED"),
    ("INACTIVE", "REPLACED"),
    ("INACTIVE", "EXPIRED"),
    ("ACTIVE", "SUSPENDED"),
    ("ACTIVE", "REVOKED"),
    ("ACTIVE", "REPLACED"),
    ("ACTIVE", "EXPIRED"),
    ("SUSPENDED", "ACTIVE"),
    ("SUSPENDED", "REVOKED"),
    ("SUSPENDED", "REPLACED"),
    ("SUSPENDED", "EXPIRED"),
}


@pytest.mark.parametrize("current", DigitalEntryPassStatus.values)
@pytest.mark.parametrize("target", DigitalEntryPassStatus.values)
def test_the_full_status_matrix_matches_the_approved_transition_list(current, target):
    """Table-driven over all 36 pairs: no transition can be added quietly."""
    assert transition_is_permitted(current, target) is ((current, target) in ALLOWED_PAIRS)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_statuses_have_no_outgoing_transition(status):
    assert PERMITTED_TRANSITIONS[status] == frozenset()


# ---------------------------------------------------------------------------
# Generation and eligibility
# ---------------------------------------------------------------------------


def test_generation_creates_an_inactive_credential(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    assert credential.status == DigitalEntryPassStatus.INACTIVE
    assert credential.credential_version == 1
    assert credential.payload_hash
    assert credential.series.registration_id == eligible_registration.pk


def test_generation_creates_a_stable_series_with_both_identifiers(
    eligible_registration, pass_admin, active_key
):
    credential = _generate(eligible_registration, pass_admin)
    series = PassCredentialSeries.objects.get(pk=credential.series_id)
    assert len(series.public_id) == 22
    assert len(series.fallback_reference) == 9
    assert series.next_credential_version == 2


def test_generation_is_blocked_for_a_registration_that_is_not_approved(
    event, organization, person, pass_admin, active_key, role, badge_type, access_profile
):
    from apps.badges.tests.conftest import grant_required_assignments, make_registration

    registration = make_registration(
        event=event,
        organization=organization,
        person=person,
        public_status=RegistrationPublicStatus.SUBMITTED,
    )
    grant_required_assignments(
        registration=registration,
        event=event,
        actor=pass_admin,
        role=role,
        badge_type=badge_type,
        access_profile=access_profile,
    )
    with pytest.raises(PassNotEligibleError) as exc:
        _generate(registration, pass_admin)
    assert exc.value.reason == "NOT_APPROVED"


def test_generation_is_blocked_without_the_required_assignments(
    event, organization, person, pass_admin, active_key
):
    from apps.badges.tests.conftest import make_registration

    registration = make_registration(event=event, organization=organization, person=person)
    with pytest.raises(PassNotEligibleError) as exc:
        _generate(registration, pass_admin)
    assert exc.value.reason == "NO_CURRENT_ROLE"


def test_a_blocked_generation_is_audited_as_denied(
    event, organization, person, pass_admin, active_key
):
    from apps.badges.tests.conftest import make_registration

    registration = make_registration(event=event, organization=organization, person=person)
    with pytest.raises(PassNotEligibleError):
        _generate(registration, pass_admin)
    entry = AuditEvent.objects.filter(
        action_code=action_codes.CREDENTIAL_GENERATION_BLOCKED
    ).latest("occurred_at")
    assert entry.result == "DENIED"
    assert entry.after_summary["reason"] == "NO_CURRENT_ROLE"


def test_generation_requires_an_active_verification_key(
    eligible_registration, pass_admin, signing_provider
):
    with pytest.raises(PassConfigurationError, match="ACTIVE verification key"):
        _generate(eligible_registration, pass_admin)


def test_a_second_current_credential_cannot_be_generated(
    eligible_registration, pass_admin, active_key
):
    _generate(eligible_registration, pass_admin)
    with pytest.raises(PassStateError, match="already has a current"):
        _generate(eligible_registration, pass_admin)


def test_the_one_current_constraint_covers_all_three_non_terminal_statuses(
    eligible_registration, pass_admin, active_key
):
    credential = _generate(eligible_registration, pass_admin)
    series = credential.series
    for status in NON_TERMINAL_STATUSES:
        credential.status = status
        credential.save(update_fields=["status"])
        with pytest.raises(IntegrityError), transaction.atomic():
            DigitalEntryPass.objects.create(
                series=series,
                registration=eligible_registration,
                event_edition=eligible_registration.event_edition,
                credential_version=99,
                jti="Z" * 22,
                status=DigitalEntryPassStatus.INACTIVE,
                role_assignment=credential.role_assignment,
                badge_assignment=credential.badge_assignment,
                access_assignment=credential.access_assignment,
                event_code=credential.event_code,
                badge_type_code=credential.badge_type_code,
                access_profile_code=credential.access_profile_code,
                payload_version=1,
                signing_key_id="v1",
                payload_hash="x" * 64,
                nonce="n" * 16,
                valid_from=credential.valid_from,
                valid_until=credential.valid_until,
            )


# ---------------------------------------------------------------------------
# Activation, suspension, resumption, revocation
# ---------------------------------------------------------------------------


def test_activation_is_explicit_and_never_automatic(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    assert credential.status == DigitalEntryPassStatus.INACTIVE
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.INACTIVE

    credential = _activate(credential, pass_admin)
    assert credential.status == DigitalEntryPassStatus.ACTIVE
    assert credential.activated_at is not None
    assert credential.activated_by_id == pass_admin.pk


def test_an_expired_window_cannot_be_activated(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    credential.valid_from = timezone.now() - timedelta(days=3)
    credential.valid_until = timezone.now() - timedelta(days=1)
    credential.save(update_fields=["valid_from", "valid_until"])
    with pytest.raises(PassStateError, match="after its validity period"):
        _activate(credential, pass_admin)


def test_suspension_then_explicit_resumption(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    suspend_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.SECURITY_CONCERN,
    )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.SUSPENDED
    assert credential.suspended_at is not None

    resume_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
    )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE
    assert credential.resumed_at is not None


def test_only_a_suspended_credential_can_be_resumed(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    with pytest.raises(PassStateError, match="Only a suspended"):
        resume_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
        )


def test_revocation_is_terminal(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    revoke_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.REVOKED
    with pytest.raises(PassStateError):
        activate_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_lock_version=credential.version,
        )


def test_an_inactive_credential_can_be_revoked_directly(
    eligible_registration, pass_admin, active_key
):
    credential = _generate(eligible_registration, pass_admin)
    revoke_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.ADMINISTRATIVE_ERROR,
    )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.REVOKED


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_inactive_to_expired_is_persisted_by_the_sweep(
    eligible_registration, pass_admin, active_key
):
    credential = _generate(eligible_registration, pass_admin)
    credential.valid_from = timezone.now() - timedelta(days=3)
    credential.valid_until = timezone.now() - timedelta(days=1)
    credential.save(update_fields=["valid_from", "valid_until"])

    assert expire_due_passes() == 1
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.EXPIRED
    assert credential.expired_at is not None


def test_the_expiry_sweep_is_idempotent(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    credential.valid_from = timezone.now() - timedelta(days=3)
    credential.valid_until = timezone.now() - timedelta(days=1)
    credential.save(update_fields=["valid_from", "valid_until"])
    assert expire_due_passes() == 1
    assert expire_due_passes() == 0


def test_the_sweep_records_an_audit_event(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    credential.valid_until = timezone.now() - timedelta(days=1)
    credential.valid_from = timezone.now() - timedelta(days=3)
    credential.save(update_fields=["valid_from", "valid_until"])
    expire_due_passes()
    assert AuditEvent.objects.filter(action_code=action_codes.CREDENTIAL_EXPIRY_PERSISTED).exists()


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


def test_replacement_supersedes_the_prior_credential(eligible_registration, pass_admin, active_key):
    original = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    original_jti = original.jti

    replacement = replace_pass(
        credential=original,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=original.jti,
        expected_lock_version=original.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    ).credential

    original.refresh_from_db()
    assert original.status == DigitalEntryPassStatus.REPLACED
    assert original.replaced_by_id == replacement.pk
    assert original.jti == original_jti, "signed evidence must never be mutated"

    assert replacement.status == DigitalEntryPassStatus.INACTIVE
    assert replacement.credential_version == original.credential_version + 1
    assert replacement.jti != original.jti
    assert replacement.nonce != original.nonce


def test_the_stable_identifiers_survive_replacement(eligible_registration, pass_admin, active_key):
    """A printed fallback reference keeps working after replacement."""
    original = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    series_public_id = original.series.public_id
    series_reference = original.series.fallback_reference

    replacement = replace_pass(
        credential=original,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=original.jti,
        expected_lock_version=original.version,
        reason_code=PassReasonCode.PARTICIPANT_REQUEST,
    ).credential

    assert replacement.series.public_id == series_public_id
    assert replacement.series.fallback_reference == series_reference
    assert PassCredentialSeries.objects.count() == 1


def test_replacement_rejects_a_stale_expected_jti(eligible_registration, pass_admin, active_key):
    """The decisive guard: a new credential also starts at lock version 1."""
    original = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    replacement = replace_pass(
        credential=original,
        actor=pass_admin,
        operation_id=new_operation_id(),
        expected_jti=original.jti,
        expected_lock_version=original.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    ).credential

    # A second request still naming the ORIGINAL credential must not replace
    # the freshly created one, even though both carry lock version 1.
    assert replacement.version == 1
    with pytest.raises(PassConcurrencyError, match="no longer the current one"):
        replace_pass(
            credential=original,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_jti=original.jti,
            expected_lock_version=1,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
    assert DigitalEntryPass.objects.count() == 2


def test_replacement_rejects_a_stale_lock_version(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    with pytest.raises(PassConcurrencyError, match="changed since"):
        replace_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_jti=credential.jti,
            expected_lock_version=credential.version + 5,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )


def test_replacement_reruns_eligibility(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    BadgeTypeAssignment.objects.filter(registration=eligible_registration).update(
        status=AssignmentStatus.REVOKED
    )
    with pytest.raises(PassNotEligibleError):
        replace_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_jti=credential.jti,
            expected_lock_version=credential.version,
            reason_code=PassReasonCode.ASSIGNMENT_CHANGED,
        )
    credential.refresh_from_db()
    assert credential.status == DigitalEntryPassStatus.ACTIVE


def test_a_stale_lock_version_transition_is_rejected(eligible_registration, pass_admin, active_key):
    credential = _generate(eligible_registration, pass_admin)
    with pytest.raises(PassConcurrencyError):
        activate_pass(
            credential=credential,
            actor=pass_admin,
            operation_id=new_operation_id(),
            expected_lock_version=credential.version + 3,
        )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_a_repeated_generate_operation_id_is_a_replay(
    eligible_registration, pass_admin, active_key
):
    operation_id = new_operation_id()
    first = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=operation_id
    )
    second = generate_pass(
        registration=eligible_registration, actor=pass_admin, operation_id=operation_id
    )
    assert second.replayed is True
    assert second.credential.pk == first.credential.pk
    assert DigitalEntryPass.objects.count() == 1
    # Count GENERATE rows specifically: promoting the verification key in
    # the fixture legitimately records its own key-lifecycle operation.
    assert (
        PassLifecycleOperation.objects.filter(
            operation_type=PassLifecycleOperationType.GENERATE
        ).count()
        == 1
    )


def test_a_repeated_replace_operation_id_does_not_mint_a_second_credential(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    operation_id = new_operation_id()
    first = replace_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    second = replace_pass(
        credential=credential,
        actor=pass_admin,
        operation_id=operation_id,
        expected_jti=credential.jti,
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )
    assert second.replayed is True
    assert second.credential.pk == first.credential.pk
    assert DigitalEntryPass.objects.count() == 2
    credential.series.refresh_from_db()
    assert credential.series.next_credential_version == 3


def test_a_replay_emits_no_second_notification(eligible_registration, pass_admin, active_key):
    operation_id = new_operation_id()
    generate_pass(registration=eligible_registration, actor=pass_admin, operation_id=operation_id)
    before = OutboxEvent.objects.filter(event_type="badges.pass.generated").count()
    generate_pass(registration=eligible_registration, actor=pass_admin, operation_id=operation_id)
    assert OutboxEvent.objects.filter(event_type="badges.pass.generated").count() == before


def test_audit_and_outbox_rows_are_written_in_the_same_transaction(
    eligible_registration, pass_admin, active_key
):
    """Neither is deferred to on_commit, so a rollback removes both."""
    credential = _generate(eligible_registration, pass_admin)
    assert AuditEvent.objects.filter(
        action_code=action_codes.CREDENTIAL_GENERATED, target_uuid=credential.pk
    ).exists()
    assert OutboxEvent.objects.filter(aggregate_id=str(credential.pk)).exists()


def test_a_malformed_operation_id_is_refused(eligible_registration, pass_admin, active_key):
    from apps.badges.services import PassServiceError

    with pytest.raises(PassServiceError):
        generate_pass(registration=eligible_registration, actor=pass_admin, operation_id="short")


# ---------------------------------------------------------------------------
# QR exposure
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
def test_only_an_active_credential_exposes_a_usable_qr(
    eligible_registration, pass_admin, active_key, status
):
    credential = _generate(eligible_registration, pass_admin)
    credential.status = status
    credential.save(update_fields=["status"])
    with pytest.raises(PassStateError, match="only for an active"):
        issue_pass_token(credential)


def test_an_active_credential_exposes_a_qr(eligible_registration, pass_admin, active_key):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    token = issue_pass_token(credential)
    assert token.count(".") == 2


def test_no_setting_can_re_enable_pre_activation_exposure(
    eligible_registration, pass_admin, active_key, settings
):
    """ACTIVE-only is an invariant, not a policy with an escape hatch.

    The removed `PASS_PRE_ACTIVATION_QR_ENABLED` switch must stay removed:
    setting it (or anything like it) has no effect at all.
    """
    credential = _generate(eligible_registration, pass_admin)
    settings.PASS_PRE_ACTIVATION_QR_ENABLED = True
    with pytest.raises(PassStateError, match="only for an active"):
        issue_pass_token(credential)


def test_the_pre_activation_setting_no_longer_exists(settings):
    from django.conf import settings as configured

    assert not hasattr(configured, "PASS_PRE_ACTIVATION_QR_ENABLED")


# ---------------------------------------------------------------------------
# Validity window
# ---------------------------------------------------------------------------


def test_the_validity_window_comes_from_the_event_with_zero_margins(
    eligible_registration, pass_admin, active_key, event
):
    credential = _generate(eligible_registration, pass_admin)
    assert credential.valid_from == event.starts_at
    assert credential.valid_until == event.ends_at


def test_configured_margins_widen_the_window(
    eligible_registration, pass_admin, active_key, event, settings
):
    settings.PASS_VALIDITY_MARGIN_BEFORE_SECONDS = 3600
    settings.PASS_VALIDITY_MARGIN_AFTER_SECONDS = 7200
    credential = _generate(eligible_registration, pass_admin)
    assert credential.valid_from == event.starts_at - timedelta(seconds=3600)
    assert credential.valid_until == event.ends_at + timedelta(seconds=7200)


def test_an_empty_validity_interval_fails_closed(
    eligible_registration, pass_admin, active_key, event, settings
):
    settings.PASS_VALIDITY_MARGIN_AFTER_SECONDS = -(10**9)
    with pytest.raises(PassConfigurationError, match="empty validity interval"):
        _generate(eligible_registration, pass_admin)


# ---------------------------------------------------------------------------
# Audit hygiene
# ---------------------------------------------------------------------------


def test_no_audit_summary_contains_a_raw_qr_or_signing_input(
    eligible_registration, pass_admin, active_key
):
    credential = _activate(_generate(eligible_registration, pass_admin), pass_admin)
    token = issue_pass_token(credential)
    header, payload, signature = token.split(".")
    for entry in AuditEvent.objects.all():
        blob = f"{entry.before_summary}{entry.after_summary}"
        assert token not in blob
        assert payload not in blob
        assert signature not in blob
        assert f"{header}.{payload}" not in blob
