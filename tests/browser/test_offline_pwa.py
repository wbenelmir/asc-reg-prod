"""Real-browser coverage for Phase 4 Prompt 2: enrolled-device PWA shell and
Offline Package preparation (ADR-0023). Chromium via Playwright.

Everything here runs in a real browser against the live test server: real
WebCrypto key generation (non-extractable), real IndexedDB, a real service
worker scoped to /entry/, real signature verification and decryption of
packages the server built for this browser. All data is synthetic.

Screenshots go to `var/test_artifacts/phase4/prompt2-offline/` -- a NEW folder,
never an approved Phase 3 evidence folder. They are evidence of what the
automated run rendered, not a claim of human review.

Not covered here (environment-blocked, never claimed): Safari/iOS storage
eviction, installed-PWA behaviour on managed devices, real device clocks,
screen readers, cameras and handheld scanners.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser.conftest import SYNTHETIC_NAME_AR, SYNTHETIC_NAME_EN
from tests.browser.database import (
    database_call,
    database_fixture,
    database_sync,
    operational_session_key,
)

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "var/test_artifacts/phase4/prompt2-offline"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
ACCEPTING_MFA = "apps.accounts.tests.mfa_double.AcceptingStepUpBackend"

IDB_HELPERS = """
          const idbGet = (db, store, key) => new Promise((resolve) => {
            const request = db.transaction([store]).objectStore(store).get(key);
            request.onsuccess = () => resolve(request.result);
          });
          const idbPut = (db, store, row) => new Promise((resolve) => {
            const t = db.transaction([store], "readwrite");
            t.objectStore(store).put(row);
            t.oncomplete = resolve;
          });
"""

APPEND = (
    "() => window.AscOffline.opstore.append(window.AscOffline._db,"
    " {{operation_id: '{0}', type: 'TEST'}})"
)

READ_DB = """
async () => {
  const db = await new Promise((resolve, reject) => {
    const r = indexedDB.open('asc-entry-offline');
    r.onsuccess = () => resolve(r.result);
    r.onerror = reject;
  });
  const all = (store) => new Promise((resolve) => {
    const t = db.transaction([store], 'readonly').objectStore(store).getAll();
    t.onsuccess = () => resolve(t.result);
  });
  const out = {keys: await all('keys'), packages: await all('packages'),
               ops: await all('ops'), meta: await all('meta')};
  db.close();
  return {
    keys: out.keys.map(k => ({name: k.name,
      extractable: (k.privateKey || k.key || {}).extractable,
      algorithm: ((k.privateKey || k.key || {}).algorithm || {}).name})),
    packages: out.packages.map(p => ({slot: p.slot, meta: p.meta || null, raw: p.raw || ''})),
    ops: out.ops.map(o => ({seq: o.seq, state: o.state, sealed: o.sealed, chain: o.chain})),
    meta: out.meta.map(m => m.name),
    text: JSON.stringify(out),
  };
}
"""


def _shot(page, name: str) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / name), full_page=True)


@pytest.fixture
@database_fixture
def offline_world(entry_world, settings, tmp_path):
    """entry_world + offline enabled, a package signer, an offline-capable
    scope, and ONE named user who is both device administrator and gate
    supervisor (so the same person can prepare the device and hold a grant)."""
    from apps.core.crypto.package_signing import (
        InMemoryPackageSigningKeyProvider,
        set_package_signing_key_provider_for_testing,
    )
    from apps.entry.tests import factories, offline_factories

    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root
    # The approved hard expiry is capped by the END OF THE EVENT DAY in the
    # event timezone. Pick a timezone in which at least 9 hours of the day
    # remain, so the Fresh/Aging/Stale/Expired bands are all reachable
    # whatever the wall-clock time of the run (the cap itself is tested in
    # apps/entry/tests/test_offline_contract.py).
    event = entry_world["event"]
    event.timezone = _timezone_with_day_left(hours=9)
    event.save(update_fields=["timezone"])
    settings.ENTRY_OFFLINE_ENABLED = True
    settings.ENTRY_CONNECTION_POLL_SECONDS = 2
    signer = InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")
    set_package_signing_key_provider_for_testing(signer)
    user = entry_world["operator"]
    factories.grant(user, "Entry Device Administrators", event=entry_world["event"])
    offline_factories.enable_event(entry_world["event"], user)
    offline_factories.make_offline_capable(
        entry_world["device"], admin=user, layout=entry_world["layout"]
    )
    yield {**entry_world, "signer": signer}
    set_package_signing_key_provider_for_testing(None)


def _timezone_with_day_left(*, hours: int) -> str:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    for name in (
        "UTC",
        "Pacific/Pago_Pago",
        "Pacific/Kiritimati",
        "America/New_York",
        "Asia/Tokyo",
    ):
        local = now.astimezone(ZoneInfo(name))
        if 24 - local.hour - local.minute / 60 >= hours:
            return name
    raise AssertionError("no timezone with enough day left")  # pragma: no cover


def _sign_in(page, live_server, world, *, language="en", viewport=DESKTOP, init_script=None):
    from django.conf import settings

    page.set_viewport_size(viewport)
    if init_script:
        page.add_init_script(init_script)
    user = world["operator"]
    session_key = operational_session_key(user)
    page.goto(f"{live_server.url}/healthz")
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session_key,
                "url": live_server.url,
            },
            {"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": live_server.url},
            {
                "name": settings.ENTRY_DEVICE_COOKIE_NAME,
                "value": world["secret"],
                "url": f"{live_server.url}/entry/",
            },
        ]
    )


def _start_checkpoint(page, live_server, world):
    page.goto(f"{live_server.url}/entry/")
    page.locator("select[name=zone_id]").select_option(str(world["layout"].main.pk))
    page.locator("form:has(input[name=action][value=start]) button[type=submit]").click()
    expect(page).to_have_url(re.compile(r"/entry/verify/$"))


def _shell(page, live_server):
    page.goto(f"{live_server.url}/entry/offline/")
    expect(page.locator("#offline-state-chip")).not_to_have_attribute("data-offline-state", "")


def _state(page):
    return page.locator("#offline-state-chip").get_attribute("data-offline-state")


def _prepare(page, live_server, world):
    _start_checkpoint(page, live_server, world)
    _shell(page, live_server)
    page.locator("[data-offline-action=prepare]").click()
    page.wait_for_function(
        "() => !!(window.AscOffline.state.offline && window.AscOffline.state.offline.ready)",
        timeout=30_000,
    )
    expect(page.locator("#offline-message")).to_have_class(re.compile("asc-notice-success"))
    page.locator("[data-offline-action=grant]").click()
    page.wait_for_function("() => window.AscOffline.state.grants.length === 1", timeout=15_000)
    page.evaluate("() => window.AscOffline.render(window.AscOffline._db)")
    expect(page.locator("#offline-state-chip")).to_have_attribute(
        "data-offline-state", "OFFLINE_READY"
    )


def _wait_for_active_worker(page, timeout_ms=15_000):
    """Poll (in Python) until the /entry/ worker is activated. A predicate that
    returns a Promise would be truthy at once in `wait_for_function`."""
    script = (
        "async () => { const r = await navigator.serviceWorker.getRegistration('/entry/');"
        " return !!(r && r.active && r.active.state === 'activated'); }"
    )
    waited = 0
    while waited < timeout_ms:
        if page.evaluate(script):
            return
        page.wait_for_timeout(250)
        waited += 250
    raise AssertionError("service worker never activated")


def _render(page):
    page.evaluate("() => window.AscOffline.render(window.AscOffline._db)")


def _go_offline(page):
    page.context.set_offline(True)
    page.wait_for_function("() => window.AscOffline.health.mode === 'offline'", timeout=60_000)
    _render(page)


def _permitted(page, action):
    return page.locator(f"#offline-actions li[data-action={action}]").get_attribute(
        "data-permitted"
    )


# ---------------------------------------------------------------------------
# Preparation: keys, trust, package, self-test, readiness
# ---------------------------------------------------------------------------


def test_preparation_reaches_offline_ready_with_non_extractable_keys(
    live_server, page, offline_world
):
    from apps.entry.models import EntryDevice, EntryDeviceStatus, OfflinePackage

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    device = database_call(lambda: EntryDevice.objects.get(pk=offline_world["device"].pk))
    assert device.status == EntryDeviceStatus.OFFLINE_READY
    assert (
        database_call(lambda: OfflinePackage.objects.filter(device=device, status="READY").count())
        == 1
    )

    data = page.evaluate(READ_DB)
    keys = {k["name"]: k for k in data["keys"]}
    assert (
        keys["device-sign"]["extractable"] is False and keys["device-sign"]["algorithm"] == "ECDSA"
    )
    assert (
        keys["device-unwrap"]["extractable"] is False
        and keys["device-unwrap"]["algorithm"] == "ECDH"
    )
    assert (
        keys["local-store"]["extractable"] is False
        and keys["local-store"]["algorithm"] == "AES-GCM"
    )
    assert "trust" in data["meta"]
    # Package plaintext is never stored: only the signed, encrypted envelope.
    assert SYNTHETIC_NAME_EN not in data["text"] and SYNTHETIC_NAME_AR not in data["text"]
    assert data["ops"] == []  # Prompt 2 never writes a real operation
    # Nothing in web storage.
    assert page.evaluate("() => localStorage.length") == 0
    # The decrypted package is in memory and carries the approved minimum.
    names = page.evaluate("() => window.AscOffline.state.body.entries.map(e => e.display_name)")
    assert sorted(names) == sorted([SYNTHETIC_NAME_EN, SYNTHETIC_NAME_AR])
    _shot(page, "01-offline-ready-en-desktop.png")


def test_service_worker_scope_and_no_dynamic_caching(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    _wait_for_active_worker(page)
    scope = page.evaluate(
        "() => navigator.serviceWorker.getRegistration('/entry/').then(r => r.scope)"
    )
    assert scope == f"{live_server.url}/entry/"
    # A verification online, a checkpoint page, and API calls: none is cached.
    page.goto(f"{live_server.url}/entry/verify/")
    field = page.locator("input[name=token]")
    field.fill(offline_world["token_en"])
    field.press("Enter")
    expect(page.locator("#entry-result")).to_be_visible()
    cached = page.evaluate(
        """async () => {
          const out = [];
          for (const name of await caches.keys()) {
            const cache = await caches.open(name);
            for (const request of await cache.keys()) out.push(new URL(request.url).pathname);
          }
          return out;
        }"""
    )
    assert "/entry/offline/" in cached
    for path in cached:
        assert path == "/entry/offline/" or path.startswith("/static/"), path
    # Back-office pages are never controlled by the worker.
    page.goto(f"{live_server.url}/ops/entry/events/{offline_world['event'].pk}/devices/")
    assert page.evaluate("() => navigator.serviceWorker.controller") is None


def test_shell_renders_from_the_worker_cache_without_a_network(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    _wait_for_active_worker(page)
    page.goto(page.url)  # a new navigation loads under the activated worker's control
    page.wait_for_function("() => navigator.serviceWorker.controller !== null", timeout=15_000)
    page.context.set_offline(True)
    page.reload()
    expect(page.locator("h1")).to_have_text("Offline readiness")
    page.wait_for_function(
        "() => !!window.AscOffline && !!window.AscOffline.state.activeMeta", timeout=15_000
    )
    # Regression: an offline start-up keeps the package; it never purges it.
    assert page.evaluate("() => window.AscOffline.state.integrityFailure") is False
    page.context.set_offline(False)


# ---------------------------------------------------------------------------
# The seven states and their forbidden actions
# ---------------------------------------------------------------------------


def test_every_state_and_its_permitted_actions_in_the_ui(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _start_checkpoint(page, live_server, offline_world)
    _shell(page, live_server)
    # Regression (found in this prompt): a fresh device has no package, and a
    # missing record must read as missing -- never as a failed integrity check.
    assert page.evaluate("() => window.AscOffline.state.integrityFailure") is False
    assert page.evaluate("() => window.AscOffline.state.lastIntegrityCode === undefined") is True
    # ONLINE: not yet prepared.
    assert _state(page) == "ONLINE"
    assert _permitted(page, "ADMIT_OFFLINE") == "false"
    assert _permitted(page, "PREPARE_DEVICE") == "true"
    page.locator("[data-offline-action=prepare]").click()
    page.wait_for_function(
        "() => !!(window.AscOffline.state.offline && window.AscOffline.state.offline.ready)",
        timeout=30_000,
    )
    page.locator("[data-offline-action=grant]").click()
    page.wait_for_function("() => window.AscOffline.state.grants.length === 1", timeout=15_000)
    _render(page)
    assert _state(page) == "OFFLINE_READY"
    _shot(page, "02-state-offline-ready-en.png")

    # SYNCING: an unacknowledged record while the cloud is healthy (a
    # synthetic record through the Prompt 3 foundation API, test-only).
    page.evaluate(APPEND.format("synthetic-1"))
    page.evaluate(
        "() => window.AscOffline.opstore.counts(window.AscOffline._db)"
        ".then(c => { window.AscOffline.state.counts = c; })"
    )
    _render(page)
    assert _state(page) == "SYNCING"
    assert _permitted(page, "ADMIT_OFFLINE") == "false"
    assert _permitted(page, "VERIFY_ONLINE") == "true"
    _shot(page, "03-state-syncing-en.png")
    page.evaluate("() => { window.AscOffline.state.counts.pending = 0; }")

    # OFFLINE_ACTIVE: repeated failures (never one) with a valid package and grant.
    _go_offline(page)
    assert _state(page) == "OFFLINE_ACTIVE"
    for action in (
        "VERIFY_OFFLINE_QR",
        "ADMIT_OFFLINE",
        "DO_NOT_ADMIT_OFFLINE",
        "MANUAL_REVIEW_OFFLINE",
    ):
        assert _permitted(page, action) == "true", action
    assert _permitted(page, "VERIFY_ONLINE") == "false"
    expect(page.locator("[data-offline-action=prepare]")).to_be_disabled()
    _shot(page, "04-state-offline-active-en.png")

    # STALE: the package clock past stale_at (no override, no normal admission).
    # A grant lasts at most 2 h after its online issue -- about as long as a
    # Standard package stays usable -- so without a later online re-issue the
    # device would be Blocked (no grant) at the same moment. Simulate that
    # re-issue by extending the in-memory grant, to observe Stale itself.
    page.evaluate("() => window.AscOffline.state.grants.forEach(g => { g.expires_at += 86400; })")
    page.evaluate(
        """() => { const s = window.AscOffline.state; const m = s.activeMeta;
          s.serverOffsetSeconds = (m.stale_at - Date.now() / 1000) + 5; }"""
    )
    _render(page)
    assert _state(page) == "STALE"
    assert _permitted(page, "ADMIT_OFFLINE") == "false"
    assert _permitted(page, "OVERRIDE_OFFLINE") == "false"
    assert _permitted(page, "MANUAL_REVIEW_OFFLINE") == "true"
    assert _permitted(page, "DO_NOT_ADMIT_OFFLINE") == "true"
    _shot(page, "05-state-stale-en.png")

    # EXPIRED: past the hard expiry -- never an admission, never "verified".
    page.evaluate(
        """() => { const s = window.AscOffline.state; const m = s.activeMeta;
          s.serverOffsetSeconds = (m.expires_at - Date.now() / 1000) + 5; }"""
    )
    _render(page)
    assert _state(page) == "EXPIRED"
    assert _permitted(page, "ADMIT_OFFLINE") == "false"
    assert _permitted(page, "VERIFY_OFFLINE_QR") == "false"
    _shot(page, "06-state-expired-en.png")

    # BLOCKED: offline with no valid operator grant.
    page.evaluate(
        "() => { window.AscOffline.state.serverOffsetSeconds = 0;"
        " window.AscOffline.state.grants = []; }"
    )
    _render(page)
    assert _state(page) == "BLOCKED"
    expect(page.locator("#offline-state-sub [data-offline-sub=NO_GRANT]")).to_be_visible()
    for action in ("VERIFY_OFFLINE_QR", "ADMIT_OFFLINE", "VERIFY_ONLINE", "REFRESH_PACKAGE"):
        assert _permitted(page, action) == "false", action
    assert _permitted(page, "MANUAL_PROCEDURE") == "true"
    _shot(page, "07-state-blocked-en.png")
    page.context.set_offline(False)


def test_health_thresholds_and_clock_discontinuity(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _shell(page, live_server)
    result = page.evaluate(
        """() => {
          const A = window.AscOffline; const h = A.health;
          Object.assign(h, {mode: 'online', consecutiveFailures: 0, firstFailureAt: null,
                            consecutiveSuccesses: 0, firstSuccessAt: null});
          const t = 1_000_000;
          const steps = [];
          steps.push(A.recordHealth(false, t));          // 1 failure
          steps.push(A.recordHealth(false, t + 5000));   // 2 failures
          steps.push(A.recordHealth(false, t + 10000));  // 3 failures, only 10 s
          steps.push(A.recordHealth(false, t + 20000));  // 4 failures spanning 20 s
          steps.push(A.recordHealth(true, t + 25000));   // 1 success
          steps.push(A.recordHealth(true, t + 30000));   // 2 successes, only 5 s apart
          steps.push(A.recordHealth(true, t + 35000));   // 3 successes spanning 10 s
          return steps;
        }"""
    )
    assert result == ["online", "online", "online", "offline", "offline", "offline", "recovering"]
    # A wall-clock jump over the approved limit is Blocked "until the next
    # successful online contact" (manual test M4). Controlled heartbeat
    # scheduling (UX-C2, item D): with the device offline no heartbeat can
    # succeed, so the page's own loop cannot re-anchor the clock while the
    # Blocked state is being asserted. Before this, a heartbeat that landed in
    # the 5 s wait legitimately cleared the block and the test failed.
    page.context.set_offline(True)
    blocked = page.evaluate(
        """() => {
          const A = window.AscOffline; const h = A.health;
          h.wallAnchor = Date.now() - 6 * 60 * 1000; h.monoAnchor = performance.now();
          h.clockDiscontinuity = false;
          return A.checkClock();
        }"""
    )
    assert blocked is True
    _render(page)
    assert _state(page) == "BLOCKED"
    expect(page.locator("#offline-state-sub [data-offline-sub=CLOCK]")).to_be_visible()
    # Still Blocked across the page's own offline heartbeats.
    page.wait_for_function(
        "() => window.AscOffline.health.consecutiveFailures >= 1", timeout=60_000
    )
    assert page.evaluate("() => window.AscOffline.health.clockDiscontinuity") is True
    # The approved way out: the next successful contact re-anchors the clock.
    page.context.set_offline(False)
    page.wait_for_function(
        "() => window.AscOffline.health.clockDiscontinuity === false", timeout=60_000
    )


# Controlled heartbeat scheduling (UX-C2, item D): heartbeat requests are held
# in the page and released one by one by the test.
_HOLD_HEARTBEATS = """(() => {
  const original = window.fetch.bind(window);
  window.__heldHeartbeats = [];
  window.fetch = (input, init) => {
    const url = String(typeof input === "string" ? input : input.url);
    if (url.includes("heartbeat")) {
      return new Promise((resolve, reject) => {
        window.__heldHeartbeats.push(() => original(input, init).then(resolve, reject));
      });
    }
    return original(input, init);
  };
})();"""


def _release_one_heartbeat(page):
    """Let the oldest held heartbeat reach the server, then wait until the
    page has fully handled its answer: the loop sends the next heartbeat only
    after the previous one is finished."""
    page.evaluate("() => window.__heldHeartbeats.shift()()")
    page.wait_for_function("() => window.__heldHeartbeats.length >= 1", timeout=60_000)


def test_a_heartbeat_sent_before_a_clock_jump_never_clears_the_block(
    live_server, page, offline_world
):
    """Production defect found in UX-C2: the answer to a heartbeat already in
    flight when a discontinuity was detected cleared the block (and could set
    the server offset from readings on both sides of a real jump). Only a
    contact started after the detection may end the block."""
    _sign_in(page, live_server, offline_world, init_script=_HOLD_HEARTBEATS)
    page.goto(f"{live_server.url}/entry/offline/")
    page.wait_for_function("() => window.__heldHeartbeats.length >= 1", timeout=60_000)
    _release_one_heartbeat(page)  # the shell is running; one heartbeat is in flight
    page.evaluate(
        """() => {
          const A = window.AscOffline; const h = A.health;
          h.wallAnchor = Date.now() - 6 * 60 * 1000; h.monoAnchor = performance.now();
          h.clockDiscontinuity = false;
          return A.checkClock();
        }"""
    )
    _release_one_heartbeat(page)  # its answer arrives after the detection
    assert page.evaluate("() => window.AscOffline.health.clockDiscontinuity") is True
    _render(page)
    assert _state(page) == "BLOCKED"
    _release_one_heartbeat(page)  # the next contact, started after the detection
    assert page.evaluate("() => window.AscOffline.health.clockDiscontinuity") is False


def test_pure_state_precedence(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _shell(page, live_server)
    cases = page.evaluate(
        """() => {
          const base = {enabled: true, notEnrolled: false, directiveBlock: false,
            storageError: false, clockDiscontinuity: false, integrityFailure: false,
            connectivity: 'online', unstable: false,
            prepared: true, serverReady: true, band: 'FRESH', grant: 'VALID', unacknowledged: 0,
            rebuildBlocking: false, rebuildRequired: false, wipe: 'NONE'};
          const s = (o) => window.AscOffline.computeState(Object.assign({}, base, o)).state;
          return {
            ready: s({}), online: s({prepared: false}), syncing: s({connectivity: 'recovering'}),
            active: s({connectivity: 'offline'}),
            aging: s({connectivity: 'offline', band: 'AGING'}),
            stale: s({connectivity: 'offline', band: 'STALE'}),
            expired: s({connectivity: 'offline', band: 'EXPIRED'}),
            expiredBeatsGrant: s({connectivity: 'offline', band: 'EXPIRED', grant: 'NONE'}),
            noPackage: s({connectivity: 'offline', band: 'NONE'}),
            locked: s({connectivity: 'offline', grant: 'LOCKED'}),
            directive: s({directiveBlock: true}), storage: s({storageError: true}),
            rebuildBlocking: s({connectivity: 'offline', rebuildBlocking: true}),
            rebuildOther: s({connectivity: 'offline', rebuildRequired: true}),
            disabledOffline: s({enabled: false, connectivity: 'offline'}),
            disabledOnline: s({enabled: false}),
            agingOnline: s({band: 'AGING'}),
            wipePending: s({wipe: 'IN_PROGRESS'}), wipeBlocked: s({wipe: 'BLOCKED'}),
            wipeFailed: s({wipe: 'FAILED'}), wiped: s({wipe: 'WIPED'}),
            wipeBeatsOffline: s({wipe: 'BLOCKED', connectivity: 'offline'}),
          };
        }"""
    )
    assert cases == {
        "ready": "OFFLINE_READY",
        "online": "ONLINE",
        "syncing": "SYNCING",
        "active": "OFFLINE_ACTIVE",
        "aging": "OFFLINE_ACTIVE",
        "stale": "STALE",
        "expired": "EXPIRED",
        "expiredBeatsGrant": "EXPIRED",
        "noPackage": "BLOCKED",
        "locked": "BLOCKED",
        "directive": "BLOCKED",
        "storage": "BLOCKED",
        "rebuildBlocking": "BLOCKED",
        "rebuildOther": "STALE",
        "disabledOffline": "BLOCKED",
        "disabledOnline": "ONLINE",
        "agingOnline": "ONLINE",
        "wipePending": "BLOCKED",
        "wipeBlocked": "BLOCKED",
        "wipeFailed": "BLOCKED",
        "wiped": "BLOCKED",
        "wipeBeatsOffline": "BLOCKED",
    }


def test_offline_inactivity_locks_operator_actions(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    _go_offline(page)
    assert _state(page) == "OFFLINE_ACTIVE"
    page.evaluate("() => { window.AscOffline.health.lastActivity = Date.now() - 11 * 60 * 1000; }")
    _render(page)
    assert _state(page) == "BLOCKED"
    expect(page.locator("#offline-state-sub [data-offline-sub=OPERATOR_LOCKED]")).to_be_visible()
    # Activity does not unlock: there is no local re-authentication.
    page.keyboard.press("Tab")
    _render(page)
    assert _state(page) == "BLOCKED"
    page.context.set_offline(False)


# ---------------------------------------------------------------------------
# Atomic activation, rollback, trust pinning, delta separation
# ---------------------------------------------------------------------------


def test_refresh_to_a_newer_version_keeps_the_previous_and_rolls_back_only_to_valid(
    live_server, page, offline_world, settings
):
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_packages import build_package

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    database_call(
        lambda: build_package(device=EntryDevice.objects.get(pk=offline_world["device"].pk))
    )  # version 2
    page.locator("[data-offline-action=refresh]").click()
    # The shell's own background cycle may activate version 2 before the
    # click does (the click then correctly answers "up to date"): wait for
    # the activation itself, whichever path performed it.
    page.wait_for_function(
        "() => (window.AscOffline.state.activeMeta || {}).package_version === 2",
        timeout=20_000,
    )
    slots = {p["slot"]: p for p in page.evaluate(READ_DB)["packages"]}
    assert slots["active"]["meta"]["package_version"] == 2
    assert slots["previous"]["meta"]["package_version"] == 1
    # Corrupt the active package at rest: start-up re-verification rolls back
    # to the still-valid previous package.
    page.evaluate(
        """async () => {
"""
        + IDB_HELPERS
        + """
          const db = window.AscOffline._db;
          const row = await idbGet(db, 'packages', 'active');
          row.raw = row.raw.replace('"ciphertext":"', '"ciphertext":"AA');
          await idbPut(db, 'packages', row);
        }"""
    )
    page.reload()
    page.wait_for_function(
        "() => window.AscOffline && window.AscOffline.state.activeMeta", timeout=15_000
    )
    assert page.evaluate("() => window.AscOffline.state.activeMeta.package_version") == 1
    # Corrupt both: nothing valid remains, package data is purged, and the
    # device cannot claim offline readiness.
    page.evaluate(
        """async () => {
"""
        + IDB_HELPERS
        + """
          const db = window.AscOffline._db;
          const row = await idbGet(db, 'packages', 'active');
          row.raw = '{"manifest":"x.y.z","ciphertext":"AA"}';
          await idbPut(db, 'packages', row);
        }"""
    )
    page.reload()
    page.wait_for_function(
        "() => window.AscOffline && window.AscOffline.state.integrityFailure === true",
        timeout=15_000,
    )
    assert page.evaluate("() => window.AscOffline.state.activeMeta") is None
    assert _state(page) != "OFFLINE_READY"


def test_a_tampered_package_never_replaces_the_active_one(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    outcome = page.evaluate(
        """async () => {
"""
        + IDB_HELPERS
        + """
          const A = window.AscOffline; const db = A._db;
          const row = await idbGet(db, 'packages', 'active');
          const envelope = JSON.parse(row.raw);
          const c = envelope.ciphertext;
          const flipped = c.slice(0, 10) + (c[10] === 'A' ? 'B' : 'A') + c.slice(11);
          const tampered = JSON.stringify({manifest: envelope.manifest, ciphertext: flipped});
          try { await A.activatePackage(db, tampered, {}); return 'ACCEPTED'; }
          catch (e) { return e.code; }
        }"""
    )
    assert outcome == "INTEGRITY"
    assert page.evaluate("() => window.AscOffline.state.activeMeta.package_version") == 1


def test_a_package_signed_by_an_unpinned_key_is_refused(live_server, page, offline_world, settings):
    from apps.core.crypto.package_signing import (
        InMemoryPackageSigningKeyProvider,
        set_package_signing_key_provider_for_testing,
    )

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    # The server now signs with a key the device never pinned (key id "p1"
    # again, different key): a key carried in a package is never trusted.
    set_package_signing_key_provider_for_testing(
        InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")
    )
    # The download request never builds (correction B): build version 2 --
    # signed by the unpinned key -- the way the worker would, then refresh.
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_packages import build_package

    database_call(
        lambda: build_package(device=EntryDevice.objects.get(pk=offline_world["device"].pk))
    )
    page.locator("[data-offline-action=refresh]").click()
    expect(page.locator("#offline-message")).to_contain_text("failed", timeout=15_000)
    assert page.evaluate("() => window.AscOffline.state.activeMeta.package_version") == 1


def test_critical_delta_advances_only_its_own_cutoff(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    before = page.evaluate("() => window.AscOffline.state.activeMeta.data_cutoff_at")
    page.wait_for_timeout(1100)
    # The shell's own background cycle may already have applied a delta, so
    # compare with whatever version is active just before the explicit refresh.
    prior = page.evaluate(
        "() => (window.AscOffline.state.deltaMeta || {delta_version: 0}).delta_version"
    )
    page.evaluate("() => window.AscOffline.refreshDelta(window.AscOffline._db)")
    state = page.evaluate(
        "() => ({pkg: window.AscOffline.state.activeMeta.data_cutoff_at,"
        " delta: window.AscOffline.state.deltaMeta})"
    )
    assert state["pkg"] == before
    assert state["delta"]["delta_version"] > prior
    assert state["delta"]["critical_delta_cutoff_at"] >= before
    # A replayed (not newer) delta is refused.
    replay = page.evaluate(
        """async () => {
"""
        + IDB_HELPERS
        + """
          const A = window.AscOffline; const db = A._db;
          const row = await idbGet(db, 'packages', 'delta');
          try { await A.applyDelta(db, row.raw); return 'APPLIED'; } catch (e) { return e.code; }
        }"""
    )
    assert replay == "NOT_NEWER"


# ---------------------------------------------------------------------------
# Evidence preservation, blocking, emergency wipe
# ---------------------------------------------------------------------------


def test_ordinary_blocking_locks_but_never_deletes_unacknowledged_records(
    live_server, page, offline_world
):
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_devices import block_offline_use

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    for number in (1, 2):
        page.evaluate(
            f"() => window.AscOffline.opstore.append(window.AscOffline._db, "
            f"{{operation_id: 'synthetic-{number}', type: 'TEST'}})"
        )
    device = database_call(lambda: EntryDevice.objects.get(pk=offline_world["device"].pk))
    database_call(
        lambda: block_offline_use(
            device=device,
            actor=offline_world["operator"],
            expected_version=device.version,
            reason_code="SECURITY_CONCERN",
        )
    )
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    data = page.evaluate(READ_DB)
    assert data["packages"] == []  # package data purged
    assert [op["state"] for op in data["ops"]] == ["LOCKED", "LOCKED"]  # evidence kept, locked
    assert all(op["sealed"]["ct"] for op in data["ops"])  # still encrypted
    # A locked store refuses new records.
    refused = page.evaluate(
        """() => window.AscOffline.opstore.append(window.AscOffline._db, {operation_id: 'x'})
                 .then(() => 'ACCEPTED', e => e.code)"""
    )
    assert refused == "STORE_LOCKED"
    # Ordinary cleanup deletes only durably acknowledged records.
    assert (
        page.evaluate("() => window.AscOffline.opstore.purgeAcknowledged(window.AscOffline._db)")
        == 0
    )
    page.evaluate(
        """async () => {
"""
        + IDB_HELPERS
        + """
          const db = window.AscOffline._db;
          const row = await idbGet(db, 'ops', 1);
          row.state = 'ACKNOWLEDGED';
          await idbPut(db, 'ops', row);
        }"""
    )
    assert (
        page.evaluate("() => window.AscOffline.opstore.purgeAcknowledged(window.AscOffline._db)")
        == 0  # A mutable state label is not a signed durable acknowledgement.
    )
    assert [op["seq"] for op in page.evaluate(READ_DB)["ops"]] == [1, 2]


def test_service_worker_update_and_package_replacement_keep_records(
    live_server, page, offline_world
):
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_packages import build_package

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    page.evaluate(APPEND.format("keep-me"))
    database_call(
        lambda: build_package(device=EntryDevice.objects.get(pk=offline_world["device"].pk))
    )
    page.locator("[data-offline-action=refresh]").click()
    expect(page.locator("#offline-message")).to_contain_text("refreshed", timeout=15_000)
    page.evaluate(
        "() => navigator.serviceWorker.getRegistration('/entry/').then(r => r && r.update())"
    )
    page.reload()
    page.wait_for_function(
        "() => window.AscOffline && window.AscOffline.state.activeMeta", timeout=15_000
    )
    assert [op["state"] for op in page.evaluate(READ_DB)["ops"]] == ["PENDING"]


def test_emergency_wipe_reports_evidence_then_destroys_local_data(
    live_server, page, offline_world, settings
):
    from apps.accounts.tests.mfa_double import ACCEPTED_TEST_RESPONSE
    from apps.entry.models import DeviceEvidenceReport, EntryDevice
    from apps.entry.services.offline_devices import order_emergency_wipe
    from apps.entry.tests import factories

    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    page.evaluate(APPEND.format("at-risk"))
    settings.MFA_BACKEND = ACCEPTING_MFA
    manager = database_call(
        lambda: factories.make_user(
            "browser.wipe@example.test",
            group_name="Security Restriction Managers",
            event=offline_world["event"],
        )
    )
    database_call(
        lambda: order_emergency_wipe(
            device=EntryDevice.objects.get(pk=offline_world["device"].pk),
            actor=manager,
            reason_code="DEVICE_STOLEN",
            note="",
            confirmed=True,
            mfa_response=ACCEPTED_TEST_RESPONSE,
        )
    )
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    page.wait_for_function("() => window.AscOffline.state.wiped === true", timeout=15_000)
    evidence = database_call(lambda: DeviceEvidenceReport.objects.get())
    assert evidence.pending_operations == 1 and evidence.sequence_high == 1
    assert len(evidence.chain_head) == 64
    names = page.evaluate("() => indexedDB.databases().then(list => list.map(d => d.name))")
    assert "asc-entry-offline" not in names
    assert (
        page.evaluate(
            "() => caches.keys().then(n => n.filter(x => x.startsWith('asc-entry-shell-')).length)"
        )
        == 0
    )
    assert (
        page.evaluate("() => navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
    )


# ---------------------------------------------------------------------------
# Emergency wipe correctness (independent re-review correction A)
# ---------------------------------------------------------------------------

DATABASES = "() => indexedDB.databases().then(list => list.map(d => d.name))"
REGISTRATION_SCOPES = (
    "() => navigator.serviceWorker.getRegistrations().then(r => r.map(x => x.scope).sort())"
)


@database_sync
def _order_wipe(world, settings, email):
    from apps.accounts.tests.mfa_double import ACCEPTED_TEST_RESPONSE
    from apps.entry.models import EntryDevice
    from apps.entry.services.offline_devices import order_emergency_wipe
    from apps.entry.tests import factories

    settings.MFA_BACKEND = ACCEPTING_MFA
    manager = factories.make_user(
        email, group_name="Security Restriction Managers", event=world["event"]
    )
    order_emergency_wipe(
        device=EntryDevice.objects.get(pk=world["device"].pk),
        actor=manager,
        reason_code="DEVICE_STOLEN",
        note="",
        confirmed=True,
        mfa_response=ACCEPTED_TEST_RESPONSE,
    )


def test_a_blocked_wipe_never_claims_success_and_completes_once_the_blocker_closes(
    live_server, page, offline_world, settings
):
    """Another open connection (here: a same-origin tab that ignores
    `versionchange`) blocks `deleteDatabase()`. The shell shows a safe
    Blocked state and never reports WIPED; once the connection closes the
    queued deletion completes."""
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    other = page.context.new_page()
    other.goto(f"{live_server.url}/healthz")
    assert other.evaluate(
        """() => new Promise((resolve, reject) => {
          const request = indexedDB.open('asc-entry-offline');
          request.onsuccess = () => { window.__held = request.result; resolve(true); };
          request.onerror = () => reject(request.error);
        })"""
    )
    _order_wipe(offline_world, settings, "browser.wipe.blocked@example.test")
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    page.wait_for_function(
        "() => window.AscOffline.state.wipe.status === 'BLOCKED'", timeout=15_000
    )
    page.wait_for_timeout(2_000)  # still queued: it must not have claimed success
    assert page.evaluate("() => window.AscOffline.state.wiped") is False
    assert page.evaluate("() => window.AscOffline.state.wipe.status") == "BLOCKED"
    assert "asc-entry-offline" in other.evaluate(DATABASES)
    _render(page)
    assert _state(page) == "BLOCKED"
    expect(page.locator("#offline-state-sub [data-offline-sub=WIPE_BLOCKED]")).to_be_visible()
    expect(page.locator("#offline-message")).to_have_class(re.compile("asc-notice-danger"))
    _shot(page, "12-wipe-blocked-en.png")

    other.evaluate("() => window.__held.close()")
    page.wait_for_function("() => window.AscOffline.state.wiped === true", timeout=15_000)
    assert page.evaluate("() => window.AscOffline.state.wipe.status") == "WIPED"
    assert "asc-entry-offline" not in page.evaluate(DATABASES)
    _render(page)
    expect(page.locator("#offline-state-sub [data-offline-sub=WIPED]")).to_be_visible()
    other.close()


def test_a_second_entry_tab_steps_aside_so_the_wipe_completes(
    live_server, page, offline_world, settings
):
    """Two shell tabs of the same device. Whichever tab carries out the signed
    order, the other closes its connection (BroadcastChannel and
    `versionchange`), so the deletion is not left blocked: the database is
    deleted, a tab reports WIPED only after that, and both tabs are Blocked."""
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    second = page.context.new_page()
    _shell(second, live_server)
    second.wait_for_function("() => !!window.AscOffline._db", timeout=15_000)
    _order_wipe(offline_world, settings, "browser.wipe.tabs@example.test")
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    waited = 0
    while "asc-entry-offline" in page.evaluate(DATABASES):
        assert waited < 20_000, "the local database was never deleted"
        page.wait_for_timeout(250)
        waited += 250
    stopped = (
        "() => window.AscOffline.state.wiped === true"
        " || window.AscOffline.state.storageError === true"
        " || window.AscOffline.state.wipe.status !== 'NONE'"
    )
    for tab in (page, second):
        tab.wait_for_function(stopped, timeout=15_000)
        tab.evaluate("() => window.AscOffline.render(null)")
        assert tab.locator("#offline-state-chip").get_attribute("data-offline-state") == "BLOCKED"
    wiped = [tab.evaluate("() => window.AscOffline.state.wiped") for tab in (page, second)]
    assert any(wiped)  # the tab that carried out the order confirmed the deletion
    second.close()


def test_the_wipe_removes_only_the_asc_entry_worker(live_server, page, offline_world, settings):
    settings.ROOT_URLCONF = "tests.browser.offline_urls"
    _sign_in(page, live_server, offline_world)
    _prepare(page, live_server, offline_world)
    _wait_for_active_worker(page)
    page.evaluate(
        "() => navigator.serviceWorker.register('/__offline_test__/unrelated-sw.js',"
        " {scope: '/__offline_test__/'}).then(() => true)"
    )
    unrelated = f"{live_server.url}/__offline_test__/"
    own = f"{live_server.url}/entry/"
    waited = 0
    while sorted(page.evaluate(REGISTRATION_SCOPES)) != sorted([unrelated, own]):
        assert waited < 15_000, page.evaluate(REGISTRATION_SCOPES)
        page.wait_for_timeout(250)
        waited += 250
    _order_wipe(offline_world, settings, "browser.wipe.workers@example.test")
    page.evaluate("() => window.AscOffline.heartbeat(window.AscOffline._db)")
    page.wait_for_function("() => window.AscOffline.state.wiped === true", timeout=15_000)
    assert page.evaluate(REGISTRATION_SCOPES) == [unrelated]


# ---------------------------------------------------------------------------
# Storage failure and quota
# ---------------------------------------------------------------------------


def test_storage_unavailable_is_blocked(live_server, page, offline_world):
    _sign_in(
        page,
        live_server,
        offline_world,
        init_script=(
            "IDBFactory.prototype.open = function () {"
            " throw new DOMException('blocked', 'SecurityError'); };"
        ),
    )
    _shell(page, live_server)
    page.wait_for_function("() => window.AscOffline.state.storageError === true", timeout=15_000)
    _render(page)
    expect(page.locator("#offline-state-chip")).to_have_attribute("data-offline-state", "BLOCKED")
    expect(page.locator("#offline-state-sub [data-offline-sub=STORAGE]")).to_be_visible()


def test_insufficient_quota_fails_the_self_test(live_server, page, offline_world):
    from apps.audit import action_codes
    from apps.audit.models import AuditEvent
    from apps.entry.models import EntryDevice, EntryDeviceStatus

    _sign_in(
        page,
        live_server,
        offline_world,
        init_script=(
            "if (navigator.storage) { navigator.storage.estimate = () => "
            "Promise.resolve({quota: 1000, usage: 900}); }"
        ),
    )
    _start_checkpoint(page, live_server, offline_world)
    _shell(page, live_server)
    page.locator("[data-offline-action=prepare]").click()
    expect(page.locator("#offline-message")).to_contain_text("Self-test failed", timeout=30_000)
    assert (
        database_call(lambda: EntryDevice.objects.get(pk=offline_world["device"].pk)).status
        == EntryDeviceStatus.ENROLLED
    )
    audit = database_call(
        lambda: AuditEvent.objects.get(action_code=action_codes.DEVICE_OFFLINE_SELF_TEST_FAILED)
    )
    assert audit.after_summary["failed"] == ["storage_quota"]


# ---------------------------------------------------------------------------
# Localization, RTL and accessibility of the shell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "viewport", "ready_text", "name"),
    [
        ("fr", DESKTOP, "Prêt hors ligne", "08-offline-ready-fr-desktop.png"),
        ("ar", DESKTOP, "جاهز دون اتصال", "09-offline-ready-ar-desktop.png"),
        ("ar", MOBILE, "جاهز دون اتصال", "10-offline-ready-ar-mobile.png"),
        ("en", MOBILE, "Offline ready", "11-offline-ready-en-mobile.png"),
    ],
)
def test_shell_localization_rtl_and_layout(
    live_server, page, offline_world, language, viewport, ready_text, name
):
    _sign_in(page, live_server, offline_world, language=language, viewport=viewport)
    _prepare(page, live_server, offline_world)
    html = page.locator("html")
    expect(html).to_have_attribute("lang", language)
    expect(html).to_have_attribute("dir", "rtl" if language == "ar" else "ltr")
    expect(page.locator("#offline-state-chip [data-offline-state-text]")).to_have_text(ready_text)
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0
    # Every action verdict is text, never colour alone.
    for verdict in page.locator("#offline-actions li span.asc-chip").all_inner_texts():
        assert verdict.strip()
    _shot(page, name)


def test_shell_accessibility_basics(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _start_checkpoint(page, live_server, offline_world)
    _shell(page, live_server)
    expect(page.locator("h1")).to_have_count(1)
    expect(page.locator("#offline-announcer")).to_have_attribute("aria-live", "polite")
    expect(page.locator("a.skip-link")).to_have_attribute("href", "#main-content")
    # Keyboard: the primary command is reachable and operable by keyboard.
    page.locator("a.skip-link").focus()
    reached = False
    for _ in range(12):
        page.keyboard.press("Tab")
        if (
            page.evaluate("() => document.activeElement.getAttribute('data-offline-action')")
            == "prepare"
        ):
            reached = True
            break
    assert reached
    page.keyboard.press("Enter")
    expect(page.locator("#offline-message")).to_contain_text("Self-test passed", timeout=30_000)
    # A state change is announced (politely, once).
    expect(page.locator("#offline-announcer")).not_to_be_empty()
    for button in page.locator("[data-offline-action]").all():
        assert button.inner_text().strip()


# ---------------------------------------------------------------------------
# Checkpoint connection indicator: repeated checks, never one failure (OFF-001)
# ---------------------------------------------------------------------------


def test_one_failed_status_check_does_not_mean_offline(live_server, page, offline_world):
    _sign_in(page, live_server, offline_world)
    _start_checkpoint(page, live_server, offline_world)
    page.route("**/entry/status/", lambda route: route.abort())
    indicator = page.locator("#entry-connection")
    expect(indicator).to_have_attribute("data-state", "checking", timeout=15_000)
    expect(page.locator("[data-entry-submit]")).to_be_enabled()  # not blocked on one failure
    expect(indicator).to_have_attribute("data-state", "offline", timeout=60_000)
    expect(page.locator("[data-entry-submit]")).to_be_disabled()
    page.unroute("**/entry/status/")
    expect(indicator).to_have_attribute("data-state", "online", timeout=30_000)


def test_disabled_offline_leaves_no_worker(live_server, page, offline_world, settings):
    settings.ENTRY_OFFLINE_ENABLED = False
    _sign_in(page, live_server, offline_world)
    page.goto(f"{live_server.url}/entry/offline/")
    expect(page.locator("#offline-disabled")).to_be_visible()
    page.wait_for_timeout(500)
    assert (
        page.evaluate("() => navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0
    )
