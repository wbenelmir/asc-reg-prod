"""Real multi-connection PostgreSQL races for UX-C2 item C: the identity,
contact, professional and combined interests writes, and the step advance,
serialized with final submission. Synthetic data only.

Each thread uses its OWN PostgreSQL connection. Proven here, with the lock
order Registration first for every writer:

1. a submission in flight makes each stale step writer wait; the writer then
   sees the committed submission and changes nothing, so the immutable
   snapshot and the live rows stay identical (submission first);
2. an identity write in flight makes a submission wait; the submission then
   records the committed new values (writer first);
3. a step advance racing a submission never moves a submitted record.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import Country, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.organizations.models import ProfessionalAffiliation
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.models import (
    ConsentPurpose,
    LegalDocument,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.registrations.models import (
    AccommodationRequest,
    InterestTopic,
    Registration,
    RegistrationInterest,
    RegistrationProfile,
    RegistrationSubmission,
)
from apps.registrations.services import (
    RegistrationNotDraft,
    advance_current_step,
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_contact_step,
    save_identity_step,
    save_interests_and_accommodation_step,
    save_professional_step,
    submit_full_registration,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]


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
        code="UXC2RACE",
        name="UX-C2 Race",
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
                version_label="uxc2-race",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=now - timedelta(days=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return event, versions


def _complete_draft(event, email):
    person = resolve_or_create_participant_for_email(email)
    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(draft)
    return draft


def _submit(registration, versions):
    privacy, terms = versions
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="uxc2-race",
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


def _identity(stale):
    return save_identity_step(
        registration=stale,
        given_names="Karima",
        family_name="Haddad",
        date_of_birth=date(1992, 3, 4),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value="123456789012345678",
    )


def _contact(stale):
    return save_contact_step(
        registration=stale, mobile_number="0661234567", mobile_country_code_id="DZ"
    )


def _professional(stale):
    return save_professional_step(
        registration=stale,
        organization_name="Changed Organization",
        organization_type="COMPANY",
        job_title="Changed Title",
        sector_code_id="TECH",
        country_code_id="DZ",
        operating_scope="NATIONAL",
    )


def _interests(stale):
    topic, _ = InterestTopic.objects.get_or_create(
        event_edition_id=stale.event_edition_id, code="RACETOPIC", defaults={"label": "Race"}
    )
    return save_interests_and_accommodation_step(
        registration=stale,
        interest_topic_ids=[topic.pk],
        objectives_text="Changed objectives.",
        accommodation_answer="YES",
        accommodation_categories=["OTHER"],
        accommodation_note="Changed note.",
    )


def _live_matches_snapshot(draft) -> None:
    snapshot = RegistrationSubmission.objects.get(registration=draft).snapshot_json
    profile = RegistrationProfile.objects.get(registration=draft)
    affiliation = ProfessionalAffiliation.objects.get(registration=draft)
    assert profile.submitted_given_names == snapshot["identity"]["given_names"]
    assert profile.objectives_text == snapshot["objectives_text"]
    assert affiliation.job_title == snapshot["professional"]["job_title"]
    assert affiliation.submitted_organization_name == snapshot["professional"]["organization_name"]
    live_topics = sorted(
        RegistrationInterest.objects.filter(registration=draft).values_list(
            "interest_topic__code", flat=True
        )
    )
    assert live_topics == sorted(snapshot["interests"])
    mobile = profile.declared_mobile_contact
    assert mobile.masked_value == snapshot["mobile"]["masked_value"]
    assert snapshot["accommodation"]["answer"] is None
    assert not AccommodationRequest.objects.filter(registration=draft).exists()


@pytest.mark.parametrize(
    "writer",
    [_identity, _contact, _professional, _interests],
    ids=["identity", "contact", "professional", "interests-and-accommodation"],
)
def test_a_submission_in_flight_blocks_then_refuses_a_stale_step_writer(world, writer) -> None:
    event, versions = world
    draft = _complete_draft(event, f"uxc2-race-{writer.__name__.strip('_')}@example.com")
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
        return writer(stale)

    submitter = _thread(submit, results, "submit")
    stale_writer = _thread(write, results, "write")
    assert holding.wait(10)
    time.sleep(0.8)
    assert stale_writer.is_alive(), "the stale writer must wait for the submission's lock"
    release.set()
    submitter.join(20)
    stale_writer.join(20)
    assert isinstance(results["submit"], RegistrationSubmission)
    assert isinstance(results["write"], RegistrationNotDraft)
    _live_matches_snapshot(draft)


def test_an_identity_write_in_flight_blocks_then_is_recorded_by_the_submission(world) -> None:
    event, versions = world
    draft = _complete_draft(event, "uxc2-race-writer-first@example.com")
    submitter_view = Registration.objects.get(pk=draft.pk)
    locked, release = threading.Event(), threading.Event()
    results: dict = {}

    def write():
        with transaction.atomic():
            _identity(Registration.objects.get(pk=draft.pk))
            locked.set()
            release.wait(10)
        return "written"

    def submit():
        locked.wait(10)
        return _submit(submitter_view, versions)

    writer = _thread(write, results, "write")
    submitter = _thread(submit, results, "submit")
    assert locked.wait(10)
    time.sleep(0.8)
    assert submitter.is_alive(), "the submission must wait for the writer's Registration lock"
    release.set()
    writer.join(20)
    submitter.join(20)
    assert results["write"] == "written"
    submission = results["submit"]
    assert isinstance(submission, RegistrationSubmission)
    assert submission.snapshot_json["identity"]["given_names"] == "Karima"
    assert RegistrationProfile.objects.get(registration=draft).submitted_given_names == "Karima"


def test_a_step_advance_racing_a_submission_never_moves_a_submitted_record(world) -> None:
    event, versions = world
    draft = _complete_draft(event, "uxc2-race-advance@example.com")
    Registration.objects.filter(pk=draft.pk).update(current_step="identity")
    stale = Registration.objects.get(pk=draft.pk)
    holding, release = threading.Event(), threading.Event()
    results: dict = {}

    def submit():
        with transaction.atomic():
            submission = _submit(Registration.objects.get(pk=draft.pk), versions)
            holding.set()
            release.wait(10)
        return submission

    def advance():
        holding.wait(10)
        advance_current_step(stale, "notices")
        return "advanced"

    submitter = _thread(submit, results, "submit")
    advancer = _thread(advance, results, "advance")
    assert holding.wait(10)
    time.sleep(0.8)
    assert advancer.is_alive(), "the conditional UPDATE must wait for the submission's lock"
    release.set()
    submitter.join(20)
    advancer.join(20)
    assert isinstance(results["submit"], RegistrationSubmission)
    assert results["advance"] == "advanced"
    assert Registration.objects.get(pk=draft.pk).current_step == "identity"
