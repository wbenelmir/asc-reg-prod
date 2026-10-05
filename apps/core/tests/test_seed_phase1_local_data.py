"""Tests for the `seed_phase1_local_data` idempotent local bootstrap command
(Prompt 5 correction pass §1)."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from apps.core.models import Country, Sector
from apps.events.models import EventEdition
from apps.privacy.models import LegalDocument, LegalDocumentVersion
from apps.registrations.models import InterestTopic

pytestmark = pytest.mark.django_db


def _run() -> str:
    out = io.StringIO()
    call_command("seed_phase1_local_data", stdout=out)
    return out.getvalue()


def test_first_execution_creates_the_minimum_reference_data() -> None:
    _run()
    assert Country.objects.filter(code="DZ").exists()
    assert Country.objects.filter(code="FR").exists()
    assert Sector.objects.filter(code="TECH").exists()
    event = EventEdition.objects.get(code="ASC2026")
    assert event.passport_identity_page_upload_enabled is False
    assert LegalDocumentVersion.objects.filter(
        legal_document__code="PRIVACY_NOTICE", language="en", status="PUBLISHED"
    ).exists()
    assert LegalDocumentVersion.objects.filter(
        legal_document__code="TERMS", language="ar", status="PUBLISHED"
    ).exists()
    assert InterestTopic.objects.filter(event_edition=event).count() >= 5


def test_repeated_execution_does_not_duplicate_rows() -> None:
    _run()
    privacy_version_count_after_first_run = LegalDocumentVersion.objects.filter(
        legal_document__code="PRIVACY_NOTICE"
    ).count()
    topic_count_after_first_run = InterestTopic.objects.filter(
        event_edition__code="ASC2026"
    ).count()
    _run()
    assert Country.objects.filter(code="DZ").count() == 1
    assert Country.objects.filter(code="FR").count() == 1
    assert Sector.objects.filter(code="TECH").count() == 1
    assert EventEdition.objects.filter(code="ASC2026").count() == 1
    assert LegalDocument.objects.filter(code="PRIVACY_NOTICE").count() == 1
    # Never a second competing PUBLISHED version for the same
    # (document, language) -- regardless of which run (this command's own,
    # or an earlier data migration's) created the first one.
    assert (
        LegalDocumentVersion.objects.filter(legal_document__code="PRIVACY_NOTICE").count()
        == privacy_version_count_after_first_run
    )
    event = EventEdition.objects.get(code="ASC2026")
    # UX-2 (C-06) added grouped topics through a migration, so the count is
    # compared with the first run instead of a fixed number.
    assert InterestTopic.objects.filter(event_edition=event).count() == topic_count_after_first_run


def test_repeated_execution_preserves_a_reviewed_row(settings) -> None:
    Country.objects.update_or_create(code="DZ", defaults={"name": "Reviewed Algeria Label"})
    _run()
    algeria = Country.objects.get(code="DZ")
    assert algeria.name == "Reviewed Algeria Label"


def test_repeated_execution_never_creates_a_second_published_version_for_the_same_language() -> (
    None
):
    """Whether the first PUBLISHED version for (TERMS, fr) came from this
    command or from an earlier data migration, the command must recognize it
    and never create a second, competing PUBLISHED version alongside it."""
    _run()
    versions = LegalDocumentVersion.objects.filter(
        legal_document__code="TERMS", language="fr", status="PUBLISHED"
    )
    assert versions.count() == 1
    version = versions.get()
    version.content = "Reviewed, legally-approved French terms text."
    version.save(update_fields=["content"])
    _run()
    assert (
        LegalDocumentVersion.objects.filter(
            legal_document__code="TERMS", language="fr", status="PUBLISHED"
        ).count()
        == 1
    )
    version.refresh_from_db()
    assert version.content == "Reviewed, legally-approved French terms text."


def test_minimum_algerian_and_international_paths_are_available() -> None:
    _run()
    dz = Country.objects.get(code="DZ")
    fr = Country.objects.get(code="FR")
    assert dz.is_active
    assert fr.is_active


def test_enable_and_disable_flags_toggle_the_passport_policy_switch() -> None:
    _run()
    call_command("seed_phase1_local_data", "--enable-passport-identity-page-upload")
    event = EventEdition.objects.get(code="ASC2026")
    assert event.passport_identity_page_upload_enabled is True

    call_command("seed_phase1_local_data", "--disable-passport-identity-page-upload")
    event.refresh_from_db()
    assert event.passport_identity_page_upload_enabled is False
