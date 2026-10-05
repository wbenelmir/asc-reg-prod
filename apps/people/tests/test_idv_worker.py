"""Post-submission verification: atomic start, durable jobs and the worker (IDV-2).

Uses the development simulation (synthetic bodies through the real contract
code) and test doubles. Nothing here is real-provider evidence. Covers: the
job committed with the submission, broker outage, redispatch, retry bounds
and backoff, duplicate delivery, lease recovery, stale results, identity
changes, the simulation guard, the no-transaction rule, retained facts and
log/audit hygiene.
"""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.people.identity_contract import Comparison, LookupKind
from apps.people.models import (
    IdentifierStatus,
    IdentityJobStatus,
    IdentityReasonCode,
    IdentityRevisionSource,
    IdentityStatus,
    IdentityVerification,
    IdentityVerificationAttempt,
    IdentityVerificationJob,
    VerificationSource,
)
from apps.people.nin_provider import (
    SIMULATED_INVALID_NIN,
    LookupOutcome,
    UnavailableSimulationNinProvider,
)
from apps.people.services import identity_verification as idv
from apps.registrations.models import RegistrationInternalStatus, RegistrationPublicStatus

from .conftest import (
    SIM_ISO_DATE_NIN,
    SIM_LEADING_ZERO_NIN,
    SIM_MATCH_NIN,
    SIM_NAME_MISMATCH_NIN,
    SIM_NO_PRESUME_NIN,
    SIM_OTHER_NIN_RETURNED,
    SIM_PRESUMED_NIN,
    UNKNOWN_NIN,
    case_for,
    process_all,
    submit_case,
)

pytestmark = pytest.mark.django_db


class ExplodingProvider:
    PROVIDER_CODE = "EXPLODING"
    IS_OFFICIAL = False

    def configuration_problems(self):
        return []

    def lookup(self, nin):  # pragma: no cover - must never run
        raise AssertionError("The provider was called.")


class ScriptedProvider:
    """A test double returning a fixed outcome; records calls."""

    PROVIDER_CODE = "SCRIPTED_TEST_DOUBLE"
    IS_OFFICIAL = False

    def __init__(self, outcome: LookupOutcome, *, on_call=None):
        self.outcome = outcome
        self.calls = 0
        self.on_call = on_call

    def configuration_problems(self):
        return []

    def lookup(self, nin):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return self.outcome


def _job(case):
    return IdentityVerificationJob.objects.get(revision=case.current_revision)


# ---------------------------------------------------------------------------
# Start, atomically with the submission
# ---------------------------------------------------------------------------


@override_settings(NIN_PROVIDER_BACKEND=f"{__name__}.ExplodingProvider")
def test_submission_persists_the_case_revision_and_job_without_calling_the_provider(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    case = case_for(registration)
    assert case.status == IdentityStatus.PENDING
    assert case.route == "NIN"
    assert case.current_revision.number == 1
    assert case.current_revision.source == IdentityRevisionSource.SUBMISSION
    job = _job(case)
    assert job.status == IdentityJobStatus.PENDING and job.attempts == 0
    assert not IdentityVerificationAttempt.objects.filter(registration=registration).exists()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED


def test_a_rolled_back_submission_leaves_no_case_and_no_job(idv_event, legal_versions) -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        submit_case(idv_event, legal_versions)
        raise RuntimeError("synthetic failure after the submission")
    assert not IdentityVerification.objects.exists()
    assert not IdentityVerificationJob.objects.exists()


@override_settings(NIN_PROVIDER_BACKEND=f"{__name__}.ExplodingProvider")
def test_a_foreign_participant_goes_to_manual_review_with_no_ministry_job(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions, nationality="FR")
    case = case_for(registration)
    assert case.route == "PASSPORT"
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == IdentityReasonCode.FOREIGN_PASSPORT_REVIEW
    assert not IdentityVerificationJob.objects.filter(verification=case).exists()
    assert process_all() == {}


def test_resubmitting_the_final_step_never_creates_a_second_case(idv_event, legal_versions) -> None:
    from apps.registrations.services import submit_full_registration

    registration = submit_case(idv_event, legal_versions)
    privacy, terms = legal_versions
    submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="again",
        idempotency_key="again",
    )
    assert IdentityVerification.objects.filter(registration=registration).count() == 1
    assert IdentityVerificationJob.objects.count() == 1


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


def test_a_simulated_match_verifies_without_approving_participation(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}
    case = case_for(registration)
    assert case.status == IdentityStatus.API_VERIFIED
    assert case.verification_source == VerificationSource.SIMULATED_API  # never official
    identifier = case.current_revision.identifier
    identifier.refresh_from_db()
    assert identifier.status == IdentifierStatus.VERIFIED
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.is_official_provider is False
    assert attempt.comparison["family_name"] == Comparison.NORMALIZED_MATCH
    assert attempt.official_family_name_latin == "BENTEST"
    assert attempt.official_birth_date == "1990-03-07"
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    assert registration.internal_status == RegistrationInternalStatus.PENDING_ASSIGNMENT
    # IDV-C1 (R-IDV-02): a case- or spacing-only difference takes the
    # official form, recorded as a revision; the originals stay in the
    # immutable submission snapshot (apps/people/tests/test_idv_c1_official_names.py).
    profile = registration.profile
    profile.refresh_from_db()
    assert (profile.submitted_given_names, profile.submitted_family_name) == ("AMINA", "BENTEST")
    initial = registration.submissions.get(submission_kind="INITIAL").snapshot_json["identity"]
    assert (initial["given_names"], initial["family_name"]) == ("Amina", "Bentest")


def test_a_presumed_date_verifies_on_nin_and_names_and_keeps_the_entered_date(
    idv_event, legal_versions
) -> None:
    entered = datetime.date(1985, 6, 15)  # differs from the official 01/01/1985
    registration = submit_case(
        idv_event,
        legal_versions,
        nin=SIM_PRESUMED_NIN,
        given="Karim",
        family="Ouztest",
        birth=entered,
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.API_VERIFIED
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.comparison["birth_date"] == Comparison.SKIPPED_PRESUMED
    assert attempt.presume_flag == "true"
    assert attempt.policy_basis == "OWNER_RULE_PRESUMED_BIRTH_DATE_2026_10_01"
    assert attempt.official_birth_date == ""  # an unused official date is not retained
    registration.profile.refresh_from_db()
    assert registration.profile.date_of_birth == entered


def test_a_name_mismatch_goes_to_manual_review_without_overwriting(
    idv_event, legal_versions
) -> None:
    registration = submit_case(
        idv_event,
        legal_versions,
        nin=SIM_NAME_MISMATCH_NIN,
        given="Samir",
        family="Bentesty",
        birth=datetime.date(1992, 6, 15),
    )
    process_all()
    case = case_for(registration)
    assert (case.status, case.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.DATA_MISMATCH,
    )
    assert case.verification_source == ""
    registration.profile.refresh_from_db()
    assert registration.profile.submitted_family_name == "Bentesty"
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.comparison["family_name"] == Comparison.MISMATCH
    assert case.current_revision.identifier.status == IdentifierStatus.DECLARED


@pytest.mark.parametrize(
    ("nin", "given", "family", "reason"),
    [
        (UNKNOWN_NIN, "Amina", "Bentest", IdentityReasonCode.NOT_FOUND),
        (
            SIM_OTHER_NIN_RETURNED,
            "Nadia",
            "Othertest",
            IdentityReasonCode.PROVIDER_IDENTITY_MISMATCH,
        ),
        (SIM_ISO_DATE_NIN, "Yacine", "Datetest", IdentityReasonCode.AMBIGUOUS_DATE),
        (SIM_NO_PRESUME_NIN, "Sara", "Flagtest", IdentityReasonCode.PRESUME_FLAG_UNRECOGNIZED),
        (SIMULATED_INVALID_NIN, "Amina", "Bentest", IdentityReasonCode.INVALID_RESPONSE),
    ],
)
def test_unverifiable_outcomes_route_to_manual_review_once(
    idv_event, legal_versions, nin, given, family, reason
) -> None:
    registration = submit_case(
        idv_event,
        legal_versions,
        nin=nin,
        given=given,
        family=family,
        birth=datetime.date(1990, 3, 7),
    )
    process_all()
    case = case_for(registration)
    assert (case.status, case.reason_code) == (IdentityStatus.MANUAL_REVIEW, reason)
    job = _job(case)
    assert job.status == IdentityJobStatus.COMPLETED and job.attempts == 1  # no retry
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    if reason == IdentityReasonCode.PROVIDER_IDENTITY_MISMATCH:
        assert attempt.official_family_name_latin == ""  # another person's data is not kept
    if reason == IdentityReasonCode.AMBIGUOUS_DATE:
        assert attempt.official_birth_date_text == "1990-03-07"
        assert attempt.official_birth_date == ""
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED  # never auto-rejected


def test_a_leading_zero_nin_round_trips(idv_event, legal_versions) -> None:
    registration = submit_case(
        idv_event,
        legal_versions,
        nin=SIM_LEADING_ZERO_NIN,
        given="Lina",
        family="Zerotest",
        birth=datetime.date(1988, 11, 20),
    )
    process_all()
    case = case_for(registration)
    assert case.status == IdentityStatus.API_VERIFIED
    assert case.current_revision.identifier.value_encrypted == SIM_LEADING_ZERO_NIN


# ---------------------------------------------------------------------------
# Retries, bounds and recovery
# ---------------------------------------------------------------------------


def _make_due(job) -> None:
    IdentityVerificationJob.objects.filter(pk=job.pk).update(
        next_attempt_at=timezone.now() - datetime.timedelta(seconds=1)
    )


def test_transient_failures_retry_with_backoff_then_go_to_manual_review(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    provider = UnavailableSimulationNinProvider()
    case = case_for(registration)
    job = _job(case)
    delays = []
    for _attempt in range(idv.max_attempts() - 1):
        before = timezone.now()
        assert idv.process_identity_job(job.pk, provider=provider) == "retry_scheduled"
        job.refresh_from_db()
        assert job.status == IdentityJobStatus.PENDING
        delays.append((job.next_attempt_at - before).total_seconds())
        assert case_for(registration).status == IdentityStatus.PENDING
        assert idv.process_identity_job(job.pk, provider=provider) == "not_due"
        _make_due(job)
    assert delays == pytest.approx(list(idv.retry_delays()), abs=5)
    assert idv.process_identity_job(job.pk, provider=provider) == IdentityStatus.MANUAL_REVIEW
    job.refresh_from_db()
    assert job.status == IdentityJobStatus.EXHAUSTED
    assert job.attempts == idv.max_attempts()
    case = case_for(registration)
    assert case.reason_code == IdentityReasonCode.PROVIDER_UNAVAILABLE
    assert IdentityVerificationAttempt.objects.filter(registration=registration).count() == (
        idv.max_attempts()
    )


def test_a_non_retryable_auth_error_goes_to_manual_review_at_once(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    provider = ScriptedProvider(LookupOutcome(LookupKind.AUTH_ERROR, detail="credentials_rejected"))
    process_all(provider)
    case = case_for(registration)
    assert (case.status, case.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.PROVIDER_AUTH_ERROR,
    )
    assert provider.calls == 1


def test_an_adapter_exception_is_contained_and_retried(idv_event, legal_versions) -> None:
    class Raising(ScriptedProvider):
        def lookup(self, nin):
            raise RuntimeError("synthetic failure 990000000000000010")

    registration = submit_case(idv_event, legal_versions)
    assert process_all(Raising(None)) == {"retry_scheduled": 1}
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.detail_code == "adapter_error"
    assert SIM_MATCH_NIN not in str(attempt.comparison) + attempt.detail_code


def test_duplicate_delivery_is_a_no_op(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    assert idv.process_identity_job(job.pk) == IdentityStatus.API_VERIFIED
    assert idv.process_identity_job(job.pk) == "not_due"
    assert idv.process_identity_job(job.pk) == "not_due"
    assert IdentityVerificationAttempt.objects.filter(registration=registration).count() == 1


def test_a_job_whose_worker_died_is_recovered_after_its_lease(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    IdentityVerificationJob.objects.filter(pk=job.pk).update(
        status=IdentityJobStatus.IN_PROGRESS,
        attempts=1,
        lease_expires_at=timezone.now() + datetime.timedelta(minutes=2),
    )
    assert job.pk not in idv.due_job_ids()  # a live lease is never taken over
    assert idv.process_identity_job(job.pk) == "not_due"
    IdentityVerificationJob.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    assert job.pk in idv.due_job_ids()
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}


def test_a_lost_lease_result_is_recorded_but_never_applied(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))

    def steal_lease():
        IdentityVerificationJob.objects.filter(pk=job.pk).update(lease_token=None)

    provider = ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND), on_call=steal_lease)
    assert idv.process_identity_job(job.pk, provider=provider) == "stale"
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    assert attempt.applied is False
    assert case_for(registration).status == IdentityStatus.PENDING


def test_a_result_for_a_superseded_revision_is_discarded(idv_event, legal_versions) -> None:
    """A staff recheck committed while the first check was in flight: the old
    result must never decide the new revision."""
    registration = submit_case(idv_event, legal_versions)
    case = case_for(registration)
    first_job = _job(case)

    def supersede():
        from apps.registrations.models import RegistrationProfile

        locked = IdentityVerification.objects.get(pk=case.pk)
        revision = idv.create_revision(
            verification=locked,
            identifier=locked.current_revision.identifier,
            profile=RegistrationProfile.objects.get(registration=registration),
            source=IdentityRevisionSource.STAFF_RECHECK,
        )
        locked.current_revision = revision
        locked.version += 1
        locked.save()
        idv.schedule_job(verification=locked, revision=revision)

    provider = ScriptedProvider(
        LookupOutcome(LookupKind.UNAVAILABLE, retryable=True), on_call=supersede
    )
    assert idv.process_identity_job(first_job.pk, provider=provider) == "stale"
    first_job.refresh_from_db()
    assert first_job.status == IdentityJobStatus.DISCARDED_STALE
    attempt = IdentityVerificationAttempt.objects.get(job=first_job)
    assert attempt.applied is False
    case.refresh_from_db()
    assert case.status == IdentityStatus.PENDING
    assert case.current_revision.number == 2
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}  # the new revision's own result


def test_an_identity_changed_elsewhere_goes_to_review_instead_of_being_checked(
    idv_event, legal_versions
) -> None:
    from apps.registrations.models import RegistrationProfile

    registration = submit_case(idv_event, legal_versions)
    RegistrationProfile.objects.filter(registration=registration).update(
        submitted_family_name="Changed"
    )
    provider = ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND))
    assert process_all(provider) == {"stale": 1}
    assert provider.calls == 0
    case = case_for(registration)
    assert (case.status, case.reason_code) == (
        IdentityStatus.MANUAL_REVIEW,
        IdentityReasonCode.IDENTITY_DATA_CHANGED,
    )


def test_a_withdrawn_registration_never_reaches_the_provider(idv_event, legal_versions) -> None:
    from apps.registrations.models import Registration

    registration = submit_case(idv_event, legal_versions)
    Registration.objects.filter(pk=registration.pk).update(
        public_status=RegistrationPublicStatus.WITHDRAWN
    )
    provider = ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND))
    assert process_all(provider) == {"stale": 1}
    assert provider.calls == 0


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@override_settings(IDENTITY_ALLOW_SIMULATED_PROVIDER=False)
def test_a_simulation_is_refused_when_not_allowed(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    provider = ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND))
    process_all(provider)
    assert provider.calls == 0
    case = case_for(registration)
    assert case.reason_code == IdentityReasonCode.PROVIDER_NOT_CONFIGURED
    assert case.status == IdentityStatus.MANUAL_REVIEW


@override_settings(NIN_PROVIDER_BACKEND="apps.people.nin_provider.DisabledNinProvider")
def test_the_disabled_backend_routes_every_case_to_manual_review(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    process_all()
    assert case_for(registration).reason_code == IdentityReasonCode.PROVIDER_NOT_CONFIGURED


def test_the_provider_is_never_called_inside_a_database_transaction(
    idv_event, legal_versions
) -> None:
    observed = []

    def inspect():
        observed.append(
            [getattr(block, "_from_testcase", False) for block in connection.atomic_blocks]
        )

    registration = submit_case(idv_event, legal_versions)
    process_all(ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND), on_call=inspect))
    assert observed and all(all(flags) for flags in observed)
    assert case_for(registration).status == IdentityStatus.MANUAL_REVIEW


def test_the_worker_refuses_to_run_inside_a_transaction(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    with transaction.atomic(), pytest.raises(RuntimeError):
        idv.process_identity_job(job.pk)


@override_settings(IDENTITY_PROVIDER_MAX_CONCURRENCY=1)
def test_busy_provider_slots_defer_the_job_without_counting_an_attempt(
    idv_event, legal_versions
) -> None:
    from django.db.backends.postgresql.base import DatabaseWrapper

    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    holder = DatabaseWrapper(dict(connection.settings_dict), alias="idv-slot-holder")
    try:
        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                [idv.ADVISORY_LOCK_CLASS_IDENTITY_PROVIDER_SLOT, 0],
            )
        provider = ScriptedProvider(LookupOutcome(LookupKind.NOT_FOUND))
        assert idv.process_identity_job(job.pk, provider=provider) == "deferred"
        assert provider.calls == 0
        job.refresh_from_db()
        assert (job.status, job.attempts) == (IdentityJobStatus.PENDING, 0)
        assert job.next_attempt_at > timezone.now()
    finally:
        holder.close()
    _make_due(job)
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}


# ---------------------------------------------------------------------------
# Dispatch and broker outage (ASYNC-01)
# ---------------------------------------------------------------------------


@override_settings(IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT=True)
def test_a_broker_outage_never_fails_a_saved_submission(
    idv_event, legal_versions, django_capture_on_commit_callbacks
) -> None:
    from apps.people import tasks

    with mock.patch.object(
        tasks.process_identity_verification_job_task,
        "delay",
        side_effect=ConnectionError("synthetic broker outage"),
    ) as delay:
        with django_capture_on_commit_callbacks(execute=True):
            registration = submit_case(idv_event, legal_versions)
        assert delay.call_count == 1  # tried after commit, failed quietly
        job = _job(case_for(registration))
        assert job.status == IdentityJobStatus.PENDING
        assert registration.public_status == RegistrationPublicStatus.SUBMITTED
        # The beat sweeper cannot reach the broker either; nothing is lost.
        assert idv.dispatch_due_identity_jobs() == 0
    # Recovery without the broker: the inline command processes the durable job.
    assert process_all() == {IdentityStatus.API_VERIFIED: 1}


@override_settings(IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT=True)
def test_dispatch_happens_only_after_commit(idv_event, legal_versions) -> None:
    from apps.people import tasks

    with mock.patch.object(tasks.process_identity_verification_job_task, "delay") as delay:
        with pytest.raises(RuntimeError), transaction.atomic():
            submit_case(idv_event, legal_versions)
            raise RuntimeError("rolled back")
        assert delay.call_count == 0


def test_the_sweeper_redispatches_due_jobs(idv_event, legal_versions) -> None:
    from apps.people import tasks

    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    with mock.patch.object(tasks.process_identity_verification_job_task, "delay") as delay:
        assert idv.dispatch_due_identity_jobs() == 1
    delay.assert_called_once_with(str(job.pk))


def test_the_celery_tasks_run_the_worker(idv_event, legal_versions) -> None:
    from apps.people import tasks

    registration = submit_case(idv_event, legal_versions)
    job = _job(case_for(registration))
    assert tasks.process_identity_verification_job_task.apply(args=[str(job.pk)]).get() == (
        IdentityStatus.API_VERIFIED
    )


def test_the_management_command_processes_due_jobs(idv_event, legal_versions) -> None:
    from io import StringIO

    from django.core.management import call_command

    submit_case(idv_event, legal_versions)
    output = StringIO()
    call_command("run_identity_verification_jobs", stdout=output)
    assert "Processed 1 identity verification job(s)." in output.getvalue()
    assert SIM_MATCH_NIN not in output.getvalue()


# ---------------------------------------------------------------------------
# Data minimization and hygiene (AS-16)
# ---------------------------------------------------------------------------


def test_attempts_store_no_clear_identity_outside_encrypted_official_facts(
    idv_event, legal_versions
) -> None:
    registration = submit_case(idv_event, legal_versions)
    process_all()
    attempt = IdentityVerificationAttempt.objects.get(registration=registration)
    plain_fields = " ".join(
        str(value)
        for value in (
            attempt.provider_code,
            attempt.request_reference,
            attempt.detail_code,
            attempt.reason_code,
            attempt.comparison,
            attempt.matched_fields,
            attempt.provider_metadata,
            attempt.presume_flag,
            attempt.policy_basis,
        )
    )
    assert SIM_MATCH_NIN not in plain_fields
    assert "BENTEST" not in plain_fields.upper()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT official_family_name_latin, official_birth_date "
            "FROM people_identity_verification_attempt WHERE id = %s",
            [attempt.pk],
        )
        raw_name, raw_date = cursor.fetchone()
    assert "BENTEST" not in raw_name  # encrypted at rest
    assert "1990" not in raw_date


def test_audit_events_carry_no_civil_data(idv_event, legal_versions) -> None:
    registration = submit_case(idv_event, legal_versions)
    process_all()
    events = AuditEvent.objects.filter(action_code__startswith="IDV_")
    assert events.exists()
    text = " ".join(f"{e.after_summary} {e.reason_code} {e.before_summary}" for e in events)
    for forbidden in (SIM_MATCH_NIN, "Bentest", "BENTEST", "Amina", "1990-03-07", "07/03/1990"):
        assert forbidden not in text
    assert registration.public_reference  # (the reference itself is not civil data)
