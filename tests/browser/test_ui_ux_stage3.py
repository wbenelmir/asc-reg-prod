"""UI/UX Completion Gate, Stage 3: behaviour, geometry and visual evidence.

Covers the UI checkpoint 1 mobile-table correction, the Thmanyah Arabic
font load proof, the F3 access-denied behaviour, the localized error pages,
the shared confirmation dialog, the F8 permission-scoped controls, and
representative screenshots of every page family migrated in Stage 3 in
English, French and Arabic at phone, tablet, desktop and wide widths.

Every capture asserts the shared layout contract of Checkpoint 1
(`_contract`: language and direction, one h1, decorative icons, design
system body class, official logo proportions, no page-level overflow) plus
Stage 3 geometry: every visible status chip and button is contained in its
card, record or cell and in the viewport, and no chip clips its text.
Arabic captures are taken only after `document.fonts.ready`, with the
supplied Thmanyah faces proven loaded.

Screenshots go to `var/test_artifacts/phase3/ui-ux-completion/screenshots/`;
they are review evidence, not a claim of human approval. All data is
synthetic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from django.test import override_settings
from playwright.sync_api import expect

from tests.browser.database import database_call, database_sync
from tests.browser.test_entry_ui_prompt5 import CONTRAST_JS
from tests.browser.test_ui_ux_checkpoint1 import (
    DESKTOP,
    MOBILE,
    OPS_PASSWORD,
    OVERFLOW_JS,
    PHONE,
    TABLET,
    WIDE,
    _contract,
    _login_participant,
)

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = ROOT / "var/test_artifacts/phase3/ui-ux-completion/screenshots"
REPORT_DIR = ROOT / "var/test_artifacts/phase3/ui-ux-completion/reports"

FONT_FILES = {
    "400": "thmanyahsans-Regular.woff2",
    "500": "thmanyahsans-Medium.woff2",
    "700": "thmanyahsans-Bold.woff2",
}
ARABIC_SAMPLE = "المؤتمر الإفريقي للشركات الناشئة ٢٠٢٦"

# Every visible chip and button must sit inside its own container and inside
# the viewport; chips must not clip their own text.
CONTAINMENT_JS = """
() => {
  const vw = document.documentElement.clientWidth;
  const bad = [];
  const containerOf = (el) => el.parentElement.closest(
    '.asc-record, .asc-result-row, td, th, .asc-notice, .asc-command, .asc-card, '
      + '.asc-dialog, .asc-kpi, .asc-page-head-row, .asc-error'
  );
  const inScroller = (el) => el.closest('.asc-table-wrap') !== null;
  const targets = '#main-content .asc-chip, #main-content .btn, dialog[open] .btn';
  for (const el of document.querySelectorAll(targets)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') continue;
    const label = (el.innerText || '').trim().slice(0, 40);
    const c = containerOf(el);
    if (c) {
      const cr = c.getBoundingClientRect();
      if (r.left < cr.left - 1 || r.right > cr.right + 1) {
        bad.push({label, why: 'outside ' + c.className,
                  r: [r.left, r.right], c: [cr.left, cr.right]});
      }
    }
    if (!inScroller(el) && (r.left < -1 || r.right > vw + 1)) {
      bad.push({label, why: 'outside viewport', r: [r.left, r.right], vw});
    }
    if (el.classList.contains('asc-chip') && el.scrollWidth > el.clientWidth + 1) {
      bad.push({label, why: 'chip clips its text', sw: el.scrollWidth, cw: el.clientWidth});
    }
  }
  return bad.slice(0, 8);
}
"""

FACES_JS = """
async () => {
  await document.fonts.ready;
  const out = {};
  for (const weight of ['400', '500', '700']) {
    try {
      const spec = `${weight} 16px "Thmanyah Sans"`;
      const faces = await document.fonts.load(spec, arguments[0] || 'نص');
      out[weight] = faces.map((f) => ({family: f.family, weight: f.weight, status: f.status}));
    } catch (error) {
      out[weight] = [{error: String(error)}];
    }
  }
  out.all = [...document.fonts]
    .filter((f) => f.family.replace(/["']/g, '') === 'Thmanyah Sans')
    .map((f) => ({weight: f.weight, status: f.status}));
  return out;
}
"""

WIDTH_JS = """
(sample) => {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = '32px "Thmanyah Sans", serif';
  const withFont = ctx.measureText(sample).width;
  ctx.font = '32px serif';
  const fallback = ctx.measureText(sample).width;
  return {withFont, fallback};
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_language(page, live_server, language: str) -> None:
    from django.conf import settings

    page.context.add_cookies(
        [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url}]
    )


def _go(page, live_server, language: str, url: str | None = None):
    _set_language(page, live_server, language)
    response = page.goto(url) if url else page.reload()
    page.wait_for_load_state("load")
    return response


def _geometry(page, name: str) -> None:
    bad = page.evaluate(CONTAINMENT_JS)
    assert bad == [], f"{name}: {bad}"


def _faces_loaded(page) -> dict:
    faces = page.evaluate(FACES_JS.replace("arguments[0]", json.dumps(ARABIC_SAMPLE)))
    for weight in FONT_FILES:
        loaded = faces[weight]
        assert loaded, (weight, faces)
        assert all(face.get("status") == "loaded" for face in loaded), (weight, loaded)
        assert any(face.get("weight") == weight for face in loaded), (weight, loaded)
    return faces


def _shot(page, name: str, language: str, viewport) -> None:
    page.set_viewport_size(viewport)
    page.evaluate("async () => { await document.fonts.ready; }")
    if language == "ar":
        _faces_loaded(page)
    page.evaluate("window.scrollTo(0, 0)")
    _contract(page, language, name)
    _geometry(page, name)
    for chip in page.locator("#main-content .asc-chip").all():
        if chip.is_visible():
            ratio = chip.evaluate(CONTRAST_JS)
            assert ratio >= 4.5, (name, chip.inner_text(), ratio)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE_DIR / name), full_page=True)


def _sign_in(live_server, page, user, password: str = OPS_PASSWORD) -> None:
    page.goto(f"{live_server.url}/accounts/ops/sign-in/")
    page.fill("#id_email", user.email_normalized)
    page.fill("#id_password", password)
    with page.expect_navigation():
        page.locator("#main-content button[type=submit]").click()
    page.wait_for_load_state("load")


@database_sync
def _superuser(email: str, name: str = "Synthetic Operator"):
    from apps.accounts.models import OperationalUser, OperationalUserStatus

    user = OperationalUser.objects.create_superuser(email=email, password=OPS_PASSWORD)
    user.status = OperationalUserStatus.ACTIVE
    user.display_name = name
    user.save(update_fields=["status", "display_name"])
    return user


@database_sync
def _scoped_user(email: str, *permissions: str, name: str = "", event=None, organization=None):
    from django.contrib.auth.models import Group, Permission

    from apps.accounts.models import OperationalUser, OperationalUserStatus, ScopedGroupMembership

    user = OperationalUser.objects.create_user(
        email=email, password=OPS_PASSWORD, status=OperationalUserStatus.ACTIVE
    )
    if name:
        user.display_name = name
        user.save(update_fields=["display_name"])
    if permissions:
        group = Group.objects.create(name=f"stage3 {email}")
        for dotted in permissions:
            app_label, codename = dotted.split(".")
            group.permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        ScopedGroupMembership.objects.create(
            user=user, group=group, granted_by=user, event_edition=event, organization=organization
        )
    return user


# ---------------------------------------------------------------------------
# Synthetic world
# ---------------------------------------------------------------------------


@pytest.fixture
@database_sync
def stage3_world(db):
    """One open synthetic event with registrations in long-label states,
    review cases, invitation campaigns, a delegation batch, accreditation
    reference data and assignments, a ready export and delivery records."""
    from django.utils import timezone

    from apps.accreditation.models import AccessProfile, AccessRule, BadgeType, ParticipantRole
    from apps.accreditation.services import assign
    from apps.communications.models import (
        CommunicationChannel,
        CommunicationMessage,
        MessageTemplate,
        MessageTemplateVersion,
    )
    from apps.communications.services import queue_communication
    from apps.core.models import Country, Sector
    from apps.events.models import EventEdition, EventEditionStatus
    from apps.exports.services import request_export
    from apps.invitations.models import InvitationCampaignStatus
    from apps.invitations.services import (
        change_campaign_status,
        create_campaign,
        upload_delegation_batch,
    )
    from apps.organizations.models import Organization, OrganizationType
    from apps.registrations.models import (
        RegistrationInternalStatus,
        RegistrationProfile,
        RegistrationPublicStatus,
    )
    from apps.reviews.models import ReviewCaseType
    from apps.reviews.services import (
        create_checklist_definition,
        create_information_request,
        open_review_case,
    )
    from apps.reviews.tests.conftest import make_registration

    Country.objects.get_or_create(code="DZ", defaults={"name": "Algeria"})
    Sector.objects.get_or_create(code="TECH", defaults={"name": "Technology"})
    EventEdition.objects.filter(status=EventEditionStatus.REGISTRATION_OPEN).update(
        status=EventEditionStatus.REGISTRATION_CLOSED
    )
    event = EventEdition.objects.create(
        code="ASCS3",
        name="African Startup Conference 2026",
        timezone="UTC",
        starts_at=timezone.now(),
        ends_at=timezone.now(),
        status=EventEditionStatus.REGISTRATION_OPEN,
        supported_languages=["en", "fr", "ar"],
    )
    lab = Organization.objects.create(
        official_name="Synthetic Innovation Lab",
        normalized_name="synthetic innovation lab",
        organization_type=OrganizationType.OTHER,
    )
    institute = Organization.objects.create(
        official_name="Institut synthétique de recherche appliquée et d’innovation numérique",
        normalized_name="institut synthetique de recherche appliquee",
        organization_type=OrganizationType.INSTITUTION,
    )
    ministry = Organization.objects.create(
        official_name="الوزارة التجريبية للاقتصاد الرقمي",
        normalized_name="synthetic ministry",
        organization_type=OrganizationType.MINISTRY,
    )
    operator = _superuser("stage3.operator@example.test")

    people = (
        ("Amine", "Benali", "ADDITIONAL_INFORMATION_REQUIRED", "AWAITING_APPLICANT", lab),
        ("سارة", "بن يوسف", "UNDER_REVIEW", "DUPLICATE_REVIEW", ministry),
        ("Lina", "Haddad", "SUBMITTED", "VERIFICATION_PENDING", institute),
        ("Karim", "Mansouri", "APPROVED", "QUALIFICATION_COMPLETE", lab),
        ("Nadia", "Saidi", "NOT_APPROVED", "CLOSED", institute),
    )
    registrations, cases = [], []
    for index, (given, family, public, internal, organization) in enumerate(people, start=1):
        registration = make_registration(
            event=event, organization=organization, public_reference=f"ASC26-S3-{index:04d}"
        )
        registration.public_status = getattr(RegistrationPublicStatus, public)
        registration.internal_status = getattr(RegistrationInternalStatus, internal)
        registration.save(update_fields=["public_status", "internal_status"])
        RegistrationProfile.objects.create(
            registration=registration,
            submitted_given_names=given,
            submitted_family_name=family,
            submitted_full_name=f"{given} {family}",
            nationality_code_id="DZ",
        )
        registrations.append(registration)
        cases.append(
            open_review_case(
                registration=registration,
                case_type=ReviewCaseType.STANDARD,
                queue_code="GENERAL",
            )
        )
    create_checklist_definition(
        event_edition=event, case_type=ReviewCaseType.STANDARD, version_label="stage3"
    )
    # Enough eligible reviewers for the assignment filter to appear.
    reviewers = [
        _scoped_user(
            f"stage3.reviewer{n}@example.test",
            "reviews.view_reviewcase",
            name=name,
            event=event,
        )
        for n, name in enumerate(
            (
                "Yasmine Belkacem",
                "Omar Cherif",
                "Leila Mansour",
                "Farid Ait Ali",
                "Salma Hamdi",
                "Rachid Benamar",
                "ريم بوزيد",
                "Hugo Lefèvre",
                "Inès Morel",
                "Samir Kaci",
            ),
            start=1,
        )
    ]

    campaigns = []
    for reference, name, status in (
        ("CAMP-S3-0001", "Delegation invitations", InvitationCampaignStatus.ACTIVE),
        (
            "CAMP-S3-0002",
            "Invitations des partenaires institutionnels",
            InvitationCampaignStatus.SUSPENDED,
        ),
        ("CAMP-S3-0003", "دعوات الشركاء", InvitationCampaignStatus.DRAFT),
    ):
        campaign = create_campaign(
            event_edition=event,
            organization=lab if reference.endswith("1") else institute,
            name=name,
            public_reference=reference,
            capacity=50,
        )
        if status != InvitationCampaignStatus.DRAFT:
            campaign = change_campaign_status(campaign, InvitationCampaignStatus.ACTIVE)
        if status == InvitationCampaignStatus.SUSPENDED:
            campaign = change_campaign_status(campaign, InvitationCampaignStatus.SUSPENDED)
        campaigns.append(campaign)
    csv_bytes = (
        "email,given_names,family_name,notes\n"
        "delegate.one@example.test,Amina,Synthétique,\n"
        ",Missing,Email,\n"
        "not-an-email,Yacine,Test,\n"
        "delegate.two@example.test,=cmd(),Formula,\n"
        "delegate.three@example.test,,NoGiven,\n"
    ).encode()
    batch = upload_delegation_batch(
        organization=lab,
        event_edition=event,
        csv_bytes=csv_bytes,
        uploaded_filename="synthetic-delegation.csv",
        created_by=operator,
        idempotency_key="stage3-delegation",
    )

    role = ParticipantRole.objects.create(
        event_edition=event, code="S3-DELEGATE", name="Delegate", name_fr="Délégué", name_ar="مندوب"
    )
    ParticipantRole.objects.create(
        event_edition=event,
        code="S3-SPEAKER",
        name="Speaker",
        name_fr="Intervenant",
        name_ar="متحدث",
    )
    badge = BadgeType.objects.create(
        event_edition=event, code="S3-STANDARD", name="Standard", name_fr="Standard", name_ar="عادي"
    )
    BadgeType.objects.create(
        event_edition=event, code="S3-VIP", name="VIP", name_fr="VIP", name_ar="كبار الشخصيات"
    )
    profile = AccessProfile.objects.create(
        event_edition=event,
        code="S3-GENERAL",
        name="General access",
        name_fr="Accès général",
        name_ar="وصول عام",
    )
    AccessRule.objects.create(
        event_edition=event,
        code="S3-HALL-A",
        name="Main hall",
        name_fr="Salle plénière",
        name_ar="القاعة الرئيسية",
    )
    approved = registrations[3]
    assign(kind="PARTICIPANT_ROLE", registration=approved, reference_obj=role, actor=operator)
    assign(kind="BADGE_TYPE", registration=approved, reference_obj=badge, actor=operator)
    assign(kind="ACCESS_PROFILE", registration=approved, reference_obj=profile, actor=operator)

    information_request = create_information_request(
        registration=registrations[0],
        review_case=cases[0],
        purpose="CLARIFICATION",
        message_en="Please confirm your job title and upload a clearer identity page.",
        message_fr="Merci de confirmer votre fonction et de joindre une page d’identité lisible.",
        message_ar="يرجى تأكيد مسماك الوظيفي وتحميل صفحة هوية أوضح.",
        items=[
            {
                "kind": "FIELD_CORRECTION",
                "field_code": "job_title",
                "instructions_en": "Current title",
            },
            {"kind": "DOCUMENT_UPLOAD", "document_type": "REQUESTED_EVIDENCE"},
        ],
        created_by=operator,
    )

    request_export(
        event_edition=event,
        organization=None,
        purpose_code="OPERATIONAL_REPORTING",
        reason="Synthetic weekly report",
        requested_by=operator,
        registration_ids=[r.pk for r in registrations],
    )

    template = MessageTemplate.objects.create(
        code="S3_DECISION", channel=CommunicationChannel.EMAIL, purpose_code="DECISION_STATUS"
    )
    for language in ("en", "fr", "ar"):
        MessageTemplateVersion.objects.create(
            template=template,
            language=language,
            version_label="v1",
            subject="Status {{public_reference}}",
            body="Status {{public_reference}}",
            allowed_variables=["public_reference"],
            status="PUBLISHED",
            effective_from=timezone.now(),
            content_hash="b" * 64,
        )
    for index, (language, status) in enumerate(
        (("en", "DELIVERED"), ("fr", "DEFERRED"), ("ar", "FAILED"), ("en", "UNDELIVERABLE")),
        start=1,
    ):
        message = queue_communication(
            purpose_code="DECISION_STATUS",
            event_edition=event,
            registration=registrations[index - 1],
            person=None,
            language=language,
            destination=f"synthetic{index}@example.test",
            context={"public_reference": registrations[index - 1].public_reference},
            idempotency_key=f"stage3-message-{index}",
        )
        if message is not None:
            CommunicationMessage.objects.filter(pk=message.pk).update(status=status)

    return {
        "event": event,
        "lab": lab,
        "institute": institute,
        "operator": operator,
        "registrations": registrations,
        "cases": cases,
        "campaigns": campaigns,
        "batch": batch,
        "approved": approved,
        "information_request": information_request,
        "reviewers": reviewers,
    }


# ---------------------------------------------------------------------------
# 1. Mobile records correction (UI checkpoint 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "fr", "ar"])
def test_phone_records_contain_every_reference_status_and_action(
    live_server, page, stage3_world, language
) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    for path in ("/ops/registrations/", "/ops/reviews/queue/"):
        _go(page, live_server, language, f"{live_server.url}{path}")
        for viewport in (PHONE, MOBILE):
            page.set_viewport_size(viewport)
            page.evaluate("async () => { await document.fonts.ready; }")
            expect(page.locator(".asc-table-wrap.asc-wide-only").first).to_be_hidden()
            records = page.locator(".asc-records.asc-narrow-only .asc-record[data-record]")
            count = records.count()
            assert count == 5, (path, count)
            for index in range(count):
                record = records.nth(index)
                expect(record).to_be_visible()
                box = record.bounding_box()
                assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 0.5
                title = record.locator(".asc-record-title")
                expect(title).to_contain_text(re.compile(r"ASC26-S3-\d{4}"))
                for element in record.locator(".asc-chip, [data-record-action]").all():
                    inner = element.bounding_box()
                    assert inner["x"] >= box["x"] - 0.5, (path, language, element.inner_text())
                    assert inner["x"] + inner["width"] <= box["x"] + box["width"] + 0.5, (
                        path,
                        language,
                        viewport,
                        element.inner_text(),
                    )
                    assert element.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), (
                        path,
                        element.inner_text(),
                    )
                action = record.locator("[data-record-action]")
                expect(action).to_have_count(1)
                assert action.bounding_box()["height"] >= 44 - 0.5
            _geometry(page, f"{path} {language} {viewport}")
            assert page.evaluate(OVERFLOW_JS)["overflow"] <= 0
        # From 768 px the same rows are a real table; the records are gone.
        page.set_viewport_size(TABLET)
        expect(page.locator(".asc-records.asc-narrow-only").first).to_be_hidden()
        table = page.get_by_role("table").first
        expect(table).to_be_visible()
        expect(table.locator("tbody tr")).to_have_count(5)
        expect(table.locator("tbody th[scope=row]")).to_have_count(5)


def test_phone_records_keep_keyboard_order_and_screen_reader_meaning(
    live_server, page, stage3_world
) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    _go(page, live_server, "ar", f"{live_server.url}/ops/registrations/")
    page.set_viewport_size(PHONE)
    # The records list has a name, and each action names its record.
    records = page.get_by_role("list", name=re.compile("."))
    assert records.count() >= 1
    first = stage3_world["registrations"][0].public_reference
    expect(page.get_by_role("link", name=re.compile(re.escape(first)))).to_have_count(1)
    # Hidden table links are never reached by Tab; record actions are, in order.
    page.locator("#main-content").focus()
    reached = []
    for _ in range(60):
        page.keyboard.press("Tab")
        info = page.evaluate(
            """() => { const a = document.activeElement;
                 return {hidden: a.closest('.asc-wide-only') !== null,
                         action: a.hasAttribute('data-record-action'),
                         text: (a.innerText || '').trim()}; }"""
        )
        assert info["hidden"] is False, info
        if info["action"]:
            reached.append(info["text"])
    assert len(reached) >= 5, reached


def test_long_translated_statuses_wrap_inside_records(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    for language in ("fr", "ar"):
        _go(page, live_server, language, f"{live_server.url}/ops/registrations/")
        page.set_viewport_size(PHONE)
        chip = page.locator(".asc-narrow-only [data-status=ADDITIONAL_INFORMATION_REQUIRED]").first
        expect(chip).to_be_visible()
        assert len(chip.inner_text()) >= 20, chip.inner_text()
        _geometry(page, f"long status {language}")


def test_mobile_records_evidence(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    for path, stem in (
        ("/ops/registrations/", "ops-01-intake-list"),
        ("/ops/reviews/queue/", "ops-03-review-queue"),
    ):
        for language in ("ar", "en", "fr"):
            _go(page, live_server, language, f"{live_server.url}{path}")
            _shot(page, f"{stem}-{language}-phone.png", language, PHONE)
            _shot(page, f"{stem}-{language}-mobile.png", language, MOBILE)
        _go(page, live_server, "ar", f"{live_server.url}{path}")
        _shot(page, f"{stem}-ar-desktop.png", "ar", DESKTOP)
        _go(page, live_server, "fr", f"{live_server.url}{path}")
        _shot(page, f"{stem}-fr-tablet.png", "fr", TABLET)


# ---------------------------------------------------------------------------
# 2. Thmanyah Arabic font: served, loaded and actually used
# ---------------------------------------------------------------------------


def _platform_fonts(page, selector: str) -> list[dict]:
    """The fonts Chromium actually used to render a node's text (CDP)."""
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
    node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
    assert node, selector
    fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
    cdp.detach()
    return fonts


ARABIC_ROLES = {
    "body text": ".asc-auth-head > p:not(.asc-kicker)",
    "heading": "h1",
    "label": "label[for=id_email]",
    "button": "#main-content button[type=submit] span",
    "validation message": "#error-summary strong",
    "field error": ".asc-field-error bdi",
}


def test_thmanyah_webfont_is_served_loaded_and_used_for_arabic(
    live_server, page, stage3_world
) -> None:
    responses: list[dict] = []
    problems: list[str] = []
    page.on(
        "response",
        lambda r: (
            responses.append(
                {"url": r.url, "status": r.status, "type": r.headers.get("content-type", "")}
            )
            if "/static/vendor/thmanyah/" in r.url
            else None
        ),
    )
    page.on(
        "console",
        lambda m: (
            problems.append(f"console {m.type}: {m.text}")
            if m.type in ("error", "warning")
            else None
        ),
    )
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on(
        "requestfailed",
        lambda r: problems.append(f"requestfailed: {r.url} {r.failure}"),
    )

    _go(page, live_server, "ar", f"{live_server.url}/accounts/start/")
    page.set_viewport_size(DESKTOP)
    # Validation state: error summary and field error, both Arabic.
    page.locator("#main-content button[type=submit]").click()
    expect(page.locator("#error-summary")).to_be_visible()
    faces = _faces_loaded(page)
    assert {face["weight"] for face in faces["all"]} >= set(FONT_FILES)
    assert all(face["status"] == "loaded" for face in faces["all"]), faces["all"]

    served = {}
    for weight, filename in FONT_FILES.items():
        hits = [r for r in responses if r["url"].endswith(filename)]
        assert hits, (filename, responses)
        assert all(hit["status"] == 200 for hit in hits), hits
        assert all("font/woff2" in hit["type"] for hit in hits), hits
        served[weight] = hits[0]
    assert not any("ZIP" in r["url"].upper() or r["url"].endswith(".zip") for r in responses)

    platform: dict[str, list[dict]] = {}
    for role, selector in ARABIC_ROLES.items():
        fonts = _platform_fonts(page, selector)
        platform[role] = fonts
        custom = [f for f in fonts if f.get("isCustomFont")]
        assert custom, (role, fonts)
        assert all("thmanyah" in f["familyName"].lower() for f in custom), (role, fonts)
    # Supplementary evidence only (recorded, not asserted): canvas metrics of
    # the sample with the Thmanyah stack and with a generic serif.
    widths = page.evaluate(WIDTH_JS, ARABIC_SAMPLE)

    # Status chips, alerts and operational tables use the same face.
    _sign_in(live_server, page, stage3_world["operator"])
    _go(page, live_server, "ar", f"{live_server.url}/ops/registrations/")
    page.set_viewport_size(DESKTOP)
    _faces_loaded(page)
    for role, selector in (
        ("status chip", ".asc-wide-only .asc-chip span"),
        ("table header", ".asc-wide-only thead th"),
        ("navigation", ".asc-sectionnav a"),
    ):
        fonts = _platform_fonts(page, selector)
        platform[role] = fonts
        assert any(
            f.get("isCustomFont") and "thmanyah" in f["familyName"].lower() for f in fonts
        ), (role, fonts)
    # Technical Latin values stay isolated left-to-right inside Arabic pages.
    unisolated = page.evaluate(
        "() => [...document.querySelectorAll('.asc-code')]"
        ".filter(e => !e.closest('[dir=ltr]')).length"
    )
    assert unisolated == 0
    # No synthetic bold: every used weight has its own supplied face.
    assert page.evaluate("getComputedStyle(document.body).fontSynthesis") in ("none", "")

    font_problems = [p for p in problems if re.search(r"font|woff|csp|decode|OTS", p, re.I)]
    assert font_problems == [], font_problems
    assert not [p for p in problems if p.startswith(("requestfailed", "pageerror"))], problems

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "thmanyah-font-verification.json").write_text(
        json.dumps(
            {
                "served": served,
                "fontfaces": faces,
                "platform_fonts_used": platform,
                "canvas_width_thmanyah_vs_serif": widths,
                "console_and_network_problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_blocked_font_request_is_detected_so_the_success_case_is_real(live_server, page) -> None:
    """With every Thmanyah request aborted, the same checks fail: the faces
    report errors and Chromium renders the Arabic text with a system font.
    So the passing test above proves the supplied font was genuinely
    downloaded and used, not merely named in the CSS."""
    page.route("**/static/vendor/thmanyah/*.woff2", lambda route: route.abort())
    _go(page, live_server, "ar", f"{live_server.url}/accounts/start/")
    page.set_viewport_size(DESKTOP)
    faces = page.evaluate(FACES_JS.replace("arguments[0]", json.dumps(ARABIC_SAMPLE)))
    statuses = {face.get("status") for face in faces["all"]}
    assert "loaded" not in statuses, faces
    fonts = _platform_fonts(page, "h1")
    assert not any(f.get("isCustomFont") for f in fonts), fonts
    # The CSS still names the family: a family-name-only check would pass.
    family = page.evaluate("getComputedStyle(document.querySelector('h1')).fontFamily")
    assert family.startswith('"Thmanyah Sans"'), family


@pytest.mark.parametrize("language", ["en", "fr"])
def test_latin_interfaces_never_load_or_use_thmanyah(live_server, page, language) -> None:
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url) if "thmanyah" in r.url else None)
    _go(page, live_server, language, f"{live_server.url}/accounts/start/")
    page.evaluate("async () => { await document.fonts.ready; }")
    assert requests == []
    fonts = _platform_fonts(page, "h1")
    assert not any(f.get("isCustomFont") for f in fonts), fonts
    family = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert "Thmanyah" not in family.split(",")[0], family


def test_arabic_font_evidence_after_fonts_ready(live_server, page, stage3_world) -> None:
    _go(page, live_server, "ar", f"{live_server.url}/accounts/start/")
    _shot(page, "font-01-arabic-auth-ar-desktop.png", "ar", DESKTOP)
    _shot(page, "font-01-arabic-auth-ar-mobile.png", "ar", MOBILE)
    _sign_in(live_server, page, stage3_world["operator"])
    _go(
        page,
        live_server,
        "ar",
        f"{live_server.url}/ops/reviews/cases/{stage3_world['cases'][0].pk}/",
    )
    _shot(page, "font-02-arabic-case-ar-desktop.png", "ar", DESKTOP)
    _shot(page, "font-02-arabic-case-ar-mobile.png", "ar", MOBILE)


# ---------------------------------------------------------------------------
# 3. F3 access denied, and 4. error pages
# ---------------------------------------------------------------------------


def test_signed_in_user_without_permission_sees_403_without_a_loop(
    live_server, page, stage3_world
) -> None:
    user = _scoped_user("stage3.badges@example.test", "badges.view_digitalentrypass")
    _sign_in(live_server, page, user)
    for language, viewport, size in (
        ("en", DESKTOP, "desktop"),
        ("fr", DESKTOP, "desktop"),
        ("ar", MOBILE, "mobile"),
    ):
        response = _go(page, live_server, language, f"{live_server.url}/ops/registrations/")
        assert response.status == 403
        assert page.url.endswith("/ops/registrations/")
        expect(page.locator("h1")).to_have_count(1)
        _shot(page, f"err-03-access-denied-{language}-{size}.png", language, viewport)
    # The operations home link and sign-out work from the 403 page.
    expect(page.locator(".asc-error-actions a.btn-primary")).to_have_attribute(
        "href", "/ops/badges/fallback-reference/"
    )


def test_anonymous_user_is_sent_to_sign_in_and_back_to_the_page(
    live_server, page, stage3_world
) -> None:
    reviewer = stage3_world["reviewers"][0]
    page.goto(f"{live_server.url}/ops/reviews/queue/?status=QUEUED")
    page.wait_for_load_state("load")
    assert "/accounts/ops/sign-in/" in page.url
    assert "next=" in page.url
    page.fill("#id_email", reviewer.email_normalized)
    page.fill("#id_password", OPS_PASSWORD)
    with page.expect_navigation():
        page.locator("#main-content button[type=submit]").click()
    assert page.url.endswith("/ops/reviews/queue/?status=QUEUED"), page.url


def test_error_pages_evidence(live_server, page, stage3_world) -> None:
    for language, viewport, size in (
        ("en", DESKTOP, "desktop"),
        ("fr", MOBILE, "mobile"),
        ("ar", DESKTOP, "desktop"),
        ("ar", PHONE, "phone"),
    ):
        response = _go(page, live_server, language, f"{live_server.url}/no-such-page/")
        assert response.status == 404
        _shot(page, f"err-04-not-found-{language}-{size}.png", language, viewport)
    # CSRF failure: the form's token is removed before submission.
    for language, viewport, size in (("en", DESKTOP, "desktop"), ("ar", MOBILE, "mobile")):
        _go(page, live_server, language, f"{live_server.url}/accounts/ops/sign-in/")
        page.evaluate(
            "document.querySelectorAll('input[name=csrfmiddlewaretoken]').forEach(e => e.remove())"
        )
        page.fill("#id_email", "csrf.probe@example.test")
        page.fill("#id_password", "__not-used__")
        with page.expect_navigation():
            page.locator("#main-content button[type=submit]").click()
        expect(page.locator("h1")).to_contain_text(
            {"en": "Your form could not be sent", "ar": "تعذّر إرسال النموذج"}[language]
        )
        assert "CSRF" not in page.inner_text("main")
        _shot(page, f"err-05-csrf-{language}-{size}.png", language, viewport)


@override_settings(ROOT_URLCONF="tests.browser.stage3_urls")
def test_static_safe_error_pages_evidence(live_server, page) -> None:
    for language, viewport, size in (
        ("en", DESKTOP, "desktop"),
        ("ar", MOBILE, "mobile"),
        ("fr", DESKTOP, "desktop"),
    ):
        response = _go(page, live_server, language, f"{live_server.url}/__stage3__/boom/")
        assert response.status == 500
        expect(page.locator("#language-switcher-select")).to_have_count(0)
        _shot(page, f"err-06-server-error-{language}-{size}.png", language, viewport)
    for language, viewport, size in (("en", DESKTOP, "desktop"), ("ar", MOBILE, "mobile")):
        response = _go(page, live_server, language, f"{live_server.url}/__stage3__/bad/")
        assert response.status == 400
        _shot(page, f"err-02-bad-request-{language}-{size}.png", language, viewport)
    for language, viewport, size in (("en", DESKTOP, "desktop"), ("ar", MOBILE, "mobile")):
        _go(page, live_server, language, f"{live_server.url}/__stage3__/claim-conflict/")
        _shot(page, f"inv-11-claim-conflict-{language}-{size}.png", language, viewport)


# ---------------------------------------------------------------------------
# 5. Shared confirmation dialog
# ---------------------------------------------------------------------------


@pytest.fixture
def participant_with_submission(live_server, page, stage3_world):
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import RegistrationPublicStatus
    from apps.reviews.tests.conftest import make_registration

    email = "stage3.participant@example.test"
    person = database_call(lambda: resolve_or_create_participant_for_email(email))
    registration = database_call(
        lambda: make_registration(
            event=stage3_world["event"], person=person, public_reference="ASC26-S3-P001"
        )
    )
    registration.public_status = RegistrationPublicStatus.SUBMITTED
    database_call(lambda: registration.save(update_fields=["public_status"]))
    _login_participant(live_server, page, email)
    return registration


def test_withdraw_dialog_focus_escape_and_confirmation(
    live_server, page, participant_with_submission
) -> None:
    registration = participant_with_submission
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    _go(page, live_server, "en", f"{live_server.url}/workspace/")
    page.set_viewport_size(DESKTOP)
    withdraw = page.get_by_role("button", name="Withdraw", exact=True)
    withdraw.click()
    dialog = page.locator("#asc-confirm-dialog")
    expect(dialog).to_have_attribute("open", "")
    expect(dialog.locator("#asc-confirm-title")).to_have_text("Withdraw this registration?")
    expect(page.get_by_role("dialog", name="Withdraw this registration?")).to_be_visible()
    # The least destructive choice has focus; Tab stays inside the modal.
    expect(dialog.get_by_role("button", name="Go back")).to_be_focused()
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Withdraw registration")).to_be_focused()
    _shot(page, "dlg-01-withdraw-en-desktop.png", "en", DESKTOP)
    # Opening the dialog executed nothing; Escape closes it and restores focus.
    assert posts == []
    page.keyboard.press("Escape")
    expect(dialog).not_to_have_attribute("open", "")
    expect(withdraw).to_be_focused()
    database_call(lambda: registration.refresh_from_db())
    assert registration.public_status == "SUBMITTED"
    # "Go back" also cancels.
    withdraw.click()
    dialog.get_by_role("button", name="Go back").click()
    expect(withdraw).to_be_focused()
    assert posts == []
    # Confirming submits the real POST form once.
    withdraw.click()
    dialog.get_by_role("button", name="Withdraw registration").click()
    expect(page.locator(".asc-flash")).to_contain_text("withdrawn", timeout=10_000)
    database_call(lambda: registration.refresh_from_db())
    assert registration.public_status == "WITHDRAWN"
    assert len([u for u in posts if "/withdraw/" in u]) == 1


def test_withdraw_dialog_is_translated_and_rtl(
    live_server, page, participant_with_submission
) -> None:
    for language, viewport, size in (("ar", MOBILE, "mobile"), ("fr", DESKTOP, "desktop")):
        _go(page, live_server, language, f"{live_server.url}/workspace/")
        page.set_viewport_size(viewport)
        page.locator(".asc-reg-card-danger button[type=submit]").click()
        dialog = page.locator("#asc-confirm-dialog")
        expect(dialog).to_have_attribute("open", "")
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if language == "ar" else "ltr")
        box = dialog.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"]
        _shot(page, f"dlg-01-withdraw-{language}-{size}.png", language, viewport)
        page.keyboard.press("Escape")


def test_withdraw_without_javascript_requires_the_consent_box(
    live_server, browser, stage3_world
) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import RegistrationPublicStatus
    from apps.reviews.tests.conftest import make_registration

    email = "stage3.nojs@example.test"
    person = database_call(lambda: resolve_or_create_participant_for_email(email))
    registration = database_call(
        lambda: make_registration(
            event=stage3_world["event"], person=person, public_reference="ASC26-S3-NOJS"
        )
    )
    registration.public_status = RegistrationPublicStatus.SUBMITTED
    database_call(lambda: registration.save(update_fields=["public_status"]))
    from apps.accounts.otp import DeterministicTestOtpGenerator
    from tests.browser.helpers import no_js_page_at_otp_verify

    with override_settings(OTP_GENERATOR_BACKEND="apps.accounts.otp.DeterministicTestOtpGenerator"):
        # The code request needs JavaScript (UX-4 human check); the rest of
        # the journey runs with JavaScript off.
        context, page = no_js_page_at_otp_verify(browser, live_server.url, email)
        page.fill("#id_code", DeterministicTestOtpGenerator.FIXED_VALUE)
        page.locator("#main-content button[type=submit]").click()
        page.wait_for_url("**/workspace/**")
    try:
        page.goto(f"{live_server.url}/workspace/")
        consent = page.locator(".asc-confirm-fallback input[type=checkbox]")
        expect(consent).to_be_visible()
        page.locator(".asc-reg-card-danger button[type=submit]").click()
        page.wait_for_timeout(500)
        database_call(lambda: registration.refresh_from_db())
        assert registration.public_status == "SUBMITTED"
        consent.check()
        with page.expect_navigation():
            page.locator(".asc-reg-card-danger button[type=submit]").click()
        database_call(lambda: registration.refresh_from_db())
        assert registration.public_status == "WITHDRAWN"
    finally:
        context.close()


def test_accreditation_revoke_uses_the_dialog(live_server, page, stage3_world) -> None:
    from apps.accreditation.models import AssignmentStatus, ParticipantRoleAssignment

    approved = stage3_world["approved"]
    _sign_in(live_server, page, stage3_world["operator"])
    _go(
        page, live_server, "fr", f"{live_server.url}/ops/accreditation/registrations/{approved.pk}/"
    )
    page.set_viewport_size(DESKTOP)
    form = page.locator("form[action*='/PARTICIPANT_ROLE/'][action$='/revoke/']")
    form.locator("input[name=reason]").fill("Rôle attribué par erreur (synthétique)")
    form.locator("button[type=submit]").click()
    dialog = page.locator("#asc-confirm-dialog")
    expect(dialog).to_have_attribute("open", "")
    _shot(page, "dlg-02-revoke-role-fr-desktop.png", "fr", DESKTOP)
    dialog.locator("[data-confirm-accept]").click()
    page.wait_for_load_state("load")
    expect(page.locator("#asc-confirm-dialog")).not_to_have_attribute("open", "")
    assert not database_call(
        lambda: ParticipantRoleAssignment.objects.filter(
            registration=approved, status=AssignmentStatus.CURRENT
        ).exists()
    )


def test_incomplete_destructive_form_goes_to_server_validation_without_dialog(
    live_server, page, stage3_world
) -> None:
    registration = stage3_world["registrations"][2]
    _sign_in(live_server, page, stage3_world["operator"])
    _go(
        page,
        live_server,
        "en",
        f"{live_server.url}/ops/reviews/registrations/{registration.pk}/cancel/",
    )
    page.locator("#main-content button.btn-danger").click()
    page.wait_for_load_state("load")
    expect(page.locator("#error-summary")).to_be_visible()
    expect(page.locator("#asc-confirm-dialog")).not_to_have_attribute("open", "")
    _shot(page, "rev-06-cancel-validation-en-desktop.png", "en", DESKTOP)
    page.fill("#id_reason", "Synthetic duplicate context")
    page.locator("#main-content button.btn-danger").click()
    expect(page.locator("#asc-confirm-dialog")).to_have_attribute("open", "")
    _shot(page, "dlg-03-cancel-registration-en-desktop.png", "en", DESKTOP)
    page.keyboard.press("Escape")
    database_call(lambda: registration.refresh_from_db())
    assert registration.internal_status != "CLOSED"


# ---------------------------------------------------------------------------
# 6. F8 scoped controls
# ---------------------------------------------------------------------------


def test_assignment_picker_filters_scoped_people(live_server, page, stage3_world) -> None:
    case = stage3_world["cases"][2]
    _sign_in(live_server, page, stage3_world["operator"])
    _go(page, live_server, "en", f"{live_server.url}/ops/reviews/cases/{case.pk}/")
    page.set_viewport_size(DESKTOP)
    select = page.locator("select[name=assigned_user_id]")
    expect(select).to_be_visible()
    filter_box = page.locator("[data-picker-filter]").first
    expect(filter_box).to_be_visible()
    filter_box.fill("yas")
    expect(page.locator("[data-picker-status]").first).to_contain_text("Choices shown: 1")
    select.select_option(label="Yasmine Belkacem")
    _shot(page, "f8-01-assign-picker-en-desktop.png", "en", DESKTOP)
    page.get_by_role("button", name="Assign", exact=True).click()
    page.wait_for_load_state("load")
    database_call(lambda: case.refresh_from_db())
    assert database_call(
        lambda: case.assignments.filter(
            is_current=True, assigned_user__display_name="Yasmine Belkacem"
        ).exists()
    )


def test_bulk_preview_execute_and_result(live_server, page, stage3_world) -> None:
    from apps.accreditation.models import AccessRule

    _sign_in(live_server, page, stage3_world["operator"])
    url = f"{live_server.url}/ops/accreditation/bulk/preview/"
    hall = str(database_call(lambda: AccessRule.objects.get(code="S3-HALL-A")).pk)

    def preview(language: str) -> None:
        _go(page, live_server, language, url)
        page.select_option("#id_kind", "ACCESS_RULE")
        # The item list narrows to the chosen type.
        assert page.locator("#id_reference_object_id optgroup:not([disabled])").count() == 1
        page.select_option("#id_reference_object_id", hall)
        page.fill("#id_reason", "Synthetic plenary access")
        with page.expect_navigation():
            page.locator("#main-content form[data-bulk-form] button[type=submit]").click()

    _go(page, live_server, "en", url)
    _shot(page, "acc-02-bulk-form-en-desktop.png", "en", DESKTOP)
    preview("ar")
    _shot(page, "acc-03-bulk-preview-ar-mobile.png", "ar", MOBILE)
    preview("en")
    expect(page.get_by_role("heading", name="Preview results")).to_be_visible()
    _shot(page, "acc-03-bulk-preview-en-desktop.png", "en", DESKTOP)
    page.set_viewport_size(DESKTOP)
    page.get_by_role("button", name="Execute this exact previewed scope").click()
    expect(page.locator("#asc-confirm-dialog")).to_have_attribute("open", "")
    _shot(page, "dlg-04-bulk-execute-en-desktop.png", "en", DESKTOP)
    with page.expect_navigation():
        page.locator("#asc-confirm-dialog [data-confirm-accept]").click()
    expect(page.locator("h1")).to_contain_text("Bulk accreditation execution result")
    _shot(page, "acc-04-bulk-result-en-desktop.png", "en", DESKTOP)
    _shot(page, "acc-04-bulk-result-en-mobile.png", "en", MOBILE)


def test_export_form_uses_scoped_choices(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    _go(page, live_server, "en", f"{live_server.url}/ops/exports/")
    assert page.locator("input#id_organization_id").count() == 0
    page.select_option("#id_event_edition_id", str(stage3_world["event"].pk))
    page.select_option("#id_organization_id", str(stage3_world["lab"].pk))
    page.select_option("#id_purpose_code", "EVENT_LOGISTICS_PLANNING")
    page.fill("#id_reason", "Synthetic catering estimate")
    page.get_by_role("button", name="Generate export").click()
    expect(page.locator(".asc-flash")).to_contain_text("Export generated", timeout=10_000)
    _shot(page, "exp-01-exports-success-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "exp-01-exports-ar-mobile.png", "ar", MOBILE)
    _shot(page, "exp-01-exports-ar-desktop.png", "ar", DESKTOP)
    _go(page, live_server, "fr")
    page.get_by_role("button", name="Générer l’exportation").or_(
        page.locator("#main-content form button[type=submit]")
    ).first.click()
    page.wait_for_load_state("load")
    expect(page.locator("#error-summary")).to_be_visible()
    _shot(page, "exp-02-exports-validation-fr-desktop.png", "fr", DESKTOP)


# ---------------------------------------------------------------------------
# 7. Page-family evidence (every migrated Stage 3 page)
# ---------------------------------------------------------------------------


def test_organization_workspace_evidence(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    base = live_server.url
    _go(page, live_server, "en", f"{base}/organizations/workspace/")
    _shot(page, "inv-01-workspace-en-desktop.png", "en", DESKTOP)
    _shot(page, "inv-01-workspace-en-wide.png", "en", WIDE)
    _go(page, live_server, "fr")
    _shot(page, "inv-01-workspace-fr-tablet.png", "fr", TABLET)
    _go(page, live_server, "ar")
    _shot(page, "inv-01-workspace-ar-mobile.png", "ar", MOBILE)
    _shot(page, "inv-01-workspace-ar-desktop.png", "ar", DESKTOP)

    _go(page, live_server, "en", f"{base}/organizations/workspace/registrations/")
    _shot(page, "inv-02-registrations-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "inv-02-registrations-ar-mobile.png", "ar", MOBILE)

    _go(page, live_server, "fr", f"{base}/organizations/search/?q=synth")
    _shot(page, "inv-03-search-results-fr-desktop.png", "fr", DESKTOP)
    _shot(page, "inv-03-search-results-fr-mobile.png", "fr", MOBILE)
    _go(page, live_server, "ar", f"{base}/organizations/search/?q=zz-none")
    _shot(page, "inv-03-search-empty-ar-mobile.png", "ar", MOBILE)
    _go(page, live_server, "en", f"{base}/organizations/search/")
    _shot(page, "inv-03-search-en-desktop.png", "en", DESKTOP)

    lab = stage3_world["lab"]
    _go(page, live_server, "en", f"{base}/organizations/workspace/campaigns/new/{lab.pk}/")
    _shot(page, "inv-04-campaign-create-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    expect(page.locator("#error-summary")).to_be_visible()
    _shot(page, "inv-04-campaign-create-errors-ar-mobile.png", "ar", MOBILE)

    draft = stage3_world["campaigns"][2]
    _go(page, live_server, "en", f"{base}/organizations/workspace/campaigns/{draft.pk}/")
    page.get_by_role("button", name="Issue link").click()
    page.wait_for_load_state("load")
    expect(page.locator(".asc-secret-value")).to_be_visible()
    _shot(page, "inv-05-campaign-link-issued-en-desktop.png", "en", DESKTOP)
    active = stage3_world["campaigns"][1]
    _go(page, live_server, "ar", f"{base}/organizations/workspace/campaigns/{active.pk}/")
    _shot(page, "inv-05-campaign-detail-ar-mobile.png", "ar", MOBILE)
    _go(page, live_server, "fr")
    _shot(page, "inv-05-campaign-detail-fr-desktop.png", "fr", DESKTOP)

    _go(page, live_server, "fr", f"{base}/organizations/workspace/delegations/upload/{lab.pk}/")
    _shot(page, "inv-06-delegation-upload-fr-desktop.png", "fr", DESKTOP)
    _go(page, live_server, "ar")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    _shot(page, "inv-06-delegation-upload-errors-ar-mobile.png", "ar", MOBILE)

    batch = stage3_world["batch"]
    _go(page, live_server, "en", f"{base}/organizations/workspace/delegations/{batch.pk}/")
    _shot(page, "inv-07-delegation-detail-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "inv-07-delegation-detail-ar-mobile.png", "ar", MOBILE)
    _go(page, live_server, "fr")
    _shot(page, "inv-07-delegation-detail-fr-phone.png", "fr", PHONE)

    _go(page, live_server, "en", f"{base}/organizations/workspace/on-behalf/new/{lab.pk}/")
    _shot(page, "inv-08-on-behalf-en-desktop.png", "en", DESKTOP)
    page.fill("#id_intended_email", "future.participant@example.test")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    expect(page.locator(".asc-secret-value")).to_be_visible()
    _shot(page, "inv-08-on-behalf-claim-link-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar", f"{base}/organizations/workspace/on-behalf/new/{lab.pk}/")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    _shot(page, "inv-08-on-behalf-errors-ar-mobile.png", "ar", MOBILE)


def test_public_invitation_states_evidence(live_server, page, stage3_world) -> None:
    for language, viewport, size in (
        ("en", DESKTOP, "desktop"),
        ("fr", MOBILE, "mobile"),
        ("ar", DESKTOP, "desktop"),
        ("ar", PHONE, "phone"),
    ):
        _go(page, live_server, language, f"{live_server.url}/invite/never-issued-token/")
        expect(page.locator("[role=alert]")).to_be_visible()
        _shot(page, f"inv-09-invitation-unavailable-{language}-{size}.png", language, viewport)
    _login_participant(live_server, page, "stage3.claimer@example.test")
    for language, viewport, size in (("en", DESKTOP, "desktop"), ("ar", MOBILE, "mobile")):
        _go(page, live_server, language, f"{live_server.url}/claim/not-a-real-claim-token/")
        _shot(page, f"inv-10-claim-unavailable-{language}-{size}.png", language, viewport)


def test_review_workflow_evidence(live_server, page, stage3_world) -> None:
    base = live_server.url
    case = stage3_world["cases"][0]
    registration = stage3_world["registrations"][0]
    _sign_in(live_server, page, stage3_world["operator"])

    _go(page, live_server, "en", f"{base}/ops/reviews/cases/{case.pk}/decision/not-approved/")
    _shot(page, "rev-01-not-approved-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "fr")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    expect(page.locator("#error-summary")).to_be_visible()
    _shot(page, "rev-01-not-approved-errors-fr-desktop.png", "fr", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "rev-01-not-approved-ar-mobile.png", "ar", MOBILE)

    _go(
        page,
        live_server,
        "en",
        f"{base}/ops/reviews/cases/{stage3_world['cases'][2].pk}/information-requests/new/",
    )
    _shot(page, "rev-02-information-request-new-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    page.locator("#main-content form button[type=submit]").click()
    page.wait_for_load_state("load")
    expect(page.locator("#error-summary")).to_be_visible()
    assert page.locator("#id_message_ar").get_attribute("dir") == "rtl"
    assert page.locator("#id_message_en").get_attribute("dir") == "ltr"
    _shot(page, "rev-02-information-request-errors-ar-desktop.png", "ar", DESKTOP)
    _go(page, live_server, "fr")
    _shot(page, "rev-02-information-request-new-fr-mobile.png", "fr", MOBILE)

    request = stage3_world["information_request"]
    _go(page, live_server, "en", f"{base}/ops/reviews/information-requests/{request.pk}/")
    _shot(page, "rev-03-information-request-detail-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "rev-03-information-request-detail-ar-mobile.png", "ar", MOBILE)
    _go(page, live_server, "fr")
    _shot(page, "rev-03-information-request-detail-fr-wide.png", "fr", WIDE)

    _go(page, live_server, "fr", f"{base}/ops/reviews/registrations/{registration.pk}/reopen/")
    _shot(page, "rev-04-reopen-fr-desktop.png", "fr", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "rev-04-reopen-ar-mobile.png", "ar", MOBILE)

    _go(page, live_server, "en", f"{base}/ops/reviews/registrations/{registration.pk}/cancel/")
    _shot(page, "rev-05-cancel-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "rev-05-cancel-ar-mobile.png", "ar", MOBILE)

    # A real stale-version conflict (409): the assignment form carries an
    # outdated version.
    _go(page, live_server, "en", f"{base}/ops/reviews/cases/{case.pk}/")
    form = page.locator(f"form[action='/ops/reviews/cases/{case.pk}/assign/']")
    form.locator("select[name=assigned_user_id]").select_option(index=1)
    form.locator("input[name=expected_version]").evaluate("el => { el.value = '999'; }")
    with page.expect_navigation():
        form.locator("button[type=submit]").click()
    expect(page.locator("h1")).to_contain_text("This changed since you loaded it")
    _shot(page, "rev-07-conflict-en-desktop.png", "en", DESKTOP)
    _shot(page, "rev-07-conflict-en-mobile.png", "en", MOBILE)

    for language, viewport, size in (("fr", DESKTOP, "desktop"), ("ar", MOBILE, "mobile")):
        _go(page, live_server, language, f"{base}/ops/reviews/cases/{stage3_world['cases'][1].pk}/")
        _shot(page, f"rev-08-case-detail-{language}-{size}.png", language, viewport)


def test_participant_information_request_evidence(live_server, page, stage3_world) -> None:
    from apps.people.services import resolve_or_create_participant_for_email
    from apps.registrations.models import Registration
    from apps.reviews.services import send_information_request

    email = "stage3.responder@example.test"
    person = database_call(lambda: resolve_or_create_participant_for_email(email))
    registration = stage3_world["registrations"][0]
    database_call(lambda: Registration.objects.filter(pk=registration.pk).update(person=person))
    request = stage3_world["information_request"]
    database_call(
        lambda: send_information_request(
            information_request=request,
            expected_version=request.version,
            actor=stage3_world["operator"],
        )
    )
    _login_participant(live_server, page, email)
    url = f"{live_server.url}/register/workspace/requests/{request.pk}/"
    _go(page, live_server, "en", f"{live_server.url}/workspace/")
    _shot(page, "par-01-workspace-action-required-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "en", url)
    _shot(page, "par-02-information-request-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "par-02-information-request-ar-mobile.png", "ar", MOBILE)
    _go(page, live_server, "fr")
    page.fill("input[name^=value_]", "Directrice des partenariats")
    page.locator("button[value=draft]").click()
    page.wait_for_load_state("load")
    expect(page.locator(".asc-flash")).to_be_visible()
    _shot(page, "par-02-information-request-draft-saved-fr-desktop.png", "fr", DESKTOP)
    _go(page, live_server, "ar", f"{live_server.url}/workspace/")
    _shot(page, "par-01-workspace-ar-mobile.png", "ar", MOBILE)


def test_accreditation_detail_evidence(live_server, page, stage3_world) -> None:
    approved = stage3_world["approved"]
    _sign_in(live_server, page, stage3_world["operator"])
    url = f"{live_server.url}/ops/accreditation/registrations/{approved.pk}/"
    _go(page, live_server, "en", url)
    for label in page.locator("#main-content form.asc-command input[type=text]").all():
        # Every inline command input has a visible label, not a placeholder.
        label_for = label.get_attribute("id")
        expect(page.locator(f"label[for='{label_for}']")).to_be_visible()
    _shot(page, "acc-01-registration-accreditation-en-desktop.png", "en", DESKTOP)
    _shot(page, "acc-01-registration-accreditation-en-wide.png", "en", WIDE)
    _go(page, live_server, "fr")
    _shot(page, "acc-01-registration-accreditation-fr-tablet.png", "fr", TABLET)
    _go(page, live_server, "ar")
    _shot(page, "acc-01-registration-accreditation-ar-mobile.png", "ar", MOBILE)
    _shot(page, "acc-01-registration-accreditation-ar-desktop.png", "ar", DESKTOP)
    # A stale revoke (409) renders the shared accreditation conflict state.
    _go(page, live_server, "en", url)
    form = page.locator("form[action*='/BADGE_TYPE/'][action$='/revoke/']")
    form.locator("input[name=reason]").fill("Synthetic stale revoke")
    form.locator("input[name=expected_version]").evaluate("el => { el.value = '999'; }")
    form.locator("button[type=submit]").click()
    page.locator("#asc-confirm-dialog [data-confirm-accept]").click()
    page.wait_for_load_state("load")
    expect(page.locator("h1")).to_contain_text("This changed since you loaded it")
    _shot(page, "acc-05-conflict-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar", f"{live_server.url}/ops/accreditation/bulk/preview/")
    _shot(page, "acc-02-bulk-form-ar-mobile.png", "ar", MOBILE)


def test_communications_evidence(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    _go(page, live_server, "en", f"{live_server.url}/ops/communications/")
    _shot(page, "com-01-delivery-en-desktop.png", "en", DESKTOP)
    _go(page, live_server, "ar")
    _shot(page, "com-01-delivery-ar-mobile.png", "ar", MOBILE)
    _shot(page, "com-01-delivery-ar-desktop.png", "ar", DESKTOP)
    _go(page, live_server, "fr")
    _shot(page, "com-01-delivery-fr-phone.png", "fr", PHONE)
    assert "DECISION_STATUS" not in page.inner_text("main")
    empty_user = _scoped_user(
        "stage3.comms@example.test",
        "communications.view_communicationmessage",
        event=stage3_world["event"],
        organization=stage3_world["institute"],
    )
    page.context.clear_cookies()
    _sign_in(live_server, page, empty_user)
    _go(page, live_server, "fr", f"{live_server.url}/ops/communications/")
    _shot(page, "com-02-delivery-scoped-fr-desktop.png", "fr", DESKTOP)


def test_every_stage3_page_is_overflow_free_at_every_width(live_server, page, stage3_world) -> None:
    _sign_in(live_server, page, stage3_world["operator"])
    base = live_server.url
    urls = (
        f"{base}/organizations/workspace/",
        f"{base}/organizations/workspace/delegations/{stage3_world['batch'].pk}/",
        f"{base}/ops/accreditation/registrations/{stage3_world['approved'].pk}/",
        f"{base}/ops/reviews/information-requests/{stage3_world['information_request'].pk}/",
        f"{base}/ops/exports/",
        f"{base}/ops/communications/",
    )
    for language in ("en", "ar"):
        for url in urls:
            _go(page, live_server, language, url)
            for viewport in (PHONE, MOBILE, TABLET, DESKTOP, WIDE):
                page.set_viewport_size(viewport)
                assert page.evaluate(OVERFLOW_JS)["overflow"] <= 0, (language, url, viewport)
                _geometry(page, f"{url} {language} {viewport}")
