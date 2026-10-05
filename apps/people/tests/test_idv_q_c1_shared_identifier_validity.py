"""IDV-Q-C1, finding 2: a draft or a correction never silently changes a verified identity.

One person's identifiers are shared across events. Since IDV-Q1 the
eligibility of an approved registration reads the expiry of its case's
identifier. These tests prove that:

* a new draft in another event that re-declares the same passport or
  identity card number with another expiry (later, earlier, or none) never
  changes the identifier a submitted case is bound to;
* a participant correction that changes only the expiry never changes a
  shared identifier in place: the case moves to a new identifier and the
  person's other cases are rebound explicitly (IDV-C1), with a decision and an
  audit event;
* eligibility changes only after an authorized verification, and a genuine
  expiry still blocks eligibility;
* an identifier only a draft uses is still updated in place (no
  proliferation of rows).

Synthetic identities and documents only.
"""

from __future__ import annotations

import datetime

import pytest

from apps.documents.tests.factories import make_test_photo
from apps.people.models import IdentityStatus, IdentityVerification
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    case_for,
    make_event,
    make_staff,
)
from apps.registrations.models import Registration
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

PASSPORT = "QPQ0000001"
CARD = "QXC0000001"
TODAY = datetime.date.today()
E1 = TODAY + datetime.timedelta(days=200)
E2 = TODAY + datetime.timedelta(days=900)
EARLIER = TODAY + datetime.timedelta(days=30)


@pytest.fixture
def reviewer(idv_event):
    return make_staff("q-c1-sv-r@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)


@pytest.fixture
def manager(idv_event):
    return make_staff("q-c1-sv-m@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)


def _person(email):
    from apps.people.services import resolve_or_create_participant_for_email

    return resolve_or_create_participant_for_email(email)


def _passport_step(draft, *, expires_at, number=PASSPORT):
    from apps.registrations.services import save_identity_step

    save_identity_step(
        registration=draft,
        given_names="Lea",
        family_name="Sharedtest",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        passport_number=number,
        passport_country_code_id="FR",
        passport_expires_at=expires_at,
        passport_identity_page_file=make_test_photo(),
    )


def _submit_passport(event, versions, person, *, expires_at):
    """Submit a foreign-passport registration with `expires_at` (synthetic)."""
    from apps.registrations.services import get_or_create_active_draft, submit_full_registration
    from apps.registrations.tests.factories import walk_draft_through_every_step

    draft = get_or_create_active_draft(person=person, event_edition=event)
    walk_draft_through_every_step(
        draft,
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        passport_number=PASSPORT,
        passport_country_code_id="FR",
        passport_expires_at=expires_at,
    )
    _passport_step(draft, expires_at=expires_at)
    privacy, terms = versions
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="q-c1-sv",
        idempotency_key=f"q-c1-sv-{draft.pk}",
    )
    return Registration.objects.get(pk=draft.pk)


def _verify_passport(registration, reviewer):
    from apps.documents.services import active_passport_identity_page

    case = case_for(registration)
    return review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=active_passport_identity_page(registration).pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )


def _bound_expiry(registration):
    return case_for(registration).current_revision.identifier.expires_at


def _cleared(registration, today=None) -> bool:
    from apps.people.selectors.clearance import identity_clearance

    return identity_clearance(registration.pk, today=today).cleared


# ---------------------------------------------------------------------------
# A new draft in another event
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("new_expiry", [E2, EARLIER], ids=["extended", "shortened"])
def test_a_new_draft_never_changes_a_verified_passport_of_another_registration(
    idv_reference_data, legal_versions, reviewer, new_expiry
) -> None:
    from apps.registrations.services import get_or_create_active_draft

    person = _person(f"q-c1-sv-pass-{new_expiry.isoformat()}@example.test")
    first = _submit_passport(make_event("QC1SVA"), legal_versions, person, expires_at=E1)
    _verify_passport(first, reviewer)
    assert _cleared(first)

    draft = get_or_create_active_draft(person=person, event_edition=make_event("QC1SVB"))
    _passport_step(draft, expires_at=new_expiry)

    assert _bound_expiry(first) == E1  # the verified case is untouched
    assert _cleared(first)
    assert _cleared(first, today=E1 - datetime.timedelta(days=1))
    # A genuine expiry still blocks eligibility.
    assert not _cleared(first, today=E1)


def test_a_new_exemption_draft_never_clears_a_verified_identity_card_expiry(
    idv_event, legal_versions, manager, reviewer
) -> None:
    from apps.documents.services import active_identity_evidence
    from apps.people.tests.test_idv_q3_nin_exemption import (
        _declare,
        _grant,
        _other_steps,
        _submit,
    )
    from apps.registrations.services import get_or_create_active_draft

    person = _person("q-c1-sv-card@example.test")
    first_draft = get_or_create_active_draft(person=person, event_edition=idv_event)
    _grant(first_draft, manager)
    _declare(first_draft, number=CARD, expires_at=E1)
    _other_steps(first_draft)
    first = _submit(first_draft, legal_versions)
    case = case_for(first)
    review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=active_identity_evidence(first)[0].pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert _cleared(first)

    second_draft = get_or_create_active_draft(person=person, event_edition=make_event("QC1SVC"))
    _grant(second_draft, manager)
    _declare(second_draft, number=CARD, expires_at=None)  # the same card, no expiry

    assert _bound_expiry(first) == E1
    assert not _cleared(first, today=E1)  # still expires


def test_an_identifier_only_a_draft_uses_is_still_updated_in_place(idv_reference_data) -> None:
    from apps.registrations.services import get_or_create_active_draft

    person = _person("q-c1-sv-draft@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=make_event("QC1SVD"))
    _passport_step(draft, expires_at=E1)
    _passport_step(draft, expires_at=E2)
    passports = person.identity_identifiers.filter(identifier_type="PASSPORT")
    assert passports.count() == 1
    assert passports.get().expires_at == E2


# ---------------------------------------------------------------------------
# A verification of the newer declaration
# ---------------------------------------------------------------------------


def test_verifying_a_newer_declaration_rebinds_the_older_case_explicitly(
    idv_reference_data, legal_versions, reviewer
) -> None:
    """Eligibility of the first registration changes only through the
    authorized verification, and then explicitly: its case returns to manual
    review with a recorded decision, never silently extended."""
    person = _person("q-c1-sv-reverify@example.test")
    first = _submit_passport(make_event("QC1SVE"), legal_versions, person, expires_at=E1)
    _verify_passport(first, reviewer)
    second = _submit_passport(make_event("QC1SVF"), legal_versions, person, expires_at=E2)
    assert _bound_expiry(first) == E1
    assert _cleared(first)

    _verify_passport(second, reviewer)

    assert _bound_expiry(second) == E2
    assert _cleared(second)
    first_case = case_for(first)
    assert first_case.status == IdentityStatus.MANUAL_REVIEW
    assert first_case.reason_code == "IDENTITY_DATA_CHANGED"
    assert first_case.decisions.filter(action="LINKED_IDENTITY_CHANGE").exists()
    assert not _cleared(first)
    # A new authorized verification of the first case restores it.
    _verify_passport(first, reviewer)
    assert _cleared(first)
    assert _bound_expiry(first) == E2


# ---------------------------------------------------------------------------
# A participant correction that changes only the expiry
# ---------------------------------------------------------------------------


def test_an_expiry_only_correction_never_changes_a_shared_verified_identifier(
    idv_reference_data, legal_versions, reviewer
) -> None:
    """Both drafts were filled before either was submitted, so both cases are
    bound to the same identifier row. The second is returned for its passport
    details and the participant changes only the expiry."""
    from apps.registrations.services import get_or_create_active_draft, submit_full_registration
    from apps.registrations.tests.factories import walk_draft_through_every_step

    person = _person("q-c1-sv-correct@example.test")
    privacy, terms = legal_versions
    drafts = []
    for code in ("QC1SVG", "QC1SVH"):
        draft = get_or_create_active_draft(person=person, event_edition=make_event(code))
        walk_draft_through_every_step(
            draft,
            nationality_code_id="FR",
            country_of_residence_id="FR",
            identity_path="PASSPORT",
            passport_number=PASSPORT,
            passport_country_code_id="FR",
            passport_expires_at=E1,
        )
        _passport_step(draft, expires_at=E1)
        drafts.append(draft)
    for draft in drafts:
        submit_full_registration(
            registration=draft,
            privacy_notice_version=privacy,
            terms_version=terms,
            data_processing_consent_granted=True,
            session_reference="q-c1-sv",
            idempotency_key=f"q-c1-sv-{draft.pk}",
        )
    first, second = (Registration.objects.get(pk=draft.pk) for draft in drafts)
    shared = case_for(first).current_revision.identifier_id
    assert case_for(second).current_revision.identifier_id == shared
    _verify_passport(first, reviewer)
    second_case = case_for(second)
    returned = review.return_identity_for_correction(
        second_case.pk,
        actor=reviewer,
        expected_version=second_case.version,
        items=["PASSPORT_DETAILS"],
    )
    second.refresh_from_db()

    review.resubmit_identity_correction(
        second,
        person=person,
        expected_version=returned.version,
        given_names="Lea",
        family_name="Sharedtest",
        date_of_birth=datetime.date(1990, 1, 1),
        passport_number=PASSPORT,
        passport_country_code_id="FR",
        passport_expires_at=E2,
    )

    from apps.people.models import IdentityIdentifier

    assert IdentityIdentifier.objects.get(pk=shared).expires_at == E1  # never changed in place
    assert _bound_expiry(second) == E2
    first_case = IdentityVerification.objects.get(registration=first)
    # The first case is not silently extended: it is rebound, recorded, and
    # needs an authorized verification again.
    assert first_case.status == IdentityStatus.MANUAL_REVIEW
    assert first_case.decisions.filter(action="LINKED_IDENTITY_CHANGE").exists()
    assert not _cleared(first)
