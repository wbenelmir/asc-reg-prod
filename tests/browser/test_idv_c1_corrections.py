"""Real-browser regressions for the IDV-C1 corrections (R-IDV-01, -02, -05, -06).

A real Chromium against `live_server`, under the enforced CSP (the autouse
collector fails any violation). Synthetic identities and images only.
Screenshots go to `var/test_artifacts/phase4/idv_c1/screenshots/`.

* R-IDV-05: a NIN typed into the queue search never appears in the address,
  in any link, or in any request the browser makes afterwards (queue, case,
  evidence preview, decision and redirect).
* R-IDV-06: once the registration is withdrawn, the workspace no longer offers
  the identity correction, and a correction form left open is refused.
* R-IDV-01: a reviewer who opened a case before the same person's NIN was
  corrected in another registration gets a clear conflict, and the refreshed
  case shows the replacement.
* R-IDV-02: the review screen shows the official name form and the names as
  entered.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import solve_staff_captcha

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "idv_c1"
PASSWORD = "__test_password__"  # noqa: S105 - the shared synthetic test password
CORRECTED_NIN = "123456789012345670"


def _shot(page, name: str) -> None:
    folder = EVIDENCE / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _versions():
    from django.utils import timezone

    from apps.core.models import Country, Sector
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Country.objects.get_or_create(code="FR", defaults={"name": "France"})
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
                version_label=f"idv-c1-b-{code}",
                content="Synthetic",
                content_hash="f" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(versions)


def _staff(group_name: str, email: str):
    from apps.people.tests.conftest import make_staff

    return make_staff(email, group_name).email_normalized


def _search_world():
    from apps.people.tests.conftest import (
        case_for,
        make_event,
        process_all,
        submit_case,
        upload_national_id_card,
    )
    from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

    versions = _versions()
    event = make_event("IDVC1BROWSE")
    registrations = [
        submit_case(event, versions, nin=f"12345678901234562{i}", family=f"Browse{chr(65 + i)}")
        for i in range(3)
    ]
    process_all()
    for registration in registrations:
        upload_national_id_card(registration)
    cases = [case_for(registration) for registration in registrations]
    return {
        "reviewer": _staff(REGISTRATION_REVIEWERS_GROUP_NAME, "idv-c1-b-search@example.test"),
        "target": str(cases[1].pk),
        "nin": cases[1].current_revision.identifier.value_encrypted,
        "target_reference": registrations[1].public_reference,
        "other_reference": registrations[0].public_reference,
    }


def _sign_in(page, live_server, email: str) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", email)
    page.fill("#id_password", PASSWORD)
    solve_staff_captcha(page)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")


def _hrefs(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('[href],[action],[src]')]"
        ".map(e => e.getAttribute('href') || e.getAttribute('action') || e.getAttribute('src'))"
    )


def test_a_nin_search_keeps_the_nin_out_of_every_url(live_server, page) -> None:
    world = database_call(_search_world)
    requested: list[str] = []
    page.on("request", lambda request: requested.append(request.url))
    _sign_in(page, live_server, world["reviewer"])
    page.goto(f"{live_server.url}/ops/identity/")
    page.fill("#id_q", world["nin"])
    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle")

    expect(page.locator("table[aria-label='Identity cases']")).to_contain_text(
        world["target_reference"]
    )
    assert world["other_reference"] not in page.locator("table").inner_text()
    expect(page.locator("[data-idv-search-active]")).to_be_visible()
    assert "ctx=" in page.url
    _shot(page, "01-search-without-nin-in-url")

    page.locator("[data-idv-open]").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => document.querySelector('[data-idv-image]').naturalWidth > 0")
    page.get_by_role("button", name="Verify and open the next case").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#main-content")).to_contain_text("Identity verified manually")

    assert requested, "no request was recorded"
    assert all(world["nin"] not in url for url in requested), "a request URL carried the NIN"
    assert all(world["nin"] not in (url or "") for url in _hrefs(page))
    assert world["nin"] not in page.url


def _withdrawal_world():
    from apps.people.services.identity_review import return_identity_for_correction
    from apps.people.tests.conftest import (
        UNKNOWN_NIN,
        case_for,
        make_event,
        make_staff,
        process_all,
        submit_case,
    )
    from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

    registration = submit_case(make_event("IDVC1WITHDRAW"), _versions(), nin=UNKNOWN_NIN)
    process_all()
    reviewer = make_staff("idv-c1-b-withdraw@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)
    case = case_for(registration)
    return_identity_for_correction(
        case.pk, actor=reviewer, expected_version=case.version, items=["NIN_NUMBER"]
    )
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


def _withdraw(registration_id: str) -> None:
    from apps.registrations.models import Registration
    from apps.reviews.services import withdraw_registration

    registration = Registration.objects.get(pk=registration_id)
    withdraw_registration(
        registration=registration, person=registration.person, expected_version=registration.version
    )


def _public_status(registration_id: str) -> str:
    from apps.registrations.models import Registration

    return Registration.objects.get(pk=registration_id).public_status


def test_a_withdrawn_registration_offers_no_identity_correction(live_server, page) -> None:
    world = database_call(_withdrawal_world)
    name, value = database_call(_participant_cookie, world["person"])
    page.context.add_cookies([{"name": name, "value": value, "url": live_server.url}])
    page.goto(f"{live_server.url}/workspace/")
    page.wait_for_load_state("networkidle")
    page.get_by_role("link", name="Correct your identity information").click()
    page.wait_for_load_state("networkidle")
    page.fill("#id_nin_value", CORRECTED_NIN)

    database_call(_withdraw, world["registration"])  # in another tab, say
    page.get_by_role("button", name="Send the correction").click()
    page.wait_for_load_state("networkidle")

    # Sent back to the workspace (a boosted form may keep the old address);
    # waiting for the notice also waits for the request to finish.
    expect(page.locator("#main-content")).to_contain_text(
        "There is no identity correction to make for this registration."
    )
    assert database_call(_public_status, world["registration"]) == "WITHDRAWN"
    expect(page.get_by_role("link", name="Correct your identity information")).to_have_count(0)
    _shot(page, "02-withdrawn-no-correction")


def _linked_world():
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import (
        UNKNOWN_NIN,
        case_for,
        make_event,
        process_all,
        submit_case,
        upload_national_id_card,
    )
    from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

    versions = _versions()
    person = resolve_or_create_participant_for_email(
        f"idv-c1-b-{uuid.uuid4().hex[:8]}@example.test"
    )
    first = submit_case(make_event("IDVC1LINKA"), versions, nin=UNKNOWN_NIN, person=person)
    second = submit_case(make_event("IDVC1LINKB"), versions, nin=UNKNOWN_NIN, person=person)
    process_all()
    for registration in (first, second):
        upload_national_id_card(registration)
    return {
        "reviewer": _staff(REGISTRATION_REVIEWERS_GROUP_NAME, "idv-c1-b-linked@example.test"),
        "first": str(case_for(first).pk),
        "second_registration": str(second.pk),
    }


def _correct_second(world) -> None:
    from django.contrib.auth import get_user_model

    from apps.people.services.identity_review import correct_nin_and_recheck
    from apps.people.tests.conftest import case_for
    from apps.registrations.models import Registration

    registration = Registration.objects.get(pk=world["second_registration"])
    case = case_for(registration)
    card = registration.documents.get(document_type="NATIONAL_ID_CARD", status="ACTIVE")
    correct_nin_and_recheck(
        case.pk,
        actor=get_user_model().objects.get(email_normalized=world["reviewer"]),
        expected_version=case.version,
        new_nin=CORRECTED_NIN,
        reason_code="TYPING_ERROR_CONFIRMED",
        evidence_document_id=card.pk,
        confirmed=True,
    )


def test_a_stale_confirmation_after_a_linked_correction_shows_a_conflict(live_server, page) -> None:
    world = database_call(_linked_world)
    _sign_in(page, live_server, world["reviewer"])
    page.goto(f"{live_server.url}/ops/identity/cases/{world['first']}/?status=MANUAL_REVIEW")
    page.wait_for_load_state("networkidle")

    database_call(_correct_second, world)
    page.get_by_role("button", name="Verify identity", exact=True).click()
    page.wait_for_load_state("networkidle")

    expect(page.locator("[data-idv-conflict-message]")).to_be_visible()
    _shot(page, "03-linked-correction-conflict")
    page.goto(f"{live_server.url}/ops/identity/cases/{world['first']}/?status=MANUAL_REVIEW")
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-reason='IDENTITY_DATA_CHANGED']")).to_be_visible()
    # A reviewer may see evidence, so the replacement NIN is shown in full.
    expect(page.locator("[data-idv-identifier]")).to_contain_text(CORRECTED_NIN)


def _official_names_world():
    from apps.people.services.identity_verification import run_due_identity_jobs
    from apps.people.tests.conftest import case_for, make_event, submit_case
    from apps.reviews.apps import REGISTRATION_REVIEWERS_GROUP_NAME

    registration = submit_case(make_event("IDVC1NAMES"), _versions())
    # The development simulation: the same rule, labelled "not official", so
    # this screenshot can never be read as ministry evidence.
    run_due_identity_jobs()
    return {
        "reviewer": _staff(REGISTRATION_REVIEWERS_GROUP_NAME, "idv-c1-b-names@example.test"),
        "case": str(case_for(registration).pk),
    }


def test_the_review_screen_shows_the_official_name_form(live_server, page) -> None:
    world = database_call(_official_names_world)
    _sign_in(page, live_server, world["reviewer"])
    page.goto(f"{live_server.url}/ops/identity/cases/{world['case']}/?status=ALL")
    page.wait_for_load_state("networkidle")
    notice = page.locator("[data-idv-official-names]")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("Amina")  # as entered
    expect(page.locator("#main-content")).to_contain_text("AMINA")  # the current, official form
    expect(page.locator("[data-idv-source='SIMULATED_API']")).to_be_visible()
    _shot(page, "04-official-name-form-simulated")
