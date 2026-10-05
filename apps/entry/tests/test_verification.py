"""Online verification: signed QR, identity-reference lookup, reference and
manual lookup, access rules, and the explicit result vocabulary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accreditation.models import (
    AccessRule,
    AccessRuleAssignment,
    AccessRuleEffect,
    AssignmentStatus,
    BadgeTypeAssignment,
    ReentryPolicy,
)
from apps.audit.models import AuditEvent
from apps.badges.models import PassReasonCode
from apps.badges.services import new_operation_id, replace_pass, revoke_pass, suspend_pass
from apps.entry.models import EntryReasonCode, EntryResult
from apps.entry.services import EntryPermissionError
from apps.entry.services.verification import (
    assess_selected_candidate,
    lookup_identity,
    lookup_reference,
    manual_search,
    verify_qr,
)
from apps.entry.tests import factories
from apps.registrations.models import RegistrationPublicStatus

pytestmark = pytest.mark.django_db

SYNTHETIC_NIN = "109870123456789012"
SYNTHETIC_PASSPORT = "ZX9087654"
UNTRUSTED_INPUT = "not-a-credential"


def _qr(checkpoint, credential, **kwargs):
    return verify_qr(checkpoint=checkpoint, raw_token=factories.token_for(credential), **kwargs)


class TestQrResults:
    def test_valid_pass_is_allowed(self, checkpoint, active_pass, registration):
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.ALLOWED
        assert outcome.assessment.registration == registration
        assert outcome.assessment.credential == active_pass
        assert outcome.assessment.blockers == ()

    def test_hierarchical_zone_is_covered_by_parent_rule(
        self, device, operator, layout, active_pass
    ):
        checkpoint = factories.open_checkpoint(device=device, user=operator, zone=layout.hall)
        assert _qr(checkpoint, active_pass).result == EntryResult.ALLOWED

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"),
            lambda t: "not-a-token",
            lambda t: t.split(".")[0] + ".." + t.split(".")[2],
            lambda t: "",
        ],
    )
    def test_tampered_or_malformed_tokens_are_invalid(self, checkpoint, active_pass, mutate):
        outcome = verify_qr(
            checkpoint=checkpoint, raw_token=mutate(factories.token_for(active_pass))
        )
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.INVALID_CREDENTIAL
        assert outcome.assessment is None

    @override_settings(QR_SUPPORTED_PAYLOAD_VERSIONS=[2])
    def test_unsupported_payload_version(self, checkpoint, active_pass):
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.UNSUPPORTED
        assert outcome.assessment is None

    def test_wrong_event_pass_is_denied_and_never_resolves_for_decision(
        self, event, other_event, device, operator, key, staff
    ):
        other_layout = factories.VenueLayout(other_event, code="OV")
        other_setup = factories.AccreditationSetup(other_event, other_layout, suffix="O")
        other_person = factories.make_person("Other Event Person")
        other_registration = factories.make_registration(event=other_event, person=other_person)
        factories.assign(registration=other_registration, setup=other_setup, actor=staff)
        other_pass = factories.issue_active_pass(registration=other_registration, actor=staff)
        checkpoint = factories.open_checkpoint(
            device=device, user=operator, zone=device.scopes.get().permitted_zones.first()
        )
        outcome = _qr(checkpoint, other_pass)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.WRONG_EVENT
        assert not outcome.assessment.may_be_overridden

    def test_revoked_pass_is_denied(self, checkpoint, active_pass, staff):
        token = factories.token_for(active_pass)
        revoke_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
        outcome = verify_qr(checkpoint=checkpoint, raw_token=token)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.PASS_REVOKED
        assert not outcome.assessment.may_be_overridden

    def test_suspended_pass_is_denied(self, checkpoint, active_pass, staff):
        token = factories.token_for(active_pass)
        suspend_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.SECURITY_CONCERN,
        )
        outcome = verify_qr(checkpoint=checkpoint, raw_token=token)
        assert outcome.reason_code == EntryReasonCode.PASS_SUSPENDED

    def test_replaced_credential_version_is_stale(self, checkpoint, active_pass, staff):
        token = factories.token_for(active_pass)
        replace_pass(
            credential=active_pass,
            actor=staff,
            operation_id=new_operation_id(),
            expected_jti=active_pass.jti,
            expected_lock_version=active_pass.version,
            reason_code=PassReasonCode.LOST_OR_COMPROMISED,
        )
        outcome = verify_qr(checkpoint=checkpoint, raw_token=token)
        assert outcome.result == EntryResult.STALE
        assert outcome.reason_code == EntryReasonCode.PASS_REPLACED

    def test_expired_pass_is_denied(self, checkpoint, active_pass):
        later = active_pass.valid_until + timedelta(hours=1)
        outcome = _qr(checkpoint, active_pass, now=later)
        assert outcome.reason_code == EntryReasonCode.PASS_EXPIRED

    def test_not_yet_valid_pass_is_denied(self, checkpoint, active_pass):
        earlier = active_pass.valid_from - timedelta(hours=1)
        outcome = _qr(checkpoint, active_pass, now=earlier)
        assert outcome.reason_code == EntryReasonCode.PASS_NOT_YET_VALID

    def test_withdrawn_registration_is_denied(self, checkpoint, active_pass, registration):
        # The QR the participant obtained BEFORE withdrawing: since Phase 3
        # Prompt 8 (P8-06) no usable token is issued afterwards, but one held
        # from earlier must still be denied at the gate.
        token = factories.token_for(active_pass)
        registration.public_status = RegistrationPublicStatus.WITHDRAWN
        registration.withdrawn_at = timezone.now()
        registration.save()
        outcome = verify_qr(checkpoint=checkpoint, raw_token=token)
        assert outcome.reason_code == EntryReasonCode.REGISTRATION_NOT_APPROVED
        assert not outcome.assessment.may_be_overridden

    def test_assignment_changed_since_issue_is_stale(
        self, checkpoint, active_pass, registration, setup, staff
    ):
        current = BadgeTypeAssignment.objects.get(
            registration=registration, status=AssignmentStatus.CURRENT
        )
        current.status = AssignmentStatus.SUPERSEDED
        current.save()
        BadgeTypeAssignment.objects.create(
            registration=registration,
            badge_type=setup.badge_type,
            event_edition=registration.event_edition,
            status=AssignmentStatus.CURRENT,
            effective_from=timezone.now() - timedelta(minutes=1),
            created_by=staff,
            supersedes=current,
        )
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.STALE
        assert outcome.reason_code == EntryReasonCode.ASSIGNMENT_CHANGED

    def test_zone_without_allow_rule_is_wrong_zone(
        self, event, layout, device_admin, operator, active_pass
    ):
        device, _ = factories.enroll_device(
            event=event, layout=layout, admin=device_admin, zones=[layout.main, layout.vip]
        )
        checkpoint = factories.open_checkpoint(device=device, user=operator, zone=layout.vip)
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.WRONG_ZONE

    def test_deny_rule_closes_a_gate_inside_an_allowed_zone(
        self, event, checkpoint, active_pass, setup, layout
    ):
        AccessRule.objects.create(
            event_edition=event,
            access_profile=setup.access_profile,
            code="CLOSE_GA",
            name="Gate A closed for general access",
            zone=layout.main,
            gate=layout.gate_a,
            effect=AccessRuleEffect.DENY,
        )
        assert _qr(checkpoint, active_pass).reason_code == EntryReasonCode.ACCESS_RULE_DENY

    def test_rule_outside_its_window_does_not_allow(self, checkpoint, active_pass, setup):
        AccessRule.objects.filter(pk=setup.main_rule.pk).update(
            valid_from=timezone.now() + timedelta(hours=1)
        )
        assert _qr(checkpoint, active_pass).reason_code == EntryReasonCode.WRONG_ZONE

    def test_future_direct_rule_assignment_does_not_allow(
        self, checkpoint, active_pass, registration, setup, layout, staff
    ):
        setup.main_rule.is_active = False
        setup.main_rule.save(update_fields=["is_active"])
        direct_rule = AccessRule.objects.create(
            event_edition=registration.event_edition,
            code="FUTURE_DIRECT_MAIN",
            name="Future direct main access",
            zone=layout.main,
        )
        AccessRuleAssignment.objects.create(
            registration=registration,
            access_rule=direct_rule,
            event_edition=registration.event_edition,
            status=AssignmentStatus.CURRENT,
            effective_from=timezone.now() + timedelta(hours=1),
            created_by=staff,
        )

        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.WRONG_ZONE

    def test_access_profile_window_is_enforced(self, checkpoint, active_pass, setup):
        setup.access_profile.valid_until = timezone.now() - timedelta(minutes=1)
        setup.access_profile.valid_from = timezone.now() - timedelta(days=1)
        setup.access_profile.save()
        assert _qr(checkpoint, active_pass).reason_code == EntryReasonCode.OUTSIDE_TIME_WINDOW

    def test_privileges_of_another_context_are_never_merged(
        self, event, layout, device_admin, operator, person, staff, key, setup
    ):
        """The same person holds a VIP context; a general-context pass at the
        VIP checkpoint is still denied (BR-ENT-001)."""
        vip_setup = factories.AccreditationSetup(event, layout, suffix="V")
        AccessRule.objects.create(
            event_edition=event,
            access_profile=vip_setup.access_profile,
            code="VIP_RULE",
            name="VIP",
            zone=layout.vip,
        )
        general = factories.make_registration(event=event, person=person)
        factories.assign(registration=general, setup=setup, actor=staff)
        vip = factories.make_registration(event=event, person=person)
        factories.assign(registration=vip, setup=vip_setup, actor=staff)
        general_pass = factories.issue_active_pass(registration=general, actor=staff)
        vip_pass = factories.issue_active_pass(registration=vip, actor=staff)
        device, _ = factories.enroll_device(
            event=event, layout=layout, admin=device_admin, zones=[layout.main, layout.vip]
        )
        checkpoint = factories.open_checkpoint(device=device, user=operator, zone=layout.vip)
        assert _qr(checkpoint, general_pass).reason_code == EntryReasonCode.WRONG_ZONE
        assert _qr(checkpoint, vip_pass).result == EntryResult.ALLOWED

    def test_technical_failure_is_an_explicit_result(self, checkpoint, active_pass, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("synthetic database outage")

        monkeypatch.setattr("apps.badges.credentials.verify.verify_pass_token", boom)
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.TECHNICAL_ERROR
        assert outcome.assessment is None

    def test_method_not_in_device_scope_is_refused(
        self, event, layout, device_admin, operator, active_pass
    ):
        device, _ = factories.enroll_device(
            event=event, layout=layout, admin=device_admin, methods=["REFERENCE"]
        )
        checkpoint = factories.open_checkpoint(device=device, user=operator, zone=layout.main)
        with pytest.raises(EntryPermissionError):
            _qr(checkpoint, active_pass)
        assert AuditEvent.objects.filter(action_code="ENT_LOOKUP_DENIED", reason_code="QR").exists()

    def test_every_verification_is_audited_without_the_token(self, checkpoint, active_pass):
        token = factories.token_for(active_pass)
        verify_qr(checkpoint=checkpoint, raw_token=token)
        verify_qr(checkpoint=checkpoint, raw_token=UNTRUSTED_INPUT)
        rows = AuditEvent.objects.filter(action_code="ENT_VERIFICATION_PERFORMED")
        assert rows.count() == 2
        for row in rows:
            assert token not in str(row.after_summary)
            assert row.after_summary["gate"] == checkpoint.gate.code


class TestReentry:
    def _admit(self, checkpoint, credential):
        from apps.entry.services.decisions import pending_from_assessment, record_entry_decision

        outcome = _qr(checkpoint, credential)
        pending = pending_from_assessment(
            assessment=outcome.assessment, checkpoint=checkpoint, method="QR"
        )
        return record_entry_decision(checkpoint=checkpoint, pending=pending, decision="ADMIT")

    def test_reentry_is_an_advisory_not_a_denial(self, checkpoint, active_pass):
        self._admit(checkpoint, active_pass)
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.ALLOWED_WITH_ADVISORY
        assert EntryReasonCode.PRIOR_ENTRY in outcome.assessment.advisory_codes
        assert EntryReasonCode.RECENT_REENTRY in outcome.assessment.advisory_codes
        assert outcome.assessment.prior_entry is not None

    @override_settings(ENTRY_RECENT_REENTRY_SECONDS=0)
    def test_old_prior_entry_is_not_flagged_recent(self, checkpoint, active_pass):
        self._admit(checkpoint, active_pass)
        advisories = _qr(checkpoint, active_pass).assessment.advisory_codes
        assert EntryReasonCode.PRIOR_ENTRY in advisories
        assert EntryReasonCode.RECENT_REENTRY not in advisories

    def test_strict_single_entry_denies_only_when_configured(self, checkpoint, active_pass, setup):
        setup.access_profile.reentry_policy = ReentryPolicy.SINGLE_ENTRY
        setup.access_profile.save()
        self._admit(checkpoint, active_pass)
        outcome = _qr(checkpoint, active_pass)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.ALREADY_ADMITTED


class TestIdentityLookup:
    def test_nin_lookup_matches_through_the_blind_index(
        self, checkpoint, person, registration, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.result == EntryResult.ALLOWED
        assert outcome.assessment.registration == registration
        assert outcome.identity_verified is True

    def test_nin_lookup_tolerates_spacing(self, checkpoint, person, registration, active_pass):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        spaced = " ".join(SYNTHETIC_NIN[i : i + 6] for i in range(0, 18, 6))
        assert lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=spaced).resolved

    def test_declared_identifier_requires_manual_review(
        self, checkpoint, person, registration, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN, verified=False
        )
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.result == EntryResult.MANUAL_REVIEW
        assert outcome.reason_code == EntryReasonCode.IDENTITY_UNVERIFIED

    def test_passport_requires_the_matching_issuing_country(
        self, checkpoint, person, registration, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="PASSPORT", country="FR", value=SYNTHETIC_PASSPORT
        )
        wrong = lookup_identity(
            checkpoint=checkpoint,
            method="PASSPORT",
            raw_value=SYNTHETIC_PASSPORT,
            country_code="TN",
        )
        assert wrong.reason_code == EntryReasonCode.NO_MATCH
        missing = lookup_identity(
            checkpoint=checkpoint, method="PASSPORT", raw_value=SYNTHETIC_PASSPORT
        )
        assert missing.reason_code == EntryReasonCode.NO_MATCH
        right = lookup_identity(
            checkpoint=checkpoint,
            method="PASSPORT",
            raw_value=SYNTHETIC_PASSPORT,
            country_code="fr",
        )
        assert right.result == EntryResult.ALLOWED

    def test_unknown_identity_is_no_match(self, checkpoint):
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.result == EntryResult.DENIED
        assert outcome.reason_code == EntryReasonCode.NO_MATCH

    def test_lookup_is_bounded_to_the_checkpoint_event(
        self, checkpoint, other_event, staff, person
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        factories.make_registration(event=other_event, person=person)
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.reason_code == EntryReasonCode.NO_MATCH

    def test_not_approved_context_is_never_surfaced(self, checkpoint, event, person):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        factories.make_registration(
            event=event, person=person, public_status=RegistrationPublicStatus.UNDER_REVIEW
        )
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.reason_code == EntryReasonCode.NO_MATCH

    def test_multiple_contexts_require_explicit_selection(
        self, checkpoint, event, person, registration, setup, staff, active_pass
    ):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        second = factories.make_registration(event=event, person=person)
        factories.assign(registration=second, setup=setup, actor=staff)
        outcome = lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert outcome.assessment is None
        assert {c.pk for c in outcome.candidates} == {registration.pk, second.pk}
        assert outcome.extra["verified_registration_ids"] == {str(registration.pk), str(second.pk)}
        chosen = assess_selected_candidate(
            checkpoint=checkpoint, method="NIN", registration_id=second.pk, identity_verified=True
        )
        assert chosen.assessment.registration == second
        # The second context has no pass of its own; it is never lent the first's.
        assert chosen.reason_code == EntryReasonCode.NO_ACTIVE_PASS

    def test_operator_without_identity_permission_is_refused(
        self, event, layout, device, person, active_pass
    ):
        from apps.entry.tests.factories import grant, make_user

        limited = make_user("limited@example.test")
        from django.contrib.auth.models import Group, Permission

        group, _ = Group.objects.get_or_create(name="QR only (synthetic)")
        group.permissions.add(
            Permission.objects.get(content_type__app_label="entry", codename="verify_entry")
        )
        grant(limited, "QR only (synthetic)", event=event, gate=layout.gate_a)
        checkpoint = factories.open_checkpoint(device=device, user=limited, zone=layout.main)
        with pytest.raises(EntryPermissionError):
            lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert _qr(checkpoint, active_pass).result == EntryResult.ALLOWED

    def test_lookup_audit_carries_counts_only(self, checkpoint, person, registration, active_pass):
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        row = AuditEvent.objects.get(action_code="ENT_IDENTITY_LOOKUP")
        assert row.after_summary["matches"] == 1
        assert SYNTHETIC_NIN not in str(row.after_summary)
        assert SYNTHETIC_NIN not in row.reason_code

    def test_no_external_identity_service_is_called(
        self, checkpoint, person, registration, active_pass, monkeypatch
    ):
        import apps.people.services as people_services

        calls = []
        for name in dir(people_services):
            if "verif" in name.lower() and callable(getattr(people_services, name)):
                monkeypatch.setattr(
                    people_services,
                    name,
                    lambda *a, _n=name, **k: calls.append(_n),
                )
        factories.add_identifier(
            person=person, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN
        )
        lookup_identity(checkpoint=checkpoint, method="NIN", raw_value=SYNTHETIC_NIN)
        assert calls == []


class TestReferenceAndManualLookup:
    def test_registration_reference_lookup(self, checkpoint, registration, active_pass):
        outcome = lookup_reference(
            checkpoint=checkpoint, raw_reference=f"  {registration.public_reference.lower()} "
        )
        assert outcome.assessment.registration == registration

    def test_fallback_reference_lookup(self, checkpoint, registration, active_pass):
        from apps.badges.references import format_fallback_reference

        display = format_fallback_reference(active_pass.series.fallback_reference)
        outcome = lookup_reference(checkpoint=checkpoint, raw_reference=display)
        assert outcome.assessment.registration == registration

    def test_unknown_reference_is_no_match(self, checkpoint):
        outcome = lookup_reference(checkpoint=checkpoint, raw_reference="ENT-NOPE0000")
        assert outcome.reason_code == EntryReasonCode.NO_MATCH

    def test_manual_search_is_supervisor_only(self, checkpoint, registration):
        with pytest.raises(EntryPermissionError):
            manual_search(checkpoint=checkpoint, raw_query="Synthetic")

    def test_manual_search_returns_candidates_only(self, supervisor_checkpoint, registration):
        outcome = manual_search(checkpoint=supervisor_checkpoint, raw_query="synthetic part")
        assert outcome.assessment is None
        assert [c.pk for c in outcome.candidates] == [registration.pk]

    def test_manual_search_minimum_length(self, supervisor_checkpoint, registration):
        outcome = manual_search(checkpoint=supervisor_checkpoint, raw_query="sy")
        assert outcome.reason_code == EntryReasonCode.NO_MATCH
        assert outcome.candidates == ()

    @override_settings(ENTRY_MANUAL_SEARCH_MAX_RESULTS=2)
    def test_manual_search_is_capped_and_audited_without_the_query(
        self, supervisor_checkpoint, event
    ):
        for index in range(4):
            factories.make_registration(
                event=event, person=factories.make_person(f"Capped Person {index}")
            )
        outcome = manual_search(checkpoint=supervisor_checkpoint, raw_query="Capped Person")
        assert len(outcome.candidates) == 2
        assert outcome.truncated is True
        row = AuditEvent.objects.get(action_code="ENT_MANUAL_SEARCH")
        assert row.after_summary["matches"] == 2
        assert row.after_summary["truncated"] is True
        assert "Capped" not in str(row.after_summary)
