"""Test-only helpers for building a fully complete Draft Registration.

Shared across this app's test modules so the Prompt 4 final closure pass §1
completeness guard is satisfied consistently, instead of duplicating the
full valid-field set in every test file.
"""

from __future__ import annotations

import datetime

from apps.documents.tests.factories import make_test_photo
from apps.registrations.models import InterestTopic, Registration
from apps.registrations.services import (
    save_contact_step,
    save_identity_step,
    save_interests_step,
    save_professional_step,
)

#: A synthetic NIN that the development simulation does not know, so a submitted
#: registration using it goes to manual review as NOT_FOUND (IDV-2).
DEFAULT_NIN = "123456789012345678"


def walk_draft_through_every_step(
    draft: Registration,
    *,
    nationality_code_id: str = "DZ",
    country_of_residence_id: str = "DZ",
    identity_path: str = "NIN",
    nin_value: str = DEFAULT_NIN,
    passport_number: str = "X1234567",
    passport_country_code_id: str = "FR",
    passport_expires_at: datetime.date | None = None,
    mobile_number: str = "0551234567",
    mobile_country_code_id: str = "DZ",
    sector_code_id: str = "TECH",
) -> InterestTopic:
    """Push `draft` through identity/contact/professional/interests with fully valid,
    V1-complete data, returning the `InterestTopic` it selected. `draft` is left ready
    for `submit_full_registration`."""
    save_identity_step(
        registration=draft,
        given_names="Amine",
        family_name="Benali",
        date_of_birth=datetime.date(1990, 1, 1),
        nationality_code_id=nationality_code_id,
        country_of_residence_id=country_of_residence_id,
        identity_path=identity_path,
        nin_value=nin_value if identity_path == "NIN" else "",
        passport_number=passport_number if identity_path == "PASSPORT" else "",
        passport_country_code_id=passport_country_code_id if identity_path == "PASSPORT" else "",
        passport_expires_at=(
            passport_expires_at or (datetime.date.today() + datetime.timedelta(days=365))
        )
        if identity_path == "PASSPORT"
        else None,
        # IDV-3 (A13-02): the passport identity page is required on the passport path.
        passport_identity_page_file=make_test_photo() if identity_path == "PASSPORT" else None,
    )
    save_contact_step(
        registration=draft,
        mobile_number=mobile_number,
        mobile_country_code_id=mobile_country_code_id,
    )
    save_professional_step(
        registration=draft,
        organization_name="Acme Corp",
        organization_type="COMPANY",
        job_title="Engineer",
        department="R&D",
        sector_code_id=sector_code_id,
        country_code_id=nationality_code_id,
        organization_website="https://acme.example",
        professional_profile_url="https://www.linkedin.example/in/amine",
        biography="A short professional biography.",
        profile_photo_file=make_test_photo(),
        operating_scope="NATIONAL",
    )
    # `get_or_create`, not `create`: a test that walks MULTIPLE registrations
    # through the SAME event_edition (e.g. Phase 2 Prompt 2 capacity tests)
    # must not collide on `reg_interesttopic_event_code_uq` -- reusing the
    # same topic row across registrations is exactly what production reuse
    # of one event's interest catalogue looks like.
    topic, _ = InterestTopic.objects.get_or_create(
        event_edition=draft.event_edition, code="FUNDING", defaults={"label": "Funding"}
    )
    save_interests_step(
        registration=draft,
        interest_topic_ids=[topic.pk],
        objectives_text="Meet investors and mentors.",
    )
    ensure_processing_consent_purpose()
    return topic


def ensure_processing_consent_purpose():
    """The active processing-consent purpose every new submission needs (UX-C1,
    UX-F02). `privacy.0004` seeds it; a `transaction=True` test flushes that
    seed row away, so this restores it without changing an existing row."""
    from apps.privacy.models import ConsentPurpose
    from apps.registrations.services import DATA_PROCESSING_PURPOSE_CODE

    purpose, _ = ConsentPurpose.objects.get_or_create(
        code=DATA_PROCESSING_PURPOSE_CODE,
        defaults={"name": "Processing of personal data for registration and participation"},
    )
    return purpose


def notices_post_data(language: str = "en", *, without: tuple[str, ...] = (), **extra) -> dict:
    """The notices form as the page submits it: the three required boxes and
    the ids of the versions the page displayed in `language` (owner
    correction, 2026-10-04: acceptance is recorded only for those versions)."""
    from apps.privacy.selectors import effective_published_version_with_fallback

    privacy = effective_published_version_with_fallback("PRIVACY_NOTICE", language)
    terms = effective_published_version_with_fallback("TERMS", language)
    data = {
        "accept_privacy_notice": "on",
        "accept_terms": "on",
        "accept_data_processing": "on",
        "privacy_version_id": str(privacy.pk) if privacy else "",
        "terms_version_id": str(terms.pk) if terms else "",
        **extra,
    }
    for name in without:
        data.pop(name)
    return data
