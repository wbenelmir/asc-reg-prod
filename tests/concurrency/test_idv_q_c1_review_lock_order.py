"""IDV-Q-C1, finding 1: a final identity rejection racing review commands.

`reject_identity` holds the Registration and the identity case, then ends the
open review work (review cases and information requests). Review commands
used to lock their own case or request FIRST and the Registration
afterwards, so the two orders could deadlock. Each test forces one
interleaving with a pause inside the transaction of the first party, on its
own PostgreSQL connection, and proves:

* no deadlock;
* the second party either proceeds after the first or is refused visibly
  (a stale version or an illegal transition), never silently;
* closed review work is never resurrected (no open case, no active request,
  the registration stays NOT_APPROVED);
* exactly one rejection participation decision and one notification.

The distinct lock patterns covered: a ReviewCase lock then the Registration
(status change, assignment); an InformationRequest lock then the
Registration (send, cancel, participant response); and an FK key-share on
the ReviewCase from an insert, then the Registration (duplicate
resolution). Synthetic data only; mail goes to the test outbox only.
"""

from __future__ import annotations

import datetime
import importlib
import threading
import time
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    UNKNOWN_NIN,
    case_for,
    make_event,
    make_staff,
    process_all,
    submit_case,
)
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

PAUSE_SECONDS = 1.5


def _run_named(callables: dict) -> tuple[dict, list[BaseException]]:
    results: dict = {}
    errors: list[BaseException] = []

    def _worker(name, target) -> None:
        try:
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


@pytest.fixture(autouse=True)
def _private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


@pytest.fixture
def world():
    """A submitted registration whose identity is in manual review, with an
    open review case, a manager and a reviewer, and the rejection template."""
    from django.apps import apps as django_apps

    from apps.core.models import Country, Sector
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus
    from apps.reviews.services import open_review_case

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-q-c1-lock-{code}",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    importlib.import_module(
        "apps.communications.migrations.0007_idv_q2_identity_rejection_template"
    ).forward(django_apps, None)
    registration = submit_case(
        make_event("IDVQC1L"),
        tuple(versions),
        nin=UNKNOWN_NIN,
        email=f"idv-q-c1-{uuid.uuid4().hex[:8]}@example.test",
    )
    process_all()
    review_case = open_review_case(
        registration=registration, case_type="STANDARD", queue_code="GENERAL"
    )
    return {
        "registration": registration,
        "review_case": review_case,
        "manager": make_staff("idv-q-c1-lock-m@example.test", ACCREDITATION_MANAGERS_GROUP_NAME),
        "reviewer": make_staff("idv-q-c1-lock-r@example.test", REGISTRATION_REVIEWERS_GROUP_NAME),
    }


def _reject(world):
    case = case_for(world["registration"])
    return review.reject_identity(
        case.pk,
        actor=world["manager"],
        expected_version=case.version,
        reason_code="NO_VALID_EVIDENCE",
        note="No valid evidence was provided (synthetic).",
        confirmed=True,
    )


def _pause_in(monkeypatch, function_name: str, thread_name: str, reached: threading.Event):
    """Pause `thread_name` inside `apps.reviews.services.<function_name>`,
    after its earlier locks and before the function's own work."""
    from apps.reviews import services

    original = getattr(services, function_name)

    def pausing(*args, **kwargs):
        if threading.current_thread().name == thread_name:
            reached.set()
            time.sleep(PAUSE_SECONDS)
        return original(*args, **kwargs)

    monkeypatch.setattr(services, function_name, pausing)


def _second(reached: threading.Event, target):
    def run():
        assert reached.wait(timeout=20), "the first party never reached its pause"
        return target()

    return run


def _assert_closed_once(world) -> None:
    from apps.communications.models import CommunicationMessage
    from apps.reviews.models import (
        InformationRequest,
        InformationRequestStatus,
        ReviewAssignment,
        ReviewCase,
        ReviewCaseStatus,
    )

    registration = Registration.objects.get(pk=world["registration"].pk)
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED
    assert registration.internal_status == "CLOSED"
    assert not ReviewCase.objects.filter(
        registration=registration, status__in=ReviewCaseStatus.open_statuses()
    ).exists(), "closed review work was resurrected"
    assert not ReviewAssignment.objects.filter(
        review_case__registration=registration, is_current=True
    ).exists()
    assert not InformationRequest.objects.filter(
        registration=registration, status__in=InformationRequestStatus.active_statuses()
    ).exists()
    decisions = list(registration.decisions.all())
    assert len(decisions) == 1 and decisions[0].internal_reason_code == "IDENTITY_REJECTED"
    assert (
        CommunicationMessage.objects.filter(
            registration=registration, template_version__template__code="IDENTITY_REJECTION"
        ).count()
        == 1
    )


def _assert_no_deadlock(errors) -> None:
    from django.db import OperationalError

    assert not any(
        isinstance(error, OperationalError) or "deadlock" in str(error).lower() for error in errors
    ), errors


def _refusals():
    from apps.reviews.services import InvalidStateTransitionError, StaleVersionError

    return (StaleVersionError, InvalidStateTransitionError, ValueError)


# ---------------------------------------------------------------------------
# ReviewCase, then Registration
# ---------------------------------------------------------------------------


def test_a_case_status_change_holding_its_case_then_a_rejection(world, monkeypatch) -> None:
    from apps.reviews.services import change_review_case_status

    reached = threading.Event()
    _pause_in(monkeypatch, "_set_registration_status", "command", reached)
    case = world["review_case"]

    _results, errors = _run_named(
        {
            "command": lambda: change_review_case_status(
                review_case=case,
                new_status="IN_PROGRESS",
                expected_version=case.version,
                actor=world["reviewer"],
            ),
            "reject": _second(reached, lambda: _reject(world)),
        }
    )

    _assert_no_deadlock(errors)
    assert errors == []
    _assert_closed_once(world)


def test_a_rejection_holding_the_registration_then_a_case_status_change(world, monkeypatch) -> None:
    from apps.reviews.services import change_review_case_status

    reached = threading.Event()
    _pause_in(monkeypatch, "_terminate_open_review_work", "reject", reached)
    case = world["review_case"]

    _results, errors = _run_named(
        {
            "reject": lambda: _reject(world),
            "command": _second(
                reached,
                lambda: change_review_case_status(
                    review_case=case,
                    new_status="IN_PROGRESS",
                    expected_version=case.version,
                    actor=world["reviewer"],
                ),
            ),
        }
    )

    _assert_no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], _refusals()), errors
    _assert_closed_once(world)


def test_an_assignment_holding_its_case_then_a_rejection(world, monkeypatch) -> None:
    from apps.reviews.services import assign_review_case

    reached = threading.Event()
    _pause_in(monkeypatch, "_set_registration_status", "command", reached)
    case = world["review_case"]

    _results, errors = _run_named(
        {
            "command": lambda: assign_review_case(
                review_case=case,
                expected_version=case.version,
                assigned_by=world["manager"],
                assigned_user=world["reviewer"],
            ),
            "reject": _second(reached, lambda: _reject(world)),
        }
    )

    _assert_no_deadlock(errors)
    assert errors == []
    _assert_closed_once(world)


# ---------------------------------------------------------------------------
# InformationRequest, then Registration
# ---------------------------------------------------------------------------


def _draft_request(world):
    from apps.reviews.services import create_information_request

    return create_information_request(
        registration=world["registration"],
        purpose="CLARIFICATION",
        message_en="Please confirm your job title (synthetic).",
        items=[{"kind": "CLARIFICATION", "field_code": "job_title"}],
        created_by=world["reviewer"],
        review_case=world["review_case"],
    )


def _send(world, information_request):
    from apps.reviews.services import send_information_request

    information_request.refresh_from_db()
    return send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=world["reviewer"],
    )


def test_sending_an_information_request_then_a_rejection(world, monkeypatch) -> None:
    information_request = _draft_request(world)
    reached = threading.Event()
    _pause_in(monkeypatch, "_set_registration_status", "command", reached)

    _results, errors = _run_named(
        {
            "command": lambda: _send(world, information_request),
            "reject": _second(reached, lambda: _reject(world)),
        }
    )

    _assert_no_deadlock(errors)
    assert errors == []
    _assert_closed_once(world)


def test_a_rejection_then_cancelling_an_information_request(world, monkeypatch) -> None:
    from apps.reviews.services import cancel_information_request

    information_request = _send(world, _draft_request(world))
    reached = threading.Event()
    _pause_in(monkeypatch, "_terminate_open_review_work", "reject", reached)

    _results, errors = _run_named(
        {
            "reject": lambda: _reject(world),
            "command": _second(
                reached,
                lambda: cancel_information_request(
                    information_request=information_request,
                    expected_version=information_request.version,
                    actor=world["reviewer"],
                    reason="No longer needed (synthetic).",
                ),
            ),
        }
    )

    _assert_no_deadlock(errors)
    assert len(errors) == 1 and isinstance(errors[0], _refusals()), errors
    _assert_closed_once(world)


def test_a_participant_response_then_a_rejection(world, monkeypatch) -> None:
    from apps.reviews.services import submit_information_response

    information_request = _send(world, _draft_request(world))
    item = information_request.items.get()
    reached = threading.Event()
    _pause_in(monkeypatch, "_set_registration_status", "command", reached)
    person = world["registration"].person

    _results, errors = _run_named(
        {
            "command": lambda: submit_information_response(
                information_request=information_request,
                person=person,
                answers=[{"request_item_id": item.pk, "value": "Engineer"}],
            ),
            "reject": _second(reached, lambda: _reject(world)),
        }
    )

    _assert_no_deadlock(errors)
    assert errors == []
    _assert_closed_once(world)


# ---------------------------------------------------------------------------
# An insert's FK key-share on the ReviewCase, then Registration
# ---------------------------------------------------------------------------


def test_a_duplicate_resolution_then_a_rejection(world, monkeypatch) -> None:
    from apps.reviews.services import open_review_case, resolve_duplicate_candidate

    duplicate_case = open_review_case(
        registration=world["registration"], case_type="DUPLICATE", queue_code="DUPLICATE"
    )
    reached = threading.Event()
    _pause_in(monkeypatch, "_set_registration_status", "command", reached)

    _results, errors = _run_named(
        {
            "command": lambda: resolve_duplicate_candidate(
                review_case=duplicate_case,
                outcome="DIFFERENT_PERSON",
                reviewed_by=world["reviewer"],
                reason="Different person (synthetic).",
            ),
            "reject": _second(reached, lambda: _reject(world)),
        }
    )

    _assert_no_deadlock(errors)
    assert errors == []
    _assert_closed_once(world)
