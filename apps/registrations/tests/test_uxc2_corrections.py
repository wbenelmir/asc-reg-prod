"""UX-C2 regressions: strict sensitive consent (A), authoritative phone
validation at submission (B), Registration-first wizard writes (C, sequential
part; the two-connection races are in `tests/concurrency/test_uxc2_wizard_races.py`)
and the approved accommodation decisions (G). Synthetic data only."""

from __future__ import annotations

import datetime
from unittest import mock

import phonenumbers
import pytest
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.audit.models import AuditEvent
from apps.core.models import Country, OutboxEvent, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.organizations.models import ProfessionalAffiliation
from apps.people.models import ContactPointType
from apps.people.services import create_contact_point, resolve_or_create_participant_for_email
from apps.privacy.models import AcceptanceRecord, ConsentRecord
from apps.privacy.selectors import effective_published_version
from apps.registrations.apps import ACCOMMODATION_SUPPORT_GROUP_NAME
from apps.registrations.forms import NoticesForm
from apps.registrations.models import (
    AccommodationRequest,
    InterestTopic,
    Registration,
    RegistrationInterest,
    RegistrationProfile,
    RegistrationPublicStatus,
    RegistrationSubmission,
)
from apps.registrations.selectors import (
    accommodation_requests_visible_to,
    has_sensitive_support_consent,
)
from apps.registrations.services import (
    IncompleteRegistrationError,
    RegistrationNotDraft,
    SensitiveConsentRequired,
    accommodation_request_for,
    advance_current_step,
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_accommodation_request,
    save_contact_step,
    save_identity_step,
    save_interests_and_accommodation_step,
    save_interests_step,
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
NOTE = "Synthetic note: a quiet seat."
SENSITIVE = "SENSITIVE_ACCOMMODATION_DATA"


def _e164(region: str, kind=phonenumbers.PhoneNumberType.MOBILE) -> str:
    number = phonenumbers.example_number_for_type(region, kind)
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


@pytest.fixture(autouse=True)
def _reference_data(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    settings.PRIVATE_STORAGE_ROOT = root
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Country.objects.get_or_create(code="IL", defaults={"name": "Excluded"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


def _event(code: str) -> EventEdition:
    now = timezone.now()
    return EventEdition.objects.create(
        code=code,
        name=f"{code} Test",
        timezone="UTC",
        starts_at=now + datetime.timedelta(days=20),
        ends_at=now + datetime.timedelta(days=21),
        status=EventEditionStatus.REGISTRATION_OPEN,
    )


@pytest.fixture
def event() -> EventEdition:
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    return _event("UXC2")


@pytest.fixture
def person():
    return resolve_or_create_participant_for_email("uxc2-person@example.com")


@pytest.fixture
def draft(event, person):
    return get_or_create_active_draft(person=person, event_edition=event)


def _submit(registration, **kwargs):
    kwargs.setdefault("data_processing_consent_granted", True)
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
        terms_version=effective_published_version("TERMS", "en"),
        session_reference="uxc2",
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


def _no_effects(registration) -> None:
    registration.refresh_from_db()
    assert registration.public_status == RegistrationPublicStatus.DRAFT
    assert not RegistrationSubmission.objects.filter(registration=registration).exists()
    assert not AcceptanceRecord.objects.filter(registration=registration).exists()
    assert not ConsentRecord.objects.filter(person=registration.person).exists()
    assert not AuditEvent.objects.filter(
        action_code="REGISTRATION_SUBMISSION_RECORDED", target_uuid=registration.pk
    ).exists()
    assert not OutboxEvent.objects.filter(
        aggregate_id=str(registration.pk), event_type="registration.submission_recorded"
    ).exists()


# ---------------------------------------------------------------------------
# A. Strict sensitive consent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "granted", [False, None, "true", "on", "False", 1, 0, 1.0, [], [True], {"granted": True}]
)
def test_only_the_boolean_true_is_a_required_sensitive_consent(draft, granted) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    with pytest.raises(SensitiveConsentRequired):
        _submit(draft, sensitive_data_consent_granted=granted)
    _no_effects(draft)


@pytest.mark.parametrize("granted", ["on", 1, None])
def test_a_non_boolean_decision_is_refused_even_when_not_required(draft, granted) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="NO", categories=[], note="")
    with pytest.raises(SensitiveConsentRequired):
        _submit(draft, sensitive_data_consent_granted=granted)
    _no_effects(draft)


def test_the_boolean_true_records_the_required_consent(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    submission = _submit(draft, sensitive_data_consent_granted=True)
    assert ConsentRecord.objects.filter(purpose__code=SENSITIVE, action="GRANTED").count() == 1
    assert submission.snapshot_json["accommodation"]["sensitive_support_consent_recorded"] is True


def test_a_completed_submission_is_returned_to_any_retry(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    first = _submit(draft, sensitive_data_consent_granted=True)
    retry = _submit(draft, sensitive_data_consent_granted="garbage")
    assert retry.pk == first.pk
    assert ConsentRecord.objects.filter(purpose__code=SENSITIVE).count() == 1


# ---------------------------------------------------------------------------
# B. The submission guard derives the country from the stored number
# ---------------------------------------------------------------------------


def _declare(draft, value: str, region: str | None) -> None:
    contact = create_contact_point(
        person=draft.person,
        contact_type=ContactPointType.MOBILE,
        raw_value=value,
        country_code_id=region,
    )
    RegistrationProfile.objects.filter(registration=draft).update(declared_mobile_contact=contact)


def test_an_excluded_number_carrying_allowed_metadata_is_refused(draft) -> None:
    walk_draft_through_every_step(draft)
    _declare(draft, _e164("IL"), "DZ")  # the metadata claims an allowed country
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "contact"
    _no_effects(draft)


@pytest.mark.parametrize(("value_region", "metadata"), [("DZ", "DZ"), ("DZ", None), ("FR", "DZ")])
def test_an_allowed_number_is_accepted_whatever_its_metadata(draft, value_region, metadata) -> None:
    walk_draft_through_every_step(draft)
    _declare(draft, _e164(value_region), metadata)
    assert _submit(draft).registration_id == draft.pk


@pytest.mark.parametrize(
    "value",
    [
        "+2130000",  # too short, invalid
        "0551234567",  # not E.164: inconsistent stored value
        _e164("DZ", phonenumbers.PhoneNumberType.FIXED_LINE),  # not a mobile number
        "",
    ],
)
def test_an_invalid_or_non_mobile_stored_number_is_refused(draft, value) -> None:
    walk_draft_through_every_step(draft)
    _declare(draft, value or "+", "DZ")
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "contact"
    _no_effects(draft)


def test_a_country_that_is_no_longer_available_is_refused(draft) -> None:
    walk_draft_through_every_step(draft, mobile_number="0612345678", mobile_country_code_id="FR")
    Country.objects.filter(code="FR").update(is_active=False)
    with pytest.raises(IncompleteRegistrationError) as excinfo:
        _submit(draft)
    assert excinfo.value.step == "contact"


def test_a_completed_record_is_never_rewritten_or_rechecked(draft) -> None:
    walk_draft_through_every_step(draft)
    first = _submit(draft)
    profile = RegistrationProfile.objects.get(registration=draft)
    contact = profile.declared_mobile_contact
    type(contact).objects.filter(pk=contact.pk).update(country_code_id="IL")
    assert _submit(draft).pk == first.pk
    contact.refresh_from_db()
    assert contact.country_code_id == "IL"  # untouched metadata; nothing rewritten


# ---------------------------------------------------------------------------
# C. Registration-first wizard writes (sequential stale instances)
# ---------------------------------------------------------------------------


def _submitted_with_stale(draft):
    walk_draft_through_every_step(draft)
    stale = Registration.objects.get(pk=draft.pk)
    submission = _submit(Registration.objects.get(pk=draft.pk))
    assert stale.public_status == RegistrationPublicStatus.DRAFT
    return stale, submission


def test_a_stale_identity_write_changes_nothing(draft) -> None:
    stale, _submission = _submitted_with_stale(draft)
    with pytest.raises(RegistrationNotDraft):
        save_identity_step(
            registration=stale,
            given_names="Changed",
            family_name="Name",
            date_of_birth=datetime.date(1991, 2, 2),
            nationality_code_id="DZ",
            country_of_residence_id="DZ",
            identity_path="NIN",
            nin_value="987654321098765432",
        )
    profile = RegistrationProfile.objects.get(registration=draft)
    assert profile.submitted_given_names == "Amine"
    assert draft.person.identity_identifiers.count() == 1


def test_a_stale_contact_write_changes_nothing(draft) -> None:
    stale, _submission = _submitted_with_stale(draft)
    before = RegistrationProfile.objects.get(registration=draft).declared_mobile_contact_id
    with pytest.raises(RegistrationNotDraft):
        save_contact_step(
            registration=stale, mobile_number="0661234567", mobile_country_code_id="DZ"
        )
    assert RegistrationProfile.objects.get(registration=draft).declared_mobile_contact_id == before
    assert draft.person.contact_points.filter(type=ContactPointType.MOBILE).count() == 1


def test_a_stale_professional_write_changes_nothing(draft) -> None:
    stale, submission = _submitted_with_stale(draft)
    with pytest.raises(RegistrationNotDraft):
        save_professional_step(
            registration=stale,
            organization_name="Changed Organization",
            organization_type="COMPANY",
            job_title="Changed Title",
            sector_code_id="TECH",
            country_code_id="DZ",
            operating_scope="NATIONAL",
        )
    affiliation = ProfessionalAffiliation.objects.get(registration=draft)
    assert affiliation.job_title == submission.snapshot_json["professional"]["job_title"]


def test_a_stale_interests_write_changes_nothing(draft) -> None:
    stale, submission = _submitted_with_stale(draft)
    other = InterestTopic.objects.create(
        event_edition=draft.event_edition, code="OTHERTOPIC", label="Other"
    )
    with pytest.raises(RegistrationNotDraft):
        save_interests_step(registration=stale, interest_topic_ids=[other.pk], objectives_text="X")
    codes = list(
        RegistrationInterest.objects.filter(registration=draft).values_list(
            "interest_topic__code", flat=True
        )
    )
    assert codes == submission.snapshot_json["interests"]


def test_a_stale_step_advance_changes_nothing(draft) -> None:
    walk_draft_through_every_step(draft)
    Registration.objects.filter(pk=draft.pk).update(current_step="identity")
    stale = Registration.objects.get(pk=draft.pk)
    _submit(Registration.objects.get(pk=draft.pk))
    advance_current_step(stale, "notices")
    assert Registration.objects.get(pk=draft.pk).current_step == "identity"
    assert stale.current_step == "identity"


def test_the_combined_interests_step_is_all_or_nothing(draft) -> None:
    walk_draft_through_every_step(draft)
    other = InterestTopic.objects.create(
        event_edition=draft.event_edition, code="OTHERTOPIC", label="Other"
    )
    from apps.registrations.services import AccommodationValidationError

    with pytest.raises(AccommodationValidationError):
        save_interests_and_accommodation_step(
            registration=draft,
            interest_topic_ids=[other.pk],
            objectives_text="New objectives.",
            accommodation_answer="MAYBE",  # out of contract: the whole POST rolls back
            accommodation_categories=[],
            accommodation_note="",
        )
    assert not RegistrationInterest.objects.filter(interest_topic=other).exists()
    assert (
        RegistrationProfile.objects.get(registration=draft).objectives_text
        == "Meet investors and mentors."
    )


def test_the_combined_interests_step_refuses_a_stale_instance(draft) -> None:
    stale, submission = _submitted_with_stale(draft)
    with pytest.raises(RegistrationNotDraft):
        save_interests_and_accommodation_step(
            registration=stale,
            interest_topic_ids=[],
            objectives_text="Changed.",
            accommodation_answer="YES",
            accommodation_categories=["OTHER"],
            accommodation_note=NOTE,
        )
    assert not AccommodationRequest.objects.filter(registration=draft).exists()
    assert (
        RegistrationProfile.objects.get(registration=draft).objectives_text
        == submission.snapshot_json["objectives_text"]
    )


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


@pytest.mark.parametrize(
    ("url_name", "data"),
    [
        (
            "registrations:step-contact",
            {"mobile_country_code": "DZ", "mobile_number": "0661234567"},
        ),
        (
            "registrations:step-professional",
            {
                "organization_name": "Changed Organization",
                "organization_type": "COMPANY",
                "job_title": "Changed Title",
                "sector": "TECH",
                "country_code": "DZ",
                "operating_scope": "NATIONAL",
            },
        ),
    ],
)
def test_a_stale_view_post_goes_to_the_workspace_not_a_500(draft, url_name, data) -> None:
    """The view checked DRAFT, then the submission committed: the service's
    locked recheck refuses and the view maps it to the workspace."""
    client = _participant_client("uxc2-person@example.com")
    stale, _submission = _submitted_with_stale(draft)
    with mock.patch("apps.registrations.views._require_open_draft", return_value=(stale, None)):
        response = client.post(reverse(url_name), data)
    assert response.status_code == 302
    assert response["Location"] == reverse("registrations:workspace")


# ---------------------------------------------------------------------------
# G. Approved accommodation decisions
# ---------------------------------------------------------------------------


def test_a_detail_free_yes_without_consent_is_hidden_but_kept_for_its_owner(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=[], note="")
    submission = _submit(draft)
    accommodation = submission.snapshot_json["accommodation"]
    assert accommodation["sensitive_data_provided"] is False
    assert accommodation["sensitive_support_consent_recorded"] is False
    coordinator = _coordinator("uxc2-coord-1@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0
    assert accommodation_request_for(draft).answer == "YES"  # the owner's own view
    assert not ConsentRecord.objects.filter(purpose__code=SENSITIVE).exists()


def test_a_voluntary_consent_for_a_detail_free_yes_is_recorded_and_shown(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=[], note="")
    submission = _submit(draft, sensitive_data_consent_granted=True)
    accommodation = submission.snapshot_json["accommodation"]
    assert accommodation["sensitive_data_provided"] is False  # a distinct fact
    assert accommodation["sensitive_support_consent_recorded"] is True
    assert ConsentRecord.objects.filter(purpose__code=SENSITIVE, action="GRANTED").count() == 1
    coordinator = _coordinator("uxc2-coord-2@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 1
    assert has_sensitive_support_consent(draft)
    # Withdrawal stays registration-scoped and hides the request.
    assert withdraw_accommodation_consent(registration=draft, person=draft.person)
    assert accommodation_requests_visible_to(coordinator).count() == 0


@pytest.mark.parametrize("answer", ["NO", "PREFER_NOT_TO_SAY"])
def test_a_consent_flag_without_a_yes_answer_records_nothing(draft, answer) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer=answer, categories=[], note="")
    submission = _submit(draft, sensitive_data_consent_granted=True)
    assert submission.snapshot_json["accommodation"]["sensitive_support_consent_recorded"] is False
    assert not ConsentRecord.objects.filter(purpose__code=SENSITIVE).exists()


def _strip_consent_key(submission) -> None:
    """Rewrite a test snapshot into the UX-C1 shape (no consent key)."""
    snapshot = dict(submission.snapshot_json)
    snapshot["accommodation"] = {
        key: value
        for key, value in snapshot["accommodation"].items()
        if key != "sensitive_support_consent_recorded"
    }
    RegistrationSubmission.objects.filter(pk=submission.pk).update(snapshot_json=snapshot)


def test_the_compatibility_rule_keeps_consented_uxc1_submissions_visible(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    _strip_consent_key(_submit(draft, sensitive_data_consent_granted=True))
    coordinator = _coordinator("uxc2-coord-3@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 1


def test_the_compatibility_rule_never_qualifies_a_detail_free_uxc1_submission(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=[], note="")
    _strip_consent_key(_submit(draft))
    coordinator = _coordinator("uxc2-coord-4@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_a_new_snapshot_is_never_read_through_the_compatibility_rule(draft, event) -> None:
    """With the key present and false, `sensitive_data_provided` is not consent."""
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    submission = _submit(draft, sensitive_data_consent_granted=True)
    snapshot = dict(submission.snapshot_json)
    snapshot["accommodation"] = dict(
        snapshot["accommodation"], sensitive_support_consent_recorded=False
    )
    RegistrationSubmission.objects.filter(pk=submission.pk).update(snapshot_json=snapshot)
    coordinator = _coordinator("uxc2-coord-5@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_another_registrations_consent_is_never_borrowed(person) -> None:
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    event_a = _event("UXC2A")
    context_a = get_or_create_active_draft(person=person, event_edition=event_a)
    walk_draft_through_every_step(context_a)
    save_accommodation_request(registration=context_a, answer="YES", categories=[], note="")
    _submit(context_a, sensitive_data_consent_granted=True)
    event_a.status = EventEditionStatus.REGISTRATION_CLOSED
    event_a.save(update_fields=["status"])
    event_b = _event("UXC2B")
    context_b = get_or_create_active_draft(person=person, event_edition=event_b)
    walk_draft_through_every_step(context_b)
    save_accommodation_request(registration=context_b, answer="YES", categories=[], note="")
    _submit(context_b)
    coordinator = _coordinator("uxc2-coord-6@example.com", event_a, event_b)
    visible = [item.registration_id for item in accommodation_requests_visible_to(coordinator)]
    assert visible == [context_a.pk]
    assert not has_sensitive_support_consent(context_b)


def test_legacy_only_accessibility_text_stays_hidden(draft, event) -> None:
    walk_draft_through_every_step(draft)
    RegistrationProfile.objects.filter(registration=draft).update(
        accessibility_needs_text="Legacy synthetic text."
    )
    _submit(draft)
    coordinator = _coordinator("uxc2-coord-7@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_the_notices_form_offers_an_optional_consent_for_a_detail_free_yes() -> None:
    form = NoticesForm(offers_optional_sensitive_consent=True)
    field = form.fields["accept_sensitive_data"]
    assert not field.required
    assert "checked" not in str(form["accept_sensitive_data"])
    assert "accept_sensitive_data" not in NoticesForm().fields
    required = NoticesForm(requires_sensitive_consent=True, offers_optional_sensitive_consent=True)
    assert required.fields["accept_sensitive_data"].required


def test_the_notices_page_offers_the_optional_consent_and_records_it(draft, event) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=[], note="")
    client = _participant_client("uxc2-person@example.com")
    page = client.get(reverse("registrations:step-notices")).content.decode()
    assert 'name="accept_sensitive_data"' in page
    assert "Optional. Without this consent" in page
    response = client.post(
        reverse("registrations:step-notices"),
        notices_post_data(accept_sensitive_data="on"),
    )
    assert response.status_code == 302
    submission = RegistrationSubmission.objects.get(registration=draft)
    assert submission.snapshot_json["accommodation"]["sensitive_support_consent_recorded"] is True
    coordinator = _coordinator("uxc2-coord-8@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 1
