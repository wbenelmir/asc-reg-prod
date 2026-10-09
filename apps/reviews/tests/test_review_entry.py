"""Verified registrations enter participation review (`apps.reviews.intake`).

Covers the identity success paths (Ministry simulation, manual document,
foreign passport, Algerian staff exception), the exclusions (unverified,
draft, correction, final decision, withdrawn, duplicate review, special
case), idempotent retries, the backfill (dry run, apply, idempotency) and the
preservation of pre-existing registrations. Synthetic data only; the
development simulation answers NINs starting with 99 and no real provider is
called.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command

from apps.audit import action_codes
from apps.audit.models import AuditEvent
from apps.communications.models import CommunicationMessage
from apps.people.models import IdentityStatus, IdentityVerification, VerificationSource
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    SIM_MATCH_NIN,
    SIM_NAME_MISMATCH_NIN,
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
    upload_national_id_card,
)
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSubmission,
)
from apps.reviews import intake
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME
from apps.reviews.models import (
    RegistrationDecision,
    ReviewCase,
    ReviewCaseStatus,
    ReviewCaseType,
    ReviewQueueCode,
)

pytestmark = pytest.mark.django_db

# `idv_event` and `legal_versions` come from the identity tests' fixtures,
# re-exported by this package's conftest.


@pytest.fixture(autouse=True)
def _isolated_private_storage_root(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "entry-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


@pytest.fixture
def manager(idv_event):
    return make_staff(
        "entry-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event
    )


def _cases(registration):
    return list(ReviewCase.objects.filter(registration=registration))


def _assert_queued(registration):
    registration.refresh_from_db()
    cases = _cases(registration)
    assert len(cases) == 1
    case = cases[0]
    assert case.case_type == ReviewCaseType.STANDARD
    assert case.queue_code == ReviewQueueCode.GENERAL
    assert case.status == ReviewCaseStatus.QUEUED
    assert case.event_edition_id == registration.event_edition_id
    assert case.organization_id == registration.source_organization_id
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    # Identity never approves participation, invites or notifies (IDV-10).
    assert not RegistrationDecision.objects.filter(registration=registration).exists()
    return case


def _not_found_case(event, versions):
    registration = submit_case(event, versions, nin=UNKNOWN_NIN)
    process_all()
    upload_national_id_card(registration)
    return case_for(registration)


def _card(case):
    return case.registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")


# ---------------------------------------------------------------------------
# Identity success paths
# ---------------------------------------------------------------------------


def test_ministry_verification_enqueues_the_registration(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    assert _cases(registration) == []
    version_before = Registration.objects.get(pk=registration.pk).version
    messages_before = CommunicationMessage.objects.count()
    process_all()
    assert case_for(registration).status == IdentityStatus.API_VERIFIED
    case = _assert_queued(registration)
    registration.refresh_from_db()
    assert registration.version == version_before + 1
    assert registration.internal_status == RegistrationInternalStatus.PENDING_ASSIGNMENT
    event = AuditEvent.objects.get(action_code=action_codes.REVIEW_CASE_ENQUEUED)
    assert event.target_uuid == case.pk
    assert event.reason_code == intake.EntryTrigger.IDENTITY_API_VERIFIED
    assert event.actor_type == "SYSTEM"
    # Only statuses and a flag: no name, reference or identifier in the summary.
    assert set(event.after_summary) == {"public_status", "internal_status", "case_reused"}
    assert CommunicationMessage.objects.count() == messages_before


def test_manual_document_verification_enqueues(idv_event, legal_versions, reviewer) -> None:
    case = _not_found_case(idv_event, legal_versions)
    assert _cases(case.registration) == []
    review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=_card(case).pk,
        reason_code="MINISTRY_RECORD_UNAVAILABLE",
    )
    _assert_queued(case.registration)
    event = AuditEvent.objects.get(action_code=action_codes.REVIEW_CASE_ENQUEUED)
    assert event.actor_type == "OPERATIONAL_USER"
    assert event.actor_user_id == reviewer.pk
    assert event.reason_code == intake.EntryTrigger.IDENTITY_MANUALLY_VERIFIED


def test_foreign_passport_manual_verification_enqueues(idv_event, legal_versions, reviewer) -> None:
    registration = submit_case(idv_event, legal_versions, nationality="FR")
    case = case_for(registration)
    page = registration.documents.get(document_type="PASSPORT_IDENTITY_PAGE", status="ACTIVE")
    review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=page.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert case_for(registration).verification_source == VerificationSource.MANUAL_PASSPORT
    _assert_queued(registration)


def test_the_staff_exception_enqueues(idv_event, legal_versions, manager) -> None:
    case = _not_found_case(idv_event, legal_versions)
    review.verify_identity_manually(
        case.pk,
        actor=manager,
        expected_version=case.version,
        evidence_document_id=_card(case).pk,
        reason_code="NO_USABLE_MINISTRY_RECORD",
        note="Ministry record unavailable; card verified in person.",
        exception=True,
    )
    assert case_for(case.registration).verification_source == VerificationSource.STAFF_EXCEPTION
    _assert_queued(case.registration)


def test_a_failed_enqueue_rolls_the_verification_back(idv_event, legal_versions, reviewer) -> None:
    """Identity success and queue entry commit together: no unhandled gap."""
    case = _not_found_case(idv_event, legal_versions)
    with (
        mock.patch("apps.reviews.services.open_review_case", side_effect=RuntimeError("synthetic")),
        pytest.raises(RuntimeError),
    ):
        review.verify_identity_manually(
            case.pk,
            actor=reviewer,
            expected_version=case.version,
            evidence_document_id=_card(case).pk,
            reason_code="MINISTRY_RECORD_UNAVAILABLE",
        )
    assert case_for(case.registration).status == IdentityStatus.MANUAL_REVIEW
    assert _cases(case.registration) == []


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_unverified_registrations_get_no_case(idv_event, legal_versions) -> None:
    from apps.people.models import IdentityVerificationJob
    from apps.people.services.identity_verification import process_identity_job

    pending = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    mismatch = submit_case(idv_event, legal_versions, nin=SIM_NAME_MISMATCH_NIN)
    # Only the mismatch is processed (manual review); the other stays pending.
    process_identity_job(IdentityVerificationJob.objects.get(verification=case_for(mismatch)).pk)
    assert case_for(mismatch).status == IdentityStatus.MANUAL_REVIEW
    assert case_for(pending).status == IdentityStatus.PENDING
    for registration in (pending, mismatch):
        assert _cases(registration) == []
        registration.refresh_from_db()
        assert registration.public_status == RegistrationPublicStatus.SUBMITTED
        result = intake.enqueue_for_participation_review(
            registration.pk, trigger=intake.EntryTrigger.BACKFILL
        )
        assert result.outcome == intake.EntryOutcome.EXCLUDED
        assert result.reason == "IDENTITY_NOT_VERIFIED"
        assert _cases(registration) == []


def test_a_draft_is_never_enqueued(idv_event) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.services import get_or_create_active_draft

    person = resolve_or_create_participant_for_email("entry-draft@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=idv_event)
    result = intake.enqueue_for_participation_review(draft.pk, trigger=intake.EntryTrigger.BACKFILL)
    assert result.reason == intake.EntryExclusion.NOT_SUBMITTED
    assert _cases(draft) == []


def _passport_verified(event, versions, passport_number: str):
    """A foreign participant whose passport page was verified manually."""
    registration = submit_case(event, versions, nationality="FR", passport_number=passport_number)
    staff = make_staff(
        f"legacy-{passport_number.lower()}@example.test",
        REGISTRATION_REVIEWERS_GROUP_NAME,
        event=event,
    )
    case = case_for(registration)
    page = registration.documents.get(document_type="PASSPORT_IDENTITY_PAGE", status="ACTIVE")
    review.verify_identity_manually(
        case.pk,
        actor=staff,
        expected_version=case.version,
        evidence_document_id=page.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    registration.refresh_from_db()
    return registration


def _verified_without_case(event, versions, *, passport_number: str | None = None):
    """A registration verified before the identity -> review link existed:
    by the Ministry simulation, or manually from a passport page."""
    with mock.patch("apps.reviews.intake.enqueue_for_participation_review", return_value=None):
        if passport_number is None:
            registration = submit_case(event, versions, nin=SIM_MATCH_NIN)
            process_all()
        else:
            registration = _passport_verified(event, versions, passport_number)
    registration.refresh_from_db()
    assert case_for(registration).status in IdentityStatus.verified_statuses()
    assert _cases(registration) == []
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    return registration


@pytest.mark.parametrize(
    ("public_status", "internal_status", "reason"),
    [
        (RegistrationPublicStatus.WITHDRAWN, None, intake.EntryExclusion.WITHDRAWN),
        (RegistrationPublicStatus.APPROVED, None, intake.EntryExclusion.FINAL_DECISION),
        (RegistrationPublicStatus.NOT_APPROVED, None, intake.EntryExclusion.FINAL_DECISION),
        (
            RegistrationPublicStatus.ADDITIONAL_INFORMATION_REQUIRED,
            None,
            intake.EntryExclusion.CORRECTION_OR_INFORMATION_REQUESTED,
        ),
        (
            None,
            RegistrationInternalStatus.DUPLICATE_REVIEW,
            intake.EntryExclusion.DUPLICATE_REVIEW,
        ),
        (
            None,
            RegistrationInternalStatus.AWAITING_APPLICANT,
            intake.EntryExclusion.CORRECTION_OR_INFORMATION_REQUESTED,
        ),
        (None, RegistrationInternalStatus.CLOSED, intake.EntryExclusion.UNEXPECTED_INTERNAL_STATUS),
    ],
)
def test_ineligible_states_are_excluded_and_unchanged(
    idv_event, legal_versions, public_status, internal_status, reason
) -> None:
    registration = _verified_without_case(idv_event, legal_versions)
    fields = {}
    if public_status:
        fields["public_status"] = public_status
    if internal_status:
        fields["internal_status"] = internal_status
    Registration.objects.filter(pk=registration.pk).update(**fields)
    before = Registration.objects.values().get(pk=registration.pk)
    result = intake.enqueue_for_participation_review(
        registration.pk, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.outcome == intake.EntryOutcome.EXCLUDED
    assert result.reason == reason
    assert Registration.objects.values().get(pk=registration.pk) == before
    assert _cases(registration) == []


def test_a_current_decision_or_a_special_case_blocks_entry(idv_event, legal_versions, manager):
    from apps.reviews.services import open_review_case

    special = _verified_without_case(idv_event, legal_versions)
    open_review_case(
        registration=special, case_type=ReviewCaseType.RESTRICTED, queue_code="RESTRICTED"
    )
    result = intake.enqueue_for_participation_review(
        special.pk, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.reason == intake.EntryExclusion.SPECIAL_REVIEW_OPEN
    assert ReviewCase.objects.filter(registration=special).count() == 1


def test_identity_rejection_or_correction_blocks_entry(idv_event, legal_versions, reviewer):
    case = _not_found_case(idv_event, legal_versions)
    review.return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
    result = intake.enqueue_for_participation_review(
        case.registration_id, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.reason == intake.EntryExclusion.CORRECTION_OR_INFORMATION_REQUESTED
    assert _cases(case.registration) == []


# ---------------------------------------------------------------------------
# Retries, reuse, reopening
# ---------------------------------------------------------------------------


def test_retries_reuse_the_open_case(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    process_all()
    case = _assert_queued(registration)
    version = Registration.objects.get(pk=registration.pk).version
    audit_count = AuditEvent.objects.count()
    for _ in range(3):
        result = intake.enqueue_for_participation_review(
            registration.pk, trigger=intake.EntryTrigger.IDENTITY_API_VERIFIED
        )
        assert result.outcome == intake.EntryOutcome.EXISTING_CASE
        assert result.case_id == case.pk
        assert not result.transitioned
    assert len(_cases(registration)) == 1
    assert Registration.objects.get(pk=registration.pk).version == version
    assert AuditEvent.objects.count() == audit_count
    # Running the worker again after success changes nothing either.
    process_all()
    assert len(_cases(registration)) == 1


def test_an_existing_open_case_is_reused_for_a_verification_pending_registration(
    idv_event, legal_versions
) -> None:
    from apps.reviews.services import change_review_case_status, open_review_case

    registration = _verified_without_case(idv_event, legal_versions)
    existing = open_review_case(
        registration=registration, case_type=ReviewCaseType.STANDARD, queue_code="GENERAL"
    )
    staff = make_staff("entry-start@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)
    existing = change_review_case_status(
        review_case=existing,
        new_status=ReviewCaseStatus.IN_PROGRESS,
        expected_version=existing.version,
        actor=staff,
    )
    Registration.objects.filter(pk=registration.pk).update(
        public_status=RegistrationPublicStatus.SUBMITTED,
        internal_status=RegistrationInternalStatus.VERIFICATION_PENDING,
    )
    result = intake.enqueue_for_participation_review(
        registration.pk, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.outcome == intake.EntryOutcome.EXISTING_CASE
    assert result.case_id == existing.pk
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.UNDER_REVIEW
    assert registration.internal_status == RegistrationInternalStatus.REVIEW_IN_PROGRESS
    assert ReviewCase.objects.filter(registration=registration).count() == 1


def test_reopening_after_a_decision_does_not_duplicate_cases(
    idv_event, legal_versions, manager
) -> None:
    from apps.reviews.services import record_not_approved_decision, reopen_registration

    registration = submit_case(idv_event, legal_versions, nin=SIM_MATCH_NIN)
    process_all()
    _assert_queued(registration)
    registration.refresh_from_db()
    record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="NOT_ELIGIBLE",
        decided_by=manager,
    )
    registration.refresh_from_db()
    new_case = reopen_registration(
        registration=registration,
        expected_version=registration.version,
        reason="Reviewed again",
        actor=manager,
    )
    result = intake.enqueue_for_participation_review(
        registration.pk, trigger=intake.EntryTrigger.BACKFILL
    )
    assert result.outcome == intake.EntryOutcome.EXISTING_CASE
    assert result.case_id == new_case.pk
    assert (
        ReviewCase.objects.filter(
            registration=registration, status__in=ReviewCaseStatus.open_statuses()
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# Backfill and preservation of existing registrations
# ---------------------------------------------------------------------------


def _snapshot():
    """Everything the backfill must preserve, per registration."""
    from apps.documents.models import Document

    rows = {}
    for registration in Registration.objects.select_related("profile").order_by("pk"):
        identity = IdentityVerification.objects.filter(registration=registration).first()
        rows[registration.pk] = {
            "public_reference": registration.public_reference,
            "person_id": registration.person_id,
            "event_edition_id": registration.event_edition_id,
            "submitted_at": registration.submitted_at,
            "names": (
                registration.profile.submitted_given_names,
                registration.profile.submitted_family_name,
                registration.profile.date_of_birth,
            )
            if hasattr(registration, "profile")
            else None,
            "submissions": sorted(
                RegistrationSubmission.objects.filter(registration=registration).values_list(
                    "pk", "snapshot_hash"
                )
            ),
            "documents": sorted(
                Document.objects.filter(registration=registration).values_list(
                    "pk", "stored_object__sha256", "status"
                )
            ),
            "identity": (
                (identity.status, identity.verification_source, identity.version)
                if identity
                else None
            ),
            "decisions": sorted(
                RegistrationDecision.objects.filter(registration=registration).values_list(
                    "pk", "outcome", "is_current"
                )
            ),
            "public_status": registration.public_status,
            "internal_status": registration.internal_status,
            "version": registration.version,
        }
    return rows


def test_backfill_dry_run_apply_and_preservation(
    idv_event, legal_versions, reviewer, manager
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.services import get_or_create_active_draft
    from apps.reviews.services import record_not_approved_decision, withdraw_registration

    legacy_api = _verified_without_case(idv_event, legal_versions)
    legacy_api_second = _verified_without_case(
        idv_event, legal_versions, passport_number="P1000001"
    )
    already_queued = _passport_verified(idv_event, legal_versions, "P1000002")
    assert len(_cases(already_queued)) == 1
    correction = _not_found_case(idv_event, legal_versions)
    review.return_identity_for_correction(
        correction.pk, actor=reviewer, expected_version=correction.version, items=["NIN_NUMBER"]
    )
    pending_manual = _not_found_case(idv_event, legal_versions)
    draft = get_or_create_active_draft(
        person=resolve_or_create_participant_for_email("entry-draft2@example.test"),
        event_edition=idv_event,
    )
    decided = _verified_without_case(idv_event, legal_versions, passport_number="P1000003")
    record_not_approved_decision(
        registration=decided,
        expected_version=decided.version,
        internal_reason_code="NOT_ELIGIBLE",
        decided_by=manager,
    )
    withdrawn = _verified_without_case(idv_event, legal_versions, passport_number="P1000004")
    withdraw_registration(
        registration=withdrawn, person=withdrawn.person, expected_version=withdrawn.version
    )
    other_event = make_event("ENTRYOTHER")
    out_of_scope = _verified_without_case(other_event, legal_versions, passport_number="P1000005")

    before = _snapshot()
    count_before = Registration.objects.count()
    cases_before = ReviewCase.objects.count()
    messages_before = CommunicationMessage.objects.count()
    outbox_before = len(mail.outbox)

    dry = intake.run_backfill(event_edition=idv_event, batch_size=2)
    assert dry.examined == Registration.objects.filter(event_edition=idv_event).count()
    assert dry.eligible_missing_case == 2
    assert dry.existing_case == 1
    assert dry.status_transitions == 2
    assert dry.excluded == {
        "CORRECTION_OR_INFORMATION_REQUESTED": 1,
        "IDENTITY_NOT_VERIFIED": 1,
        "NOT_SUBMITTED": 1,
        "FINAL_DECISION": 1,
        "WITHDRAWN": 1,
    }
    # The dry run writes nothing.
    assert _snapshot() == before
    assert ReviewCase.objects.count() == cases_before

    applied = intake.run_backfill(event_edition=idv_event, apply=True, batch_size=2)
    assert applied.created == 2 and applied.transitioned == 2 and applied.conflicts == 0
    after = _snapshot()
    assert Registration.objects.count() == count_before
    assert set(after) == set(before)
    changed = {pk for pk in before if before[pk] != after[pk]}
    assert changed == {legacy_api.pk, legacy_api_second.pk}
    for pk in changed:
        # Only the documented transition: status and version.
        diff = {key for key in before[pk] if before[pk][key] != after[pk][key]}
        assert diff == {"public_status", "version"}
        assert after[pk]["public_status"] == RegistrationPublicStatus.UNDER_REVIEW
        assert after[pk]["version"] == before[pk]["version"] + 1
    for registration in (legacy_api, legacy_api_second):
        _assert_queued(registration)
    assert ReviewCase.objects.count() == cases_before + 2
    assert _cases(out_of_scope) == []
    for registration in (correction.registration, pending_manual.registration, draft):
        assert _cases(registration) == []

    # Idempotent: a second apply changes nothing.
    again = intake.run_backfill(event_edition=idv_event, apply=True)
    assert again.created == 0 and again.transitioned == 0
    assert again.eligible_missing_case == 0 and again.existing_case == 3
    assert _snapshot() == after
    # No communication and no decision from the backfill.
    assert CommunicationMessage.objects.count() == messages_before
    assert len(mail.outbox) == outbox_before
    assert not RegistrationDecision.objects.filter(
        registration__in=[legacy_api, legacy_api_second]
    ).exists()


def test_backfill_command_prints_counts_only(idv_event, legal_versions, capsys) -> None:
    registration = _verified_without_case(idv_event, legal_versions)
    call_command("backfill_review_queue", "--event", idv_event.code)
    out = capsys.readouterr().out
    assert "dry-run" in out and "eligible_missing_case: 1" in out
    assert registration.public_reference not in out
    assert _cases(registration) == []
    call_command("backfill_review_queue", "--event", idv_event.code, "--apply")
    out = capsys.readouterr().out
    assert "created: 1" in out
    assert registration.public_reference not in out
    _assert_queued(registration)


def test_backfill_command_requires_an_explicit_scope() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("backfill_review_queue")
    with pytest.raises(CommandError):
        call_command("backfill_review_queue", "--event", "NO-SUCH-EVENT")
