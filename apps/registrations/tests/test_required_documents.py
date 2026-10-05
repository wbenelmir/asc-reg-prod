"""Registration correction package (2026-10-04), items 2 and 3: the required
profile photograph and the passport copy on the passport route.

* The photograph was shown as optional, then the submission sent the
  participant back for it. It is now required at the professional step (with
  a required indication, never "Optional") and at the final submission.
* On the passport route, a passport identity page is required before the
  final submission, whatever the country of residence. The route follows the
  nationality: an Algerian living abroad stays on the NIN route.
* Only an eligible document counts: ACTIVE, scanned CLEAN, of the right type
  and purpose, uploaded by the registration's own participant for this
  registration.

Synthetic data only.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

from apps.documents.models import (
    Document,
    DocumentStatus,
    DocumentType,
    MalwareScanStatus,
    StoredObject,
)
from apps.documents.services import active_passport_identity_page, active_profile_photo
from apps.documents.tests.factories import make_test_photo
from apps.people.services import resolve_or_create_participant_for_email
from apps.people.tests.conftest import make_event, participant_client
from apps.privacy.models import AcceptanceRecord
from apps.registrations.forms import IdentityStepForm, ProfessionalStepForm
from apps.registrations.models import Registration, RegistrationSubmission
from apps.registrations.services import (
    IncompleteRegistrationError,
    get_or_create_active_draft,
    submit_full_registration,
)
from apps.registrations.tests.factories import notices_post_data, walk_draft_through_every_step

pytestmark = pytest.mark.django_db

REJECTING = "apps.documents.scanning.RejectingStubScanner"
UNAVAILABLE = "apps.documents.scanning.UnavailableStubScanner"


@pytest.fixture
def event(db):
    from apps.core.models import Country, Sector

    for code, name in (("DZ", "Algeria"), ("FR", "France"), ("TN", "Tunisia"), ("SN", "Senegal")):
        Country.objects.get_or_create(code=code, defaults={"name": name})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    return make_event(f"DOC{uuid.uuid4().hex[:5].upper()}")


@pytest.fixture
def legal(db):
    """The current published notices (privacy.0005 seeds v3; a flushed test
    database gets a synthetic stand-in)."""
    from django.utils import timezone

    from apps.privacy.models import LegalDocument, LegalDocumentVersion
    from apps.privacy.selectors import effective_published_version

    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        version = effective_published_version(code, "en")
        if version is None:
            document, _ = LegalDocument.objects.get_or_create(
                code=code, defaults={"document_type": code}
            )
            version = LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label="docs-test",
                content="Synthetic text",
                content_hash="0" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status="PUBLISHED",
            )
        versions.append(version)
    return tuple(versions)


def _person(prefix: str = "docs"):
    return resolve_or_create_participant_for_email(f"{prefix}-{uuid.uuid4().hex[:8]}@example.test")


def _draft(event, **walk):
    draft = get_or_create_active_draft(person=_person(), event_edition=event)
    walk_draft_through_every_step(draft, **walk)
    return Registration.objects.get(pk=draft.pk)


def _submit(draft, legal):
    privacy, terms = legal
    return submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="docs",
        idempotency_key=f"docs-{draft.pk}",
    )


def _retire(registration, document_type: str) -> None:
    Document.objects.filter(registration=registration, document_type=document_type).update(
        status=DocumentStatus.REPLACED
    )


def _attach(registration, *, document_type, purpose_code, person, scan=MalwareScanStatus.CLEAN):
    """A document row as if uploaded (metadata only; eligibility never reads bytes)."""
    stored = StoredObject.objects.create(
        storage_key=f"synthetic/{uuid.uuid4().hex}",
        bucket_class="RESTRICTED",
        content_type="image/png",
        size_bytes=10,
        sha256="0" * 64,
        malware_scan_status=scan,
    )
    return Document.objects.create(
        stored_object=stored,
        registration=registration,
        person=person,
        document_type=document_type,
        purpose_code=purpose_code,
        status=DocumentStatus.ACTIVE,
    )


def _same_person_other_event(person, **walk):
    """The same participant's registration for another event, every step done."""
    from apps.people.tests.conftest import make_event

    other = get_or_create_active_draft(
        person=person, event_edition=make_event(f"DOCS{uuid.uuid4().hex[:4].upper()}")
    )
    walk_draft_through_every_step(other, **walk)
    return other


def _nothing_submitted(draft) -> None:
    assert not RegistrationSubmission.objects.filter(registration=draft).exists()
    assert not AcceptanceRecord.objects.filter(registration=draft).exists()
    assert Registration.objects.get(pk=draft.pk).public_status == "DRAFT"


def _professional_data(**overrides):
    data = {
        "organization_name": "Acme Corp",
        "organization_type": "COMPANY",
        "job_title": "Engineer",
        "department": "",
        "sector": "TECH",
        "country_code": "DZ",
        "operating_scope": "NATIONAL",
        "organization_website": "",
        "professional_profile_url": "",
        "biography": "",
    }
    data.update(overrides)
    return data


def _active(client, registration) -> None:
    from apps.accounts import participant_auth

    session = client.session
    session[participant_auth.ACTIVE_REGISTRATION_SESSION_KEY] = str(registration.pk)
    session.save()


# ---------------------------------------------------------------------------
# Item 2: the profile photograph
# ---------------------------------------------------------------------------


def test_the_professional_form_requires_a_photo_unless_one_is_on_file(event) -> None:
    missing = ProfessionalStepForm(_professional_data())
    assert not missing.is_valid() and "profile_photo" in missing.errors
    on_file = ProfessionalStepForm(_professional_data(), has_profile_photo=True)
    assert on_file.is_valid(), on_file.errors
    uploaded = ProfessionalStepForm(_professional_data(), {"profile_photo": make_test_photo()})
    assert uploaded.is_valid(), uploaded.errors
    gif = make_test_photo(image_format="GIF")  # a real GIF: the field detects the format
    assert (
        "profile_photo" in ProfessionalStepForm(_professional_data(), {"profile_photo": gif}).errors
    )
    not_an_image = make_test_photo()
    not_an_image.file.seek(0)
    not_an_image.file.write(b"not an image at all")
    not_an_image.file.truncate()
    not_an_image.file.seek(0)
    assert (
        "profile_photo"
        in ProfessionalStepForm(_professional_data(), {"profile_photo": not_an_image}).errors
    )


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_step_marks_the_photo_required_never_optional(event, language) -> None:
    draft = _draft(event)
    _retire(draft, DocumentType.PROFILE_PHOTO)
    client = participant_client(draft.person)
    _active(client, draft)
    client.cookies["django_language"] = language
    html = client.get(reverse("registrations:step-professional")).content.decode()
    section = html[html.index('id="professional-photo-heading"') :]
    section = section[: section.index("</section>")]
    assert "data-profile-photo-required" in section
    assert 'aria-required="true"' in section
    with translation.override(language):
        from django.utils.translation import gettext

        assert gettext("Optional") not in section
        assert gettext("A profile photograph is required to submit your registration.") in section


def test_posting_the_step_without_a_photo_is_refused_and_does_not_advance(event) -> None:
    draft = _draft(event)
    _retire(draft, DocumentType.PROFILE_PHOTO)
    client = participant_client(draft.person)
    _active(client, draft)
    response = client.post(reverse("registrations:step-professional"), _professional_data())
    assert response.status_code == 200
    assert b'id="id_profile_photo_error"' in response.content
    assert active_profile_photo(draft) is None

    with_photo = client.post(
        reverse("registrations:step-professional"),
        {**_professional_data(), "profile_photo": make_test_photo()},
    )
    assert with_photo.status_code == 302 and with_photo.url == reverse(
        "registrations:step-interests"
    )
    assert active_profile_photo(draft) is not None
    # Resuming the draft later: the photo on file satisfies the step.
    resumed = client.post(reverse("registrations:step-professional"), _professional_data())
    assert resumed.status_code == 302


@pytest.mark.parametrize(("backend", "reason"), [(REJECTING, "rejected"), (UNAVAILABLE, "retry")])
def test_an_unsafe_or_unscanned_photo_never_satisfies_the_requirement(
    event,
    backend,
    reason,
) -> None:
    draft = _draft(event)
    _retire(draft, DocumentType.PROFILE_PHOTO)
    client = participant_client(draft.person)
    _active(client, draft)
    with override_settings(MALWARE_SCANNER_BACKEND=backend):
        response = client.post(
            reverse("registrations:step-professional"),
            {**_professional_data(), "profile_photo": make_test_photo()},
        )
    assert response.status_code == 200 and b'id="id_profile_photo_error"' in response.content
    assert active_profile_photo(draft) is None


def test_a_direct_submission_without_an_eligible_photo_is_refused(event, legal) -> None:
    draft = _draft(event)
    _retire(draft, DocumentType.PROFILE_PHOTO)
    with pytest.raises(IncompleteRegistrationError) as refused:
        _submit(draft, legal)
    assert refused.value.step == "professional"
    _nothing_submitted(draft)

    client = participant_client(draft.person)
    _active(client, draft)
    response = client.post(reverse("registrations:step-notices"), notices_post_data())
    assert response.status_code == 302
    assert response.url == reverse("registrations:step-professional")
    _nothing_submitted(draft)


@pytest.mark.parametrize(
    "document",
    ["other_person", "other_registration", "wrong_type", "wrong_purpose", "pending", "rejected"],
)
def test_only_an_eligible_photo_counts(event, legal, document) -> None:
    draft = _draft(event)
    _retire(draft, DocumentType.PROFILE_PHOTO)
    owner = draft.person
    if document == "other_person":
        _attach(
            draft,
            document_type=DocumentType.PROFILE_PHOTO,
            purpose_code="PROFESSIONAL_PROFILE_PHOTO",
            person=_person("other"),
        )
    elif document == "other_registration":
        other = _same_person_other_event(owner)
        assert active_profile_photo(other) is not None  # a valid photo, but not of this draft
    elif document == "wrong_type":
        _attach(
            draft,
            document_type=DocumentType.PASSPORT_IDENTITY_PAGE,
            purpose_code="PROFESSIONAL_PROFILE_PHOTO",
            person=owner,
        )
    elif document == "wrong_purpose":
        _attach(
            draft,
            document_type=DocumentType.PROFILE_PHOTO,
            purpose_code="REQUESTED_EVIDENCE:synthetic",
            person=owner,
        )
    else:
        _attach(
            draft,
            document_type=DocumentType.PROFILE_PHOTO,
            purpose_code="PROFESSIONAL_PROFILE_PHOTO",
            person=owner,
            scan=MalwareScanStatus.PENDING if document == "pending" else MalwareScanStatus.REJECTED,
        )
    assert active_profile_photo(draft) is None
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal)
    _nothing_submitted(draft)


def _origin_draft(event, origin):
    """A draft of the given origin, claimed by its participant, every step done."""
    from apps.organizations.models import Organization, OrganizationType

    person = _person(origin.lower())
    organization = Organization.objects.create(
        official_name=f"Origin Org {uuid.uuid4().hex[:6]}",
        normalized_name=f"origin org {uuid.uuid4().hex[:6]}",
        organization_type=OrganizationType.MINISTRY,
    )
    if origin == "OPEN":
        draft = get_or_create_active_draft(person=person, event_edition=event)
    elif origin == "INVITATION":
        from apps.invitations.services import (
            change_campaign_status,
            create_campaign,
            create_invited_draft_registration,
            issue_initial_link,
        )

        campaign = create_campaign(
            event_edition=event,
            organization=organization,
            name="Docs campaign",
            public_reference=f"CMP-{uuid.uuid4().hex[:8].upper()}",
        )
        change_campaign_status(campaign, "ACTIVE")
        link, _token = issue_initial_link(campaign)
        draft = create_invited_draft_registration(
            link, preferred_language="en", person_id=person.pk
        ).registration
    else:
        from apps.accounts.models import OperationalUser
        from apps.invitations.services import (
            claim_on_behalf_registration,
            create_on_behalf_draft,
        )
        from apps.people.models import ContactPoint

        staff = OperationalUser.objects.create_user(
            email=f"staff-{uuid.uuid4().hex[:6]}@example.test", password=None
        )
        email = ContactPoint.objects.filter(person=person).first().value_encrypted
        draft, token = create_on_behalf_draft(
            event_edition=event,
            source_organization=organization,
            created_by=staff,
            intended_email=email,
        )
        draft = claim_on_behalf_registration(
            raw_claim_token=token, verified_email=email, person=person
        )
    walk_draft_through_every_step(draft)
    return Registration.objects.get(pk=draft.pk)


@pytest.mark.parametrize("origin", ["OPEN", "INVITATION", "ON_BEHALF"])
def test_every_origin_requires_the_photo_the_same_way(event, legal, origin) -> None:
    draft = _origin_draft(event, origin)
    assert draft.source_kind == origin
    _retire(draft, DocumentType.PROFILE_PHOTO)
    client = participant_client(draft.person)
    _active(client, draft)
    refused = client.post(reverse("registrations:step-professional"), _professional_data())
    assert refused.status_code == 200 and b'id="id_profile_photo_error"' in refused.content
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal)
    client.post(
        reverse("registrations:step-professional"),
        {**_professional_data(), "profile_photo": make_test_photo()},
    )
    _submit(draft, legal)
    assert Registration.objects.get(pk=draft.pk).public_status == "SUBMITTED"


def test_a_registration_submitted_before_the_rule_is_not_withdrawn_or_rejected(
    event,
    legal,
) -> None:
    draft = _draft(event)
    _submit(draft, legal)
    # The photo goes away after the submission (as for a record submitted
    # before the rule): nothing re-checks or changes the submitted record.
    _retire(draft, DocumentType.PROFILE_PHOTO)
    submitted = Registration.objects.get(pk=draft.pk)
    assert submitted.public_status == "SUBMITTED"
    client = participant_client(submitted.person)
    assert client.get(reverse("registrations:workspace")).status_code == 200
    page = client.get(
        reverse("registrations:confirmation", kwargs={"reference": submitted.public_reference})
    )
    assert page.status_code == 200
    assert Registration.objects.get(pk=draft.pk).public_status == "SUBMITTED"


# ---------------------------------------------------------------------------
# Item 3: the passport copy on the passport route
# ---------------------------------------------------------------------------

FUTURE = datetime.date.today() + datetime.timedelta(days=400)


def _identity_data(nationality, residence, path, **overrides):
    data = {
        "given_names": "Amina",
        "family_name": "Bentest",
        "date_of_birth": "1990-03-07",
        "nationality_code": nationality,
        "country_of_residence": residence,
        "identity_path": path,
        "nin_value": "123456789012345678" if path == "NIN" else "",
        "passport_number": "X1234567" if path == "PASSPORT" else "",
        "passport_country_code": nationality if path == "PASSPORT" else "",
        "passport_expires_at": FUTURE.isoformat() if path == "PASSPORT" else "",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("nationality", "residence"),
    [("FR", "DZ"), ("TN", "FR"), ("SN", "SN")],
    ids=["foreign-resident-in-algeria", "foreign-resident-abroad", "foreign-at-home"],
)
def test_every_foreign_passport_route_requires_the_copy(
    event,
    nationality,
    residence,
) -> None:
    missing = IdentityStepForm(
        _identity_data(nationality, residence, "PASSPORT"), event_edition=event
    )
    assert not missing.is_valid() and "passport_identity_page" in missing.errors
    with_copy = IdentityStepForm(
        _identity_data(nationality, residence, "PASSPORT"),
        {"passport_identity_page": make_test_photo()},
        event_edition=event,
    )
    assert with_copy.is_valid(), with_copy.errors
    on_file = IdentityStepForm(
        _identity_data(nationality, residence, "PASSPORT"),
        event_edition=event,
        has_passport_identity_page=True,
    )
    assert on_file.is_valid(), on_file.errors
    nin = IdentityStepForm(_identity_data(nationality, residence, "NIN"), event_edition=event)
    assert not nin.is_valid() and "identity_path" in nin.errors


def test_an_algerian_living_abroad_stays_on_the_nin_route_without_a_passport_copy(
    event,
    legal,
) -> None:
    from apps.people.models import IdentityRoute, IdentityVerification

    form = IdentityStepForm(_identity_data("DZ", "FR", "NIN"), event_edition=event)
    assert form.is_valid(), form.errors
    passport = IdentityStepForm(
        _identity_data("DZ", "FR", "PASSPORT"),
        {"passport_identity_page": make_test_photo()},
        event_edition=event,
    )
    assert not passport.is_valid() and "identity_path" in passport.errors
    with_copy_on_nin = IdentityStepForm(
        _identity_data("DZ", "FR", "NIN"),
        {"passport_identity_page": make_test_photo()},
        event_edition=event,
    )
    assert "passport_identity_page" in with_copy_on_nin.errors
    draft = _draft(event, nationality_code_id="DZ", country_of_residence_id="FR")
    assert active_passport_identity_page(draft) is None
    _submit(draft, legal)
    case = IdentityVerification.objects.get(registration=draft)
    assert case.route == IdentityRoute.NIN


@pytest.mark.parametrize(("nationality", "residence"), [("FR", "DZ"), ("TN", "FR")])
def test_the_final_submission_requires_the_copy_and_accepts_a_valid_one(
    event,
    legal,
    nationality,
    residence,
) -> None:
    from apps.people.models import IdentityReasonCode, IdentityRoute, IdentityVerification

    draft = _draft(
        event,
        nationality_code_id=nationality,
        country_of_residence_id=residence,
        identity_path="PASSPORT",
        passport_country_code_id=nationality,
    )
    page = active_passport_identity_page(draft)
    assert page is not None
    _retire(draft, DocumentType.PASSPORT_IDENTITY_PAGE)
    with pytest.raises(IncompleteRegistrationError) as refused:
        _submit(draft, legal)
    assert refused.value.step == "identity"
    _nothing_submitted(draft)
    Document.objects.filter(pk=page.pk).update(status=DocumentStatus.ACTIVE)
    submission = _submit(draft, legal)
    assert submission.snapshot_json["passport_identity_page"]["document_id"] == str(page.pk)
    case = IdentityVerification.objects.get(registration=draft)
    assert case.route == IdentityRoute.PASSPORT
    assert case.reason_code == IdentityReasonCode.FOREIGN_PASSPORT_REVIEW  # manual review kept


@pytest.mark.parametrize(
    "document",
    [
        "national_id_card",
        "requested_evidence",
        "profile_photo_as_page",
        "other_person",
        "other_registration",
        "pending",
        "rejected",
    ],
)
def test_another_document_never_satisfies_the_passport_copy(
    event,
    legal,
    document,
) -> None:
    draft = _draft(event, nationality_code_id="FR", identity_path="PASSPORT")
    _retire(draft, DocumentType.PASSPORT_IDENTITY_PAGE)
    owner = draft.person
    kinds = {
        "national_id_card": (DocumentType.NATIONAL_ID_CARD, "NATIONAL_ID_CARD", owner, "CLEAN"),
        "requested_evidence": (
            DocumentType.REQUESTED_EVIDENCE,
            "PASSPORT_IDENTITY_PAGE",
            owner,
            "CLEAN",
        ),
        "profile_photo_as_page": (
            DocumentType.PASSPORT_IDENTITY_PAGE,
            "PROFESSIONAL_PROFILE_PHOTO",
            owner,
            "CLEAN",
        ),
        "other_person": (
            DocumentType.PASSPORT_IDENTITY_PAGE,
            "PASSPORT_IDENTITY_PAGE",
            _person("intruder"),
            "CLEAN",
        ),
        "pending": (
            DocumentType.PASSPORT_IDENTITY_PAGE,
            "PASSPORT_IDENTITY_PAGE",
            owner,
            "PENDING",
        ),
        "rejected": (
            DocumentType.PASSPORT_IDENTITY_PAGE,
            "PASSPORT_IDENTITY_PAGE",
            owner,
            "REJECTED",
        ),
    }
    if document == "other_registration":
        other = _same_person_other_event(owner, nationality_code_id="FR", identity_path="PASSPORT")
        assert active_passport_identity_page(other) is not None
    else:
        document_type, purpose, person, scan = kinds[document]
        _attach(draft, document_type=document_type, purpose_code=purpose, person=person, scan=scan)
    assert active_passport_identity_page(draft) is None
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal)
    _nothing_submitted(draft)


def test_a_posted_field_or_document_reference_never_replaces_the_upload(event) -> None:
    other = _draft(event, nationality_code_id="FR", identity_path="PASSPORT")
    forged = _identity_data(
        "FR",
        "DZ",
        "PASSPORT",
        has_passport_identity_page="1",
        passport_identity_page=str(active_passport_identity_page(other).pk),
        current_passport_identity_page=str(active_passport_identity_page(other).pk),
    )
    form = IdentityStepForm(forged, event_edition=event)
    assert not form.is_valid() and "passport_identity_page" in form.errors

    draft = get_or_create_active_draft(person=_person("forge"), event_edition=event)
    client = participant_client(draft.person)
    _active(client, draft)
    response = client.post(reverse("registrations:step-identity"), forged)
    assert response.status_code == 200
    assert b'id="id_passport_identity_page_error"' in response.content
    assert active_passport_identity_page(draft) is None


@pytest.mark.parametrize(("backend", "reason"), [(REJECTING, "rejected"), (UNAVAILABLE, "retry")])
def test_an_unsafe_passport_copy_is_refused_and_not_stored(event, backend, reason) -> None:
    draft = get_or_create_active_draft(person=_person("unsafe"), event_edition=event)
    client = participant_client(draft.person)
    _active(client, draft)
    with override_settings(MALWARE_SCANNER_BACKEND=backend):
        response = client.post(
            reverse("registrations:step-identity"),
            {
                **_identity_data("FR", "DZ", "PASSPORT"),
                "passport_identity_page": make_test_photo(),
            },
        )
    assert response.status_code == 200
    assert b'id="id_passport_identity_page_error"' in response.content
    assert not Document.objects.filter(registration=draft).exists()


def test_an_expired_passport_stays_refused(event, legal) -> None:
    expired = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    form = IdentityStepForm(
        _identity_data("FR", "DZ", "PASSPORT", passport_expires_at=expired),
        {"passport_identity_page": make_test_photo()},
        event_edition=event,
    )
    assert not form.is_valid() and "passport_expires_at" in form.errors
    draft = _draft(event, nationality_code_id="FR", identity_path="PASSPORT")
    from apps.people.models import IdentifierType

    draft.person.identity_identifiers.filter(identifier_type=IdentifierType.PASSPORT).update(
        expires_at=datetime.date.today()
    )
    with pytest.raises(IncompleteRegistrationError):
        _submit(draft, legal)


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_the_identity_step_explains_the_requirement_in_every_language(event, language) -> None:
    from django.utils.translation import gettext

    draft = get_or_create_active_draft(person=_person("explain"), event_edition=event)
    client = participant_client(draft.person)
    _active(client, draft)
    client.cookies["django_language"] = language
    page = client.get(reverse("registrations:step-identity")).content.decode()
    refused = client.post(
        reverse("registrations:step-identity"), _identity_data("FR", "DZ", "PASSPORT")
    ).content.decode()
    with translation.override(language):
        for text in (
            "For foreign nationals, wherever they live. A photo or scan of the passport "
            "identity page is required.",
            "For Algerian nationals, including those living abroad. 18 digits.",
            "The passport identity page is required for every foreign participant, so that the "
            "details you entered can be checked by our team.",
        ):
            translated = gettext(text)
            assert translated in page.replace("&#x27;", "'")
            if language != "en":
                assert translated != text, f"untranslated in {language}: {text}"
        message = gettext("Upload a photo or scan of the passport identity page.")
        assert message in refused.replace("&#x27;", "'")
        if language != "en":
            assert message != "Upload a photo or scan of the passport identity page."
