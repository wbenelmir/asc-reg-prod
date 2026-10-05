"""Shared fixtures for the real-browser (Playwright) accessibility/i18n/RTL suite
(Prompt 4 final closure pass §9). `live_server` needs transactional database
access, so every test here re-establishes the seed data it needs rather than
relying on migration-seeded rows surviving a flush between tests.
"""

from __future__ import annotations

from inspect import unwrap

import pytest
from django.utils import timezone
from pytest_playwright import pytest_playwright as playwright_fixtures

from tests.browser.database import database_fixture, database_sync

# Reuse the installed plugin's fixture bodies and options, changing only their
# lifetime. A session-wide sync driver leaves its event loop on the test thread
# during Django's next setup/flush. Function scope closes it before DB teardown.
browser_type = pytest.fixture(scope="function")(unwrap(playwright_fixtures.browser_type))
launch_browser = pytest.fixture(scope="function")(unwrap(playwright_fixtures.launch_browser))
browser = pytest.fixture(scope="function")(unwrap(playwright_fixtures.browser))
browser_context_args = pytest.fixture(scope="function")(
    unwrap(playwright_fixtures.browser_context_args)
)


@pytest.fixture
def playwright(request):
    # Explicit ordering keeps the real PostgreSQL test setup outside the loop;
    # the DB-free offline-runtime tests still need no database at all.
    if request.node.get_closest_marker("django_db") or any(
        name in request.fixturenames for name in ("db", "transactional_db", "live_server")
    ):
        request.getfixturevalue("transactional_db")
    yield from unwrap(playwright_fixtures.playwright)()


@pytest.fixture
@database_sync
def seeded_open_event(db):
    from apps.core.models import Country, Sector
    from apps.events.models import EventEdition, EventEditionStatus

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    # Exactly one open edition -- `events.0002` may have already seeded
    # "ASC2026"; reuse it by code rather than risking a second OPEN edition,
    # which the Prompt 4 final closure pass §10 fail-closed selector rejects.
    EventEdition.objects.exclude(code="ASC2026").filter(
        status=EventEditionStatus.REGISTRATION_OPEN
    ).update(status=EventEditionStatus.REGISTRATION_CLOSED)
    edition, _ = EventEdition.objects.get_or_create(
        code="ASC2026",
        defaults={
            "name": "ASC 2026",
            "timezone": "UTC",
            "starts_at": timezone.now(),
            "ends_at": timezone.now(),
            "status": EventEditionStatus.REGISTRATION_OPEN,
            "supported_languages": ["en", "fr", "ar"],
        },
    )
    if edition.status != EventEditionStatus.REGISTRATION_OPEN:
        edition.status = EventEditionStatus.REGISTRATION_OPEN
        edition.save(update_fields=["status"])
    return edition


@pytest.fixture
@database_sync
def seeded_legal_notices(db):
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus

    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        for language in ("en", "fr", "ar"):
            LegalDocumentVersion.objects.get_or_create(
                legal_document=document,
                language=language,
                version_label="browser-test",
                defaults={
                    "content": f"[TEST] {code} content in {language}.",
                    "content_hash": "0" * 64,
                    "effective_from": timezone.now(),
                    "status": LegalDocumentVersionStatus.PUBLISHED,
                },
            )
    # The required processing consent needs its active purpose (UX-C1,
    # UX-F02); the flush between live-server tests removes the seeded row.
    from apps.registrations.tests.factories import ensure_processing_consent_purpose

    ensure_processing_consent_purpose()


#: Reports every `securitypolicyviolation` of the page to the console, where
#: the fixture below collects it with the browser's own CSP messages.
_CSP_COLLECTOR = """(() => {
  document.addEventListener('securitypolicyviolation', (e) => {
    console.warn('ASC-CSP-VIOLATION ' + e.effectiveDirective + ' ' + (e.blockedURI || '-') +
      ' ' + (e.sourceFile || '-') + ':' + (e.lineNumber || 0));
  }, true);
})();"""
_CSP_MARKERS = ("ASC-CSP-VIOLATION", "Content Security Policy", "Permissions-Policy")


@pytest.fixture(autouse=True)
def _no_csp_or_permissions_policy_violation(request):
    """P4-4: every Playwright journey that uses the `page` fixture runs under the
    enforced Content Security Policy and fails on any violation or on a
    Permissions-Policy header error. Contexts a test opens itself are not
    covered by this fixture."""
    if "page" not in request.fixturenames:
        yield
        return
    page = request.getfixturevalue("page")
    found: list[str] = []

    def collect(message) -> None:
        text = message.text
        if any(marker in text for marker in _CSP_MARKERS):
            found.append(text[:300])

    page.on("console", collect)
    page.add_init_script(_CSP_COLLECTOR)
    yield
    if found:
        pytest.fail(
            "Content Security Policy or Permissions-Policy problem(s): " + " | ".join(found)
        )


@pytest.fixture
def isolated_private_storage(tmp_path, settings):
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root


# Synthetic identities shared by the Prompt 5 entry browser suites.
SYNTHETIC_NAME_EN = "Synthetic Participant Nadia"
SYNTHETIC_NAME_AR = "مشاركة تجريبية نادية"
SYNTHETIC_NIN = "109990000000000042"


@pytest.fixture
@database_fixture
def entry_world():
    """A synthetic event, checkpoint device, supervisor and two active passes
    for the Phase 3 Prompt 5 entry browser suites."""
    from apps.core.crypto.signing import (
        InMemorySigningKeyProvider,
        set_signing_key_provider_for_testing,
    )
    from apps.entry.tests import factories

    provider = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
    set_signing_key_provider_for_testing(provider)
    factories.seed_reference_values()
    event = factories.make_event("ASCUI26")
    layout = factories.VenueLayout(event)
    layout.gate_a.name = "North Gate"
    layout.gate_a.save(update_fields=["name"])
    setup = factories.AccreditationSetup(event, layout)
    staff = factories.make_user("staff@example.test")
    factories.publish_key(provider=provider, actor=staff)

    def participant(name):
        person = factories.make_person(name)
        registration = factories.make_registration(event=event, person=person)
        factories.assign(registration=registration, setup=setup, actor=staff)
        credential = factories.issue_active_pass(registration=registration, actor=staff)
        return person, registration, factories.token_for(credential)

    person_en, _reg_en, token_en = participant(SYNTHETIC_NAME_EN)
    _person_ar, _reg_ar, token_ar = participant(SYNTHETIC_NAME_AR)
    # A declared (unverified) NIN: the identity lookup must ask for a manual check.
    factories.add_identifier(
        person=person_en, identifier_type="NIN", country="DZ", value=SYNTHETIC_NIN, verified=False
    )
    admin = factories.make_user(
        "device.admin@example.test", group_name="Entry Device Administrators", event=event
    )
    supervisor = factories.make_user(
        "gate.supervisor@example.test",
        group_name="Entry Supervisors",
        event=event,
        gate=layout.gate_a,
    )
    supervisor.display_name = "Samir (supervisor)"
    supervisor.save(update_fields=["display_name"])
    device, secret = factories.enroll_device(event=event, layout=layout, admin=admin)
    device.public_name = "North Gate tablet 1"
    device.save(update_fields=["public_name"])
    yield {
        "event": event,
        "layout": layout,
        "operator": supervisor,
        "secret": secret,
        "device": device,
        "token_en": token_en,
        "token_ar": token_ar,
    }
    set_signing_key_provider_for_testing(None)
