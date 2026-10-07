"""UX-C1 correction regressions (integrated UX review findings UX-F01, UX-F02,
UX-F03, UX-F05 and UX-F06). Synthetic data only.

Each test pins a failure the review found by reading the UX-2 tree:

* UX-F01: draft or otherwise unconsented accommodation data reached the
  support list; another registration's consent must never qualify a row.
* UX-F02: the service accepted a submission without the explicit processing
  consent, or with a missing or wrong purpose.
* UX-F03: the professional rules and the selectable-country policy were only
  enforced by the forms, not by the step services or the submission guard.
* UX-F05 (sequential part; the two-connection races are in
  `tests/concurrency/test_uxc1_accommodation_races.py`): a writer holding a
  stale DRAFT instance changed a submitted request or reset a withdrawal.
* UX-F06: the withdrawal texts promised a deletion a legal hold prevents.
"""

from __future__ import annotations

import datetime

import phonenumbers
import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.accounts.tests.sign_in import staff_sign_in
from apps.core.models import Country, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.organizations.models import ProfessionalAffiliation
from apps.people.models import IdentifierType
from apps.people.services import PhoneValidationError, resolve_or_create_participant_for_email
from apps.privacy.models import AcceptanceRecord, ConsentPurpose, ConsentRecord, LegalHold
from apps.privacy.selectors import effective_published_version
from apps.registrations.apps import ACCOMMODATION_SUPPORT_GROUP_NAME
from apps.registrations.models import (
    AccommodationRequest,
    Registration,
    RegistrationProfile,
    RegistrationPublicStatus,
    RegistrationSubmission,
)
from apps.registrations.selectors import accommodation_requests_visible_to
from apps.registrations.services import (
    IncompleteRegistrationError,
    ProcessingConsentRequired,
    RegistrationNotDraft,
    SensitiveConsentRequired,
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_accommodation_request,
    save_contact_step,
    save_identity_step,
    save_professional_step,
    submit_full_registration,
    withdraw_accommodation_consent,
)
from apps.registrations.tests.factories import (
    notices_post_data,
    walk_draft_through_every_step,
)

pytestmark = pytest.mark.django_db

PASSWORD = "__test_password__"  # noqa: S105
NOTE = "Synthetic note: a seat near the aisle."


@pytest.fixture(autouse=True)
def _reference_data(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    settings.PRIVATE_STORAGE_ROOT = root
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def excluded_country():
    """An IL row, as a future catalog import could create; it stays excluded."""
    country, _ = Country.objects.get_or_create(code="IL", defaults={"name": "Excluded"})
    return country


def _event(code: str, *, open_: bool = True) -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code=code,
        name=f"{code} Test",
        timezone="UTC",
        starts_at=now + datetime.timedelta(days=20),
        ends_at=now + datetime.timedelta(days=21),
        status=EventEditionStatus.REGISTRATION_OPEN
        if open_
        else EventEditionStatus.REGISTRATION_CLOSED,
    )


@pytest.fixture
def event() -> EventEdition:
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    return _event("UXC1")


@pytest.fixture
def person():
    return resolve_or_create_participant_for_email("uxc1-person@example.com")


@pytest.fixture
def draft(event, person):
    return get_or_create_active_draft(person=person, event_edition=event)


def _submit(registration, **kwargs):
    kwargs.setdefault("data_processing_consent_granted", True)
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
        terms_version=effective_published_version("TERMS", "en"),
        session_reference="uxc1",
        idempotency_key=initial_submission_operation_key(registration.pk),
        **kwargs,
    )


def _coordinator(email: str, *events) -> OperationalUser:
    user = OperationalUser.objects.create_user(
        email=email, password=PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    group = Group.objects.get(name=ACCOMMODATION_SUPPORT_GROUP_NAME)
    for event_edition in events:
        ScopedGroupMembership.objects.create(
            user=user, group=group, event_edition=event_edition, granted_by=user
        )
    return user


def _operational_client(email: str) -> Client:
    client = Client()
    staff_sign_in(client, email, PASSWORD)
    return client


def _participant_client(email: str) -> Client:
    from apps.accounts.otp import DeterministicTestOtpGenerator
    from apps.core.testing import otp_request_data

    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(reverse("accounts:otp-request"), otp_request_data(email, client=client))
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    return client


def _nothing_was_recorded(registration) -> None:
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.DRAFT
    assert not RegistrationSubmission.objects.filter(registration=registration).exists()
    assert not AcceptanceRecord.objects.filter(registration=registration).exists()
    assert not ConsentRecord.objects.filter(person=registration.person).exists()


# ---------------------------------------------------------------------------
# UX-F01: no draft or unconsented accommodation data for support staff
# ---------------------------------------------------------------------------


def test_a_draft_with_support_details_is_never_listed(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(
        registration=draft, answer="YES", categories=["CAPTIONING"], note=NOTE
    )
    coordinator = _coordinator("uxc1-coord-draft@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_the_support_screen_never_renders_a_draft_note(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(
        registration=draft, answer="YES", categories=["CAPTIONING"], note=NOTE
    )
    _coordinator("uxc1-coord-screen@example.com", event)
    response = _operational_client("uxc1-coord-screen@example.com").get(
        reverse("registrations:ops-accommodation-list")
    )
    assert response.status_code == 200
    assert NOTE.encode() not in response.content
    assert b"Live captioning" not in response.content


def test_a_refused_submission_without_sensitive_consent_stays_hidden(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    with pytest.raises(SensitiveConsentRequired):
        _submit(draft)
    coordinator = _coordinator("uxc1-coord-refused@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_a_consented_submission_is_listed(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    _submit(draft, sensitive_data_consent_granted=True)
    coordinator = _coordinator("uxc1-coord-listed@example.com", event)
    assert list(accommodation_requests_visible_to(coordinator)) == [
        AccommodationRequest.objects.get(registration=draft)
    ]


def test_details_added_after_a_submission_without_them_stay_hidden(draft, event) -> None:
    """Data that did not exist when this registration was submitted has no
    consent: the snapshot fact, not the row's current content, decides."""
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=[], note="")
    _submit(draft)
    coordinator = _coordinator("uxc1-coord-late@example.com", event)
    # UX-C2 (G, owner decision): a YES answer without details and without the
    # registration's own consent is hidden too (UX-C1 listed it).
    assert accommodation_requests_visible_to(coordinator).count() == 0
    # Details written behind the services' back (as the pre-fix race could).
    request = AccommodationRequest.objects.get(registration=draft)
    request.categories = ["QUIET_SPACE"]
    request.note_encrypted = NOTE
    request.save(update_fields=["categories", "note_encrypted", "updated_at"])
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_another_registrations_consent_is_never_borrowed(person) -> None:
    """Same person, two contexts: a consent recorded for context A never
    qualifies unconsented data in context B, drafted or submitted."""
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    event_a = _event("UXC1A")
    context_a = get_or_create_active_draft(person=person, event_edition=event_a)
    walk_draft_through_every_step(context_a)
    save_accommodation_request(
        registration=context_a, answer="YES", categories=["CAPTIONING"], note=NOTE
    )
    _submit(context_a, sensitive_data_consent_granted=True)
    assert ConsentRecord.objects.filter(
        person=person, purpose__code="SENSITIVE_ACCOMMODATION_DATA", action="GRANTED"
    ).exists()

    event_a.status = EventEditionStatus.REGISTRATION_CLOSED
    event_a.save(update_fields=["status"])
    event_b = _event("UXC1B")
    context_b = get_or_create_active_draft(person=person, event_edition=event_b)
    walk_draft_through_every_step(context_b)
    save_accommodation_request(
        registration=context_b, answer="YES", categories=["QUIET_SPACE"], note="Other note."
    )
    coordinator = _coordinator("uxc1-coord-contexts@example.com", event_a, event_b)
    visible = list(accommodation_requests_visible_to(coordinator))
    assert [item.registration_id for item in visible] == [context_a.pk]

    # Submitted without details, then details appear: still not borrowed.
    save_accommodation_request(registration=context_b, answer="YES", categories=[], note="")
    _submit(context_b)
    AccommodationRequest.objects.filter(registration=context_b).update(categories=["OTHER"])
    visible = list(accommodation_requests_visible_to(coordinator))
    assert [item.registration_id for item in visible] == [context_a.pk]


def test_the_owner_still_sees_their_own_draft_details(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(
        registration=draft, answer="YES", categories=["CAPTIONING"], note=NOTE
    )
    client = _participant_client("uxc1-person@example.com")
    response = client.get(reverse("registrations:step-interests"))
    assert response.status_code == 200
    assert NOTE.encode() in response.content


# ---------------------------------------------------------------------------
# UX-F02: affirmative processing consent and the active purpose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("granted", [False, None, "on", 1])
def test_submission_without_an_affirmative_processing_consent_is_refused(draft, granted) -> None:
    walk_draft_through_every_step(draft)
    with pytest.raises(ProcessingConsentRequired):
        _submit(draft, data_processing_consent_granted=granted)
    _nothing_was_recorded(draft)


def test_the_consent_decision_is_a_required_argument(draft) -> None:
    walk_draft_through_every_step(draft)
    with pytest.raises(TypeError):
        submit_full_registration(
            registration=draft,
            privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
            terms_version=effective_published_version("TERMS", "en"),
            session_reference="uxc1",
            idempotency_key=initial_submission_operation_key(draft.pk),
        )
    _nothing_was_recorded(draft)


def test_a_wrong_purpose_is_refused(draft) -> None:
    walk_draft_through_every_step(draft)
    marketing, _ = ConsentPurpose.objects.get_or_create(
        code="MARKETING", defaults={"name": "Marketing"}
    )
    with pytest.raises(ProcessingConsentRequired):
        _submit(draft, data_processing_consent_purpose=marketing)
    _nothing_was_recorded(draft)


def test_a_missing_or_inactive_purpose_fails_closed(draft) -> None:
    walk_draft_through_every_step(draft)
    ConsentPurpose.objects.filter(code="PERSONAL_DATA_PROCESSING").update(is_active=False)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "notices"
    _nothing_was_recorded(draft)


def test_the_purpose_is_resolved_on_the_server(draft) -> None:
    walk_draft_through_every_step(draft)
    _submit(draft)  # no purpose supplied by the caller
    record = ConsentRecord.objects.get(person=draft.person)
    assert record.purpose.code == "PERSONAL_DATA_PROCESSING"
    assert record.action == "GRANTED"


def test_a_sensitive_consent_without_its_active_purpose_fails_before_evidence(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note="")
    ConsentPurpose.objects.filter(code="SENSITIVE_ACCOMMODATION_DATA").update(is_active=False)
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, sensitive_data_consent_granted=True)
    _nothing_was_recorded(draft)


def test_a_completed_submission_is_still_returned_to_a_retry(draft) -> None:
    walk_draft_through_every_step(draft)
    first = _submit(draft)
    ConsentPurpose.objects.filter(code="PERSONAL_DATA_PROCESSING").update(is_active=False)
    retry = _submit(draft, data_processing_consent_granted=False)
    assert retry.pk == first.pk
    assert ConsentRecord.objects.filter(person=draft.person).count() == 1


def test_the_notices_view_refuses_when_the_purpose_is_missing(draft) -> None:
    walk_draft_through_every_step(draft)
    ConsentPurpose.objects.filter(code="PERSONAL_DATA_PROCESSING").update(is_active=False)
    client = _participant_client("uxc1-person@example.com")
    response = client.post(
        reverse("registrations:step-notices"),
        notices_post_data(),
    )
    assert response.status_code == 200
    assert b"Legal notices are not currently available" in response.content
    _nothing_was_recorded(draft)


def test_the_notices_view_refuses_without_the_consent_box(draft) -> None:
    walk_draft_through_every_step(draft)
    client = _participant_client("uxc1-person@example.com")
    response = client.post(
        reverse("registrations:step-notices"),
        notices_post_data(without=("accept_data_processing",)),
    )
    assert response.status_code == 200
    _nothing_was_recorded(draft)


# ---------------------------------------------------------------------------
# UX-F03: the approved rules in the step services and the submission guard
# ---------------------------------------------------------------------------


def _professional(draft, **overrides):
    values = {
        "organization_name": "Example Technologies SARL",
        "organization_type": "COMPANY",
        "job_title": "Product Manager",
        "department": "Research and Development",
        "sector_code_id": "TECH",
        "country_code_id": "DZ",
        "operating_scope": "NATIONAL",
        "biography": "A short synthetic biography.",
    }
    values.update(overrides)
    save_professional_step(registration=draft, **values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"job_title": "مدير المنتج"},
        {"organization_name": "Пример"},
        {"department": "R&D 😀"},
        {"job_title": "J" * 121},
        {"department": "D" * 121},
        {"organization_name": "O" * 201},
        {"biography": "b" * 501},
        {"organization_type": "ROCKET"},
        {"organization_type": "INSTITUTION"},
        {"operating_scope": "GALACTIC"},
        {"sector_code_id": "NOT_A_SECTOR"},
    ],
)
def test_the_professional_service_refuses_out_of_policy_values(draft, overrides) -> None:
    with pytest.raises(ValidationError):
        _professional(draft, **overrides)
    assert not ProfessionalAffiliation.objects.filter(registration=draft).exists()


def test_the_professional_service_refuses_an_inactive_sector(draft) -> None:
    Sector.objects.create(code="RETIRED", name="Retired", is_active=False)
    with pytest.raises(ValidationError):
        _professional(draft, sector_code_id="RETIRED")


def test_the_professional_service_refuses_an_excluded_headquarters(draft, excluded_country) -> None:
    with pytest.raises(ValidationError):
        _professional(draft, country_code_id=excluded_country.pk)


def test_the_professional_service_stores_the_normalized_values(draft) -> None:
    _professional(draft, job_title="  Product   Manager ", biography="Line one\r\nLine two ")
    affiliation = ProfessionalAffiliation.objects.get(registration=draft)
    assert affiliation.job_title == "Product Manager"
    assert affiliation.biography == "Line one\nLine two"


def test_a_legacy_institution_type_is_kept_only_where_it_is_already_held(draft) -> None:
    _professional(draft)
    ProfessionalAffiliation.objects.filter(registration=draft).update(
        organization_type="INSTITUTION"
    )
    _professional(draft, organization_type="INSTITUTION")  # re-saving the held value
    assert (
        ProfessionalAffiliation.objects.get(registration=draft).organization_type == "INSTITUTION"
    )


@pytest.mark.parametrize(
    "field", ["nationality_code_id", "country_of_residence_id", "passport_country_code_id"]
)
def test_the_identity_service_refuses_an_excluded_country(draft, excluded_country, field) -> None:
    values = {
        "registration": draft,
        "given_names": "Amina",
        "family_name": "Benali",
        "date_of_birth": datetime.date(1990, 1, 1),
        "nationality_code_id": "FR",
        "country_of_residence_id": "FR",
        "identity_path": "PASSPORT",
        "passport_number": "AB1234567",
        "passport_country_code_id": "FR",
        "passport_expires_at": timezone.now().date() + datetime.timedelta(days=365),
    }
    values[field] = excluded_country.pk
    with pytest.raises(ValidationError):
        save_identity_step(**values)
    assert not RegistrationProfile.objects.filter(registration=draft).exists()


def test_the_contact_service_refuses_a_number_from_an_excluded_country(
    draft, excluded_country
) -> None:
    walk_draft_through_every_step(draft)
    example = phonenumbers.example_number_for_type("IL", phonenumbers.PhoneNumberType.MOBILE)
    pasted = phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.E164)
    before = RegistrationProfile.objects.get(registration=draft).declared_mobile_contact_id
    with pytest.raises(PhoneValidationError):
        save_contact_step(registration=draft, mobile_number=pasted, mobile_country_code_id="DZ")
    assert RegistrationProfile.objects.get(registration=draft).declared_mobile_contact_id == before


def _stale_affiliation(draft, **values) -> None:
    ProfessionalAffiliation.objects.filter(registration=draft).update(**values)


@pytest.mark.parametrize(
    "values",
    [
        {"job_title": "مدير المنتج"},
        {"job_title": "J" * 121},
        {"department": "قسم"},
        {"submitted_organization_name": "O" * 201},
        {"biography": "b" * 501},
        {"organization_type": "ROCKET"},
        {"operating_scope": "GALACTIC"},
        {"organization_website": "javascript:alert(1)"},
    ],
)
def test_a_stale_draft_cannot_submit_out_of_policy_professional_data(draft, values) -> None:
    walk_draft_through_every_step(draft)
    _stale_affiliation(draft, **values)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "professional"
    _nothing_was_recorded(draft)


def test_a_stale_draft_cannot_submit_an_inactive_sector_or_excluded_country(
    draft, excluded_country
) -> None:
    walk_draft_through_every_step(draft)
    _stale_affiliation(draft, country_code_id=excluded_country.pk)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "professional"
    _stale_affiliation(draft, country_code_id="DZ")
    Sector.objects.filter(code="TECH").update(is_active=False)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "professional"
    _nothing_was_recorded(draft)


def test_a_stale_draft_cannot_submit_an_excluded_residence(draft, excluded_country) -> None:
    walk_draft_through_every_step(draft)
    RegistrationProfile.objects.filter(registration=draft).update(
        country_of_residence_id=excluded_country.pk
    )
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "identity"
    _nothing_was_recorded(draft)


def test_a_stale_draft_cannot_submit_an_excluded_passport_country(draft, excluded_country) -> None:
    walk_draft_through_every_step(
        draft,
        nationality_code_id="FR",
        country_of_residence_id="FR",
        identity_path="PASSPORT",
        mobile_number="0612345678",
        mobile_country_code_id="FR",
    )
    draft.person.identity_identifiers.filter(identifier_type=IdentifierType.PASSPORT).update(
        country_code_id=excluded_country.pk
    )
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "identity"
    _nothing_was_recorded(draft)


def test_a_stale_draft_cannot_submit_a_number_from_an_excluded_country(
    draft, excluded_country
) -> None:
    from apps.people.models import ContactPointType
    from apps.people.services import create_contact_point

    walk_draft_through_every_step(draft)
    example = phonenumbers.example_number_for_type("IL", phonenumbers.PhoneNumberType.MOBILE)
    contact = create_contact_point(
        person=draft.person,
        contact_type=ContactPointType.MOBILE,
        raw_value=phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.E164),
        country_code_id=None,  # a legacy row without a stored region
    )
    RegistrationProfile.objects.filter(registration=draft).update(declared_mobile_contact=contact)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "contact"
    _nothing_was_recorded(draft)


def test_a_draft_already_holding_the_legacy_institution_type_can_submit(draft) -> None:
    walk_draft_through_every_step(draft)
    _stale_affiliation(draft, organization_type="INSTITUTION")
    submission = _submit(draft)
    assert submission.snapshot_json["professional"]["organization_type"] == "INSTITUTION"


def test_a_completed_historical_record_is_never_rechecked(draft) -> None:
    walk_draft_through_every_step(draft)
    first = _submit(draft)
    # A value that no longer meets the rules on an already-submitted record.
    _stale_affiliation(draft, job_title="مدير المنتج")
    assert _submit(draft).pk == first.pk
    assert ProfessionalAffiliation.objects.get(registration=draft).job_title == "مدير المنتج"


# ---------------------------------------------------------------------------
# UX-F05 (sequential): a stale DRAFT instance changes nothing sensitive
# ---------------------------------------------------------------------------


def test_a_stale_draft_instance_cannot_change_a_submitted_request(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    stale = Registration.objects.get(pk=draft.pk)
    _submit(Registration.objects.get(pk=draft.pk), sensitive_data_consent_granted=True)
    assert stale.public_status == RegistrationPublicStatus.DRAFT  # the stale view
    with pytest.raises(RegistrationNotDraft):
        save_accommodation_request(
            registration=stale, answer="YES", categories=["QUIET_SPACE"], note="Changed."
        )
    request = AccommodationRequest.objects.get(registration=draft)
    assert request.categories == ["OTHER"] and request.note_encrypted == NOTE


def test_a_stale_draft_instance_cannot_reset_a_withdrawal(draft, person) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    stale = Registration.objects.get(pk=draft.pk)
    _submit(Registration.objects.get(pk=draft.pk), sensitive_data_consent_granted=True)
    assert withdraw_accommodation_consent(registration=draft, person=person)
    with pytest.raises(RegistrationNotDraft):
        save_accommodation_request(
            registration=stale, answer="YES", categories=["OTHER"], note="Reopened."
        )
    request = AccommodationRequest.objects.get(registration=draft)
    assert request.withdrawn_at is not None
    assert request.categories == [] and request.note_encrypted == ""


def test_a_stale_empty_answer_cannot_delete_a_submitted_request(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="NO", categories=[], note="")
    stale = Registration.objects.get(pk=draft.pk)
    _submit(Registration.objects.get(pk=draft.pk))
    with pytest.raises(RegistrationNotDraft):
        save_accommodation_request(registration=stale, answer="", categories=[], note="")
    assert AccommodationRequest.objects.filter(registration=draft).exists()


# ---------------------------------------------------------------------------
# UX-F06: truthful withdrawal texts, with and without a legal hold
# ---------------------------------------------------------------------------

_EXPECTED = {
    "en": (
        "Your consent is withdrawn. The support team can no longer see your accessibility "
        "support details. They are deleted, unless a legal hold requires keeping them.",
        "The support team will no longer see your accessibility support details. They are "
        "deleted, unless a legal hold requires keeping them. Your registration is not affected.",
    ),
    "fr": (
        "Votre consentement est retiré. L'équipe d'assistance ne peut plus voir vos "
        "informations d'aide à l'accessibilité. Elles sont supprimées, sauf si une "
        "conservation à titre de preuve (legal hold) impose de les garder.",
        "L'équipe d'assistance ne verra plus vos informations d'aide à l'accessibilité. "
        "Elles sont supprimées, sauf si une conservation à titre de preuve (legal hold) "
        "impose de les garder. Votre inscription n'est pas affectée.",
    ),
    "ar": (
        "تم سحب موافقتك. لم يعد بإمكان فريق الدعم الاطلاع على تفاصيل دعم إمكانية "
        "الوصول الخاصة بك. تُحذف هذه التفاصيل، ما لم يقتضِ حفظٌ لأغراض قانونية "
        "الإبقاء عليها.",
        "لن يطّلع فريق الدعم بعد الآن على تفاصيل دعم إمكانية الوصول الخاصة بك. "
        "تُحذف هذه التفاصيل، ما لم يقتضِ حفظٌ لأغراض قانونية الإبقاء عليها. "
        "لا يتأثر تسجيلك.",
    ),
}

#: The pre-correction wording, which promised a deletion in every case.
_UNTRUE = {
    "en": ("were removed", "will be removed"),
    "fr": ("ont été supprimées", "seront supprimées"),
    "ar": ("حُذفت تفاصيل", "ستُحذف تفاصيل"),
}


@pytest.mark.parametrize("held", [False, True], ids=["no-hold", "legal-hold"])
@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_withdrawal_texts_are_truthful_and_reveal_no_hold(draft, person, language, held) -> None:
    import html

    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    _submit(draft, sensitive_data_consent_granted=True)
    if held:
        holder = OperationalUser.objects.create_user(
            email="uxc1-holder@example.com", password=PASSWORD, status=OperationalUserStatus.ACTIVE
        )
        LegalHold.objects.create(
            registration=draft,
            reason="Synthetic confidential dispute reason.",
            placed_by=holder,
            placed_at=timezone.now(),
        )
    success, confirmation = _EXPECTED[language]
    client = _participant_client("uxc1-person@example.com")
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language

    page = html.unescape(client.get(reverse("registrations:workspace")).content.decode())
    assert confirmation in page
    assert not any(claim in page for claim in _UNTRUE[language])

    response = client.post(reverse("registrations:withdraw-accommodation", kwargs={"pk": draft.pk}))
    assert response.status_code == 302
    shown = [str(message) for message in get_messages(response.wsgi_request)]
    assert shown == [success]  # the same text whether or not a hold applies
    assert "dispute" not in success.lower() and "Synthetic" not in success

    request = AccommodationRequest.objects.get(registration=draft)
    assert request.withdrawn_at is not None
    if held:
        assert request.note_encrypted == NOTE  # kept, hidden, as the hold requires
    else:
        assert request.note_encrypted == "" and request.categories == []
