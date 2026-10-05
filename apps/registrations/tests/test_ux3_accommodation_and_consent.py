"""UX-3: voluntary accommodation request, explicit consents and legal drafts
(M21, M22; decisions D-10, D-11, C-08, S-15, S-18). Synthetic data only."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.checks import run_checks
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import participant_auth
from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership
from apps.audit.models import AuditEvent
from apps.core.models import Country, Sector
from apps.events.models import EventEdition, EventEditionStatus
from apps.people.services import resolve_or_create_participant_for_email
from apps.privacy.checks import legal_release_blockers
from apps.privacy.models import (
    ConsentPurpose,
    ConsentRecord,
    LegalDocumentVersion,
    LegalHold,
)
from apps.privacy.selectors import effective_published_version
from apps.registrations.apps import ACCOMMODATION_SUPPORT_GROUP_NAME
from apps.registrations.forms import InterestsStepForm, NoticesForm
from apps.registrations.models import AccommodationRequest, RegistrationSubmission
from apps.registrations.selectors import accommodation_requests_visible_to
from apps.registrations.services import (
    AccommodationValidationError,
    SensitiveConsentRequired,
    get_or_create_active_draft,
    initial_submission_operation_key,
    save_accommodation_request,
    submit_full_registration,
    withdraw_accommodation_consent,
)
from apps.registrations.tests.factories import walk_draft_through_every_step

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "__test_password__"  # noqa: S105
NOTE = "Please keep a seat near the aisle."


@pytest.fixture(autouse=True)
def _reference_data():
    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})


@pytest.fixture
def event() -> EventEdition:
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    now = timezone.now()
    return EventEdition.objects.create(
        code="UX3TEST",
        name="UX-3 Test",
        timezone="UTC",
        starts_at=now + datetime.timedelta(days=20),
        ends_at=now + datetime.timedelta(days=21),
        status=EventEditionStatus.REGISTRATION_OPEN,
    )


@pytest.fixture
def draft(event):
    person = resolve_or_create_participant_for_email("ux3-person@example.com")
    return get_or_create_active_draft(person=person, event_edition=event)


def _versions():
    return (
        effective_published_version("PRIVACY_NOTICE", "en"),
        effective_published_version("TERMS", "en"),
    )


def _submit(registration, **kwargs):
    privacy, terms = _versions()
    # The required processing consent is given (UX-C1, UX-F02).
    kwargs.setdefault("data_processing_consent_granted", True)
    return submit_full_registration(
        registration=registration,
        privacy_notice_version=privacy,
        terms_version=terms,
        session_reference="ux3",
        idempotency_key=initial_submission_operation_key(registration.pk),
        **kwargs,
    )


def _user(email, group=None, event=None):
    user = OperationalUser.objects.create_user(
        email=email, password=PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    if group:
        ScopedGroupMembership.objects.create(
            user=user, group=Group.objects.get(name=group), event_edition=event, granted_by=user
        )
    return user


# ---------------------------------------------------------------------------
# The accommodation request itself
# ---------------------------------------------------------------------------


def test_yes_keeps_categories_and_an_encrypted_note(draft) -> None:
    save_accommodation_request(
        registration=draft,
        answer="YES",
        categories=["QUIET_SPACE", "STEP_FREE_ACCESS"],
        note=NOTE,
    )
    request = AccommodationRequest.objects.get(registration=draft)
    assert request.categories == ["STEP_FREE_ACCESS", "QUIET_SPACE"]  # catalogue order
    assert request.note_encrypted == NOTE
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT note_encrypted FROM registrations_accommodation_request WHERE id = %s",
            [str(request.pk)],
        )
        stored = cursor.fetchone()[0]
    assert NOTE not in stored and stored


@pytest.mark.parametrize("answer", ["NO", "PREFER_NOT_TO_SAY"])
def test_other_answers_keep_no_detail(draft, answer) -> None:
    save_accommodation_request(
        registration=draft, answer=answer, categories=["SIGN_LANGUAGE"], note=NOTE
    )
    request = AccommodationRequest.objects.get(registration=draft)
    assert request.categories == [] and request.note_encrypted == ""
    assert not request.has_sensitive_data


def test_an_empty_answer_removes_the_request(draft) -> None:
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note="")
    save_accommodation_request(registration=draft, answer="", categories=[], note="")
    assert not AccommodationRequest.objects.filter(registration=draft).exists()


@pytest.mark.parametrize(
    ("answer", "categories", "note"),
    [
        ("MAYBE", [], ""),
        ("YES", ["DIAGNOSIS"], ""),
        ("YES", [], "x" * 301),
    ],
)
def test_out_of_contract_values_are_refused(draft, answer, categories, note) -> None:
    with pytest.raises(AccommodationValidationError):
        save_accommodation_request(
            registration=draft, answer=answer, categories=categories, note=note
        )


def test_the_note_limit_counts_normalized_code_points(draft) -> None:
    # 150 two-code-unit emoji are 150 code points; CR LF counts as one.
    note = "\U0001f9d1" * 150 + "\r\n" + "é" * 74  # 150 + 1 + 74 (NFC) = 225
    save_accommodation_request(registration=draft, answer="YES", categories=[], note=note)
    stored = AccommodationRequest.objects.get(registration=draft).note_encrypted
    assert len(stored) == 225 and "\r" not in stored


def test_the_legacy_free_text_is_preserved_and_never_written_again(draft) -> None:
    from apps.registrations.models import RegistrationProfile
    from apps.registrations.services import save_interests_step

    walk_draft_through_every_step(draft)
    RegistrationProfile.objects.filter(registration=draft).update(
        accessibility_needs_text="Legacy synthetic text."
    )
    save_interests_step(registration=draft, interest_topic_ids=[], objectives_text="Meet people.")
    assert (
        RegistrationProfile.objects.get(registration=draft).accessibility_needs_text
        == "Legacy synthetic text."
    )


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


def test_the_form_has_no_free_text_accessibility_field_and_no_language_preservation() -> None:
    form = InterestsStepForm()
    assert "accessibility_needs_text" not in form.fields
    for name in ("accommodation_answer", "accommodation_categories", "accommodation_note"):
        assert name in form.fields
        assert name not in form.language_preserve_fields
        assert "data-language-preserve-field" not in str(form[name])
    assert 'data-asc-char-counter="300"' in str(form["accommodation_note"])


def test_the_form_drops_details_unless_the_answer_is_yes() -> None:
    form = InterestsStepForm(
        {
            "objectives_text": "Meet people.",
            "accommodation_answer": "NO",
            "accommodation_categories": ["CAPTIONING"],
            "accommodation_note": NOTE,
        }
    )
    form.is_valid()
    assert form.cleaned_data["accommodation_categories"] == []
    assert form.cleaned_data["accommodation_note"] == ""


def test_the_form_refuses_a_note_over_300_characters() -> None:
    form = InterestsStepForm({"accommodation_answer": "YES", "accommodation_note": "y" * 301})
    assert not form.is_valid()
    assert "accommodation_note" in form.errors


def test_the_notices_form_has_three_separate_required_statements() -> None:
    form = NoticesForm()
    for name in ("accept_privacy_notice", "accept_terms", "accept_data_processing"):
        assert form.fields[name].required
    assert not form.fields["marketing_consent"].required
    assert "accept_sensitive_data" not in form.fields
    html = form.as_p()
    assert "checked" not in html  # nothing is pre-checked


def test_the_sensitive_consent_appears_only_when_needed() -> None:
    form = NoticesForm(requires_sensitive_consent=True)
    assert form.fields["accept_sensitive_data"].required
    missing = NoticesForm(
        {"accept_privacy_notice": "on", "accept_terms": "on", "accept_data_processing": "on"},
        requires_sensitive_consent=True,
    )
    assert not missing.is_valid() and "accept_sensitive_data" in missing.errors


# ---------------------------------------------------------------------------
# Submission, consent evidence and the snapshot
# ---------------------------------------------------------------------------


def test_submission_records_the_processing_consent_with_its_version(draft) -> None:
    walk_draft_through_every_step(draft)
    purpose = ConsentPurpose.objects.get(code="PERSONAL_DATA_PROCESSING")
    _submit(draft, data_processing_consent_purpose=purpose)
    record = ConsentRecord.objects.get(person=draft.person, purpose=purpose)
    assert record.action == "GRANTED"
    # The official v3 versions replaced v2-draft (owner correction, 2026-10-04).
    assert record.legal_document_version.version_label == "v3"


def test_accommodation_details_cannot_be_submitted_without_explicit_consent(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note="")
    with pytest.raises(SensitiveConsentRequired):
        _submit(draft)
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()
    assert AccommodationRequest.objects.filter(registration=draft).exists()  # kept


def test_explicit_consent_is_recorded_separately_when_given(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    _submit(draft, sensitive_data_consent_granted=True)
    assert ConsentRecord.objects.filter(
        person=draft.person, purpose__code="SENSITIVE_ACCOMMODATION_DATA", action="GRANTED"
    ).exists()


def test_no_or_prefer_not_to_say_needs_no_sensitive_consent(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(
        registration=draft, answer="PREFER_NOT_TO_SAY", categories=[], note=""
    )
    _submit(draft)
    assert not ConsentRecord.objects.filter(purpose__code="SENSITIVE_ACCOMMODATION_DATA").exists()


def test_the_snapshot_holds_the_answer_only(draft) -> None:
    walk_draft_through_every_step(draft)
    save_accommodation_request(registration=draft, answer="YES", categories=["OTHER"], note=NOTE)
    submission = _submit(draft, sensitive_data_consent_granted=True)
    snapshot = submission.snapshot_json
    # UX-C2 (G): the consent is its own registration-scoped fact.
    assert snapshot["accommodation"] == {
        "answer": "YES",
        "sensitive_data_provided": True,
        "sensitive_support_consent_recorded": True,
    }
    assert "accessibility_needs_text" not in snapshot
    flat = str(snapshot)
    assert NOTE not in flat and "OTHER" not in flat


# ---------------------------------------------------------------------------
# Who can see it
# ---------------------------------------------------------------------------


@pytest.fixture
def submitted_with_support(draft):
    walk_draft_through_every_step(draft)
    save_accommodation_request(
        registration=draft, answer="YES", categories=["CAPTIONING"], note=NOTE
    )
    _submit(draft, sensitive_data_consent_granted=True)
    return draft


def test_only_the_support_group_sees_accommodation_requests(submitted_with_support, event) -> None:
    coordinator = _user("ux3-coord@example.com", ACCOMMODATION_SUPPORT_GROUP_NAME, event)
    intake = _user("ux3-intake@example.com", "Registration Intake", event)
    assert accommodation_requests_visible_to(coordinator).count() == 1
    assert accommodation_requests_visible_to(intake).count() == 0
    for group in Group.objects.exclude(name=ACCOMMODATION_SUPPORT_GROUP_NAME):
        assert not group.permissions.filter(codename="coordinate_accommodation_support").exists()


def _signed_in(email):
    client = Client()
    client.post(reverse("accounts:operational-sign-in"), {"email": email, "password": PASSWORD})
    return client


def test_the_support_screen_is_scoped_audited_and_never_cached(
    submitted_with_support, event
) -> None:
    _user("ux3-screen@example.com", ACCOMMODATION_SUPPORT_GROUP_NAME, event)
    response = _signed_in("ux3-screen@example.com").get(
        reverse("registrations:ops-accommodation-list")
    )
    assert response.status_code == 200
    assert NOTE.encode() in response.content
    assert response["Cache-Control"] == "private, no-store"
    audit = AuditEvent.objects.get(action_code="REG_ACCOMMODATION_REQUEST_VIEWED")
    assert NOTE not in str(audit.after_summary)


def test_the_support_screen_is_forbidden_without_the_permission(submitted_with_support, event):
    _user("ux3-other@example.com", "Registration Intake", event)
    response = _signed_in("ux3-other@example.com").get(
        reverse("registrations:ops-accommodation-list")
    )
    assert response.status_code == 403


def test_the_intake_detail_page_never_shows_accommodation(submitted_with_support, event) -> None:
    # Even a superuser's general intake page never shows accommodation data.
    admin = OperationalUser.objects.create_superuser(
        email="ux3-admin@example.com", password=PASSWORD
    )
    admin.status = OperationalUserStatus.ACTIVE
    admin.save(update_fields=["status"])
    response = _signed_in("ux3-admin@example.com").get(
        reverse("registrations:ops-intake-detail", kwargs={"pk": submitted_with_support.pk})
    )
    assert response.status_code == 200
    assert NOTE.encode() not in response.content
    assert b"Live captioning" not in response.content


def test_the_data_never_reaches_passes_packages_exports_or_messages() -> None:
    from apps.exports.models import EXPORT_DATASET_FIELD_SCHEMAS

    for fields in EXPORT_DATASET_FIELD_SCHEMAS.values():
        assert not any("accommodation" in field or "accessib" in field for field in fields)
    for relative in (
        "apps/badges/credentials",
        "apps/entry/services/offline_packages.py",
        "apps/communications",
        "apps/exports",
        "apps/registrations/confirmation.py",
    ):
        path = ROOT / relative
        files = [path] if path.is_file() else list(path.rglob("*.py"))
        for file in files:
            if "tests" in file.parts or "migrations" in file.parts:
                continue
            text = file.read_text(encoding="utf-8")
            assert "AccommodationRequest" not in text, file
            assert "accommodation_request" not in text, file
            assert "accessibility_needs_text" not in text, file


# ---------------------------------------------------------------------------
# Withdrawal and legal hold
# ---------------------------------------------------------------------------


def test_withdrawal_clears_the_details_and_is_recorded(submitted_with_support) -> None:
    person = submitted_with_support.person
    assert withdraw_accommodation_consent(registration=submitted_with_support, person=person)
    request = AccommodationRequest.objects.get(registration=submitted_with_support)
    assert request.withdrawn_at is not None
    assert request.categories == [] and request.note_encrypted == ""
    assert ConsentRecord.objects.filter(
        person=person, purpose__code="SENSITIVE_ACCOMMODATION_DATA", action="WITHDRAWN"
    ).exists()
    assert AuditEvent.objects.filter(action_code="REG_ACCOMMODATION_CONSENT_WITHDRAWN").exists()
    # Idempotent: a second withdrawal changes nothing.
    assert not withdraw_accommodation_consent(registration=submitted_with_support, person=person)


def test_withdrawal_under_legal_hold_keeps_but_hides_the_details(
    submitted_with_support, event
) -> None:
    holder = _user("ux3-hold@example.com")
    LegalHold.objects.create(
        registration=submitted_with_support,
        reason="Synthetic dispute.",
        placed_by=holder,
        placed_at=timezone.now(),
    )
    withdraw_accommodation_consent(
        registration=submitted_with_support, person=submitted_with_support.person
    )
    request = AccommodationRequest.objects.get(registration=submitted_with_support)
    assert request.note_encrypted == NOTE and request.withdrawn_at is not None
    coordinator = _user("ux3-coord-hold@example.com", ACCOMMODATION_SUPPORT_GROUP_NAME, event)
    assert accommodation_requests_visible_to(coordinator).count() == 0


def test_another_person_cannot_withdraw(submitted_with_support) -> None:
    stranger = resolve_or_create_participant_for_email("ux3-stranger@example.com")
    assert not withdraw_accommodation_consent(registration=submitted_with_support, person=stranger)


def test_the_withdraw_url_is_scoped_to_the_signed_in_participant(submitted_with_support) -> None:
    from apps.accounts.otp import DeterministicTestOtpGenerator
    from apps.core.testing import otp_request_data

    client = Client()
    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        client.post(
            reverse("accounts:otp-request"),
            otp_request_data("ux3-intruder@example.com", client=client),
        )
        client.post(
            reverse("accounts:otp-verify"), {"code": DeterministicTestOtpGenerator.FIXED_VALUE}
        )
    assert participant_auth.PARTICIPANT_SESSION_KEY in client.session
    response = client.post(
        reverse("registrations:withdraw-accommodation", kwargs={"pk": submitted_with_support.pk})
    )
    assert response.status_code == 404
    assert (
        AccommodationRequest.objects.get(registration=submitted_with_support).withdrawn_at is None
    )


# ---------------------------------------------------------------------------
# Legal drafts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_complete_official_notices_are_effective_in_every_language(language) -> None:
    # Owner correction (2026-10-04): the official v3 versions (privacy.0005)
    # replaced the complete v2 drafts, with the same twelve sections.
    for code in ("PRIVACY_NOTICE", "TERMS"):
        version = effective_published_version(code, language)
        assert version.version_label == "v3"
    privacy = effective_published_version("PRIVACY_NOTICE", language).content
    assert "18-07" in privacy and "25-11" in privacy
    assert privacy.count("\n\n") >= 11  # twelve sections


def test_the_replaced_versions_are_retired_not_rewritten() -> None:
    old = LegalDocumentVersion.objects.filter(version_label="v1-draft")
    assert old.exists()
    assert set(old.values_list("status", flat=True)) == {"RETIRED"}
    assert all(version.effective_until is not None for version in old)
    assert all("[DRAFT -- pending Legal review]" in v.content or v.language != "en" for v in old)


def test_no_approval_is_claimed_and_markers_block_release() -> None:
    blockers = legal_release_blockers()
    assert blockers, "unconfirmed institutional facts must stay visible as blockers"
    codes = {code for code, _language, _marker in blockers}
    assert codes == {"PRIVACY_NOTICE", "TERMS"}
    english = effective_published_version("PRIVACY_NOTICE", "en").content
    # The official v3 text carries no draft banner, and still claims no approval.
    assert "approved by the ANPDP" not in english
    assert "[TO BE CONFIRMED: ANPDP receipt or authorization reference]" in english
    assert "privacy.W001" in {m.id for m in run_checks(databases=["default"])}


def test_consent_purposes_are_localized() -> None:
    for code in ("PERSONAL_DATA_PROCESSING", "SENSITIVE_ACCOMMODATION_DATA"):
        purpose = ConsentPurpose.objects.get(code=code)
        assert purpose.name and purpose.name_fr and purpose.name_ar and purpose.is_active
