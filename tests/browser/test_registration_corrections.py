"""Real-browser checks of the registration correction package (2026-10-04).

A real Chromium against `live_server`, under the enforced CSP. Synthetic data
only. Screenshots go to `var/test_artifacts/registration_corrections/screenshots/`.

* The official footer: the two exact lines, the directorate line smaller, no
  horizontal scrolling on a 375 px phone, wrapped lines, and in Arabic the
  French block keeps its left-to-right order while it sits on the page's
  reading side.
* The professional step marks the photograph as required (never "Optional")
  and refuses to continue without one.
* A withdrawn registration offers "Register again", which opens a new draft.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE = (
    Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "registration_corrections"
)
MINISTRY = "Ministère de l'Économie de la Connaissance, des Start-up et des Micro-entreprise"
DIRECTORATE = "Direction des Systèmes d’Information (DSI)"


def _shot(page, name: str) -> None:
    folder = EVIDENCE / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _cookie(person_id: str):
    from django.conf import settings
    from django.contrib.sessions.backends.db import SessionStore
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry

    session = SessionStore()
    now = timezone.now().isoformat()
    session[participant_auth.PARTICIPANT_SESSION_KEY] = person_id
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.save()
    return settings.SESSION_COOKIE_NAME, session.session_key


def _sign_in(page, live_server, person_id: str, language: str = "en") -> None:
    name, value = database_call(_cookie, person_id)
    page.context.add_cookies(
        [
            {"name": name, "value": value, "url": live_server.url},
            {"name": "django_language", "value": language, "url": live_server.url},
        ]
    )


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

_LAYOUT = """() => {
  const block = document.querySelector('[data-footer-institution]');
  const ministry = block.querySelector('.asc-footer-ministry');
  const directorate = block.querySelector('.asc-footer-directorate');
  const container = block.parentElement.getBoundingClientRect();
  const box = block.getBoundingClientRect();
  const line = parseFloat(getComputedStyle(ministry).lineHeight);
  return {
    overflow: document.documentElement.scrollWidth > window.innerWidth,
    ministrySize: parseFloat(getComputedStyle(ministry).fontSize),
    directorateSize: parseFloat(getComputedStyle(directorate).fontSize),
    ministryLines: Math.round(ministry.getBoundingClientRect().height / line),
    direction: getComputedStyle(block).direction,
    font: getComputedStyle(ministry).fontFamily,
    align: getComputedStyle(ministry).textAlign,
    pageDirection: getComputedStyle(document.documentElement).direction,
    rightGap: container.right - box.right,
    leftGap: box.left - container.left,
    textRight: Math.max(...Array.from(ministry.getClientRects()).map((r) => r.right)),
  };
}"""


@pytest.mark.parametrize(
    ("language", "width"),
    [("ar", 375), ("fr", 375), ("en", 1280)],
    ids=["arabic-phone", "french-phone", "english-desktop"],
)
def test_the_official_footer_is_readable_on_every_screen(live_server, page, language, width):
    page.set_viewport_size({"width": width, "height": 812})
    page.context.add_cookies(
        [{"name": "django_language", "value": language, "url": live_server.url}]
    )
    page.goto(f"{live_server.url}/accounts/start/")
    block = page.locator("[data-footer-institution]")
    expect(block.locator(".asc-footer-ministry")).to_have_text(MINISTRY)
    expect(block.locator(".asc-footer-directorate")).to_have_text(DIRECTORATE)
    block.scroll_into_view_if_needed()
    layout = page.evaluate(_LAYOUT)
    assert layout["overflow"] is False  # no horizontal scrolling
    assert layout["directorateSize"] < layout["ministrySize"]
    assert layout["direction"] == "ltr"  # the French lines keep their own order
    if width == 375:
        assert layout["ministryLines"] >= 2  # wraps instead of overflowing
    assert "Thmanyah" not in layout["font"]  # the Arabic font is for Arabic text only
    if language == "ar":
        assert layout["pageDirection"] == "rtl"
        assert layout["align"] == "right"  # on the Arabic page's reading side
    _shot(page, f"footer-{language}-{width}")


# ---------------------------------------------------------------------------
# The required profile photograph
# ---------------------------------------------------------------------------


def _draft_without_photo():
    from apps.core.models import Country, Sector
    from apps.documents.models import Document
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import make_event
    from apps.registrations.services import advance_current_step, get_or_create_active_draft
    from apps.registrations.tests.factories import walk_draft_through_every_step

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    person = resolve_or_create_participant_for_email(f"rc-{uuid.uuid4().hex[:8]}@example.test")
    draft = get_or_create_active_draft(
        person=person, event_edition=make_event(f"RC{uuid.uuid4().hex[:5].upper()}")
    )
    walk_draft_through_every_step(draft)
    Document.objects.filter(registration=draft, document_type="PROFILE_PHOTO").update(
        status="REPLACED"
    )
    advance_current_step(draft, "interests")
    return {"person": str(person.pk), "draft": str(draft.pk)}


def _activate(page, live_server, draft_id: str) -> None:
    page.goto(f"{live_server.url}/workspace/")
    page.locator(f"form[action$='/continue/{draft_id}/'] button").click()
    page.wait_for_load_state("networkidle")


def _has_eligible_photo(draft_id: str) -> bool:
    from apps.documents.services import active_profile_photo
    from apps.registrations.models import Registration

    return active_profile_photo(Registration.objects.get(pk=draft_id)) is not None


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_photo_is_required_at_the_professional_step(
    live_server, page, tmp_path, settings, language
):
    settings.PRIVATE_STORAGE_ROOT = tmp_path / "private"
    world = database_call(_draft_without_photo)
    _sign_in(page, live_server, world["person"], language)
    _activate(page, live_server, world["draft"])
    page.goto(f"{live_server.url}/register/professional/")
    section = page.locator("section[aria-labelledby='professional-photo-heading']")
    expect(section.locator("[data-profile-photo-required]")).to_be_visible()
    expect(section.locator(".asc-optional")).to_have_count(0)
    expect(page.locator("#id_profile_photo")).to_have_attribute("aria-required", "true")
    _shot(page, f"photo-required-{language}")

    page.locator("form.asc-wizard-form button[type='submit']").click()
    expect(page.locator("#id_profile_photo_error")).to_be_visible()
    _shot(page, f"photo-missing-error-{language}")
    assert database_call(_has_eligible_photo, world["draft"]) is False

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), color=(90, 120, 150)).save(buffer, format="PNG")
    photo = tmp_path / "photo.png"
    photo.write_bytes(buffer.getvalue())
    page.set_input_files("#id_profile_photo", str(photo))
    page.locator("form.asc-wizard-form button[type='submit']").click()
    expect(page).to_have_url(f"{live_server.url}/register/interests/")
    assert database_call(_has_eligible_photo, world["draft"]) is True


# ---------------------------------------------------------------------------
# Register again after a withdrawal
# ---------------------------------------------------------------------------


def _withdrawn_registration():
    import datetime

    from django.utils import timezone

    from apps.privacy.models import LegalDocument, LegalDocumentVersion
    from apps.registrations.models import Registration
    from apps.registrations.services import submit_full_registration
    from apps.reviews.services import withdraw_registration

    world = _draft_without_photo()
    draft = Registration.objects.get(pk=world["draft"])
    from apps.documents.services import save_profile_photo
    from apps.documents.tests.factories import make_test_photo

    save_profile_photo(registration=draft, person=draft.person, uploaded_file=make_test_photo())
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"rc-{code}-{uuid.uuid4().hex[:6]}",
                content="Synthetic",
                content_hash="c" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status="PUBLISHED",
            )
        )
    submit_full_registration(
        registration=draft,
        privacy_notice_version=versions[0],
        terms_version=versions[1],
        data_processing_consent_granted=True,
        session_reference="rc",
        idempotency_key=f"rc-{draft.pk}",
    )
    draft.refresh_from_db()
    withdraw_registration(registration=draft, person=draft.person, expected_version=draft.version)
    return world


def _drafts(person_id: str) -> list[str]:
    from apps.registrations.models import Registration

    return list(
        Registration.objects.filter(person_id=person_id, public_status="DRAFT").values_list(
            "public_reference", flat=True
        )
    )


def test_a_withdrawn_registration_offers_register_again(live_server, page, tmp_path, settings):
    settings.PRIVATE_STORAGE_ROOT = tmp_path / "private"
    world = database_call(_withdrawn_registration)
    _sign_in(page, live_server, world["person"], "fr")
    page.goto(f"{live_server.url}/workspace/")
    notice = page.locator("[data-withdrawn-next-step]")
    expect(notice).to_contain_text("Vous avez retiré cette inscription")
    button = notice.locator("[data-register-again='OPEN']")
    expect(button).to_have_text("S'inscrire à nouveau")
    _shot(page, "withdrawn-register-again-fr")
    button.click()
    expect(page.locator("form[data-identity-panels]")).to_be_visible()
    assert len(database_call(_drafts, world["person"])) == 1
    page.goto(f"{live_server.url}/workspace/")
    expect(page.locator("[data-withdrawn-next-step]")).to_contain_text(
        "Votre nouvelle inscription à cet événement figure sur cette page."
    )
    _shot(page, "withdrawn-registered-again-fr")
