"""Owner decision IDV-Q3 (2026-10-02): an Algerian participant without a usable NIN.

A narrow, staff-authorized manual path for ONE draft registration:

* staff with `people.grant_nin_exemption` in the registration's exact scope
  grant it with a preset reason, a written explanation and a confirmation;
  it is audited and revocable while the registration is a draft;
* the participant then completes the identity step with an Algerian national
  identity card or Algerian passport (number, expiry, photo) instead of a NIN;
* the submission routes the identity to manual documentary review
  (`NIN_EXEMPTION`, reason `NIN_EXEMPTION_REVIEW`), with a clear source when
  verified (`MANUAL_NIN_EXEMPTION`);
* no NIN is invented or created, no ministry job is scheduled, and no NIN
  tool (correction, recheck, NIN-route exception) applies;
* nobody without a grant sees or can use the route, and the foreign-national
  passport route is unchanged.

Synthetic identities and documents only; no provider is contacted (a spy
provider proves it is never called). Imports of the new API are inside the
tests (before/after evidence).
"""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.documents.tests.factories import make_test_photo
from apps.people.models import (
    IdentifierType,
    IdentityStatus,
    IdentityVerificationJob,
)
from apps.people.services import identity_review as review
from apps.people.tests.conftest import (
    case_for,
    make_event,
    make_staff,
    participant_client,
    process_all,
    staff_client,
    submit_case,
)
from apps.registrations.models import Registration, RegistrationPublicStatus
from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME, REGISTRATION_REVIEWERS_GROUP_NAME

pytestmark = pytest.mark.django_db

CARD_NUMBER = "QX0001234"  # synthetic national identity card number, not a NIN
PASSPORT_NUMBER = "QP7654321"  # synthetic Algerian passport number
EXPLANATION = "The participant's identity card predates the NIN (synthetic)."


@pytest.fixture
def manager(idv_event):
    return make_staff("q3-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=idv_event)


@pytest.fixture
def reviewer(idv_event):
    return make_staff(
        "q3-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=idv_event
    )


class SpyProvider:
    PROVIDER_CODE = "SPY"
    IS_OFFICIAL = True
    calls = 0

    def configuration_problems(self):
        return []

    def lookup(self, nin):  # pragma: no cover - must never run
        SpyProvider.calls += 1
        raise AssertionError("No provider may be called on the NIN exemption route.")


def _draft(event, email):
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.services import get_or_create_active_draft

    person = resolve_or_create_participant_for_email(email)
    return get_or_create_active_draft(person=person, event_edition=event)


def _participant_on(draft):
    """A signed-in participant whose active registration is `draft` (the
    workspace's Continue action sets it)."""
    client = participant_client(draft.person)
    client.post(reverse("registrations:continue", kwargs={"pk": draft.pk}))
    return client


def _grant(draft, manager, **kwargs):
    from apps.people.services.nin_exemption import grant_nin_exemption

    return grant_nin_exemption(
        draft.pk,
        actor=manager,
        reason_code=kwargs.get("reason_code", "NIN_NOT_ON_DOCUMENT"),
        explanation=kwargs.get("explanation", EXPLANATION),
        confirmed=kwargs.get("confirmed", True),
    )


def _declare(draft, *, kind="NATIONAL_ID_CARD", number=CARD_NUMBER, expires_at=None, photo=True):
    from apps.registrations.services import save_identity_step

    save_identity_step(
        registration=draft,
        given_names="Nour",
        family_name="Exemptest",
        date_of_birth=datetime.date(1991, 5, 4),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN_EXEMPTION",
        exemption_document_kind=kind,
        exemption_document_number=number,
        exemption_document_expires_at=expires_at,
        exemption_document_file=make_test_photo() if photo else None,
    )


def _other_steps(draft):
    from apps.registrations.models import InterestTopic
    from apps.registrations.services import (
        save_contact_step,
        save_interests_step,
        save_professional_step,
    )
    from apps.registrations.tests.factories import ensure_processing_consent_purpose

    save_contact_step(registration=draft, mobile_number="0551234567", mobile_country_code_id="DZ")
    save_professional_step(
        registration=draft,
        organization_name="Acme Corp",
        organization_type="COMPANY",
        job_title="Engineer",
        department="R&D",
        sector_code_id="TECH",
        country_code_id="DZ",
        organization_website="https://acme.example",
        professional_profile_url="https://www.linkedin.example/in/nour",
        biography="A short professional biography.",
        profile_photo_file=make_test_photo(),
        operating_scope="NATIONAL",
    )
    topic, _ = InterestTopic.objects.get_or_create(
        event_edition=draft.event_edition, code="FUNDING", defaults={"label": "Funding"}
    )
    save_interests_step(
        registration=draft, interest_topic_ids=[topic.pk], objectives_text="Meet investors."
    )
    ensure_processing_consent_purpose()


def _submit(draft, legal_versions):
    from apps.registrations.services import submit_full_registration

    privacy, terms = legal_versions
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="q3-session",
        idempotency_key=f"q3-{draft.pk}",
    )
    return Registration.objects.get(pk=draft.pk)


def _exempted_submission(event, legal_versions, manager, email, **declare):
    draft = _draft(event, email)
    _grant(draft, manager)
    _declare(draft, **declare)
    _other_steps(draft)
    return _submit(draft, legal_versions)


# ---------------------------------------------------------------------------
# No grant, no route
# ---------------------------------------------------------------------------


def test_without_a_grant_the_documentary_route_does_not_exist(idv_event, legal_versions) -> None:
    from apps.registrations.forms import IdentityStepForm

    draft = _draft(idv_event, "q3-nogrant@example.test")
    with pytest.raises(ValidationError):
        _declare(draft)
    form = IdentityStepForm(data={"identity_path": "NIN_EXEMPTION"}, event_edition=idv_event)
    assert not form.is_valid()
    assert "identity_path" in form.errors
    assert "exemption_document_number" not in form.fields
    client = _participant_on(draft)
    page = client.get(reverse("registrations:step-identity"))
    assert page.status_code == 200
    assert "data-nin-help" in page.text  # the way to ask the registration team
    assert "data-nin-exemption-panel" not in page.text
    assert "NIN_EXEMPTION" not in page.text
    # The NIN path remains required for an Algerian national.
    from apps.registrations.services import IncompleteRegistrationError

    _other_steps(draft)
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal_versions)


# ---------------------------------------------------------------------------
# The grant: permission, scope, reason, explanation, state
# ---------------------------------------------------------------------------


def test_the_grant_needs_the_dedicated_permission_in_scope(idv_event, reviewer) -> None:
    from apps.accounts.apps import REGISTRATION_INTAKE_GROUP_NAME
    from apps.people.policies import IdentityPermissionDenied

    draft = _draft(idv_event, "q3-scope@example.test")
    outsider = make_staff(
        "q3-outsider@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=make_event("IDVQ3X")
    )
    intake = make_staff("q3-intake@example.test", REGISTRATION_INTAKE_GROUP_NAME, event=idv_event)
    for actor in (reviewer, outsider, intake):
        with pytest.raises(IdentityPermissionDenied):
            _grant(draft, actor)
    assert not draft.nin_exemptions.exists()


def test_the_grant_needs_a_preset_reason_an_explanation_and_a_confirmation(
    idv_event, manager
) -> None:
    draft = _draft(idv_event, "q3-inputs@example.test")
    for kwargs in (
        {"reason_code": "INVENTED"},
        {"explanation": "short"},
        {"confirmed": False},
    ):
        with pytest.raises(review.InvalidIdentityInput):
            _grant(draft, manager, **kwargs)
    exemption = _grant(draft, manager)
    assert exemption.status == "ACTIVE"
    assert exemption.explanation_encrypted == EXPLANATION
    event = AuditEvent.objects.get(
        action_code="IDV_NIN_EXEMPTION_GRANTED", target_uuid=exemption.pk
    )
    assert event.reason_code == "NIN_NOT_ON_DOCUMENT"
    assert EXPLANATION not in str(event.after_summary)  # the explanation is never audited


def test_one_live_grant_per_draft_and_never_after_submission_or_for_a_foreigner(
    idv_event, legal_versions, manager
) -> None:
    draft = _draft(idv_event, "q3-once@example.test")
    _grant(draft, manager)
    with pytest.raises(review.IdentityStateError, match="exemption_exists"):
        _grant(draft, manager)

    submitted = submit_case(idv_event, legal_versions, email="q3-submitted@example.test")
    with pytest.raises(review.IdentityStateError, match="not_a_draft"):
        _grant(submitted, manager)

    from apps.registrations.services import save_identity_step

    foreign = _draft(idv_event, "q3-foreign@example.test")
    save_identity_step(
        registration=foreign,
        given_names="Lea",
        family_name="Foreign",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        passport_number="F1234567",
        passport_country_code_id="FR",
        passport_expires_at=datetime.date.today() + datetime.timedelta(days=500),
        passport_identity_page_file=make_test_photo(),
    )
    with pytest.raises(review.IdentityStateError, match="not_algerian"):
        _grant(foreign, manager)


# ---------------------------------------------------------------------------
# Completion and routing
# ---------------------------------------------------------------------------


def test_an_algerian_without_a_nin_completes_registration_with_an_identity_card(
    idv_event, legal_versions, manager
) -> None:
    from apps.people.models import NinExemption

    registration = _exempted_submission(idv_event, legal_versions, manager, "q3-card@example.test")
    assert registration.public_status == RegistrationPublicStatus.SUBMITTED
    case = case_for(registration)
    assert case.route == "NIN_EXEMPTION"
    assert case.status == IdentityStatus.MANUAL_REVIEW
    assert case.reason_code == "NIN_EXEMPTION_REVIEW"
    identifier = case.current_revision.identifier
    assert identifier.identifier_type == IdentifierType.NATIONAL_ID_CARD
    assert identifier.country_code_id == "DZ"
    assert identifier.value_encrypted == CARD_NUMBER
    # No NIN exists for this person, none was invented, and no job exists.
    assert not registration.person.identity_identifiers.filter(identifier_type="NIN").exists()
    assert not IdentityVerificationJob.objects.filter(verification=case).exists()
    SpyProvider.calls = 0
    process_all(provider=SpyProvider())
    assert SpyProvider.calls == 0
    exemption = NinExemption.objects.get(registration=registration)
    assert exemption.status == "USED"
    assert exemption.document_identifier_id == identifier.pk
    for code in (
        "IDV_NIN_EXEMPTION_GRANTED",
        "IDV_NIN_EXEMPTION_DOCUMENT_DECLARED",
        "IDV_NIN_EXEMPTION_USED",
    ):
        assert AuditEvent.objects.filter(action_code=code, target_uuid=exemption.pk).exists()
    # The snapshot keeps only the masked document number.
    snapshot = registration.submissions.get(submission_kind="INITIAL").snapshot_json
    assert CARD_NUMBER not in str(snapshot)


def test_the_passport_variant_needs_an_expiry_date(idv_event, legal_versions, manager) -> None:
    draft = _draft(idv_event, "q3-passport@example.test")
    _grant(draft, manager)
    with pytest.raises(ValidationError):
        _declare(draft, kind="PASSPORT", number=PASSPORT_NUMBER, expires_at=None)
    _declare(
        draft,
        kind="PASSPORT",
        number=PASSPORT_NUMBER,
        expires_at=datetime.date.today() + datetime.timedelta(days=700),
    )
    _other_steps(draft)
    registration = _submit(draft, legal_versions)
    case = case_for(registration)
    identifier = case.current_revision.identifier
    assert (identifier.identifier_type, identifier.country_code_id) == ("PASSPORT", "DZ")
    assert review.manual_evidence_types(case) == {"PASSPORT_IDENTITY_PAGE"}


def test_documentary_verification_is_labelled_and_clears_approval(
    idv_event, legal_versions, manager, reviewer
) -> None:
    from apps.documents.services import active_identity_evidence
    from apps.people.selectors.clearance import identity_clearance

    registration = _exempted_submission(
        idv_event, legal_versions, manager, "q3-verify@example.test"
    )
    case = case_for(registration)
    card = next(
        doc
        for doc in active_identity_evidence(registration)
        if doc.document_type == "NATIONAL_ID_CARD"
    )
    verified = review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=card.pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    assert verified.status == IdentityStatus.MANUALLY_VERIFIED
    assert verified.verification_source == "MANUAL_NIN_EXEMPTION"
    assert identity_clearance(registration.pk).cleared


def test_no_nin_tool_applies_to_the_exemption_route(
    idv_event, legal_versions, manager, reviewer
) -> None:
    registration = _exempted_submission(idv_event, legal_versions, manager, "q3-tools@example.test")
    case = case_for(registration)
    with pytest.raises(review.IdentityStateError, match="not_nin_route"):
        review.correct_nin_and_recheck(
            case.pk,
            actor=manager,
            expected_version=case.version,
            new_nin="123456789012345678",
            reason_code="RECHECK_AFTER_TECHNICAL_FAILURE",
        )
    from apps.documents.services import active_identity_evidence

    card = active_identity_evidence(registration)[0]
    with pytest.raises(review.IdentityStateError, match="exception_is_for_the_nin_route"):
        review.verify_identity_manually(
            case.pk,
            actor=manager,
            expected_version=case.version,
            evidence_document_id=card.pk,
            reason_code="NO_USABLE_MINISTRY_RECORD",
            note="An explanation that is long enough.",
            exception=True,
        )
    assert not IdentityVerificationJob.objects.filter(verification=case).exists()


# ---------------------------------------------------------------------------
# Stale forms, revocation, unused grants
# ---------------------------------------------------------------------------


def test_a_revoked_grant_refuses_a_stale_identity_form_and_the_submission(
    idv_event, legal_versions, manager
) -> None:
    from apps.people.services.nin_exemption import revoke_nin_exemption
    from apps.registrations.services import IncompleteRegistrationError

    draft = _draft(idv_event, "q3-revoke@example.test")
    exemption = _grant(draft, manager)
    _declare(draft)
    _other_steps(draft)
    exemption.refresh_from_db()
    revoke_nin_exemption(
        exemption.pk,
        actor=manager,
        expected_version=exemption.version,
        note="Granted to the wrong registration.",
    )
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal_versions)
    with pytest.raises(ValidationError):
        _declare(draft)  # the form the participant still has open
    from apps.people.models import IdentityVerification

    assert not IdentityVerification.objects.filter(registration=draft).exists()


def test_a_used_grant_cannot_be_revoked(idv_event, legal_versions, manager) -> None:
    from apps.people.models import NinExemption
    from apps.people.services.nin_exemption import revoke_nin_exemption

    registration = _exempted_submission(idv_event, legal_versions, manager, "q3-used@example.test")
    exemption = NinExemption.objects.get(registration=registration)
    with pytest.raises(review.IdentityStateError):
        revoke_nin_exemption(
            exemption.pk, actor=manager, expected_version=exemption.version, note="Too late now."
        )


def test_an_unused_grant_is_closed_when_the_nin_route_is_submitted(
    idv_event, legal_versions, manager
) -> None:
    from apps.people.models import NinExemption
    from apps.registrations.services import save_identity_step

    draft = _draft(idv_event, "q3-unused@example.test")
    _grant(draft, manager)
    _declare(draft)
    save_identity_step(  # the participant found a usable NIN after all
        registration=draft,
        given_names="Nour",
        family_name="Exemptest",
        date_of_birth=datetime.date(1991, 5, 4),
        nationality_code_id="DZ",
        country_of_residence_id="DZ",
        identity_path="NIN",
        nin_value="123456789012345611",
    )
    _other_steps(draft)
    registration = _submit(draft, legal_versions)
    assert case_for(registration).route == "NIN"
    exemption = NinExemption.objects.get(registration=registration)
    assert exemption.status == "REVOKED"
    assert exemption.revoked_by is None  # closed by the system, audited
    assert AuditEvent.objects.filter(
        action_code="IDV_NIN_EXEMPTION_REVOKED",
        target_uuid=exemption.pk,
        reason_code="unused_at_submission",
    ).exists()


def test_a_document_number_held_by_another_person_goes_to_duplicate_review(
    idv_event, legal_versions, manager, reviewer
) -> None:
    from apps.documents.services import active_identity_evidence

    first = _exempted_submission(idv_event, legal_versions, manager, "q3-dup-a@example.test")
    case = case_for(first)
    review.verify_identity_manually(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        evidence_document_id=active_identity_evidence(first)[0].pk,
        reason_code="DOCUMENT_MATCHES_SUBMISSION",
    )
    second = _exempted_submission(idv_event, legal_versions, manager, "q3-dup-b@example.test")
    assert case_for(second).reason_code == "DUPLICATE_IDENTIFIER"


def test_the_foreign_passport_route_is_unchanged(idv_event, legal_versions) -> None:
    registration = submit_case(
        idv_event, legal_versions, nationality="FR", email="q3-fr@example.test"
    )
    case = case_for(registration)
    assert (case.route, case.reason_code) == ("PASSPORT", "FOREIGN_PASSPORT_REVIEW")


# ---------------------------------------------------------------------------
# Participant correction on the documentary route
# ---------------------------------------------------------------------------


def test_a_returned_documentary_case_is_corrected_and_reviewed_again(
    idv_event, legal_versions, manager, reviewer
) -> None:
    registration = _exempted_submission(
        idv_event, legal_versions, manager, "q3-correct@example.test"
    )
    case = case_for(registration)
    returned = review.return_identity_for_correction(
        case.pk,
        actor=reviewer,
        expected_version=case.version,
        items=["DOCUMENT_DETAILS", "DOCUMENT_IMAGE"],
    )
    registration.refresh_from_db()
    with pytest.raises(review.InvalidIdentityInput):
        review.resubmit_identity_correction(
            registration,
            person=registration.person,
            expected_version=returned.version,
            given_names="Nour",
            family_name="Exemptest",
            date_of_birth=datetime.date(1991, 5, 4),
            document_number="QX-0009999-",
            document_expires_at=datetime.date.today() - datetime.timedelta(days=1),
        )
    # Nothing was kept from the refused attempt (expired document).
    resubmitted = review.resubmit_identity_correction(
        registration,
        person=registration.person,
        expected_version=returned.version,
        given_names="Nour",
        family_name="Exemptest",
        date_of_birth=datetime.date(1991, 5, 4),
        document_number="QX0009999",
        document_file=make_test_photo(),
    )
    assert resubmitted.status == IdentityStatus.MANUAL_REVIEW
    assert resubmitted.reason_code == "PARTICIPANT_RESUBMITTED"
    identifier = resubmitted.current_revision.identifier
    assert identifier.value_encrypted == "QX0009999"
    assert identifier.identifier_type == IdentifierType.NATIONAL_ID_CARD
    assert not IdentityVerificationJob.objects.filter(verification=resubmitted).exists()


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


def test_the_intake_page_offers_the_grant_only_to_its_holders(idv_event, manager, reviewer) -> None:
    draft = _draft(idv_event, "q3-screen@example.test")
    url = reverse("registrations:ops-intake-detail", kwargs={"pk": draft.pk})
    assert "data-nin-exemption-panel" not in staff_client(reviewer).get(url).text
    grant_url = reverse("identity:nin-exemption-grant", kwargs={"pk": draft.pk})
    assert staff_client(reviewer).post(grant_url, {}).status_code == 403

    client = staff_client(manager)
    page = client.get(url)
    assert 'data-nin-exemption-form="grant"' in page.text
    response = client.post(
        grant_url,
        {"reason_code": "NIN_NOT_AVAILABLE", "explanation": EXPLANATION, "confirmed": "on"},
        follow=True,
    )
    assert "NIN exemption granted" in response.text
    assert 'data-nin-exemption-status="ACTIVE"' in response.text
    assert 'data-nin-exemption-form="revoke"' in response.text
    # Found by its exact reference (never by a name or an identifier).
    listing = client.get(
        reverse("registrations:ops-intake-list"), {"reference": draft.public_reference}
    )
    assert draft.public_reference in listing.text


def test_the_identity_step_offers_the_documentary_route_after_a_grant(idv_event, manager) -> None:
    draft = _draft(idv_event, "q3-step@example.test")
    _grant(draft, manager)
    client = _participant_on(draft)
    page = client.get(reverse("registrations:step-identity"))
    assert page.status_code == 200
    assert "data-nin-exemption-panel" in page.text
    response = client.post(
        reverse("registrations:step-identity"),
        {
            "given_names": "Nour",
            "family_name": "Exemptest",
            "date_of_birth": "1991-05-04",
            "nationality_code": "DZ",
            "country_of_residence": "DZ",
            "identity_path": "NIN_EXEMPTION",
            "exemption_document_kind": "NATIONAL_ID_CARD",
            "exemption_document_number": "qx 000-1234",
            "exemption_document_image": make_test_photo(),
        },
    )
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-contact")
    from apps.people.models import NinExemption

    exemption = NinExemption.objects.get(registration=draft)
    assert exemption.document_identifier.value_encrypted == CARD_NUMBER


def test_the_queue_and_review_screen_label_the_route(
    idv_event, legal_versions, manager, reviewer
) -> None:
    registration = _exempted_submission(idv_event, legal_versions, manager, "q3-queue@example.test")
    client = staff_client(reviewer)
    queue = client.get(reverse("identity:queue"), {"route": "NIN_EXEMPTION"})
    assert registration.public_reference in queue.text
    page = client.get(reverse("identity:case", kwargs={"pk": case_for(registration).pk}))
    assert "data-idv-nin-exemption" in page.text
    assert 'data-idv-document-kind="NATIONAL_ID_CARD"' in page.text
    assert 'data-idv-action="correct"' not in page.text
    assert 'data-idv-action="exception"' not in page.text
