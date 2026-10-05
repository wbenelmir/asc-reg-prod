"""Registration correction package (2026-10-04), item 5: the official v3
Privacy Notice and Registration Terms (`privacy.0005`).

New versions through the established publication mechanism; the previous
versions, their hashes and the acceptance records that reference them are
kept; new registrations accept and record exactly the current version they
were shown; the unconfirmed institutional facts stay identified, never
invented. Synthetic data only.
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
OWNER_FR_NAME = "Ministère de l'Économie de la Connaissance, des Start-up et des Micro-entreprise"


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


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_substantive_protection_of_v2_is_kept(language):
    """v3 changes only the banner, the owner-supplied French name and the
    collection facts in force (photo and passport copy required)."""
    for code, sections in (("PRIVACY_NOTICE", 12), ("TERMS", 11)):
        v2_text = V2.CONTENT[(code, language)].split("\n\n", 1)[1]  # without the banner
        v3_text = V3.CONTENT[(code, language)]
        assert len(re.findall(r"(?m)^\d+\. ", v3_text)) == sections
        v2_paragraphs = [p for p in v2_text.split("\n") if p.strip()]
        v3_paragraphs = [p for p in v3_text.split("\n") if p.strip()]
        assert len(v2_paragraphs) == len(v3_paragraphs)
        changed = [new for old, new in zip(v2_paragraphs, v3_paragraphs, strict=True) if old != new]
        expected = {
            "PRIVACY_NOTICE": {"en": 2, "fr": 3, "ar": 2},
            "TERMS": {"en": 0, "fr": 1, "ar": 0},
        }
        assert len(changed) == expected[code][language], changed


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_unconfirmed_facts_stay_identified_and_none_is_invented(language):
    for code in ("PRIVACY_NOTICE", "TERMS"):
        v2_markers = MARKER_PATTERN.findall(V2.CONTENT[(code, language)])
        v3_markers = MARKER_PATTERN.findall(V3.CONTENT[(code, language)])
        removed = [marker for marker in v2_markers if marker not in v3_markers]
        assert all(marker in v2_markers for marker in v3_markers)
        if code == "PRIVACY_NOTICE" and language == "fr":
            # Only the official French name was supplied (by the owner, 2026-10-04).
            assert removed == ["[À CONFIRMER : dénomination officielle en français.]"]
            assert OWNER_FR_NAME in V3.CONTENT[(code, language)]
        else:
            assert removed == []
    assert "approved by the ANPDP" not in V3.CONTENT[("PRIVACY_NOTICE", "en")]
    blockers = legal_release_blockers()
    assert {(code, lang) for code, lang, _marker in blockers} == {
        (code, lang) for code in ("PRIVACY_NOTICE", "TERMS") for lang in LANGUAGES
    }


def test_the_collection_facts_match_the_rules_in_force():
    english = V3.CONTENT[("PRIVACY_NOTICE", "en")]
    assert "a profile photograph (required)" in english
    assert "optional profile photograph" not in english
    assert "a photo or scan of the passport identity page is required" in english
    assert "only when the event requires it" not in english
    assert "photo de profil (obligatoire)" in V3.CONTENT[("PRIVACY_NOTICE", "fr")]
    assert "وصورة شخصية (إلزامية)" in V3.CONTENT[("PRIVACY_NOTICE", "ar")]
    assert OWNER_FR_NAME in V3.CONTENT[("TERMS", "fr")]


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
