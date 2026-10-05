"""Entry Events, idempotency, stale verifications, overrides, and restrictions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.badges.models import PassReasonCode
from apps.badges.services import OperationConflictError, new_operation_id, revoke_pass
from apps.entry.models import (
    EntryDecision,
    EntryEvent,
    EntryOverride,
    EntryOverrideReason,
    EntryReasonCode,
    EntryResult,
    RestrictionCategory,
    RestrictionSeverity,
    SecurityRestriction,
)
from apps.entry.services import EntryPermissionError, EntryStateError
from apps.entry.services.decisions import (
    OverrideNotPermittedError,
    PendingVerification,
    StaleVerificationError,
    available_override_reasons,
    pending_from_assessment,
    record_entry_decision,
    record_override,
)
from apps.entry.services.restrictions import (
    RestrictionConfigurationError,
    create_restriction,
    revoke_restriction,
)
from apps.entry.services.verification import verify_qr
from apps.entry.tests import factories

pytestmark = pytest.mark.django_db


def _verify(checkpoint, credential):
    outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential))
    pending = pending_from_assessment(
        assessment=outcome.assessment, checkpoint=checkpoint, method="QR"
    )
    return outcome, pending


def _roundtrip(pending):
    """Exactly what the session store does between two requests."""
    return PendingVerification.from_session(pending.to_session())


@pytest.fixture
def restriction_manager(event):
    return factories.make_user(
        "restrictions@example.test", group_name="Security Restriction Managers", event=event
    )


@pytest.fixture
def expired_pass_catalogue(event):
    return EntryOverrideReason.objects.create(
        event_edition=event,
        code="EXPIRED_PASS_ACCEPTED",
        name="Expired pass accepted by supervisor",
        overridable_reason_codes=[EntryReasonCode.PASS_EXPIRED],
        requires_note=True,
    )


class TestRecordingDecisions:
    def test_admission_writes_an_immutable_entry_event(self, checkpoint, active_pass, registration):
        _, pending = _verify(checkpoint, active_pass)
        outcome = record_entry_decision(
            checkpoint=checkpoint, pending=_roundtrip(pending), decision=EntryDecision.ADMIT
        )
        event = outcome.entry_event
        assert outcome.replayed is False
        assert event.registration == registration
        assert event.digital_entry_pass == active_pass
        assert event.badge_assignment is not None
        assert event.gate == checkpoint.gate
        assert event.zone == checkpoint.zone
        assert event.device == checkpoint.device
        assert event.operator_user == checkpoint.user
        assert event.verification_method == "QR"
        assert event.result == EntryResult.ALLOWED
        assert event.decision == EntryDecision.ADMIT
        assert event.offline is False
        assert event.operation_id == pending.operation_id
        assert AuditEvent.objects.filter(
            action_code="ENT_EVENT_RECORDED", target_uuid=event.pk
        ).exists()

    def test_verification_alone_never_writes_an_entry_event(self, checkpoint, active_pass):
        _verify(checkpoint, active_pass)
        assert not EntryEvent.objects.exists()

    def test_retry_with_the_same_operation_is_idempotent(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        first = record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        second = record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        assert second.replayed is True
        assert second.entry_event.pk == first.entry_event.pk
        assert EntryEvent.objects.count() == 1

    def test_retry_after_the_verification_window_still_replays(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        with override_settings(ENTRY_DECISION_TICKET_SECONDS=0):
            replay = record_entry_decision(
                checkpoint=checkpoint,
                pending=pending,
                decision="ADMIT",
                now=timezone.now() + timedelta(minutes=5),
            )
        assert replay.replayed is True

    def test_same_operation_for_a_different_decision_is_a_conflict(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        with pytest.raises(OperationConflictError):
            record_entry_decision(
                checkpoint=checkpoint,
                pending=pending,
                decision="DO_NOT_ADMIT",
                decision_reason="IDENTITY_MISMATCH",
            )
        assert EntryEvent.objects.count() == 1

    def test_non_admission_is_recorded_with_a_reason(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        event = record_entry_decision(
            checkpoint=checkpoint,
            pending=pending,
            decision="DO_NOT_ADMIT",
            decision_reason="IDENTITY_MISMATCH",
        ).entry_event
        assert event.decision == EntryDecision.DO_NOT_ADMIT
        assert event.decision_reason_code == "IDENTITY_MISMATCH"

    def test_denied_result_cannot_be_admitted(self, checkpoint, active_pass, staff):
        revoke_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
        active_pass.refresh_from_db()
        from apps.entry.services.verification import lookup_reference

        outcome = lookup_reference(
            checkpoint=checkpoint, raw_reference=active_pass.registration.public_reference
        )
        assert outcome.reason_code == EntryReasonCode.PASS_REVOKED
        pending = pending_from_assessment(
            assessment=outcome.assessment, checkpoint=checkpoint, method="REFERENCE"
        )
        with pytest.raises(EntryStateError):
            record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        assert not EntryEvent.objects.exists()
        assert AuditEvent.objects.filter(
            action_code="ENT_DECISION_REJECTED", reason_code="NOT_ADMITTABLE"
        ).exists()
        denied = record_entry_decision(
            checkpoint=checkpoint, pending=pending, decision="DO_NOT_ADMIT"
        ).entry_event
        assert denied.result == EntryResult.DENIED
        assert denied.reason_code == EntryReasonCode.PASS_REVOKED
        assert denied.decision_reason_code == "FOLLOWS_RESULT"

    def test_state_change_after_verification_is_stale(self, checkpoint, active_pass, staff):
        _, pending = _verify(checkpoint, active_pass)
        revoke_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
        with pytest.raises(StaleVerificationError):
            record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        assert not EntryEvent.objects.exists()

    def test_expired_verification_is_stale(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        later = timezone.now() + timedelta(minutes=10)
        with pytest.raises(StaleVerificationError):
            record_entry_decision(
                checkpoint=checkpoint, pending=pending, decision="ADMIT", now=later
            )

    def test_another_operator_session_cannot_use_the_verification(
        self, checkpoint, active_pass, supervisor
    ):
        from apps.entry.services.sessions import join_checkpoint_session, resolve_checkpoint

        _, pending = _verify(checkpoint, active_pass)
        joined = join_checkpoint_session(device_session=checkpoint.device_session, user=supervisor)
        other = resolve_checkpoint(
            device=checkpoint.device,
            device_session_id=checkpoint.device_session.pk,
            operator_session_id=joined.pk,
            user=supervisor,
        )
        with pytest.raises(StaleVerificationError):
            record_entry_decision(checkpoint=other, pending=pending, decision="ADMIT")

    def test_duplicate_scan_second_admission_becomes_stale_then_advisory(
        self, checkpoint, active_pass
    ):
        _, first = _verify(checkpoint, active_pass)
        _, second = _verify(checkpoint, active_pass)
        record_entry_decision(checkpoint=checkpoint, pending=first, decision="ADMIT")
        with pytest.raises(StaleVerificationError):
            record_entry_decision(checkpoint=checkpoint, pending=second, decision="ADMIT")
        assert EntryEvent.objects.count() == 1
        outcome, third = _verify(checkpoint, active_pass)
        assert outcome.result == EntryResult.ALLOWED_WITH_ADVISORY
        reentry = record_entry_decision(checkpoint=checkpoint, pending=third, decision="ADMIT")
        assert reentry.entry_event.prior_entry_event is not None
        assert EntryEvent.objects.count() == 2


class TestDatabaseGuards:
    def test_admission_without_allowed_result_or_override_is_impossible(
        self, checkpoint, active_pass
    ):
        _, pending = _verify(checkpoint, active_pass)
        event = record_entry_decision(
            checkpoint=checkpoint, pending=pending, decision="DO_NOT_ADMIT"
        ).entry_event
        with pytest.raises(IntegrityError), transaction.atomic():
            EntryEvent.objects.create(
                operation_id=new_operation_id(),
                command_fingerprint="x" * 64,
                event_edition=event.event_edition,
                registration=event.registration,
                gate=event.gate,
                zone=event.zone,
                device=event.device,
                device_session=event.device_session,
                operator_session=event.operator_session,
                operator_user=event.operator_user,
                verification_method="QR",
                result=EntryResult.DENIED,
                decision=EntryDecision.ADMIT,
                occurred_at=timezone.now(),
                recorded_at=timezone.now(),
            )

    @pytest.mark.parametrize(
        "statement", ["UPDATE entry_event SET decision = 'ADMIT'", "DELETE FROM entry_event"]
    )
    def test_entry_events_are_append_only(self, checkpoint, active_pass, statement):
        _, pending = _verify(checkpoint, active_pass)
        record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        with pytest.raises(Exception, match="append-only"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(statement)

    def test_orm_update_is_refused(self, checkpoint, active_pass):
        _, pending = _verify(checkpoint, active_pass)
        record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")
        with pytest.raises(Exception, match="append-only"), transaction.atomic():
            EntryEvent.objects.update(decision_reason_code="IDENTITY_MISMATCH")


class TestOverrides:
    def _expired(self, checkpoint, active_pass):
        later = active_pass.valid_until + timedelta(minutes=30)
        outcome = verify_qr(
            checkpoint=checkpoint, raw_token=factories.token_for(active_pass), now=later
        )
        pending = pending_from_assessment(
            assessment=outcome.assessment, checkpoint=checkpoint, method="QR"
        )
        # Fresh relative to `later`, the instant the override is attempted.
        pending = PendingVerification(**{**pending.__dict__, "verified_at": later.isoformat()})
        return outcome, pending, later

    def test_no_catalogue_means_no_override(self, supervisor_checkpoint, active_pass):
        outcome, pending, later = self._expired(supervisor_checkpoint, active_pass)
        assert outcome.reason_code == EntryReasonCode.PASS_EXPIRED
        assert (
            available_override_reasons(
                checkpoint=supervisor_checkpoint, assessment=outcome.assessment
            )
            == []
        )

    def test_supervisor_override_admits_with_full_evidence(
        self, supervisor_checkpoint, active_pass, expired_pass_catalogue
    ):
        outcome, pending, later = self._expired(supervisor_checkpoint, active_pass)
        assert available_override_reasons(
            checkpoint=supervisor_checkpoint, assessment=outcome.assessment
        ) == [expired_pass_catalogue]
        result = record_override(
            checkpoint=supervisor_checkpoint,
            pending=pending,
            override_reason_id=expired_pass_catalogue.pk,
            note="Synthetic: organiser confirmed extension",
            now=later,
        )
        event = result.entry_event
        override = event.override
        assert event.decision == EntryDecision.ADMIT
        assert event.result == EntryResult.DENIED
        assert override.original_result == EntryResult.DENIED
        assert override.original_reason_code == EntryReasonCode.PASS_EXPIRED
        assert override.reason_code == "EXPIRED_PASS_ACCEPTED"
        assert override.user == supervisor_checkpoint.user
        assert override.gate == supervisor_checkpoint.gate
        assert override.device == supervisor_checkpoint.device
        audit = AuditEvent.objects.get(action_code="ENT_OVERRIDE_RECORDED")
        assert "Synthetic" not in str(audit.after_summary)
        # The note is encrypted at rest.
        with connection.cursor() as cursor:
            cursor.execute("SELECT note_encrypted FROM entry_override WHERE id = %s", [override.pk])
            raw = cursor.fetchone()[0]
        assert "Synthetic" not in raw
        assert EntryOverride.objects.get(pk=override.pk).note_encrypted.startswith("Synthetic")

    def test_override_requires_the_override_permission(
        self, checkpoint, active_pass, expired_pass_catalogue
    ):
        outcome, pending, later = self._expired(checkpoint, active_pass)
        assert (
            available_override_reasons(checkpoint=checkpoint, assessment=outcome.assessment) == []
        )
        with pytest.raises(EntryPermissionError):
            record_override(
                checkpoint=checkpoint,
                pending=pending,
                override_reason_id=expired_pass_catalogue.pk,
                note="x",
                now=later,
            )
        assert not EntryEvent.objects.exists()

    def test_note_is_required_when_configured(
        self, supervisor_checkpoint, active_pass, expired_pass_catalogue
    ):
        _, pending, later = self._expired(supervisor_checkpoint, active_pass)
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint,
                pending=pending,
                override_reason_id=expired_pass_catalogue.pk,
                note="  ",
                now=later,
            )

    def test_catalogue_cannot_make_a_fixed_exclusion_overrideable(
        self, event, supervisor_checkpoint, active_pass, staff
    ):
        reason = EntryOverrideReason.objects.create(
            event_edition=event,
            code="MISCONFIGURED",
            name="Misconfigured catalogue entry",
            overridable_reason_codes=[EntryReasonCode.PASS_REVOKED, EntryReasonCode.WRONG_EVENT],
            requires_note=False,
        )
        from apps.entry.services.verification import lookup_reference

        revoke_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
        outcome = lookup_reference(
            checkpoint=supervisor_checkpoint,
            raw_reference=active_pass.registration.public_reference,
        )
        assert outcome.reason_code == EntryReasonCode.PASS_REVOKED
        assert not outcome.assessment.may_be_overridden
        pending = pending_from_assessment(
            assessment=outcome.assessment, checkpoint=supervisor_checkpoint, method="REFERENCE"
        )
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint, pending=pending, override_reason_id=reason.pk
            )
        assert not EntryEvent.objects.exists()
        assert AuditEvent.objects.filter(
            action_code="ENT_OVERRIDE_REJECTED", reason_code="NON_OVERRIDEABLE"
        ).exists()

    def test_override_must_cover_every_blocker(
        self, supervisor_checkpoint, active_pass, expired_pass_catalogue, setup
    ):
        # Expired pass AND no allow rule for this zone: the catalogue entry
        # covers only the first, so the override is refused.
        from apps.accreditation.models import AccessRule

        AccessRule.objects.filter(pk=setup.main_rule.pk).update(is_active=False)
        outcome, pending, later = self._expired(supervisor_checkpoint, active_pass)
        assert set(outcome.assessment.blocker_codes) == {
            EntryReasonCode.PASS_EXPIRED,
            EntryReasonCode.WRONG_ZONE,
        }
        assert (
            available_override_reasons(
                checkpoint=supervisor_checkpoint, assessment=outcome.assessment
            )
            == []
        )
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint,
                pending=pending,
                override_reason_id=expired_pass_catalogue.pk,
                note="x",
                now=later,
            )

    def test_override_cannot_be_used_for_an_allowed_result(
        self, event, supervisor_checkpoint, active_pass
    ):
        reason = EntryOverrideReason.objects.create(
            event_edition=event,
            code="ANY",
            name="Any",
            overridable_reason_codes=list(EntryReasonCode.values),
            requires_note=False,
        )
        _, pending = _verify(supervisor_checkpoint, active_pass)
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint, pending=pending, override_reason_id=reason.pk
            )

    def test_catalogue_of_another_event_is_unavailable(
        self, other_event, supervisor_checkpoint, active_pass
    ):
        foreign = EntryOverrideReason.objects.create(
            event_edition=other_event,
            code="FOREIGN",
            name="Foreign",
            overridable_reason_codes=[EntryReasonCode.PASS_EXPIRED],
            requires_note=False,
        )
        _, pending, later = self._expired(supervisor_checkpoint, active_pass)
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint,
                pending=pending,
                override_reason_id=foreign.pk,
                now=later,
            )


class TestRestrictions:
    def test_person_level_restriction_denies_every_context(
        self,
        event,
        checkpoint,
        person,
        registration,
        setup,
        staff,
        active_pass,
        restriction_manager,
    ):
        second = factories.make_registration(event=event, person=person)
        factories.assign(registration=second, setup=setup, actor=staff)
        second_pass = factories.issue_active_pass(registration=second, actor=staff)
        create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic restricted reason text",
        )
        for credential in (active_pass, second_pass):
            outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential))
            assert outcome.result == EntryResult.DENIED
            assert outcome.reason_code == EntryReasonCode.SECURITY_RESTRICTION
            assert not outcome.assessment.may_be_overridden

    def test_context_level_restriction_affects_only_that_context(
        self,
        event,
        checkpoint,
        person,
        registration,
        setup,
        staff,
        active_pass,
        restriction_manager,
    ):
        second = factories.make_registration(event=event, person=person)
        factories.assign(registration=second, setup=setup, actor=staff)
        second_pass = factories.issue_active_pass(registration=second, actor=staff)
        create_restriction(
            actor=restriction_manager,
            registration=registration,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.ACCREDITATION_HOLD,
            reason="Synthetic hold",
        )
        assert (
            verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass)).reason_code
            == EntryReasonCode.SECURITY_RESTRICTION
        )
        assert (
            verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(second_pass)).result
            == EntryResult.ALLOWED
        )

    def test_review_severity_requires_manual_review(
        self, event, checkpoint, person, active_pass, restriction_manager
    ):
        create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.MANUAL_REVIEW,
            category=RestrictionCategory.IDENTITY_DISPUTE,
            reason="Synthetic dispute",
            is_overrideable=True,
        )
        outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass))
        assert outcome.result == EntryResult.MANUAL_REVIEW
        assert outcome.reason_code == EntryReasonCode.RESTRICTION_REVIEW
        assert outcome.assessment.may_be_overridden

    def test_non_overrideable_restriction_refuses_every_override(
        self, event, supervisor_checkpoint, person, active_pass, restriction_manager
    ):
        create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.MANUAL_REVIEW,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic",
            is_overrideable=False,
        )
        reason = EntryOverrideReason.objects.create(
            event_edition=event,
            code="REVIEWED",
            name="Reviewed",
            overridable_reason_codes=[EntryReasonCode.RESTRICTION_REVIEW],
            requires_note=False,
        )
        _, pending = _verify(supervisor_checkpoint, active_pass)
        with pytest.raises(OverrideNotPermittedError):
            record_override(
                checkpoint=supervisor_checkpoint, pending=pending, override_reason_id=reason.pk
            )
        assert not EntryEvent.objects.exists()

    def test_overrideable_restriction_can_be_overridden_with_catalogue(
        self, event, supervisor_checkpoint, person, active_pass, restriction_manager
    ):
        create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.MANUAL_REVIEW,
            category=RestrictionCategory.IDENTITY_DISPUTE,
            reason="Synthetic",
            is_overrideable=True,
        )
        reason = EntryOverrideReason.objects.create(
            event_edition=event,
            code="IDENTITY_CONFIRMED",
            name="Identity confirmed at desk",
            overridable_reason_codes=[EntryReasonCode.RESTRICTION_REVIEW],
            requires_note=False,
        )
        _, pending = _verify(supervisor_checkpoint, active_pass)
        event_row = record_override(
            checkpoint=supervisor_checkpoint, pending=pending, override_reason_id=reason.pk
        ).entry_event
        assert event_row.override is not None
        assert event_row.result == EntryResult.MANUAL_REVIEW

    def test_revoked_restriction_no_longer_applies(
        self, event, checkpoint, person, active_pass, restriction_manager
    ):
        restriction = create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic",
        )
        revoke_restriction(
            restriction=restriction, actor=restriction_manager, reason_code="CLEARED"
        )
        outcome = verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(active_pass))
        assert outcome.result == EntryResult.ALLOWED

    def test_restriction_reason_is_encrypted_and_never_audited(
        self, event, person, restriction_manager
    ):
        restriction = create_restriction(
            actor=restriction_manager,
            person=person,
            event_edition=event,
            severity=RestrictionSeverity.DENY_ENTRY,
            category=RestrictionCategory.SECURITY_CONCERN,
            reason="Synthetic restricted reason text",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT reason_encrypted FROM entry_security_restriction WHERE id = %s",
                [restriction.pk],
            )
            assert "Synthetic" not in cursor.fetchone()[0]
        audit = AuditEvent.objects.get(action_code="ENT_SECURITY_RESTRICTION_CREATED")
        assert "Synthetic" not in str(audit.after_summary)

    def test_restriction_management_is_permissioned_and_validated(
        self, event, person, operator, restriction_manager, registration
    ):
        with pytest.raises(EntryPermissionError):
            create_restriction(
                actor=operator,
                person=person,
                event_edition=event,
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                reason="x",
            )
        with pytest.raises(RestrictionConfigurationError):
            create_restriction(
                actor=restriction_manager,
                person=person,
                registration=registration,
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                reason="x",
            )
        with pytest.raises(RestrictionConfigurationError):
            create_restriction(
                actor=restriction_manager,
                person=person,
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                reason="x",
            )

    def test_exactly_one_target_is_enforced_by_the_database(self, person, registration, staff):
        with pytest.raises(IntegrityError), transaction.atomic():
            SecurityRestriction.objects.create(
                person=person,
                registration=registration,
                severity=RestrictionSeverity.DENY_ENTRY,
                category=RestrictionCategory.SECURITY_CONCERN,
                starts_at=timezone.now(),
                reason_encrypted="x",
                created_by=staff,
            )
