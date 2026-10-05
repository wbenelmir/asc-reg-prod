"""Owner decisions IDV-Q1 to IDV-Q3: real multi-connection PostgreSQL races.

Threads on their OWN PostgreSQL connections (the pattern of
`test_idv_c1_concurrency.py`). Proven here, each without a deadlock:

1. IDV-Q1: an approval racing the identity verification it needs -- an
   approval is recorded only if the identity was verified when it committed;
2. IDV-Q1: an approval racing a NIN correction in the same person's other
   registration -- whatever the order, the registration ends not eligible,
   and an approval that won recorded the verified identity it saw;
3. IDV-Q2: two final rejections of one case at once -- one wins, the other
   is a visible conflict; one participation decision and one message;
4. IDV-Q2: two "register again" requests at once -- one new draft;
5. IDV-Q3: a final submission racing the revocation of the NIN exemption --
   never a submitted documentary case on a revoked grant;
6. IDV-Q3: two grants at once -- one exemption.

Synthetic data only; mail is never sent (the test outbox only).
"""

from __future__ import annotations

import datetime
import importlib
import threading
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.people.models import IdentityStatus
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
    upload_national_id_card,
)
from apps.people.tests.identity_fixtures import assign_approval_prerequisites
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_named(callables: dict) -> tuple[dict, list[BaseException]]:
    barrier = threading.Barrier(len(callables))
    results: dict = {}
    errors: list[BaseException] = []

    def _worker(name, target) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=_worker, args=(name, target), name=name)
        for name, target in callables.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a thread did not finish"
    return results, errors


def _no_deadlock(errors) -> None:
    from django.db import OperationalError

    assert not any(
        isinstance(error, OperationalError) or "deadlock" in str(error).lower() for error in errors
    ), errors


@pytest.fixture(autouse=True)
def _private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def versions():
    from apps.core.models import Country, Sector
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    result = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        result.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-q-c-{code}",
                content="Synthetic",
                content_hash="f" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(result)


@pytest.fixture
def manager(versions):
    return make_staff("idv-q-conc-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


@pytest.fixture
def second_manager(versions):
    return make_staff("idv-q-conc-manager2@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


@pytest.fixture
def reviewer(versions):
    return make_staff("idv-q-conc-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


@pytest.fixture
def rejection_template():
    """A `transaction=True` test flushes the seeded templates; restore the
    identity-rejection one from its own migration."""
    from django.apps import apps as django_apps

    module = importlib.import_module(
        "apps.communications.migrations.0007_idv_q2_identity_rejection_template"
    )
    module.forward(django_apps, None)


def _person():
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email(f"idv-qc-{uuid.uuid4().hex[:10]}@example.test")


def _manual_case(event_code, versions, person=None):
    registration = submit_case(make_event(event_code), versions, nin=UNKNOWN_NIN, person=person)
    process_all()
    upload_national_id_card(registration)
    return registration


def _verify(registration, reviewer, version=None):
    case = case_for(registration)
    card = registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    return review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version if version is None else version,
        evidence_document_id=card.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )


def _approve(registration, manager, version):
    from apps.reviews.services import record_approved_decision

    return record_approved_decision(
        registration=registration, expected_version=version, decided_by=manager
    )


# ---------------------------------------------------------------------------
# IDV-Q1
# ---------------------------------------------------------------------------


def test_an_approval_racing_the_identity_verification(versions, manager, reviewer) -> None:
    from apps.reviews.services import ApprovalRequiresVerifiedIdentityError

    registration = _manual_case("IDVQCA", versions)
    assign_approval_prerequisites(registration, manager)
    registration.refresh_from_db()
    version = registration.version

    results, errors = _run_named(
        {
            "approve": lambda: _approve(registration, manager, version),
            "verify": lambda: _verify(registration, reviewer),
        }
    )

    _no_deadlock(errors)
    assert all(isinstance(error, ApprovalRequiresVerifiedIdentityError) for error in errors)
    registration.refresh_from_db()
    case = case_for(registration)
    assert case.status == IdentityStatus.MANUALLY_VERIFIED  # the verification always lands
    if registration.public_status == RegistrationPublicStatus.APPROVED:
        assert "approve" in results  # approved only after the verification committed
    else:
        assert len(errors) == 1


def test_an_approval_racing_a_linked_correction_never_leaves_an_eligible_stale_identity(
    versions, manager, reviewer
) -> None:
    from apps.registrations.selectors import is_active_approved_context
    from apps.reviews.services import ApprovalRequiresVerifiedIdentityError

    person = _person()
    first = _manual_case("IDVQCB1", versions, person=person)
    second = _manual_case("IDVQCB2", versions, person=person)
    _verify(first, reviewer)
    assign_approval_prerequisites(first, manager)
    first.refresh_from_db()
    version = first.version

    def correct():
        case = case_for(second)
        return review.correct_nin_and_recheck(
            case.pk,
            actor=manager,
            expected_version=case.version,
            new_nin="123456789012345672",
            reason_code="TYPING_ERROR_CONFIRMED",
            evidence_document_id=second.documents.get(
                document_type="NATIONAL_ID_CARD", status="ACTIVE"
            ).pk,
            confirmed=True,
        )

    _results, errors = _run_named(
        {"approve": lambda: _approve(first, manager, version), "correct": correct}
    )

    _no_deadlock(errors)
    assert all(isinstance(error, ApprovalRequiresVerifiedIdentityError) for error in errors)
    first.refresh_from_db()
    assert case_for(first).status == IdentityStatus.MANUAL_REVIEW  # rebound in every order
    assert is_active_approved_context(first) is False
    if first.public_status == RegistrationPublicStatus.APPROVED:
        from apps.audit.models import AuditEvent

        approval = AuditEvent.objects.get(
            action_code="REV_DECISION_RECORDED", target_uuid=first.pk, result="SUCCESS"
        )
        assert approval.after_summary["identity_status"] == IdentityStatus.MANUALLY_VERIFIED


# ---------------------------------------------------------------------------
# IDV-Q2
# ---------------------------------------------------------------------------


def test_two_final_rejections_at_once_record_one_outcome_and_one_message(
    versions, manager, second_manager, rejection_template
) -> None:
    from apps.communications.models import CommunicationMessage

    registration = _manual_case("IDVQCC", versions)
    case = case_for(registration)
    version = case.version

    def reject(actor):
        return review.reject_identity(
            case.pk,
            actor=actor,
            expected_version=version,
            reason_code="NO_VALID_EVIDENCE",
            note="No valid evidence was provided (synthetic).",
            confirmed=True,
        )

    _results, errors = _run_named(
        {"one": lambda: reject(manager), "two": lambda: reject(second_manager)}
    )

    _no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], review.StaleIdentityVersion), errors
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert registration.decisions.count() == 1
    assert (
        CommunicationMessage.objects.filter(
            registration=registration, template_version__template__code="IDENTITY_REJECTION"
        ).count()
        == 1
    )


def test_two_register_again_requests_at_once_open_one_draft(versions, manager) -> None:
    from apps.registrations.services import start_registration_after_identity_rejection

    registration = _manual_case("IDVQCD", versions)
    case = case_for(registration)
    review.reject_identity(
        case.pk,
        actor=manager,
        expected_version=case.version,
        reason_code="NO_VALID_EVIDENCE",
        note="No valid evidence was provided (synthetic).",
        confirmed=True,
    )
    registration.refresh_from_db()
    person = registration.person

    def restart():
        return start_registration_after_identity_rejection(
            registration=registration, person=person
        ).pk

    results, errors = _run_named({"one": restart, "two": restart})

    assert errors == []
    assert results["one"] == results["two"]
    assert (
        Registration.objects.filter(
            person=person, event_edition=registration.event_edition, is_current_context=True
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# IDV-Q3
# ---------------------------------------------------------------------------


def _exemption_draft(event, manager):
    from apps.people.tests.test_idv_q3_nin_exemption import _declare, _grant, _other_steps
    from apps.registrations.services import get_or_create_active_draft

    draft = get_or_create_active_draft(person=_person(), event_edition=event)
    exemption = _grant(draft, manager)
    _declare(draft)
    _other_steps(draft)
    exemption.refresh_from_db()
    return draft, exemption


def test_a_submission_racing_the_revocation_of_its_exemption(versions, manager) -> None:
    from apps.people.models import IdentityVerification, NinExemption
    from apps.people.services.nin_exemption import revoke_nin_exemption
    from apps.people.tests.test_idv_q3_nin_exemption import _submit
    from apps.registrations.services import IncompleteRegistrationError

    draft, exemption = _exemption_draft(make_event("IDVQCE"), manager)

    def revoke():
        return revoke_nin_exemption(
            exemption.pk,
            actor=manager,
            expected_version=exemption.version,
            note="Granted to the wrong registration.",
        )

    _results, errors = _run_named({"submit": lambda: _submit(draft, versions), "revoke": revoke})

    _no_deadlock(errors)
    assert len(errors) == 1, errors
    # The loser is refused: an incomplete submission, or a revocation that
    # finds the exemption used (a stale version or a state refusal).
    assert isinstance(
        errors[0],
        (IncompleteRegistrationError, review.IdentityStateError, review.StaleIdentityVersion),
    )
    draft.refresh_from_db()
    exemption.refresh_from_db()
    if exemption.status == "USED":
        assert draft.public_status == RegistrationPublicStatus.SUBMITTED
        assert case_for(draft).route == "NIN_EXEMPTION"
    else:
        assert exemption.status == "REVOKED"
        assert draft.public_status == RegistrationPublicStatus.DRAFT
        assert not IdentityVerification.objects.filter(registration=draft).exists()
    assert NinExemption.objects.filter(registration=draft).count() == 1


def test_two_grants_at_once_create_one_exemption(versions, manager, second_manager) -> None:
    from apps.people.models import NinExemption
    from apps.people.tests.test_idv_q3_nin_exemption import _grant
    from apps.registrations.services import get_or_create_active_draft

    draft = get_or_create_active_draft(person=_person(), event_edition=make_event("IDVQCF"))

    _results, errors = _run_named(
        {"one": lambda: _grant(draft, manager), "two": lambda: _grant(draft, second_manager)}
    )

    _no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], review.IdentityStateError), errors
    assert NinExemption.objects.filter(registration=draft).count() == 1
