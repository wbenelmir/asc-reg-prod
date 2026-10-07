"""Registration correction package (2026-10-04), item 5: the official v3
Privacy Notice and Registration Terms (`privacy.0005`).

New versions through the established publication mechanism; the previous
versions, their hashes and the acceptance records that reference them are
kept; new registrations accept and record exactly the current version they
were shown. The owner's general wording (same migration) invents no
institutional fact. Synthetic data only.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.privacy.checks import MARKER_PATTERN, legal_release_blockers
from apps.privacy.models import (
    AcceptanceRecord,
    ConsentRecord,
    LegalDocumentVersion,
    LegalDocumentVersionStatus,
)
from apps.privacy.selectors import effective_published_version

pytestmark = pytest.mark.django_db

V2 = importlib.import_module("apps.privacy.migrations.0004_ux3_consent_purposes_and_v2_notices")


class _V3:
    """The v3 migration module, imported on use (a baseline run fails per test)."""

    @property
    def CONTENT(self):  # noqa: N802 - mirrors the module constant
        return importlib.import_module(
            "apps.privacy.migrations.0005_official_registration_notices_v3"
        ).CONTENT


V3 = _V3()
LANGUAGES = ("en", "fr", "ar")


def _current(code, language):
    return effective_published_version(code, language)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("code", ["PRIVACY_NOTICE", "TERMS"])
def test_the_current_versions_are_the_official_v3_without_draft_presentation(code, language):
    version = _current(code, language)
    assert version.version_label == "v3" and "draft" not in version.version_label.lower()
    assert not re.search(r"\[(?:DRAFT|BROUILLON|مسودة)", version.content)
    assert version.content == V3.CONTENT[(code, language)]
    assert version.content_hash == hashlib.sha256(version.content.encode("utf-8")).hexdigest()
    assert version.effective_until is None


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("code", ["PRIVACY_NOTICE", "TERMS"])
def test_the_previous_versions_are_retired_never_rewritten(code, language):
    v2 = LegalDocumentVersion.objects.get(
        legal_document__code=code, language=language, version_label="v2-draft"
    )
    v3 = _current(code, language)
    assert v2.status == LegalDocumentVersionStatus.RETIRED
    assert v2.effective_until == v3.effective_from
    assert v2.content == V2.CONTENT[(code, language)]
    assert v2.content_hash == hashlib.sha256(v2.content.encode("utf-8")).hexdigest()
    v1 = LegalDocumentVersion.objects.get(
        legal_document__code=code, language=language, version_label="v1-draft"
    )
    assert v1.status == LegalDocumentVersionStatus.RETIRED


# The owner replaced the drafted v3 text with a general wording in the same
# migration (`privacy.0005` module docstring, 2026-10-04): unresolved factual
# details are omitted at the owner's request, which "does not establish legal
# completeness or production readiness". These tests pin that approved
# wording: what it covers, that it invents no institutional fact, and that the
# collection facts it states match the rules in force.

_PARAGRAPHS = {"PRIVACY_NOTICE": 8, "TERMS": 7}


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_general_wording_has_the_same_structure_in_every_language(language):
    for code, paragraphs in _PARAGRAPHS.items():
        text = V3.CONTENT[(code, language)]
        assert len([p for p in text.split("\n\n") if p.strip()]) == paragraphs, (code, language)


def test_the_general_wording_covers_the_participant_facing_topics():
    privacy = V3.CONTENT[("PRIVACY_NOTICE", "en")]
    for statement in (
        "collects information needed to manage participation applications",
        "used to process your application",
        "limited to authorized personnel",
        "Required fields are identified in the form",
        "requires explicit, separate consent",
        "withdraw this consent from your personal workspace",
        "Never share your sign-in codes",
        "does not automatically erase",
        "Requests concerning your personal data",
    ):
        assert statement in privacy, statement
    terms = V3.CONTENT[("TERMS", "en")]
    for statement in (
        "Submitting an application does not confirm admission",
        "accurate information and legible documents",
        "do not share your sign-in codes",
        "reviewed by authorized personnel",
        "withdraw an active application",
        "forged documents",
        "as described in the Privacy Notice",
    ):
        assert statement in terms, statement


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_general_wording_invents_no_institutional_fact(language):
    """No placeholder is left to read as a fact, and no controller address,
    contact, authority reference or approval is asserted."""
    for code in ("PRIVACY_NOTICE", "TERMS"):
        text = V3.CONTENT[(code, language)]
        assert MARKER_PATTERN.findall(text) == []
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)  # no email address
        assert not re.search(r"https?://|www\.", text)  # no address of a site
        assert not re.search(r"\+?\d[\d .-]{7,}\d", text)  # no telephone number
        for claim in ("ANPDP", "approved by", "approuvé", "18-07", "25-11"):
            assert claim not in text, (code, language, claim)
    assert legal_release_blockers() == []


def test_the_collection_facts_match_the_rules_in_force():
    english = V3.CONTENT[("PRIVACY_NOTICE", "en")]
    assert "a photograph and the supporting documents requested in the form" in english
    assert "optional profile photograph" not in english
    assert "only when the event requires it" not in english
    assert (
        "photographie et pièces justificatives demandées dans le formulaire"
        in (V3.CONTENT[("PRIVACY_NOTICE", "fr")])
    )
    assert "والصورة الشخصية والوثائق المطلوبة في الاستمارة" in V3.CONTENT[("PRIVACY_NOTICE", "ar")]


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_legal_page_shows_the_official_versions(language):
    html = Client().get(reverse("privacy:legal-information"), HTTP_ACCEPT_LANGUAGE=language)
    html = html.content.decode().replace("&#x27;", "'")
    assert html.count('<bdi dir="ltr">v3</bdi>') == 2
    for code in ("PRIVACY_NOTICE", "TERMS"):
        assert _current(code, language).content.splitlines()[0] in html
    assert "v2-draft" not in html and "[DRAFT" not in html and "[BROUILLON" not in html


# ---------------------------------------------------------------------------
# Acceptance through the wizard
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_draft(db):
    from apps.core.models import Country, Sector
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import make_event, participant_client
    from apps.registrations.services import get_or_create_active_draft
    from apps.registrations.tests.factories import walk_draft_through_every_step

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    person = resolve_or_create_participant_for_email(f"v3-{uuid.uuid4().hex[:8]}@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=make_event("V3NOTICES"))
    walk_draft_through_every_step(draft)
    client = participant_client(person)
    from apps.accounts import participant_auth

    session = client.session
    session[participant_auth.ACTIVE_REGISTRATION_SESSION_KEY] = str(draft.pk)
    session.save()
    return draft, client


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_new_registration_records_the_v3_versions_it_was_shown(ready_draft, language):
    from apps.registrations.models import RegistrationSubmission
    from apps.registrations.tests.factories import notices_post_data

    draft, client = ready_draft
    client.cookies["django_language"] = language
    page = client.get(reverse("registrations:step-notices")).content.decode()
    privacy, terms = _current("PRIVACY_NOTICE", language), _current("TERMS", language)
    assert f'value="{privacy.pk}"' in page and f'value="{terms.pk}"' in page
    response = client.post(reverse("registrations:step-notices"), notices_post_data(language))
    assert response.status_code == 302 and "/confirmation/" in response.url
    records = {
        r.legal_document_version_id: r for r in AcceptanceRecord.objects.filter(registration=draft)
    }
    assert set(records) == {privacy.pk, terms.pk}
    assert {r.language for r in records.values()} == {language}
    consent = ConsentRecord.objects.get(
        person=draft.person, purpose__code="PERSONAL_DATA_PROCESSING"
    )
    assert consent.legal_document_version_id == privacy.pk
    legal = RegistrationSubmission.objects.get(registration=draft).snapshot_json["legal_acceptance"]
    assert legal["privacy_notice"]["version_label"] == "v3"
    assert legal["terms"]["version_label"] == "v3"


def test_a_page_showing_a_superseded_version_records_nothing(ready_draft):
    """The notices were republished after the page was shown (or the posted
    ids are missing): the participant is shown the current version again and
    nothing is recorded for a version they did not see."""
    from apps.registrations.models import RegistrationSubmission
    from apps.registrations.tests.factories import notices_post_data

    draft, client = ready_draft
    v2_privacy = LegalDocumentVersion.objects.get(
        legal_document__code="PRIVACY_NOTICE", language="en", version_label="v2-draft"
    )
    for stale in (
        notices_post_data(privacy_version_id=str(v2_privacy.pk)),
        notices_post_data(without=("privacy_version_id", "terms_version_id")),
    ):
        response = client.post(reverse("registrations:step-notices"), stale)
        assert response.status_code == 302
        assert response.url == reverse("registrations:step-notices")
        assert not RegistrationSubmission.objects.filter(registration=draft).exists()
        assert not AcceptanceRecord.objects.filter(registration=draft).exists()
        assert not ConsentRecord.objects.filter(person=draft.person).exists()
    follow = client.get(reverse("registrations:step-notices")).content.decode()
    assert "have changed" in follow
    assert (
        client.post(reverse("registrations:step-notices"), notices_post_data()).status_code == 302
    )
    assert RegistrationSubmission.objects.filter(registration=draft).exists()


def test_the_required_acceptances_are_still_required(ready_draft):
    from apps.registrations.models import RegistrationSubmission
    from apps.registrations.tests.factories import notices_post_data

    draft, client = ready_draft
    for missing in ("accept_privacy_notice", "accept_terms", "accept_data_processing"):
        response = client.post(
            reverse("registrations:step-notices"), notices_post_data(without=(missing,))
        )
        assert response.status_code == 200
        assert not RegistrationSubmission.objects.filter(registration=draft).exists()


def test_an_earlier_acceptance_keeps_its_version(ready_draft):
    """A registration that accepted v2 keeps exactly that record; the v3
    publication changed nothing it references."""
    draft, _client = ready_draft
    v2_terms = LegalDocumentVersion.objects.get(
        legal_document__code="TERMS", language="en", version_label="v2-draft"
    )
    from django.utils import timezone

    record = AcceptanceRecord.objects.create(
        person=draft.person,
        registration=draft,
        legal_document_version=v2_terms,
        action="ACCEPTED",
        accepted_at=timezone.now(),
        language="en",
        source="PUBLIC_WEB",
        session_reference="v2-era",
    )
    record.refresh_from_db()
    assert record.legal_document_version.version_label == "v2-draft"
    assert record.legal_document_version.content == V2.CONTENT[("TERMS", "en")]
