"""Real-browser regressions for IDV-Q-C1, finding 3: register again by origin.

A real Chromium against `live_server`, under the enforced CSP. Synthetic data
only. Screenshots go to `var/test_artifacts/phase4/idv_q_c1/screenshots/`.

* An invitation registration rejected on identity grounds offers "Register
  again with your invitation" while its invitation is still valid, and the
  button opens a new draft through that invitation.
* When the invitation can no longer be used, the workspace offers no button
  and says that a new invitation from the inviting organization is needed.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "idv_q_c1"


def _shot(page, name: str) -> None:
    folder = EVIDENCE / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _versions():
    from django.utils import timezone

    from apps.core.models import Country, Sector
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    versions = []
    for code in ("PRIVACY_NOTICE", "TERMS"):
        document, _ = LegalDocument.objects.get_or_create(
            code=code, defaults={"document_type": code}
        )
        versions.append(
            LegalDocumentVersion.objects.create(
                legal_document=document,
                language="en",
                version_label=f"idv-q-c1-b-{code}-{uuid.uuid4().hex[:6]}",
                content="Synthetic",
                content_hash="b" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(versions)


def _rejected_invitation(revoke: bool):
    from apps.invitations.services import revoke_link
    from apps.people.tests.conftest import make_event, make_staff
    from apps.people.tests.test_idv_q_c1_register_again_origin import (
        _campaign,
        _invited_submission,
        _reject,
    )
    from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

    manager = make_staff(
        f"idv-q-c1-b-{uuid.uuid4().hex[:6]}@example.test", ACCREDITATION_MANAGERS_GROUP_NAME
    )
    event = make_event(f"QC1B{uuid.uuid4().hex[:4].upper()}")
    campaign, link = _campaign(event)
    registration = _reject(
        _invited_submission(
            event, _versions(), link, f"idv-q-c1-b-{uuid.uuid4().hex[:8]}@example.test"
        ),
        manager,
    )
    if revoke:
        revoke_link(campaign)
    return {"registration": str(registration.pk), "person": str(registration.person_id)}


def _participant_cookie(person_id: str):
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


def _new_invited_drafts(person_id: str) -> list[str]:
    from apps.registrations.models import Registration

    return list(
        Registration.objects.filter(
            person_id=person_id, public_status="DRAFT", source_kind="INVITATION"
        ).values_list("public_reference", flat=True)
    )


def _open(page, live_server, person_id: str) -> None:
    name, value = database_call(_participant_cookie, person_id)
    page.context.add_cookies([{"name": name, "value": value, "url": live_server.url}])
    page.goto(f"{live_server.url}/workspace/")
    page.wait_for_load_state("networkidle")


def test_a_valid_invitation_registers_again_through_the_invitation(live_server, page) -> None:
    world = database_call(_rejected_invitation, False)
    _open(page, live_server, world["person"])
    button = page.locator("[data-register-again='INVITATION']")
    expect(button).to_have_text("Register again with your invitation")
    _shot(page, "01-invitation-register-again")
    button.click()
    # A boosted form: wait for the visible result before reading the database.
    expect(page.locator("form[data-identity-panels]")).to_be_visible()
    assert len(database_call(_new_invited_drafts, world["person"])) == 1


def test_an_unusable_invitation_asks_for_a_new_one(live_server, page) -> None:
    world = database_call(_rejected_invitation, True)
    _open(page, live_server, world["person"])
    notice = page.locator("[data-register-again-next='NEW_INVITATION_NEEDED']")
    expect(notice).to_contain_text("ask the organization that invited you for a new invitation")
    expect(page.locator("[data-register-again]")).to_have_count(0)
    _shot(page, "02-invitation-needs-a-new-invitation")
    assert database_call(_new_invited_drafts, world["person"]) == []
