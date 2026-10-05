"""Real-browser regressions for the owner decisions IDV-Q1 to IDV-Q3 (2026-10-02).

A real Chromium against `live_server`, under the enforced CSP (the autouse
collector fails any violation). Synthetic identities and images only.
Screenshots go to `var/test_artifacts/phase4/idv_q/screenshots/`.

* IDV-Q1: a decider sees why an unverified identity cannot be approved, and
  the approve control refuses it.
* IDV-Q2: after a final identity rejection through the review screen, the
  participant's workspace says the registration was rejected and how to
  register again; "Register again" opens a new draft on the same account.
* IDV-Q3: a manager grants the NIN exemption on the intake page; the
  participant then completes the identity step with an Algerian identity
  card, without a NIN; the reviewer finds the case labelled in the queue.
"""

from __future__ import annotations

import datetime
import io
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.database import database_call
from tests.browser.helpers import fill_date

pytestmark = pytest.mark.django_db(transaction=True)

EVIDENCE = Path(__file__).resolve().parents[2] / "var" / "test_artifacts" / "phase4" / "idv_q"
PASSWORD = "__test_password__"  # noqa: S105 - the shared synthetic test password


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
                version_label=f"idv-q-b-{code}-{uuid.uuid4().hex[:6]}",
                content="Synthetic",
                content_hash="d" * 64,
                effective_from=timezone.now() - datetime.timedelta(minutes=1),
                status=LegalDocumentVersionStatus.PUBLISHED,
            )
        )
    return tuple(versions)


def _sign_in(page, live_server, email: str) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", email)
    page.fill("#id_password", PASSWORD)
    page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("networkidle")


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


def _as_participant(page, live_server, person_id: str) -> None:
    name, value = database_call(_participant_cookie, person_id)
    page.context.add_cookies([{"name": name, "value": value, "url": live_server.url}])


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (320, 220), color=(140, 150, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# IDV-Q1
# ---------------------------------------------------------------------------


def _approval_world():
    from apps.people.tests.conftest import (
        UNKNOWN_NIN,
        make_event,
        make_staff,
        process_all,
        submit_case,
    )
    from apps.people.tests.identity_fixtures import assign_approval_prerequisites
    from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME
    from apps.reviews.services import open_review_case

    manager = make_staff("idv-q-b-approve@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)
    registration = submit_case(make_event("IDVQBAPPROVE"), _versions(), nin=UNKNOWN_NIN)
    process_all()
    assign_approval_prerequisites(registration, manager)
    case = open_review_case(registration=registration, case_type="STANDARD", queue_code="GENERAL")
    return {
        "manager": manager.email_normalized,
        "case": str(case.pk),
        "registration": str(registration.pk),
    }


def _public_status(registration_id: str) -> str:
    from apps.registrations.models import Registration

    return Registration.objects.get(pk=registration_id).public_status


def test_an_unverified_identity_cannot_be_approved(live_server, page) -> None:
    world = database_call(_approval_world)
    _sign_in(page, live_server, world["manager"])
    page.goto(f"{live_server.url}/ops/reviews/cases/{world['case']}/")
    page.wait_for_load_state("networkidle")
    notice = page.locator("[data-identity-clearance='IDENTITY_NOT_VERIFIED']")
    expect(notice).to_be_visible()
    page.get_by_role("button", name="Record Approved decision").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#main-content")).to_contain_text("The identity is not verified yet")
    assert database_call(_public_status, world["registration"]) == "SUBMITTED"
    _shot(page, "01-approval-refused-unverified-identity")


# ---------------------------------------------------------------------------
# IDV-Q2
# ---------------------------------------------------------------------------


def _rejection_world():
    from apps.people.tests.conftest import (
        UNKNOWN_NIN,
        case_for,
        make_event,
        make_staff,
        process_all,
        submit_case,
    )
    from apps.reviews.apps import ACCREDITATION_MANAGERS_GROUP_NAME

    manager = make_staff("idv-q-b-reject@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)
    registration = submit_case(make_event("IDVQBREJECT"), _versions(), nin=UNKNOWN_NIN)
    process_all()
    return {
        "manager": manager.email_normalized,
        "case": str(case_for(registration).pk),
        "registration": str(registration.pk),
        "reference": registration.public_reference,
        "person": str(registration.person_id),
    }


def _current_drafts(person_id: str) -> list[str]:
    from apps.registrations.models import Registration

    return list(
        Registration.objects.filter(
            person_id=person_id, public_status="DRAFT", is_current_context=True
        ).values_list("public_reference", flat=True)
    )


def test_a_final_rejection_is_explained_and_the_participant_can_register_again(
    live_server, page, browser
) -> None:
    world = database_call(_rejection_world)
    _sign_in(page, live_server, world["manager"])
    page.goto(f"{live_server.url}/ops/identity/cases/{world['case']}/?status=MANUAL_REVIEW")
    page.wait_for_load_state("networkidle")
    page.locator("[data-idv-action='reject'] summary").click()
    form = page.locator("[data-idv-form='reject']")
    form.locator("textarea").fill("The document does not match the submitted identity.")
    form.locator("input[type=checkbox]").check()
    form.get_by_role("button", name="Reject finally").click()
    dialog = page.locator("#asc-confirm-dialog")
    expect(dialog).to_contain_text("register again")
    dialog.locator("[data-confirm-accept]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-idv-rejection-outcome]")).to_be_visible()
    _shot(page, "02-staff-rejection-outcome")
    assert database_call(_public_status, world["registration"]) == "NOT_APPROVED"

    participant_context = browser.new_context()
    participant = participant_context.new_page()
    try:
        _as_participant(participant, live_server, world["person"])
        participant.goto(f"{live_server.url}/workspace/")
        participant.wait_for_load_state("networkidle")
        notice = participant.locator("[data-identity-rejected]")
        expect(notice).to_contain_text("This registration was rejected")
        expect(notice).to_contain_text("register again from this account")
        assert "does not match" not in participant.locator("#main-content").inner_text()
        _shot(participant, "03-participant-rejection-notice")
        participant.locator("[data-register-again]").click()
        # A boosted form: wait for the visible result before reading the database.
        expect(participant.locator("form[data-identity-panels]")).to_be_visible()
        drafts = database_call(_current_drafts, world["person"])
        assert len(drafts) == 1 and drafts[0] != world["reference"]
        expect(participant.locator("[data-nin-help]")).to_contain_text(drafts[0])
        _shot(participant, "04-register-again-new-draft")
    finally:
        participant_context.close()


# ---------------------------------------------------------------------------
# IDV-Q3
# ---------------------------------------------------------------------------


def _exemption_world():
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.people.tests.conftest import make_event, make_staff
    from apps.registrations.services import get_or_create_active_draft
    from apps.reviews.apps import (
        ACCREDITATION_MANAGERS_GROUP_NAME,
        REGISTRATION_REVIEWERS_GROUP_NAME,
    )

    _versions()
    manager = make_staff("idv-q-b-exempt@example.test", ACCREDITATION_MANAGERS_GROUP_NAME)
    reviewer = make_staff("idv-q-b-exempt-r@example.test", REGISTRATION_REVIEWERS_GROUP_NAME)
    person = resolve_or_create_participant_for_email(f"idv-q-b-{uuid.uuid4().hex[:8]}@example.test")
    draft = get_or_create_active_draft(person=person, event_edition=make_event("IDVQBEXEMPT"))
    return {
        "manager": manager.email_normalized,
        "reviewer": reviewer.email_normalized,
        "draft": str(draft.pk),
        "reference": draft.public_reference,
        "person": str(person.pk),
    }


def _finish_and_submit(draft_id: str) -> str:
    from apps.people.tests.conftest import case_for
    from apps.people.tests.test_idv_q3_nin_exemption import _other_steps
    from apps.registrations.models import Registration
    from apps.registrations.services import submit_full_registration

    draft = Registration.objects.get(pk=draft_id)
    _other_steps(draft)
    privacy, terms = _versions()
    submit_full_registration(
        registration=draft,
        privacy_notice_version=privacy,
        terms_version=terms,
        data_processing_consent_granted=True,
        session_reference="idv-q-b",
        idempotency_key=f"idv-q-b-{draft.pk}",
    )
    return str(case_for(draft).pk)


def _exemption_identifier(draft_id: str) -> tuple[str, str]:
    from apps.people.models import NinExemption

    exemption = NinExemption.objects.get(registration_id=draft_id)
    return exemption.status, exemption.document_identifier.identifier_type


def test_an_algerian_without_a_nin_completes_the_identity_step_after_a_grant(
    live_server, page, browser, tmp_path
) -> None:
    world = database_call(_exemption_world)
    _sign_in(page, live_server, world["manager"])
    page.goto(f"{live_server.url}/ops/registrations/?reference={world['reference']}")
    page.wait_for_load_state("networkidle")
    page.locator("table").get_by_role("link", name="View").first.click()
    page.wait_for_load_state("networkidle")
    panel = page.locator("[data-nin-exemption-panel]")
    expect(panel).to_be_visible()
    panel.locator("select[name=reason_code]").select_option("NIN_NOT_ON_DOCUMENT")
    panel.locator("textarea[name=explanation]").fill(
        "The participant's identity card shows no NIN (synthetic)."
    )
    panel.locator("input[name=confirmed]").check()
    panel.get_by_role("button", name="Grant the exemption").click()
    dialog = page.locator("#asc-confirm-dialog")
    expect(dialog).to_be_visible()
    dialog.locator("[data-confirm-accept]").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-nin-exemption-status='ACTIVE']")).to_be_visible()
    _shot(page, "05-staff-nin-exemption-granted")

    image = tmp_path / "card.png"
    image.write_bytes(_png_bytes())
    participant_context = browser.new_context()
    participant = participant_context.new_page()
    try:
        _as_participant(participant, live_server, world["person"])
        participant.goto(f"{live_server.url}/workspace/")
        participant.wait_for_load_state("networkidle")
        participant.get_by_role("button", name="Continue registration").click()
        participant.wait_for_load_state("networkidle")
        participant.fill("#id_given_names", "Nour")
        participant.fill("#id_family_name", "Exemptest")
        fill_date(participant, "id_date_of_birth", "1991-05-04")
        participant.locator("input[name=identity_path][value=NIN_EXEMPTION]").check()
        exemption_panel = participant.locator("[data-nin-exemption-panel]")
        expect(exemption_panel).to_be_visible()
        expect(participant.locator("[data-identity-panel='NIN']")).to_be_hidden()
        exemption_panel.locator(
            "input[name=exemption_document_kind][value=NATIONAL_ID_CARD]"
        ).check()
        participant.fill("#id_exemption_document_number", "QX0001234")
        participant.set_input_files("#id_exemption_document_image", str(image))
        _shot(participant, "06-participant-documentary-route")
        participant.locator("#main-content button[type=submit]").last.click()
        participant.wait_for_url("**/register/contact/**")
    finally:
        participant_context.close()
    assert database_call(_exemption_identifier, world["draft"]) == ("ACTIVE", "NATIONAL_ID_CARD")

    case_id = database_call(_finish_and_submit, world["draft"])
    reviewer_context = browser.new_context()
    reviewer = reviewer_context.new_page()
    try:
        _sign_in(reviewer, live_server, world["reviewer"])
        reviewer.goto(f"{live_server.url}/ops/identity/?route=NIN_EXEMPTION")
        reviewer.wait_for_load_state("networkidle")
        expect(reviewer.locator("#main-content")).to_contain_text(world["reference"])
        reviewer.goto(f"{live_server.url}/ops/identity/cases/{case_id}/?route=NIN_EXEMPTION")
        reviewer.wait_for_load_state("networkidle")
        expect(reviewer.locator("[data-idv-nin-exemption]")).to_be_visible()
        expect(reviewer.locator("[data-idv-reason='NIN_EXEMPTION_REVIEW']")).to_be_visible()
        expect(reviewer.locator("[data-idv-action='correct']")).to_have_count(0)
        _shot(reviewer, "07-reviewer-documentary-case")
    finally:
        reviewer_context.close()
