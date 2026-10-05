"""Real-browser coverage of the identity review interface (IDV-3, IDV-4).

A real Chromium against `live_server`, under the enforced CSP (the autouse
collector fails any violation). Synthetic identities and synthetic images
only. Screenshots and the timing record go to
`var/test_artifacts/phase4/idv/` (this package's own evidence folder).

The timing test is a CONTROLLED, automated exercise on a small synthetic
queue: it records what it observed and asserts no speed target. The human
timing exercise is in the UAT guide.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "idv"
PASSWORD = "__test_password__"  # noqa: S105 - the shared synthetic test password


def _shot(page, name: str) -> None:
    folder = EVIDENCE / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _world(case_count: int = 3):
    """Seed an event, `case_count` NOT_FOUND cases with a card, one verified
    case, a reviewer and a manager. Runs on the database thread."""
    from django.utils import timezone

    from apps.core.models import Country, Sector
    from apps.people.tests.conftest import (
        SIM_MATCH_NIN,
        case_for,
        make_event,
        make_staff,
        process_all,
        submit_case,
        upload_national_id_card,
    )
    from apps.privacy.models import LegalDocument, LegalDocumentVersion, LegalDocumentVersionStatus
    from apps.reviews.apps import (
        ACCREDITATION_MANAGERS_GROUP_NAME,
        REGISTRATION_REVIEWERS_GROUP_NAME,
    )

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
                version_label=f"idv-b-{code}",
                content="Synthetic",
                content_hash="d" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    event = make_event("IDVBROWSER")
    registrations = []
    for index in range(case_count):
        registration = submit_case(
            event,
            tuple(versions),
            nin=f"1234567890123456{index:02d}",
            given="Amina",
            family=f"Synthetic{chr(65 + index)}",
        )
        registrations.append(registration)
    verified = submit_case(event, tuple(versions), nin=SIM_MATCH_NIN)
    process_all()
    for registration in registrations:
        upload_national_id_card(registration)
    reviewer = make_staff(
        "idv-b-reviewer@example.test", REGISTRATION_REVIEWERS_GROUP_NAME, event=event
    )
    manager = make_staff(
        "idv-b-manager@example.test", ACCREDITATION_MANAGERS_GROUP_NAME, event=event
    )
    return {
        "cases": [str(case_for(r).pk) for r in registrations],
        "references": [r.public_reference for r in registrations],
        "verified_reference": verified.public_reference,
        "reviewer": reviewer.email_normalized,
        "manager": manager.email_normalized,
        "registrations": [str(r.pk) for r in registrations],
        "people": [str(r.person_id) for r in registrations],
    }


def _sign_in(page, live_server, email: str) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", email)
    page.fill("#id_password", PASSWORD)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")


def _open_case(page, live_server, case_id: str, query: str = "status=MANUAL_REVIEW") -> None:
    page.goto(f"{live_server.url}/ops/identity/cases/{case_id}/?{query}")
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-image]")).to_be_visible()
    page.wait_for_function("() => document.querySelector('[data-idv-image]').naturalWidth > 0")


def _transform(image) -> tuple[str, str]:
    """(zoom class, rotation class) currently on the evidence image."""
    classes = image.evaluate("el => [...el.classList]")
    zoom = next(c for c in classes if c.startswith("asc-idv-z"))
    rotation = next(c for c in classes if c.startswith("asc-idv-r"))
    return zoom, rotation


def _decisions(case_id: str) -> int:
    from apps.people.models import IdentityDecision

    return IdentityDecision.objects.filter(verification_id=case_id).count()


def _status(case_id: str) -> str:
    from apps.people.models import IdentityVerification

    return IdentityVerification.objects.get(pk=case_id).status


def test_the_queue_and_the_side_by_side_review_screen(live_server, page) -> None:
    world = database_call(_world)
    page.set_viewport_size({"width": 1440, "height": 900})
    _sign_in(page, live_server, world["reviewer"])
    page.goto(f"{live_server.url}/ops/identity/?status=MANUAL_REVIEW&route=NIN")
    for reference in world["references"]:
        expect(page.locator("table[aria-label='Identity cases']")).to_contain_text(reference)
    expect(page.locator("[data-status-count='MANUAL_REVIEW']")).to_have_text("3")
    assert world["verified_reference"] not in page.locator("table").inner_text()
    _shot(page, "01-queue-desktop-en")

    page.locator("[data-idv-open]").first.click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-reason='NOT_FOUND']")).to_be_visible()
    page.wait_for_function("() => document.querySelector('[data-idv-image]').naturalWidth > 0")
    facts = page.locator(".asc-idv-facts").bounding_box()
    viewer = page.locator("[data-idv-viewer]").bounding_box()
    assert viewer["x"] > facts["x"] + facts["width"] - 1  # side by side on desktop
    assert abs(viewer["y"] - facts["y"]) < 40
    _shot(page, "02-review-desktop-en")


def test_viewer_controls_and_safe_keyboard_shortcuts(live_server, page) -> None:
    world = database_call(_world)
    page.set_viewport_size({"width": 1440, "height": 900})
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    image = page.locator("[data-idv-image]")
    page.get_by_role("button", name="Zoom in").click()
    expect(page.locator("[data-idv-zoom-level]")).to_have_text("125%")
    assert _transform(image) == ("asc-idv-z1", "asc-idv-r0")
    page.get_by_role("button", name="Rotate right").click()
    assert _transform(image) == ("asc-idv-z1", "asc-idv-r90")
    page.locator("body").press("0")
    assert _transform(image) == ("asc-idv-z0", "asc-idv-r0")
    page.locator("body").press("+")
    page.locator("body").press("r")
    assert _transform(image) == ("asc-idv-z1", "asc-idv-r90")
    _shot(page, "03-viewer-zoomed-rotated")
    page.locator("body").press("0")

    # Typing in a field never triggers a shortcut.
    note = page.locator("[data-idv-form='verify'] textarea")
    note.click()
    note.type("r+0 v n q")
    expect(note).to_have_value("r+0 v n q")
    assert _transform(image) == ("asc-idv-z0", "asc-idv-r0")
    url_before = page.url

    # "V" only moves focus to the Verify form; nothing is submitted.
    page.locator("[data-idv-stage]").focus()
    page.keyboard.press("v")
    focused_in_verify = page.evaluate(
        "() => !!document.activeElement.closest(\"[data-idv-action='verify']\")"
    )
    assert focused_in_verify
    assert page.url == url_before
    assert database_call(_decisions, world["cases"][0]) == 0


def test_verify_and_open_the_next_case_keeps_the_queue_filters(live_server, page) -> None:
    world = database_call(_world)
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0], "status=MANUAL_REVIEW&route=NIN")
    page.get_by_role("button", name="Verify and open the next case").click()
    page.wait_for_url(f"**/ops/identity/cases/{world['cases'][1]}/**")
    assert "route=NIN" in page.url and "status=MANUAL_REVIEW" in page.url
    expect(page.locator("#main-content")).to_contain_text("Identity verified manually")
    assert database_call(_status, world["cases"][0]) == "MANUALLY_VERIFIED"
    _shot(page, "04-next-case-after-verify")


def test_a_double_click_records_one_decision(live_server, page) -> None:
    world = database_call(_world)
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    page.get_by_role("button", name="Verify identity", exact=True).dblclick()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-status='MANUALLY_VERIFIED']")).to_be_visible()
    assert database_call(_decisions, world["cases"][0]) == 1


def test_a_second_reviewer_gets_a_clear_conflict(live_server, browser, page) -> None:
    world = database_call(_world)
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    other_context = browser.new_context()
    other = other_context.new_page()
    try:
        _sign_in(other, live_server, world["manager"])
        _open_case(other, live_server, world["cases"][0])
        page.get_by_role("button", name="Verify identity", exact=True).click()
        page.wait_for_load_state("networkidle")
        other.get_by_role("button", name="Verify identity", exact=True).click()
        other.wait_for_load_state("networkidle")
        expect(other.locator("[data-idv-conflict-message]")).to_contain_text(
            "changed after you opened it"
        )
        _shot(other, "05-concurrent-reviewer-conflict")
    finally:
        other_context.close()
    assert database_call(_decisions, world["cases"][0]) == 1


def test_final_rejection_needs_a_deliberate_confirmation(live_server, page) -> None:
    world = database_call(_world)
    _sign_in(page, live_server, world["manager"])
    _open_case(page, live_server, world["cases"][0])
    page.locator("[data-idv-action='reject'] summary").click()
    form = page.locator("[data-idv-form='reject']")
    form.locator("textarea").fill("The document does not match the submitted identity.")
    form.locator("input[type=checkbox]").check()
    form.get_by_role("button", name="Reject finally").click()
    dialog = page.locator("#asc-confirm-dialog")
    expect(dialog).to_be_visible()
    assert database_call(_status, world["cases"][0]) == "MANUAL_REVIEW"  # nothing yet
    _shot(page, "06-reject-confirmation-dialog")
    dialog.locator("[data-confirm-accept]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-status='REJECTED']")).to_be_visible()


def test_the_review_screen_stacks_on_a_phone(live_server, page) -> None:
    world = database_call(_world)
    page.set_viewport_size({"width": 390, "height": 844})
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    facts = page.locator(".asc-idv-facts").bounding_box()
    viewer = page.locator("[data-idv-viewer]").bounding_box()
    assert viewer["y"] >= facts["y"] + facts["height"] - 1  # stacked, facts first
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    _shot(page, "07-review-phone")


def test_the_review_screen_in_arabic_is_right_to_left(live_server, page) -> None:
    from tests.browser.helpers import switch_language

    world = database_call(_world)
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    switch_language(page, "ar")
    page.wait_for_load_state("networkidle")
    assert page.locator("html").get_attribute("dir") == "rtl"
    expect(page.locator("[data-idv-viewer]")).to_be_visible()
    _shot(page, "08-review-arabic-rtl")


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


def _return_first_case(world) -> None:
    from django.contrib.auth import get_user_model

    from apps.people.models import IdentityVerification
    from apps.people.services.identity_review import return_identity_for_correction

    case = IdentityVerification.objects.get(pk=world["cases"][0])
    return_identity_for_correction(
        case.pk,
        actor=get_user_model().objects.get(email_normalized=world["reviewer"]),
        expected_version=case.version,
        items=["NIN_NUMBER", "NATIONAL_ID_CARD"],
    )


def test_the_participant_corrects_on_the_same_registration(live_server, page, tmp_path) -> None:
    from apps.documents.tests.factories import make_test_photo

    world = database_call(_world)
    database_call(_return_first_case, world)
    name, value = database_call(_participant_cookie, world["people"][0])
    page.context.add_cookies([{"name": name, "value": value, "url": live_server.url}])
    page.goto(f"{live_server.url}/workspace/")
    page.wait_for_load_state("networkidle")
    page.get_by_role("link", name="Correct your identity information").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-requested]")).to_contain_text("national identity number")
    _shot(page, "09-participant-correction")
    # A NIN nobody else holds (990000000000000010 belongs to the verified case).
    page.fill("#id_nin_value", "990000000000000028")
    image = tmp_path / "card.png"
    image.write_bytes(make_test_photo().read())
    page.set_input_files("#id_national_id_card", str(image))
    page.get_by_role("button", name="Send the correction").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#main-content")).to_contain_text("Your corrected information was received")
    assert database_call(_status, world["cases"][0]) == "PENDING"


def test_controlled_timing_of_a_small_synthetic_queue(live_server, page) -> None:
    """Observed, not promised: the time to verify five synthetic cases with
    the "Verify and open the next case" flow (reviewer already on the first
    case, evidence loaded, no human reading time)."""
    world = database_call(_world, 5)
    page.set_viewport_size({"width": 1440, "height": 900})
    _sign_in(page, live_server, world["reviewer"])
    _open_case(page, live_server, world["cases"][0])
    durations = []
    for index, case_id in enumerate(world["cases"]):
        started = time.perf_counter()
        page.get_by_role("button", name="Verify and open the next case").click()
        if index < len(world["cases"]) - 1:
            page.wait_for_url(f"**/ops/identity/cases/{world['cases'][index + 1]}/**")
            page.wait_for_function(
                "() => document.querySelector('[data-idv-image]').naturalWidth > 0"
            )
        else:
            page.wait_for_url("**/ops/identity/**")
        durations.append(round((time.perf_counter() - started) * 1000))
        assert database_call(_status, case_id) == "MANUALLY_VERIFIED"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "review-timing.json").write_text(
        json.dumps(
            {
                "exercise": "automated controlled timing, 5 synthetic NOT_FOUND cases",
                "measure": "click on 'Verify and open the next case' until the next case's "
                "evidence image is loaded (ms); excludes human reading and decision time",
                "durations_ms": durations,
                "total_ms": sum(durations),
                "note": "observed on the local development host; not a performance target",
            },
            indent=1,
        ),
        encoding="utf-8",
    )
