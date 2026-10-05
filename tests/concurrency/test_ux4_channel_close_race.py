"""Real multi-connection PostgreSQL races for UX-4 (M01, M24, D-12, S-16).

Each thread uses its OWN PostgreSQL connection. Proven here:

1. a closure that holds its lock blocks a concurrent final submission, which
   then sees the closure and is refused (close wins when it commits first);
2. a submission in flight blocks a concurrent channel change until it commits
   (the change waits; nothing is half-applied);
3. two submissions for different drafts proceed together under `FOR SHARE`
   and never deadlock;
4. the same human-check solution posted twice at once is accepted once.
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
from apps.core import human_check
from apps.core.models import Country, HumanChallengeUse, Sector
from apps.core.testing import solved_human_check
from apps.events.apps import REGISTRATION_CHANNEL_MANAGER_GROUP_NAME
from apps.events.models import EventEdition, EventEditionStatus, PublicRegistrationMode
from apps.events.policies.registration_channels import (
    RegistrationChannelClosed,
    lock_event_for_channel_read,
)
from apps.events.services import change_registration_channel
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus
from apps.registrations.models import RegistrationSubmission
from apps.registrations.services import (
    get_or_create_active_draft,
    initial_submission_operation_key,
    submit_full_registration,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


@pytest.fixture
def world(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    now = timezone.now()
    event = EventEdition.objects.create(
        code="UX4RACE",
        name="UX-4 Race",
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
                version_label="ux4-race",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=now - timedelta(days=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    group = Group.objects.get(name=REGISTRATION_CHANNEL_MANAGER_GROUP_NAME)
    manager = OperationalUser.objects.create_user(
        email="ux4-race-manager@example.com",
        password="__test_password__",  # noqa: S106
        status=OperationalUserStatus.ACTIVE,
    )
    ScopedGroupMembership.objects.create(
        user=manager, group=group, event_edition=event, granted_by=manager
    )
    return event, versions, manager


def _complete_draft(event, email):
    person = resolve_or_create_participant_for_email(email)
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    return draft


def _submit(draft, versions):
    privacy, terms = versions
    return submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="race",
        idempotency_key=initial_submission_operation_key(draft.pk),
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


def test_a_closure_holding_its_lock_blocks_then_refuses_a_concurrent_submission(world) -> None:
    event, versions, manager = world
    draft = _complete_draft(event, "ux4-race-1@example.com")
    locked, release = threading.Event(), threading.Event()
    results: dict = {}

    def close():
        with transaction.atomic():
            change_registration_channel(
                event_edition_id=event.pk,
                actor=manager,
                mode=PublicRegistrationMode.CLOSED,
                opens_at=None,
                closes_at=None,
                reason="Race test closure.",
                expected_settings_version=event.settings_version,
            )
            locked.set()
            release.wait(10)
        return "closed"

    def submit():
        locked.wait(10)
        return _submit(draft, versions)

    closer = _thread(close, results, "close")
    submitter = _thread(submit, results, "submit")
    assert locked.wait(10)
    time.sleep(0.8)
    assert submitter.is_alive(), "the submission must wait for the closure's lock"
    release.set()
    closer.join(20)
    submitter.join(20)
    assert results["close"] == "closed"
    assert isinstance(results["submit"], RegistrationChannelClosed)
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()


def test_a_submission_in_flight_makes_a_channel_change_wait(world) -> None:
    event, _versions, manager = world
    holding, release = threading.Event(), threading.Event()
    results: dict = {}

    def in_flight_submission():
        with transaction.atomic():
            lock_event_for_channel_read(event.pk)
            holding.set()
            release.wait(10)
        return "submitted"

    def change():
        holding.wait(10)
        return change_registration_channel(
            event_edition_id=event.pk,
            actor=manager,
            mode=PublicRegistrationMode.INVITATION_ONLY,
            opens_at=None,
            closes_at=None,
            reason="Race test restriction.",
            expected_settings_version=event.settings_version,
        ).changed

    holder = _thread(in_flight_submission, results, "submission")
    changer = _thread(change, results, "change")
    assert holding.wait(10)
    time.sleep(0.8)
    assert changer.is_alive(), "the change must wait for the in-flight submission"
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.OPEN
    release.set()
    holder.join(20)
    changer.join(20)
    assert results == {"submission": "submitted", "change": True}
    event.refresh_from_db()
    assert event.public_registration_mode == PublicRegistrationMode.INVITATION_ONLY


def test_two_submissions_proceed_together_without_deadlock(world) -> None:
    event, versions, _manager = world
    first = _complete_draft(event, "ux4-race-a@example.com")
    second = _complete_draft(event, "ux4-race-b@example.com")
    barrier = threading.Barrier(2)
    results: dict = {}

    def submitter(draft):
        def run():
            barrier.wait(10)
            return _submit(draft, versions)

        return run

    threads = [
        _thread(submitter(first), results, "first"),
        _thread(submitter(second), results, "second"),
    ]
    for thread in threads:
        thread.join(30)
    assert isinstance(results["first"], RegistrationSubmission)
    assert isinstance(results["second"], RegistrationSubmission)


def test_one_human_check_solution_posted_twice_at_once_is_accepted_once(world) -> None:
    session: dict = {}  # the one issuing session both posts come from (UX-C2 binding)
    payload = solved_human_check(session=session)
    barrier = threading.Barrier(2)
    results: dict = {}

    def attempt():
        barrier.wait(10)
        return human_check.verify_and_consume(
            payload, action=human_check.ACTION_OTP_REQUEST, session=session
        )

    threads = [_thread(attempt, results, "one"), _thread(attempt, results, "two")]
    for thread in threads:
        thread.join(20)
    outcomes = sorted(
        "OK" if value == "OK" else getattr(value, "reason", repr(value))
        for value in results.values()
    )
    assert outcomes == ["OK", "REPLAYED"]
    assert HumanChallengeUse.objects.count() == 1
