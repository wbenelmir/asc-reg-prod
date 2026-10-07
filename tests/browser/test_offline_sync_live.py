"""Browser-to-server offline synchronization and Prompt 3 visual evidence
(Phase 4 Prompt 3 correction 4, G-01 and G-02). Chromium via Playwright,
against the live test server and real PostgreSQL. All data is synthetic.

Nothing on the synchronization path is a test double. The device is prepared
through the production shell (non-extractable WebCrypto keys, the real
package build, signature verification, decryption and activation, the
self-test and an operator grant). An offline admission is recorded by the
production verifier and decision commands (canonical JSON, a P-256 WebCrypto
signature, the hash chain and strict IndexedDB durability), uploaded to the
real `/entry/api/v1/offline/sync/` endpoint, verified, bound and processed
by the real service, and settled only after the production client verified
the real signed `ASC-OACK` acknowledgement against its pinned keys.

Transient conditions come only from controlled networking: the browser
context goes offline (application heartbeats fail), one response is dropped
after the real server committed it (a lost acknowledgement), or one real
upload is held in flight while its state is captured. Nothing fabricates
DOM, application state or a screenshot: the page only refreshes its own
counts from IndexedDB and renders through the production code.

Screenshots go to `var/test_artifacts/phase4/prompt3-visual/` -- a NEW folder.
They are evidence of what the automated run rendered, not a claim of human,
screen-reader or physical-device review.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from playwright.sync_api import expect

from apps.entry.offline_contract import TYP_ACKNOWLEDGEMENT
from apps.entry.services.offline_crypto import jws_verify, public_key_der
from tests.browser import test_offline_pwa as pwa
from tests.browser.conftest import SYNTHETIC_NAME_AR
from tests.browser.database import database_sync

pytestmark = pytest.mark.django_db(transaction=True)

# The Prompt 2 preparation world: offline enabled, an offline-capable scope,
# a pinned package signer and one supervisor who is also device administrator.
offline_world = pwa.offline_world

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "var/test_artifacts/phase4/prompt3-visual"
SYNC_PATTERN = "**/entry/api/v1/offline/sync/"
PHONE = {"width": 390, "height": 844}
TABLET = {"width": 820, "height": 1180}
DESKTOP = {"width": 1440, "height": 900}
LOGO_RATIO = 537 / 240

LOCAL_QUEUE = """
async () => {
  const db = window.AscOffline._db;
  const read = (store, key) => new Promise((resolve, reject) => {
    const objects = db.transaction([store]).objectStore(store);
    const request = key === undefined ? objects.getAll() : objects.get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const rows = await read('ops');
  const chain = await read('meta', 'chain');
  const signer = await read('keys', 'device-sign');
  return {
    rows: rows.map((row) => ({
      seq: row.seq, operation_id: row.operation_id, state: row.state, chain: row.chain,
      sealed: !!(row.sealed && row.sealed.ct), plain_operation: 'op' in row,
      outcome: row.outcome || null,
    })),
    chain: chain ? {store: chain.store, sequence: chain.sequence, head: chain.head} : null,
    sign_spki: signer ? signer.spki : null,
    sign_extractable: signer ? signer.privateKey.extractable : null,
  };
}
"""

VISUAL_CONTRACT = """
async (language) => {
  await document.fonts.ready;
  const html = document.documentElement;
  const shown = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden';
  };
  const inScroller = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return true;
    }
    return false;
  };
  const width = html.clientWidth;
  return {
    lang: html.getAttribute('lang'),
    dir: html.getAttribute('dir'),
    h1: document.querySelectorAll('h1').length,
    ascUi: document.body.classList.contains('asc-ui'),
    exposedIcons: document.querySelectorAll('svg.asc-icon:not([aria-hidden=true])').length,
    logos: [...document.querySelectorAll("img[src$='asc-logo.svg']")].filter(shown)
      .map((el) => {
        const r = el.getBoundingClientRect();
        const identity = el.closest('[data-conference-identity]');
        return {
          w: r.width, h: r.height, alt: el.alt,
          decorative: el.getAttribute('aria-hidden') === 'true',
          named: identity ? identity.innerText : '',
        };
      }),
    headingFont: getComputedStyle(document.querySelector('h1')).fontFamily,
    bodyFont: getComputedStyle(document.body).fontFamily,
    thmanyahLoaded: document.fonts.check('16px "Thmanyah Sans"', 'تحقق دون اتصال'),
    overflow: html.scrollWidth - width,
    offenders: [...document.querySelectorAll('body *')].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width && (r.right > width + 1 || r.left < -1) && !inScroller(el);
    }).slice(0, 5).map((el) => el.tagName + '.' + el.className),
    unreadable: [...document.querySelectorAll('button, a.btn, select, input[type=submit]')]
      .filter(shown).filter((el) => {
        const name = (el.innerText || el.getAttribute('aria-label') || el.value || '').trim();
        return !name || parseFloat(getComputedStyle(el).fontSize) < 12;
      }).map((el) => el.outerHTML.slice(0, 80)),
    clippedChips: [...document.querySelectorAll('.asc-chip')].filter(shown)
      .filter((el) => el.scrollWidth > el.clientWidth + 1).map((el) => el.textContent.trim()),
  };
}
"""

FOCUSED_OUTLINE = """
() => {
  const el = document.activeElement;
  const style = getComputedStyle(el);
  return {tag: el.tagName, style: style.outlineStyle, width: parseFloat(style.outlineWidth)};
}
"""


# ---------------------------------------------------------------------------
# Helpers (database work only through the explicit boundary)
# ---------------------------------------------------------------------------


def _admit_offline(page, signed_qr: str, *, pending: int) -> None:
    """Verify a real signed QR offline and admit through the production UI."""
    page.locator("#offline-qr").fill(signed_qr)
    page.locator("#offline-verify-form button[type=submit]").click()
    admit = page.locator("[data-offline-decision=ADMIT]")
    expect(admit).to_be_enabled(timeout=15_000)
    admit.click()
    # Recorded only once the strict IndexedDB transaction committed.
    page.wait_for_function(
        f"() => window.AscOffline.state.counts.pending === {pending}", timeout=15_000
    )


def _wait_until(page, predicate, *, timeout_ms: int, message: str) -> None:
    """Poll in Python (route handlers run while Playwright is waiting)."""
    waited = 0
    while not predicate():
        assert waited < timeout_ms, message
        page.wait_for_timeout(250)
        waited += 250


def _field(page, name: str) -> str:
    return page.locator(f"[data-offline-field={name}]").inner_text().strip()


def _shell_state(page) -> dict:
    """Diagnostics for a failed assertion: the production state computation."""
    return page.evaluate(
        "() => ({computed: window.AscOffline.computeState(window.AscOffline.currentContext()),"
        " mode: window.AscOffline.health.mode, counts: window.AscOffline.state.counts})"
    )


@database_sync
def _server_evidence(device_pk) -> dict:
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.entry.models import EntryEvent, SyncOperation, VerificationSample

    operations = [
        {
            "id": str(row.pk),
            "operation_id": row.operation_id,
            "sequence": row.device_sequence,
            "store": row.store_id,
            "status": row.status,
            "outcome": row.outcome_code,
            "conflict_type": row.conflict_type,
            "payload_hash": row.payload_hash,
            "chain_hash": row.chain_hash,
            "signing_key_spki": row.signing_key.public_key_spki if row.signing_key else None,
            "payload_device": row.payload_json.get("device"),
            "duplicates": row.duplicate_submissions,
            "processed_at": int(row.processed_at.timestamp()) if row.processed_at else None,
            "result_reference": str(row.result_reference) if row.result_reference else None,
        }
        for row in SyncOperation.objects.filter(device_id=device_pk)
        .select_related("signing_key")
        .order_by("device_sequence")
    ]
    events = [
        {
            "id": str(event.pk),
            "operation_id": event.operation_id,
            "sync_operation_id": str(event.sync_operation_id),
            "decision": event.decision,
            "offline": event.offline,
            "offline_conflict": event.offline_conflict,
            "package_version": event.package_version,
        }
        for event in EntryEvent.objects.filter(device_id=device_pk).order_by("recorded_at")
    ]
    batches = list(
        AuditEvent.objects.filter(
            target_uuid=device_pk, action_code=action_codes.OFFLINE_SYNC_BATCH_RECEIVED
        ).values_list("after_summary", flat=True)
    )
    return {
        "operations": operations,
        "events": events,
        "batches": batches,
        "applied_audits": AuditEvent.objects.filter(
            action_code=action_codes.OFFLINE_EVENT_APPLIED
        ).count(),
        "offline_samples": VerificationSample.objects.filter(
            device_id=device_pk, mode="OFFLINE"
        ).count(),
    }


def _verified_ack(world, compact: str) -> dict:
    """The acknowledgement the browser accepted, verified again here against
    the server's package-signing key (never a key the response carried)."""
    der = public_key_der(load_pem_public_key(world["signer"].public_key_pem("p1")))
    return jws_verify(compact, trusted_keys_der={"p1": der}, typ=TYP_ACKNOWLEDGEMENT)


def _assert_applied_once(world, local, server, package_version) -> dict:
    """The browser's record and the server's durable evidence agree."""
    [record] = local["rows"]
    [operation] = server["operations"]
    [event] = server["events"]
    assert record["state"] == "ACKNOWLEDGED"
    assert record["sealed"] and not record["plain_operation"]  # still encrypted at rest
    assert (record["outcome"]["status"], record["outcome"]["outcome"]) == (
        "APPLIED",
        "ADMISSION_APPLIED",
    )
    # The server accepted the browser's canonical bytes, its WebCrypto
    # signature under the key the browser registered, and its hash chain.
    assert operation["operation_id"] == record["operation_id"]
    assert operation["sequence"] == record["seq"] == 1
    assert operation["store"] == local["chain"]["store"]
    assert operation["chain_hash"] == record["chain"] == local["chain"]["head"]
    assert operation["signing_key_spki"] == local["sign_spki"]
    assert operation["payload_device"] == world["device"].public_id
    assert (operation["status"], operation["outcome"]) == ("APPLIED", "ADMISSION_APPLIED")
    assert event["operation_id"] == record["operation_id"]
    assert event["sync_operation_id"] == operation["id"]
    assert operation["result_reference"] == event["id"]
    assert event["decision"] == "ADMIT" and event["offline"] and not event["offline_conflict"]
    assert event["package_version"] == package_version  # the package the browser used
    assert server["applied_audits"] == 1 and server["offline_samples"] == 1
    # The signed acknowledgement names exactly this durable outcome.
    payload = _verified_ack(world, record["outcome"]["ack"])
    assert payload == {
        "typ": "ASC-OACK",
        "schema_version": 1,
        "device": world["device"].public_id,
        "store": local["chain"]["store"],
        "operation_id": record["operation_id"],
        "sequence": 1,
        "payload_hash": operation["payload_hash"],
        "status": "APPLIED",
        "outcome": "ADMISSION_APPLIED",
        "conflict_type": "",
        "processed_at": operation["processed_at"],
    }
    return operation


# ---------------------------------------------------------------------------
# G-01: real browser-to-server synchronization
# ---------------------------------------------------------------------------


def test_a_browser_recorded_offline_admission_is_applied_by_the_real_server(
    live_server, page, offline_world
):
    pwa._sign_in(page, live_server, offline_world)
    pwa._prepare(page, live_server, offline_world)
    pwa._go_offline(page)
    assert pwa._state(page) == "OFFLINE_ACTIVE"
    _admit_offline(page, offline_world["token_en"], pending=1)
    local = page.evaluate(LOCAL_QUEUE)
    assert local["sign_extractable"] is False  # the private key never leaves WebCrypto
    assert [row["state"] for row in local["rows"]] == ["PENDING"]
    assert _server_evidence(offline_world["device"].pk)["operations"] == []
    package_version = page.evaluate("() => window.AscOffline.state.activeMeta.package_version")

    uploads = []
    page.on(
        "request",
        lambda request: (
            uploads.append(request.headers)
            if request.url.endswith("/entry/api/v1/offline/sync/")
            else None
        ),
    )
    page.context.set_offline(False)
    page.wait_for_function(
        "() => window.AscOffline.state.counts.acknowledged === 1"
        " && window.AscOffline.state.counts.unacknowledged === 0",
        timeout=120_000,
    )
    # One signed, nonce-bound upload through the device API.
    assert len(uploads) == 1
    assert uploads[0]["x-asc-device-nonce"] and uploads[0]["x-asc-device-signature"]
    local = page.evaluate(LOCAL_QUEUE)
    server = _server_evidence(offline_world["device"].pk)
    operation = _assert_applied_once(offline_world, local, server, package_version)
    assert operation["duplicates"] == 0
    assert [batch["channel"] for batch in server["batches"]] == ["SYNC"]
    pwa._render(page)
    expect(page.locator("#offline-sync-status")).to_have_attribute("data-sync-status", "sync.done")
    assert _field(page, "acknowledged") == "1" and _field(page, "pending") == "0"


def test_a_lost_acknowledgement_is_retried_without_a_second_application(
    live_server, page, offline_world
):
    pwa._sign_in(page, live_server, offline_world)
    pwa._prepare(page, live_server, offline_world)
    pwa._go_offline(page)
    _admit_offline(page, offline_world["token_en"], pending=1)
    package_version = page.evaluate("() => window.AscOffline.state.activeMeta.package_version")
    delivered = []

    def drop_the_first_acknowledgement(route):
        if delivered:
            route.continue_()
            return
        # The real server receives, verifies and commits the upload...
        response = route.fetch()
        delivered.append(response.json())
        # ...and its acknowledgement is lost on the way back to the page.
        route.abort("connectionreset")

    page.route(SYNC_PATTERN, drop_the_first_acknowledgement)
    page.context.set_offline(False)
    _wait_until(
        page, lambda: delivered, timeout_ms=120_000, message="the upload never reached the server"
    )
    [first] = delivered[0]["acknowledgements"]
    assert (first["status"], first["durable"], first["replayed"]) == ("APPLIED", True, False)
    committed = _server_evidence(offline_world["device"].pk)
    assert len(committed["operations"]) == 1 and len(committed["events"]) == 1
    # Committed on the server, but never shown as synchronized in the browser.
    counts = page.evaluate("() => window.AscOffline.opstore.counts(window.AscOffline._db)")
    assert counts["acknowledged"] == 0 and counts["unacknowledged"] == 1

    # The production retry (bounded backoff) resends the SAME signed record.
    page.wait_for_function(
        "() => window.AscOffline.state.counts.acknowledged === 1", timeout=120_000
    )
    page.unroute(SYNC_PATTERN)
    local = page.evaluate(LOCAL_QUEUE)
    server = _server_evidence(offline_world["device"].pk)
    operation = _assert_applied_once(offline_world, local, server, package_version)
    assert operation["duplicates"] == 1  # counted, never applied a second time
    assert operation["processed_at"] == committed["operations"][0]["processed_at"]
    assert len(server["batches"]) == 2


# ---------------------------------------------------------------------------
# G-02: Prompt 3 visual evidence (documented coverage matrix)
# ---------------------------------------------------------------------------

#: language -> (primary layout, extra layouts). Arabic is captured at both
#: ends so right-to-left is shown on a phone and on a desktop.
VISUAL_RUNS = {
    "en": (("desktop", DESKTOP), ()),
    "fr": (("tablet", TABLET), ()),
    "ar": (("phone", PHONE), (("desktop", DESKTOP),)),
}


def _visual_contract(page, language: str, label: str) -> None:
    result = page.evaluate(VISUAL_CONTRACT, language)
    assert result["lang"] == language, label
    assert result["dir"] == ("rtl" if language == "ar" else "ltr"), label
    assert result["h1"] == 1 and result["ascUi"], label
    assert result["exposedIcons"] == 0, label  # every icon is decorative
    assert result["logos"], f"{label}: the ASC logo is not shown"
    for logo in result["logos"]:
        # Either a meaningful image (the offline shell's own logo has a text
        # alternative), or -- version 1.1 UI work package 02 (owner,
        # 2026-10-05) -- a decorative logo beside the visible conference name
        # and year in the shared header. Proportions stay the original.
        if logo["alt"]:
            assert not logo["decorative"], (label, logo)
        else:
            assert logo["decorative"] and "2026" in logo["named"], (label, logo)
        assert abs(logo["w"] / logo["h"] - LOGO_RATIO) < 0.02, (label, logo)
    if language == "ar":
        assert result["thmanyahLoaded"], f"{label}: Thmanyah Sans is not loaded"
        assert result["headingFont"].startswith('"Thmanyah Sans"'), (label, result)
        assert result["bodyFont"].startswith('"Thmanyah Sans"'), (label, result)
    else:
        assert "Thmanyah" not in result["headingFont"] + result["bodyFont"], label
    assert result["overflow"] <= 0, f"{label}: horizontal overflow {result['offenders']}"
    assert result["unreadable"] == [], (label, result["unreadable"])
    assert result["clippedChips"] == [], (label, result["clippedChips"])


def _keyboard_focus_is_visible(page, label: str) -> None:
    page.locator("a.skip-link").focus()
    page.keyboard.press("Tab")
    focused = page.evaluate(FOCUSED_OUTLINE)
    assert focused["style"] == "solid" and focused["width"] >= 2, (label, focused)
    page.evaluate("() => document.activeElement.blur()")  # no focus ring in the captures


def _capture(page, language: str, name: str, layouts) -> list[str]:
    captured = []
    for layout, viewport in layouts:
        label = f"{name}-{language}-{layout}"
        page.set_viewport_size(viewport)
        page.evaluate("window.scrollTo(0, 0)")
        _visual_contract(page, language, label)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(EVIDENCE / f"{label}.png"), full_page=True)
        captured.append(f"{label}.png")
    return captured


@database_sync
def _revoke_pass_of(world, display_name: str) -> None:
    from apps.badges.models import DigitalEntryPass, PassReasonCode
    from apps.badges.services import new_operation_id, revoke_pass
    from apps.entry.tests import factories

    credential = DigitalEntryPass.objects.get(
        event_edition=world["event"],
        registration__person__display_name=display_name,
        status="ACTIVE",
    )
    revoke_pass(
        credential=credential,
        actor=factories.make_user("browser.revocation@example.test"),
        operation_id=new_operation_id(),
        expected_lock_version=credential.version,
        reason_code=PassReasonCode.LOST_OR_COMPROMISED,
    )


@database_sync
def _block_offline_use(world) -> None:
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_devices import block_offline_use

    device = EntryDevice.objects.get(pk=world["device"].pk)
    block_offline_use(
        device=device,
        actor=world["operator"],
        expected_version=device.version,
        reason_code="SECURITY_CONCERN",
    )


@database_sync
def _conflict_case_public_id(world) -> str:
    from apps.entry.models import ReconciliationCase

    return ReconciliationCase.objects.get(
        event_edition=world["event"], case_type="PASS_REVOKED"
    ).public_id


@pytest.mark.parametrize("language", sorted(VISUAL_RUNS))
def test_prompt3_offline_states_and_reconciliation_visual_evidence(
    live_server, page, offline_world, language
):
    primary, extra = VISUAL_RUNS[language]
    layout, viewport = primary
    every = (primary, *extra)
    pwa._sign_in(page, live_server, offline_world, language=language, viewport=viewport)
    pwa._prepare(page, live_server, offline_world)
    pwa._go_offline(page)
    # Revoked on the server while the device is disconnected: it cannot know.
    # Offline evidence is timestamped to the whole second, so the admission
    # follows the revocation by a clear margin (as the server suite does).
    _revoke_pass_of(offline_world, SYNTHETIC_NAME_AR)
    page.wait_for_timeout(3_000)
    _admit_offline(page, offline_world["token_en"], pending=1)
    _admit_offline(page, offline_world["token_ar"], pending=2)
    pwa._render(page)

    # 1. Offline Active, with two durable records waiting.
    assert pwa._state(page) == "OFFLINE_ACTIVE"
    assert _field(page, "pending") == "2" and _field(page, "acknowledged") == "0"
    expect(page.locator("#offline-announcer")).to_have_attribute("aria-live", "polite")
    expect(page.locator("#offline-alert")).to_have_attribute("aria-live", "assertive")
    expect(page.locator("#offline-announcer")).not_to_be_empty()
    _keyboard_focus_is_visible(page, f"offline-active-{language}")
    _capture(page, language, "01-offline-active", every)

    # 2. Syncing: the real upload is held in flight by the network layer.
    page.set_viewport_size(viewport)
    held = []
    page.route(SYNC_PATTERN, lambda route: held.append(route))
    page.context.set_offline(False)
    _wait_until(page, lambda: held, timeout_ms=120_000, message="no upload started")
    page.evaluate(
        "() => window.AscOffline.refreshCounts()"
        ".then(() => window.AscOffline.render(window.AscOffline._db))"
    )
    assert pwa._state(page) == "SYNCING", _shell_state(page)
    assert _field(page, "in_flight") == "2" and _field(page, "acknowledged") == "0"
    _capture(page, language, "02-syncing", (primary,))
    held[0].continue_()  # released to the real server
    page.unroute(SYNC_PATTERN)
    page.wait_for_function(
        "() => window.AscOffline.state.counts.acknowledged === 2", timeout=120_000
    )
    # Recovery completes on its own (records first, then the critical delta).
    page.wait_for_function("() => window.AscOffline.health.mode === 'online'", timeout=60_000)
    pwa._render(page)

    # 3. The real server's conflict, surfaced for supervisor review.
    assert _field(page, "conflicted") == "1", (
        page.evaluate(LOCAL_QUEUE)["rows"],
        _server_evidence(offline_world["device"].pk)["operations"],
    )
    notice = page.locator("#offline-conflicts-notice")
    expect(notice).to_be_visible()
    expect(notice).to_have_attribute("role", "status")
    _capture(page, language, "03-conflict-notice", every)

    # 4. Incident procedure: offline use is blocked on the server (the device
    # purges its package and locks its records on the next heartbeat), then
    # the connection fails -- no valid package: verification cannot proceed.
    _block_offline_use(offline_world)
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    pwa._go_offline(page)
    assert pwa._state(page) == "BLOCKED", _shell_state(page)
    expect(page.locator("#offline-incident")).to_be_visible()
    expect(page.locator("#offline-incident ol li")).to_have_count(4)
    _capture(page, language, "04-incident-procedure", every)
    page.context.set_offline(False)

    # 5-6. The supervisor's reconciliation queue and the conflict case.
    page.set_viewport_size(viewport)
    page.goto(f"{live_server.url}/ops/entry/events/{offline_world['event'].pk}/reconciliation/")
    expect(page.locator("#recovery-counts [data-count=conflict] .asc-kpi-value")).to_have_text("1")
    expect(page.locator("#recovery-counts [data-count=accepted] .asc-kpi-value")).to_have_text("1")
    _keyboard_focus_is_visible(page, f"reconciliation-queue-{language}")
    _capture(page, language, "05-reconciliation-queue", every)
    page.set_viewport_size(viewport)
    page.goto(
        f"{live_server.url}/ops/entry/reconciliation/{_conflict_case_public_id(offline_world)}/"
    )
    expect(page.locator("#case-conflict-notice")).to_be_visible()
    _capture(page, language, "06-reconciliation-case", every)
    events = _server_evidence(offline_world["device"].pk)["events"]
    assert [event["offline_conflict"] for event in events] == [False, True]
