"""Real multi-connection PostgreSQL races for UX-C1 (integrated UX review
finding UX-F05). Synthetic data only.

Each thread uses its OWN PostgreSQL connection. Proven here, with the lock
order Registration, then AccommodationRequest, taken by every writer:

1. an accommodation write in flight makes a concurrent final submission wait;
   the submission then sees the committed details and refuses to proceed
   without their explicit consent (writer first);
2. a submission in flight makes a writer holding a stale DRAFT instance wait;
   the writer then sees the committed submission and changes nothing
   (submission first);
3. a withdrawal in flight makes a stale writer wait; the writer then changes
   nothing, so the withdrawal is not reset and the details stay cleared.

In every outcome the support list shows no unconsented detail.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.core.models import Country, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    ConsentPurpose,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.apps import ACCOMMODATION_SUPPORT_GROUP_NAME
from apps.registrations.models import AccommodationRequest, Registration, RegistrationSubmission
from apps.registrations.selectors import accommodation_requests_visible_to
from apps.registrations.services import (
    RegistrationNotDraft,
    SensitiveConsentRequired,
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_accommodation_request,
    submit_full_registration,
    withdraw_accommodation_consent,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

NOTE = "Synthetic race note."


@pytest.fixture
def world(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    settings.PRIVATE_STORAGE_ROOT = root
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    for code in ("PERSONAL_DATA_PROCESSING", "SENSITIVE_ACCOMMODATION_DATA"):
        ConsentPurpose.objects.get_or_create(code=code, defaults={"name": code})
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    now = timezone.now()
    event = EventEdition.objects.create(
        code="UXC1RACE",
        name="UX-C1 Race",
        timezone="UTC",
        starts_at=now + timedelta(days=5),
        ends_at=now + timedelta(days=6),
        status=EventEditionStatus.REGISTRATION_OPEN,
    )
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label="uxc1-race",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=now - timedelta(days=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    coordinator = OperationalUser.objects.create_user(
        email="uxc1-race-coordinator@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=coordinator,
        group=Group.objects.get(name=ACCOMMODATION_SUPPORT_GROUP_NAME),
        event_edition=event,
        granted_by=coordinator,
    )
    return event, versions, coordinator


def _complete_draft(event, email):
    person = resolve_or_create_participant_for_email(email)
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    return draft


def _submit(registration, versions, *, sensitive=False):
    privacy, terms = versions
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        sensitive_data_consent_granted=sensitive,
        session_reference="uxc1-race",
        idempotency_key=initial_submission_operation_key(registration.pk),
    )


def _thread(target, results, key):
    def run():
        try:
            results[key] = target()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread
            results[key] = exc
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_write_in_flight_blocks_then_constrains_a_concurrent_submission(world) -> None:
    event, versions, coordinator = world
    draft = _complete_draft(event, "uxc1-race-1@example.com")
    submitter_view = Registration.objects.get(pk=draft.pk)  # the notices page's instance
    locked, release = threading.Event(), threading.Event()
    results: dict = {}

    def write():
        with transaction.atomic():
            save_accommodation_request(
                registration=Registration.objects.get(pk=draft.pk),
                answer="YES",
                categories=["CAPTIONING"],
                note=NOTE,
            )
            locked.set()
            release.wait(10)
        return "written"

    def submit():
        locked.wait(10)
        return _submit(submitter_view, versions)  # rendered before the details existed

    writer = _thread(write, results, "write")
    submitter = _thread(submit, results, "submit")
    assert locked.wait(10)
    time.sleep(0.8)
    assert submitter.is_alive(), "the submission must wait for the writer's Registration lock"
    release.set()
    writer.join(20)
    submitter.join(20)
    assert results["write"] == "written"
    assert isinstance(results["submit"], SensitiveConsentRequired)
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()
    assert AccommodationRequest.objects.get(registration=draft).note_encrypted == NOTE
    assert accommodation_requests_visible_to(coordinator).count() == 0  # still a draft


def test_a_submission_in_flight_blocks_then_refuses_a_stale_writer(world) -> None:
    event, versions, coordinator = world
    draft = _complete_draft(event, "uxc1-race-2@example.com")
    stale = Registration.objects.get(pk=draft.pk)  # a wizard tab loaded before submission
    holding, release = threading.Event(), threading.Event()
    results: dict = {}

    def submit():
        with transaction.atomic():
            submission = _submit(Registration.objects.get(pk=draft.pk), versions)
            holding.set()
            release.wait(10)
        return submission

    def write():
        holding.wait(10)
        return save_accommodation_request(
            registration=stale, answer="YES", categories=["QUIET_SPACE"], note=NOTE
        )

    submitter = _thread(submit, results, "submit")
    writer = _thread(write, results, "write")
    assert holding.wait(10)
    time.sleep(0.8)
    assert writer.is_alive(), "the stale writer must wait for the submission's lock"
    release.set()
    submitter.join(20)
    writer.join(20)
    assert isinstance(results["submit"], RegistrationSubmission)
    assert isinstance(results["write"], RegistrationNotDraft)
    assert not AccommodationRequest.objects.filter(registration=draft).exists()
    snapshot = RegistrationSubmission.objects.get(registration=draft).snapshot_json
    assert snapshot["accommodation"] == {
        "answer": None,
        "sensitive_data_provided": False,
        "sensitive_support_consent_recorded": False,  # UX-C2 (G): its own fact
    }
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_a_withdrawal_in_flight_blocks_then_refuses_a_stale_writer(world) -> None:
    event, versions, coordinator = world
    draft = _complete_draft(event, "uxc1-race-3@example.com")
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    stale = Registration.objects.get(pk=draft.pk)
    _submit(Registration.objects.get(pk=draft.pk), versions, sensitive=True)
    assert accommodation_requests_visible_to(coordinator).count() == 1
    holding, release = threading.Event(), threading.Event()
    results: dict = {}

    def withdraw():
        with transaction.atomic():
            changed = withdraw_accommodation_consent(
                registration=Registration.objects.get(pk=draft.pk), person=draft.person
            )
            holding.set()
            release.wait(10)
        return changed

    def write():
        holding.wait(10)
        return save_accommodation_request(
            registration=stale, answer="YES", categories=["OTHER"], note="Reopened."
        )

    withdrawer = _thread(withdraw, results, "withdraw")
    writer = _thread(write, results, "write")
    assert holding.wait(10)
    time.sleep(0.8)
    assert writer.is_alive(), "the stale writer must wait for the withdrawal's lock"
    release.set()
    withdrawer.join(20)
    writer.join(20)
    assert results["withdraw"] is True
    assert isinstance(results["write"], RegistrationNotDraft)
    request = AccommodationRequest.objects.get(registration=draft)
    assert request.withdrawn_at is not None
    assert request.categories == [] and request.note_encrypted == ""
    assert accommodation_requests_visible_to(coordinator).count() == 0
