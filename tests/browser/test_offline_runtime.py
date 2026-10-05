"""Real Chromium + WebCrypto + IndexedDB tests, without a database.

Network acknowledgement replies are explicitly synthetic. Server transaction
and idempotency guarantees require the separate PostgreSQL suites.
"""

from copy import deepcopy
from uuid import uuid4

import pytest

from tests.offline_browser_support import OfflineBrowserWorld


def test_offline_success_is_durable_and_cannot_be_double_clicked(page):
    world = OfflineBrowserWorld(page)
    assert world.record()["pending"] == 1
    page.evaluate("() => Promise.all([AscOffline.decide('ADMIT'),AscOffline.decide('ADMIT')])")
    assert page.evaluate("() => AscOffline.opstore.counts(AscOffline._db)")["pending"] == 1
    assert page.evaluate("() => AscOffline.state.result") is None
    assert page.evaluate("() => testRows('ops').then(rows=>rows.every(r=>r.sealed.ct && !r.op))")
    assert page.evaluate(
        "() => testRows('keys').then(rows=>rows.every(r=>!(r.privateKey||r.key).extractable))"
    )
    assert page.evaluate("() => AscOffline.opstore.purgeAcknowledged(AscOffline._db,0)") == 0


@pytest.mark.parametrize("field,value", [("event", "OTHER"), ("issued_at", 1), ("entry_count", 99)])
def test_signed_manifest_body_mismatch_cannot_activate(page, field, value):
    world = OfflineBrowserWorld(page)
    body = deepcopy(world.body)
    body.update(package_version=2, package_id="Z" * 22)
    raw = world.envelope(body, manifest_changes={field: value})
    code = page.evaluate(
        "raw => AscOffline.activatePackage(AscOffline._db,raw,{}).then(()=>'',e=>e.code)", raw
    )
    assert code == "INTEGRITY"
    assert page.evaluate("() => AscOffline.state.activeMeta.package_version") == 1


def test_signed_package_with_undeclared_nested_data_is_rejected(page):
    world = OfflineBrowserWorld(page)
    body = deepcopy(world.body)
    body.update(package_version=2, package_id="Z" * 22)
    body["entries"][0]["badge_label"]["extra"] = "synthetic-prohibited-field"
    raw = world.envelope(body)
    assert (
        page.evaluate(
            "raw => AscOffline.activatePackage(AscOffline._db,raw,{}).then(()=>'',e=>e.code)", raw
        )
        == "ALLOW_LIST"
    )


@pytest.mark.parametrize("valid,wrong_binding", [(False, False), (True, True)])
def test_unverifiable_or_misbound_ack_retains_pending_evidence(page, valid, wrong_binding):
    world = OfflineBrowserWorld(page)
    world.record()
    world.mock_uploads(valid=valid, wrong_binding=wrong_binding)
    page.evaluate("() => AscOffline.syncNow({force:true})")
    counts = page.evaluate("() => AscOffline.opstore.counts(AscOffline._db)")
    assert counts["pending"] == 1 and counts["acknowledged"] == 0
    assert page.evaluate("() => AscOffline.opstore.purgeAcknowledged(AscOffline._db,0)") == 0


def test_transport_failure_retries_and_verified_ack_retains_anti_passback(page):
    world = OfflineBrowserWorld(page)
    world.record()
    world.mock_uploads(lose_first=True)
    page.evaluate("() => AscOffline.syncNow({force:true}).catch(()=>null)")
    assert page.evaluate("() => AscOffline.state.counts.pending") == 1
    page.evaluate("() => AscOffline.syncNow({force:true})")
    assert world.uploads == [[1], [1]]
    assert page.evaluate("() => AscOffline.state.counts.acknowledged") == 1
    assert page.evaluate("() => AscOffline.opstore.purgeAcknowledged(AscOffline._db,0)") == 0
    page.evaluate(
        "() => {AscOffline.state.body.entries[0].last_admitted_at=Math.floor(Date.now()/1000)+1;}"
    )
    assert page.evaluate("() => AscOffline.opstore.purgeAcknowledged(AscOffline._db,0)") == 1


def test_reconnect_storm_has_one_upload_loop(page):
    world = OfflineBrowserWorld(page)
    world.record()
    world.mock_uploads()
    outcomes = page.evaluate(
        "() => Promise.all(Array.from({length:8},()=>AscOffline.syncNow({force:true})))"
    )
    assert "BUSY" in outcomes
    assert world.uploads == [[1]]
    assert page.evaluate("() => AscOffline.state.counts.acknowledged") == 1


def test_changed_result_cannot_be_admitted(page):
    world = OfflineBrowserWorld(page)
    page.evaluate("qr => AscOffline.verify(qr)", world.qr)
    page.evaluate("() => {AscOffline.state.body.entries[0].valid_until=1;}")
    page.evaluate("() => AscOffline.decide('ADMIT')")
    assert page.evaluate("() => AscOffline.opstore.counts(AscOffline._db)")["pending"] == 0


def test_failed_transaction_never_reports_a_recorded_admission(page):
    world = OfflineBrowserWorld(page)
    page.evaluate("""() => {
      const original=IDBObjectStore.prototype.add;
      IDBObjectStore.prototype.add=function(...args) {
        if(this.name==='ops') {
          throw new DOMException('Synthetic quota failure','QuotaExceededError');
        }
        return original.apply(this,args);
      };
    }""")
    assert world.record()["pending"] == 0
    assert page.evaluate("() => !!AscOffline.state.result.recorded") is False


def test_reload_preserves_encrypted_pending_operation(page):
    from tests.offline_browser_support import ROOT

    world = OfflineBrowserWorld(page)
    world.record()
    page.reload()
    page.add_script_tag(path=str(ROOT / "static/js/entry-offline.js"))
    page.evaluate("async () => {AscOffline._db=await AscOffline.openDb();}")
    assert page.evaluate("() => AscOffline.opstore.counts(AscOffline._db)")["pending"] == 1
    assert page.evaluate("() => AscOffline.loadActive(AscOffline._db,{})") == "ACTIVE"


def test_upload_batches_remain_in_device_order(page):
    from apps.entry.tests.test_offline_operation_contract import operation

    world = OfflineBrowserWorld(page)
    drafts = []
    for _ in range(25):
        draft = operation()
        draft["operation_id"] = str(uuid4())
        drafts.append(draft)
    page.evaluate(
        """async drafts => {
      for(const draft of drafts) await AscOffline.opstore.append(AscOffline._db,draft);
    }""",
        drafts,
    )
    world.mock_uploads()
    page.evaluate("() => AscOffline.syncNow({force:true})")
    assert world.uploads == [list(range(1, 21)), list(range(21, 26))]
    assert page.evaluate("() => AscOffline.state.counts.acknowledged") == 25
