"""Real multi-connection PostgreSQL concurrency tests for the review
decision and information-response submission paths (Phase 2 Prompt 3 §12).

Marked `concurrency`, matching `pyproject.toml`'s definition: "real
multi-connection PostgreSQL concurrency tests (not Redis integration)".
"""

from __future__ import annotations

import threading
import uuid

import pytest
from django.db import connection
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.organizations.models import Organization, OrganizationType
from apps.people.services import resolve_or_create_participant_for_email
from apps.registrations.models import (
    Registration,
    RegistrationInternalStatus,
    RegistrationPublicStatus,
    RegistrationSourceKind,
)
from apps.reviews.models import (
    InformationRequestPurpose,
    InformationRequestStatus,
    RequestItemKind,
)
from apps.reviews.services import (
    StaleVersionError,
    create_information_request,
    record_not_approved_decision,
    send_information_request,
    submit_information_response,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


def _run_callables_in_threads(callables: list) -> tuple[list, list[BaseException]]:
    count = len(callables)
    barrier = threading.Barrier(count)
    results: list = [None] * count
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = callables[index]()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def _seed():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


def _make_event(code: str) -> EventEdition:
    return EventEdition.objects.create(
        code=code,
        name=code,
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status="REGISTRATION_OPEN",
    )


def _make_registration(event, organization=None, person=None) -> Registration:
    return Registration.objects.create(
        public_reference=f"REVCONC-{uuid.uuid4().hex[:10].upper()}",
        event_edition=event,
        person=person,
        source_kind=RegistrationSourceKind.OPEN,
        source_context_key=f"open:{uuid.uuid4().hex}",
        source_organization=organization,
        public_status=RegistrationPublicStatus.SUBMITTED,
        internal_status=RegistrationInternalStatus.PENDING_ASSIGNMENT,
        submitted_at=timezone.now(),
    )


def _make_manager(email: str, *, event, organization):
    from django.contrib.auth.models import Group

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    user = OperationalUser.objects.create_user(
        email=email, password=None, status=OperationalUserStatus.ACTIVE
    )
    group = Group.objects.get(name="Accreditation Managers")
    ScopedGroupMembership.objects.create(
        user=user, group=group, event_edition=event, organization=organization, granted_by=user
    )
    return user


def test_concurrent_not_approved_decisions_leave_exactly_one_current_decision() -> None:
    _seed()
    event = _make_event("REVDECCONC")
    organization = Organization.objects.create(
        official_name="Decision Concurrency Org",
        normalized_name="decision concurrency org",
        organization_type=OrganizationType.OTHER,
    )
    registration = _make_registration(event, organization)
    manager = _make_manager(
        "decision-conc-manager@example.com", event=event, organization=organization
    )
    expected_version = registration.version

    def _decide():
        return record_not_approved_decision(
            registration=registration,
            expected_version=expected_version,
            internal_reason_code="INCOMPLETE_INFORMATION",
            decided_by=manager,
        )

    results, errors = _run_callables_in_threads([_decide, _decide])

    successes = [r for r in results if r is not None]
    stale_errors = [e for e in errors if isinstance(e, StaleVersionError)]
    assert len(successes) == 1
    assert len(stale_errors) == 1

    registration.refresh_from_db()
    assert registration.decisions.filter(is_current=True).count() == 1
    assert registration.public_status == RegistrationPublicStatus.NOT_APPROVED

    # A retry of the WINNING call with the now-current version must not
    # create a second decision merely by being called again.
    record_not_approved_decision(
        registration=registration,
        expected_version=registration.version,
        internal_reason_code="INCOMPLETE_INFORMATION",
        decided_by=manager,
    )
    registration.refresh_from_db()
    assert registration.decisions.filter(is_current=True).count() == 1
    assert registration.decisions.count() == 1


def test_concurrent_response_submissions_never_create_two_responses() -> None:
    _seed()
    event = _make_event("REVRESPCONC")
    person = resolve_or_create_participant_for_email("concurrent-responder@example.com")
    registration = _make_registration(event, person=person)
    manager = _make_manager("response-conc-manager@example.com", event=event, organization=None)

    information_request = create_information_request(
        registration=registration,
        purpose=InformationRequestPurpose.CLARIFICATION,
        message_en="Please clarify.",
        items=[{"kind": RequestItemKind.CLARIFICATION, "field_code": "given_names"}],
        created_by=manager,
    )
    information_request = send_information_request(
        information_request=information_request,
        expected_version=information_request.version,
        actor=manager,
    )
    item = information_request.items.get()

    def _submit():
        return submit_information_response(
            information_request=information_request,
            person=person,
            answers=[{"request_item_id": item.pk, "value": "Amine", "document": None}],
        )

    results, errors = _run_callables_in_threads([_submit, _submit])

    assert errors == []  # never a raw IntegrityError/traceback
    assert results[0] is not None and results[1] is not None
    assert results[0].pk == results[1].pk  # converged on the SAME response

    information_request.refresh_from_db()
    assert information_request.status == InformationRequestStatus.SUBMITTED
    from apps.reviews.models import InformationResponse

    assert InformationResponse.objects.filter(information_request=information_request).count() == 1
