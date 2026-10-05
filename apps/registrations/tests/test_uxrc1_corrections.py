"""UXR-C1 regressions (UX-R findings UXR-F02 to UXR-F06 and the developer's
support-visibility decision). UXR-F01 is a browser behaviour: see
`tests/browser/test_uxrc1_identity_panels_and_legal.py`. Synthetic data only."""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.audit.models import AuditEvent
from apps.core.forms import localized_sort_key
from apps.core.models import Country, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.people.services import calling_code_label, resolve_or_create_participant_for_email
from apps.privacy.models import (
    AcceptanceRecord,
    ConsentRecord,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
    LegalHold,
)
from apps.privacy.selectors import effective_published_version
from apps.registrations.apps import ACCOMMODATION_SUPPORT_GROUP_NAME
from apps.registrations.forms import (
    ContactStepForm,
    IdentityStepForm,
    InterestsStepForm,
    ProfessionalStepForm,
)
from apps.registrations.models import (
    AccommodationRequest,
    InterestTopic,
    Registration,
    RegistrationInterest,
    RegistrationProfile,
)
from apps.registrations.selectors import accommodation_requests_visible_to
from apps.registrations.services import (
    OBJECTIVES_MAX_LENGTH,
    IncompleteRegistrationError,
    get_or_create_active_draft,
    initial_submission_operation_key,
    require_complete_registration,
    save_accommodation_request,
    save_contact_step,
    save_interests_and_accommodation_step,
    save_interests_step,
    submit_full_registration,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db

PASSWORD = "__test_password__"  # noqa: S105
NOTE = "Synthetic note: a ramp at the entrance."
LTR_ISOLATE, POP_ISOLATE = "⁦", "⁩"

COUNTRIES = (
    ("DZ", "Algeria", "Algérie", "الجزائر"),
    ("FR", "France", "France", "فرنسا"),
    ("DE", "Germany", "Allemagne", "ألمانيا"),
    ("ZA", "South Africa", "Afrique du Sud", "جنوب أفريقيا"),
    ("AE", "United Arab Emirates", "Émirats arabes unis", "الإمارات العربية المتحدة"),
    ("ES", "Spain", "Espagne", "إسبانيا"),
    ("EG", "Egypt", "Égypte", "مصر"),
    ("IL", "Israel", "Israël", "إسرائيل"),
)


@pytest.fixture(autouse=True)
def _reference_data(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir(exist_ok=True)
    settings.PRIVATE_STORAGE_ROOT = root
    for code, name, name_fr, name_ar in COUNTRIES:
        Country.objects.update_or_create(
            code=code, defaults={"name": name, "name_fr": name_fr, "name_ar": name_ar}
        )
    # The ordering tests below list every selectable country. Since the
    # approved catalog (core.0005) is installed, keep only this module's
    # countries selectable, inside the test transaction.
    Country.objects.exclude(code__in=[row[0] for row in COUNTRIES]).update(is_active=False)
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
    return _event("UXRC1")


@pytest.fixture
def person():
    return resolve_or_create_participant_for_email("uxrc1-person@example.com")


@pytest.fixture
def draft(event, person):
    return get_or_create_active_draft(person=person, event_edition=event)


def _submit(registration, **kwargs):
    kwargs.setdefault("data_processing_consent_granted", True)
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
        terms_version=effective_published_version("TERMS", "en"),
        session_reference="uxrc1",
        idempotency_key=initial_submission_operation_key(registration.pk),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# UXR-F02: public, read-only legal information
# ---------------------------------------------------------------------------

LEGAL_URL = "/legal/"
_DRAFT_LABEL = {"en": "[DRAFT v2", "fr": "[BROUILLON v2", "ar": "[مسودة v2"}
#: Owner correction (2026-10-04): the current official versions (privacy.0005).
CURRENT_LABEL = "v3"


def _record_counts():
    return (
        AcceptanceRecord.objects.count(),
        ConsentRecord.objects.count(),
        AuditEvent.objects.count(),
    )


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_legal_page_is_public_read_only_and_never_indexed_or_cached(language) -> None:
    before = _record_counts()
    response = Client().get(LEGAL_URL, HTTP_ACCEPT_LANGUAGE=language)
    assert reverse("privacy:legal-information") == LEGAL_URL
    assert response.status_code == 200
    assert response["X-Robots-Tag"] == "noindex, nofollow"
    assert response["Cache-Control"] == "private, no-store"
    html = response.content.decode()
    assert '<meta name="robots" content="noindex, nofollow">' in html
    # Both current published versions, in the page language, exactly as
    # published (unresolved markers included). Since the owner correction of
    # 2026-10-04 they are the official v3 versions: no draft label any more.
    for code in ("PRIVACY_NOTICE", "TERMS"):
        version = effective_published_version(code, language)
        assert version is not None and version.content.splitlines()[0] in html.replace(
            "&#x27;", "'"
        )
        assert version.version_label == CURRENT_LABEL
    assert _DRAFT_LABEL[language] not in html
    assert html.count(f'<bdi dir="ltr">{CURRENT_LABEL}</bdi>') == 2
    assert f'lang="{language}"' in html
    assert "data-legal-fallback" not in html and "data-legal-unavailable" not in html
    assert "approved by the ANPDP" not in html.replace("Not approved by the ANPDP", "")
    # Reading records nothing.
    assert _record_counts() == before


def test_the_legal_page_shows_the_fallback_language_honestly() -> None:
    LegalDocumentVersion.objects.filter(
        legal_document__code="PRIVACY_NOTICE", language="fr"
    ).update(status=LegalDocumentVersionStatus.RETIRED)
    html = Client().get(LEGAL_URL, HTTP_ACCEPT_LANGUAGE="fr").content.decode()
    assert "data-legal-fallback" in html
    assert "Il est affiché en English." in html
    assert '<div class="asc-legal-text" lang="en" dir="ltr">' in html
    english_privacy = effective_published_version("PRIVACY_NOTICE", "en")
    french_terms = effective_published_version("TERMS", "fr")
    unescaped = html.replace("&#x27;", "'")
    assert english_privacy.content.splitlines()[0] in unescaped  # the English privacy notice
    assert french_terms.content.splitlines()[0] in unescaped  # the French terms, in French


def test_a_missing_document_is_shown_unavailable_never_replaced() -> None:
    terms = LegalDocumentVersion.objects.filter(legal_document__code="TERMS")
    terms.update(status=LegalDocumentVersionStatus.RETIRED)
    retired_text = terms.filter(language="en").first().content.splitlines()[0]
    before = _record_counts()
    response = Client().get(LEGAL_URL, HTTP_ACCEPT_LANGUAGE="en")
    html = response.content.decode()
    assert response.status_code == 200
    assert html.count("data-legal-unavailable") == 1
    assert retired_text not in html
    assert _record_counts() == before


def test_a_future_version_is_not_shown_before_it_is_in_effect() -> None:
    current = effective_published_version("PRIVACY_NOTICE", "en")
    LegalDocumentVersion.objects.create(
        legal_document=current.legal_document,
        language="en",
        version_label="uxrc1-future",
        content="[SYNTHETIC FUTURE VERSION]",
        content_hash="0" * 64,
        effective_from=timezone.now() + datetime.timedelta(days=5),
        status=LegalDocumentVersionStatus.PUBLISHED,
    )
    html = Client().get(LEGAL_URL, HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert "[SYNTHETIC FUTURE VERSION]" not in html
    assert current.version_label in html


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_start_form_links_to_the_legal_page(language) -> None:
    """UXR-F02, as amended by A-10 (P4-4, FOOTER-01 option (a)): the start
    form keeps its legal link; the footer copy of it is removed with the rest
    of the footer content."""
    html = Client().get(reverse("accounts:otp-request"), HTTP_ACCEPT_LANGUAGE=language).content
    html = html.decode()
    assert "data-legal-link" in html
    assert html.count(f'href="{LEGAL_URL}"') == 1  # the form note only
    form_note = html[: html.index('<footer class="asc-footer">')]
    assert f'href="{LEGAL_URL}"' in form_note
    assert "data-footer-legal" not in html


def test_the_legal_page_accepts_get_only() -> None:
    assert Client().post(LEGAL_URL).status_code == 405


# ---------------------------------------------------------------------------
# UXR-F03: country choices ordered by the localized label
# ---------------------------------------------------------------------------


def _choice_labels(field) -> list[str]:
    return [str(label) for value, label in field.choices if value != ""]


def test_the_sort_key_ignores_accents_case_isolates_and_hamza_forms() -> None:
    assert localized_sort_key("Émirats") == "emirats"
    assert localized_sort_key("Algérie") < localized_sort_key("Allemagne")
    assert localized_sort_key("إسبانيا")[0] == "ا"
    assert localized_sort_key(f"France {LTR_ISOLATE}(+33){POP_ISOLATE}") == "france (+33)"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            [
                "Algeria",
                "Egypt",
                "France",
                "Germany",
                "South Africa",
                "Spain",
                "United Arab Emirates",
            ],
        ),
        (
            "fr",
            [
                "Afrique du Sud",
                "Algérie",
                "Allemagne",
                "Égypte",
                "Émirats arabes unis",
                "Espagne",
                "France",
            ],
        ),
        # Hamza forms fold to bare alef, then Unicode order, which follows the
        # Arabic alphabet: "اس" before "ال".
        (
            "ar",
            [
                "إسبانيا",
                "الإمارات العربية المتحدة",
                "الجزائر",
                "ألمانيا",
                "جنوب أفريقيا",
                "فرنسا",
                "مصر",
            ],
        ),
    ],
)
def test_country_lists_follow_the_localized_label(language, expected) -> None:
    with translation.override(language):
        identity = IdentityStepForm()
        professional = ProfessionalStepForm()
        for name in ("nationality_code", "country_of_residence", "passport_country_code"):
            assert _choice_labels(identity.fields[name]) == expected
        assert _choice_labels(professional.fields["country_code"]) == expected
        rendered = str(identity["nationality_code"])
        positions = [rendered.index(f">{label}</option>") for label in expected]
        assert positions == sorted(positions)  # the native select has the same order


def test_the_calling_code_list_follows_the_localized_label() -> None:
    with translation.override("fr"):
        labels = _choice_labels(ContactStepForm().fields["mobile_country_code"])
    names = [label.split(f" {LTR_ISOLATE}")[0] for label in labels]
    assert names == [
        "Afrique du Sud",
        "Algérie",
        "Allemagne",
        "Égypte",
        "Émirats arabes unis",
        "Espagne",
        "France",
    ]


def test_ordering_leaves_membership_and_exclusions_unchanged() -> None:
    with translation.override("en"):
        form = IdentityStepForm()
    values = [value for value, _label in form.fields["nationality_code"].choices if value != ""]
    assert "IL" not in values and len(values) == 7
    bound = IdentityStepForm({"nationality_code": "IL"})
    bound.is_valid()
    assert "nationality_code" in bound.errors
    bound = IdentityStepForm({"nationality_code": "DZ"})
    bound.is_valid()
    assert "nationality_code" not in bound.errors


def test_a_saved_selection_is_still_the_selected_option() -> None:
    with translation.override("fr"):
        rendered = str(
            IdentityStepForm(initial={"country_of_residence": "FR"})["country_of_residence"]
        )
    assert '<option value="FR" selected>' in rendered


# ---------------------------------------------------------------------------
# UXR-F04: bidirectional presentation
# ---------------------------------------------------------------------------


def test_the_calling_code_token_is_isolated_left_to_right() -> None:
    with translation.override("ar"):
        label = calling_code_label(Country.objects.get(code="FR"))
    assert label == f"فرنسا {LTR_ISOLATE}(+33){POP_ISOLATE}"


def test_free_text_areas_set_their_own_direction_and_digit_fields_stay_ltr() -> None:
    assert ProfessionalStepForm().fields["biography"].widget.attrs["dir"] == "auto"
    interests = InterestsStepForm()
    assert interests.fields["objectives_text"].widget.attrs["dir"] == "auto"
    assert interests.fields["accommodation_note"].widget.attrs["dir"] == "auto"
    from apps.accounts.forms import OtpVerifyForm

    assert OtpVerifyForm().fields["code"].widget.attrs["dir"] == "ltr"
    identity = str(IdentityStepForm()["date_of_birth"])
    assert 'dir="ltr"' in identity


def test_no_isolation_character_reaches_a_stored_phone(draft) -> None:
    walk_draft_through_every_step(draft)
    form = ContactStepForm({"mobile_country_code": "FR", "mobile_number": "06 12 34 56 78"})
    assert form.is_valid(), form.errors
    save_contact_step(
        registration=draft,
        mobile_number=form.cleaned_data["mobile_number"],
        mobile_country_code_id=form.cleaned_data["mobile_country_code"].pk,
    )
    contact = RegistrationProfile.objects.get(registration=draft).declared_mobile_contact
    assert contact.value_encrypted == "+33612345678"
    assert contact.country_code_id == "FR"
    for value in (contact.value_encrypted, contact.masked_value, contact.country_code_id):
        assert LTR_ISOLATE not in value and POP_ISOLATE not in value


# ---------------------------------------------------------------------------
# UXR-F05: Arabic Latin-letter wording
# ---------------------------------------------------------------------------


def test_the_arabic_latin_letter_messages_use_the_approved_wording() -> None:
    from django.utils.translation import gettext

    with translation.override("ar"):
        name_message = gettext("Use Latin letters only (accents are allowed).")
        text_message = gettext(
            "Use Latin letters, digits and common punctuation only (accents are allowed)."
        )
    assert name_message == "استخدم الحروف اللاتينية فقط؛ يُسمح بالحروف ذات العلامات مثل é."
    assert text_message == (
        "استخدم الحروف اللاتينية والأرقام وعلامات الترقيم الشائعة فقط؛ "
        "يُسمح بالحروف ذات العلامات مثل é."
    )
    assert "التشكيل" not in name_message + text_message


def test_the_latin_rule_itself_is_unchanged() -> None:
    with translation.override("ar"):
        form = IdentityStepForm({"given_names": "أمينة", "family_name": "Zoé"})
        form.is_valid()
    assert "given_names" in form.errors and "family_name" not in form.errors


# ---------------------------------------------------------------------------
# UXR-F06: authoritative interests and objectives
# ---------------------------------------------------------------------------


def _topic(event, code, **extra) -> InterestTopic:
    return InterestTopic.objects.create(event_edition=event, code=code, label=code.title(), **extra)


def _state(draft):
    return (
        sorted(
            RegistrationInterest.objects.filter(registration=draft).values_list(
                "interest_topic__code", flat=True
            )
        ),
        RegistrationProfile.objects.get(registration=draft).objectives_text,
    )


@pytest.mark.parametrize("problem", ["foreign", "inactive", "unknown", "malformed"])
def test_an_invalid_topic_refuses_the_whole_request(draft, problem) -> None:
    walk_draft_through_every_step(draft)
    before = _state(draft)
    valid = _topic(draft.event_edition, "UXRC1VALID")
    bad = {
        "foreign": lambda: _topic(_event("UXRC1B"), "FOREIGN").pk,
        "inactive": lambda: _topic(draft.event_edition, "UXRC1OFF", is_active=False).pk,
        "unknown": lambda: "00000000-0000-7000-8000-000000000000",
        "malformed": lambda: "not-a-topic-id",
    }[problem]()
    with pytest.raises(ValidationError):
        save_interests_step(
            registration=draft, interest_topic_ids=[valid.pk, bad], objectives_text="Changed."
        )
    assert _state(draft) == before  # nothing saved, not even the valid subset


def test_the_combined_step_rolls_back_the_accommodation_part_too(draft) -> None:
    walk_draft_through_every_step(draft)
    foreign = _topic(_event("UXRC1C"), "FOREIGN")
    with pytest.raises(ValidationError):
        save_interests_and_accommodation_step(
            registration=draft,
            interest_topic_ids=[foreign.pk],
            objectives_text="Changed.",
            accommodation_answer="YES",
            accommodation_categories=["OTHER"],
            accommodation_note=NOTE,
        )
    assert not AccommodationRequest.objects.filter(registration=draft).exists()


def test_valid_topics_and_normalized_objectives_are_saved(draft) -> None:
    walk_draft_through_every_step(draft)
    first = _topic(draft.event_edition, "UXRC1A")
    second = _topic(draft.event_edition, "UXRC1B")
    save_interests_step(
        registration=draft,
        interest_topic_ids=[str(first.pk), second.pk, first.pk],
        objectives_text="  Meet\r\ninvestors.  ",
    )
    assert _state(draft) == (["UXRC1A", "UXRC1B"], "Meet\ninvestors.")


def test_objectives_over_the_limit_are_refused_by_the_service(draft) -> None:
    walk_draft_through_every_step(draft)
    before = _state(draft)
    with pytest.raises(ValidationError):
        save_interests_step(
            registration=draft,
            interest_topic_ids=[],
            objectives_text="é" * (OBJECTIVES_MAX_LENGTH + 1),
        )
    assert _state(draft) == before
    # Exactly the limit, counted after CR LF becomes LF, is accepted.
    text = ("a\r\n" * 1000)[: OBJECTIVES_MAX_LENGTH + 500]
    save_interests_step(registration=draft, interest_topic_ids=[], objectives_text=text)
    assert len(_state(draft)[1]) <= OBJECTIVES_MAX_LENGTH


def test_the_form_counts_objectives_like_the_service() -> None:
    topic_queryset = InterestTopic.objects.none()
    long_crlf = "a\r\n" * 700  # 2100 raw characters, 1399 after normalization
    form = InterestsStepForm({"objectives_text": long_crlf}, interest_topic_queryset=topic_queryset)
    form.is_valid()
    assert "objectives_text" not in form.errors
    form = InterestsStepForm(
        {"objectives_text": "b" * (OBJECTIVES_MAX_LENGTH + 1)},
        interest_topic_queryset=topic_queryset,
    )
    form.is_valid()
    assert "objectives_text" in form.errors


@pytest.mark.parametrize("problem", ["foreign", "inactive", "blank_objectives"])
def test_the_submission_guard_refuses_a_nonconforming_saved_draft(draft, problem) -> None:
    walk_draft_through_every_step(draft)
    interest = RegistrationInterest.objects.filter(registration=draft).first()
    if problem == "foreign":
        RegistrationInterest.objects.filter(pk=interest.pk).update(
            interest_topic=_topic(_event("UXRC1D"), "FOREIGN")
        )
    elif problem == "inactive":
        InterestTopic.objects.filter(pk=interest.interest_topic_id).update(is_active=False)
    else:
        # The column holds at most 2000 characters, so the over-limit case can
        # only be proven at the service; blank text after normalization is
        # the guard's own case.
        RegistrationProfile.objects.filter(registration=draft).update(objectives_text=" \r\n ")
    with pytest.raises(IncompleteRegistrationError) as raised:
        require_complete_registration(
            Registration.objects.get(pk=draft.pk),
            privacy_notice_version=effective_published_version("PRIVACY_NOTICE", "en"),
            terms_version=effective_published_version("TERMS", "en"),
        )
    assert raised.value.step == "interests"


def test_a_completed_record_is_never_rechecked(draft) -> None:
    walk_draft_through_every_step(draft)
    submission = _submit(draft)
    interest = RegistrationInterest.objects.filter(registration=draft).first()
    InterestTopic.objects.filter(pk=interest.interest_topic_id).update(is_active=False)
    assert _submit(Registration.objects.get(pk=draft.pk)).pk == submission.pk


def test_the_view_turns_a_service_refusal_into_a_form_error(event) -> None:
    from django.test import override_settings

    from apps.accounts.otp import DeterministicTestOtpGenerator
    from apps.core.testing import otp_request_data

    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(
            reverse("accounts:otp-request"),
            otp_request_data("uxrc1-view@example.com", client=client),
        )
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    client.get(reverse("registrations:step-identity"))
    registration = Registration.objects.get(person__isnull=False, event_edition=event)
    walk_draft_through_every_step(registration)
    topic = InterestTopic.objects.filter(event_edition=event).first()
    with mock.patch(
        "apps.registrations.views.save_interests_and_accommodation_step",
        side_effect=ValidationError("changed meanwhile"),
    ):
        response = client.post(
            reverse("registrations:step-interests"),
            {"interest_topics": [str(topic.pk)], "objectives_text": "Meet investors."},
        )
    assert response.status_code == 200
    assert "error-summary" in response.content.decode()


# ---------------------------------------------------------------------------
# Developer decision: no support visibility after withdrawal or cancellation
# ---------------------------------------------------------------------------


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


def _consented_request(draft):
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    _submit(draft, sensitive_data_consent_granted=True)
    return Registration.objects.get(pk=draft.pk)


def test_a_consented_request_is_listed_until_the_registration_is_withdrawn(draft, person, event):
    from apps.reviews.services import withdraw_registration

    registration = _consented_request(draft)
    coordinator = _coordinator("uxrc1-support@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 1
    withdraw_registration(
        registration=registration, person=person, expected_version=registration.version
    )
    assert accommodation_requests_visible_to(coordinator).count() == 0
    kept = AccommodationRequest.objects.get(registration=draft)
    assert kept.categories == ["OTHER"] and kept.note_encrypted == NOTE  # hidden, not deleted


def test_an_operationally_cancelled_registration_is_hidden_from_support(draft, event) -> None:
    from apps.reviews.services import cancel_registration_operationally

    registration = _consented_request(draft)
    coordinator = _coordinator("uxrc1-support2@example.com", event)
    cancel_registration_operationally(
        registration=registration,
        expected_version=registration.version,
        reason="Synthetic operational reason.",
        actor=coordinator,
    )
    assert accommodation_requests_visible_to(coordinator).count() == 0
    assert AccommodationRequest.objects.filter(registration=draft).exists()


@pytest.mark.parametrize("field", ["withdrawn_at", "cancelled_at"])
def test_either_timestamp_alone_hides_the_request(draft, event, field) -> None:
    _consented_request(draft)
    coordinator = _coordinator(f"uxrc1-{field}@example.com", event)
    Registration.objects.filter(pk=draft.pk).update(**{field: timezone.now()})
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_a_legal_hold_keeps_the_data_of_a_withdrawn_registration_hidden(draft, person, event):
    from apps.reviews.services import withdraw_registration

    registration = _consented_request(draft)
    holder = _coordinator("uxrc1-holder@example.com")
    LegalHold.objects.create(
        registration=draft,
        reason="Synthetic confidential dispute reason.",
        placed_by=holder,
        placed_at=timezone.now(),
    )
    withdraw_registration(
        registration=registration, person=person, expected_version=registration.version
    )
    coordinator = _coordinator("uxrc1-support3@example.com", event)
    assert accommodation_requests_visible_to(coordinator).count() == 0
    kept = AccommodationRequest.objects.get(registration=draft)
    assert kept.note_encrypted == NOTE and kept.withdrawn_at is None


def test_the_support_screen_no_longer_renders_a_withdrawn_registrations_note(
    draft, person, event
) -> None:
    from apps.reviews.services import withdraw_registration

    registration = _consented_request(draft)
    _coordinator("uxrc1-screen@example.com", event)
    client = Client()
    client.post(
        reverse("accounts:operational-sign-in"),
        {"email": "uxrc1-screen@example.com", "password": PASSWORD},
    )
    assert NOTE in client.get(reverse("registrations:ops-accommodation-list")).content.decode()
    withdraw_registration(
        registration=registration, person=person, expected_version=registration.version
    )
    assert NOTE not in client.get(reverse("registrations:ops-accommodation-list")).content.decode()


def test_the_owner_keeps_access_to_their_request_after_withdrawing(draft, person, event) -> None:
    """Hiding from support never takes the request away from its owner: the
    owner still reads it, and can still withdraw the sensitive consent, which
    then clears the details as before."""
    from django.test import override_settings

    from apps.accounts.otp import DeterministicTestOtpGenerator
    from apps.core.testing import otp_request_data
    from apps.registrations.services import accommodation_request_for
    from apps.reviews.services import withdraw_registration

    registration = _consented_request(draft)
    withdraw_registration(
        registration=registration, person=person, expected_version=registration.version
    )
    own = accommodation_request_for(Registration.objects.get(pk=draft.pk))
    assert own is not None and own.categories == ["OTHER"] and own.note_encrypted == NOTE

    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(
            reverse("accounts:otp-request"),
            otp_request_data("uxrc1-person@example.com", client=client),
        )
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    withdraw_url = reverse("registrations:withdraw-accommodation", kwargs={"pk": draft.pk})
    assert withdraw_url in client.get(reverse("registrations:workspace")).content.decode()
    client.post(withdraw_url)
    cleared = AccommodationRequest.objects.get(registration=draft)
    assert cleared.withdrawn_at is not None
    assert cleared.categories == [] and cleared.note_encrypted == ""


def test_hiding_is_registration_specific_and_keeps_the_scope(draft, person, event) -> None:
    """The same person's other, still active registration stays listed through
    its own consent; the scoped permission still decides who sees it."""
    from apps.reviews.services import withdraw_registration

    first = _consented_request(draft)
    other_event = _event("UXRC1E")
    second = _consented_request(
        get_or_create_active_draft(person=person, event_edition=other_event)
    )
    both = _coordinator("uxrc1-both@example.com", event, other_event)
    first_only = _coordinator("uxrc1-first@example.com", event)
    assert accommodation_requests_visible_to(both).count() == 2
    withdraw_registration(registration=first, person=person, expected_version=first.version)
    assert list(
        accommodation_requests_visible_to(both).values_list("registration_id", flat=True)
    ) == [second.pk]
    assert accommodation_requests_visible_to(first_only).count() == 0
    assert AccommodationRequest.objects.filter(registration=first).exists()
