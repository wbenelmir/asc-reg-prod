"""Synthetic real-browser fixture; no database and no external service.

Package encryption/signing uses production adapters. Device keys, IndexedDB,
QR verification and queue operations use the unmodified application runtime.
The HTTP acknowledgement endpoint is a test double, not a server integration.
"""

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key

from apps.core.crypto.package_signing import InMemoryPackageSigningKeyProvider
from apps.core.crypto.signing import InMemorySigningKeyProvider
from apps.entry.offline_contract import client_config, validate_package_body
from apps.entry.services.offline_crypto import (
    b64url_decode,
    b64url_encode,
    canonical_json,
    encrypt_for_device,
    jws_sign,
    public_key_der,
)
from apps.entry.tests.test_offline_contract import _body

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://offline.example.test"

SETUP_KEYS = """async (anchor) => {
  const a = AscOffline;
  const db = await a.openDb(); a._db = db;
  window.testPut = (store, value) => new Promise((resolve, reject) => {
    const t = db.transaction([store], 'readwrite', {durability:'strict'});
    t.objectStore(store).put(value); t.oncomplete = resolve; t.onabort = reject;
  });
  window.testRows = (store) => new Promise((resolve, reject) => {
    const r=db.transaction([store]).objectStore(store).getAll();
    r.onsuccess=()=>resolve(r.result); r.onerror=reject;
  });
  const sign=await crypto.subtle.generateKey(
    {name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);
  const unwrap=await crypto.subtle.generateKey(
    {name:'ECDH',namedCurve:'P-256'},false,['deriveBits']);
  const signSpki=a.b64u(await crypto.subtle.exportKey('spki',sign.publicKey));
  const unwrapSpki=a.b64u(await crypto.subtle.exportKey('spki',unwrap.publicKey));
  await testPut('keys',{name:'device-sign',privateKey:sign.privateKey,spki:signSpki});
  await testPut('keys',{name:'device-unwrap',privateKey:unwrap.privateKey,spki:unwrapSpki});
  await testPut('meta',{name:'trust',anchors:[anchor]});
  return unwrapSpki;
}"""


class OfflineBrowserWorld:
    def __init__(self, page, *, count=2):
        self.page = page
        self.signer = InMemoryPackageSigningKeyProvider(key_ids=("p1",), current="p1")
        self.qr_signer = InMemorySigningKeyProvider(key_ids=("v1",), current="v1")
        self.now = int(time.time())
        config = client_config()
        config.update(
            enabled=True,
            language="en",
            csrf_cookie="csrftoken",
            api={
                "sync": "/sync",
                "heartbeat": "/heartbeat",
                "sync-quarantine": "/quarantine",
            },
        )
        html = '<!doctype html><html lang="en"><body><script type="application/json" '
        html += 'id="asc-offline-config">' + json.dumps(config) + "</script></body></html>"
        page.route(ORIGIN + "/", lambda route: route.fulfill(content_type="text/html", body=html))
        page.goto(ORIGIN + "/")
        page.add_script_tag(path=str(ROOT / "static/js/entry-offline.js"))
        anchor = {
            "kid": "p1",
            "spki": b64url_encode(
                public_key_der(load_pem_public_key(self.signer.public_key_pem("p1")))
            ),
        }
        self.unwrap_spki = page.evaluate(SETUP_KEYS, anchor)
        body = _body()
        body.update(
            event="SYNTHETIC",
            issued_at=self.now,
            data_cutoff_at=self.now,
            aging_at=self.now + 900,
            stale_at=self.now + 7200,
            expires_at=self.now + 14400,
            revoked_passes=[],
            override_reasons=[],
            qr_keys=[
                {
                    "kid": "v1",
                    "status": "ACTIVE",
                    "not_before": None,
                    "not_after": None,
                    "spki": b64url_encode(
                        public_key_der(load_pem_public_key(self.qr_signer.public_key_pem("v1")))
                    ),
                }
            ],
        )
        template = body["entries"][0]
        template.update(
            valid_from=self.now - 60,
            valid_until=self.now + 3600,
            restrictions=[],
            last_admitted_at=None,
            reentry="SINGLE_ENTRY",
        )
        body["entries"] = []
        for i in range(count):
            entry = deepcopy(template)
            entry.update(
                jti=b64url_encode((i + 1).to_bytes(16)),
                pid=b64url_encode((i + 100_001).to_bytes(16)),
                bai=b64url_encode((i + 200_001).to_bytes(16)),
                display_name=f"Synthetic Participant {i + 1}",
            )
            body["entries"].append(entry)
        validate_package_body(body)
        self.body = body
        self.raw = self.envelope(body)
        self.package_bytes = len(self.raw.encode())
        entry = body["entries"][0]
        claims = {key: entry[key] for key in ("apc", "bai", "btc", "cv", "jti", "pid")}
        claims.update(
            eid=body["event"], n="N" * 16, nbf=entry["valid_from"], exp=entry["valid_until"], v=1
        )
        self.qr, _kid = jws_sign(claims, typ="ASC-PASS", provider=self.qr_signer)
        assert (
            page.evaluate("raw => AscOffline.activatePackage(AscOffline._db, raw, {})", self.raw)
            == "ACTIVATED"
        )
        page.evaluate(
            """({body, now}) => {
          const a=AscOffline;
          a.state.device={public_id:body.device};
          a.state.offline={available:true,prepared:true,ready:true};
          a.state.grants=[{schema:2,grant_id:'G'.repeat(22),event:body.event,gate:body.gate,
            zone:'MAIN',scope_version:body.scope_version,issued_at:now,expires_at:now+3600,
            permissions:['verify','admit'],locked:false}];
          a.health.mode='offline'; a.health.lastActivity=Date.now();
        }""",
            {
                "body": {k: body[k] for k in ("device", "event", "gate", "scope_version")},
                "now": self.now,
            },
        )
        self.uploads = []

    def envelope(self, body, *, manifest_changes=None):
        der = b64url_decode(self.unwrap_spki)
        ciphertext, enc, wrap = encrypt_for_device(
            canonical_json(body),
            recipient=load_der_public_key(der),
            recipient_fingerprint=hashlib.sha256(der).hexdigest(),
            device_public_id=body["device"],
            object_id=body["package_id"],
            kind="OPKG",
        )
        names = (
            "schema_version",
            "package_id",
            "package_version",
            "device",
            "event",
            "scope_version",
            "sensitivity",
            "issued_at",
            "data_cutoff_at",
            "aging_at",
            "stale_at",
            "expires_at",
        )
        manifest = {key: body[key] for key in names}
        manifest.update(
            typ="ASC-OPKG",
            entry_count=len(body["entries"]),
            ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
            ciphertext_length=len(ciphertext),
            enc=enc,
            wrap=wrap,
        )
        manifest.update(manifest_changes or {})
        compact, _kid = jws_sign(manifest, typ="ASC-OPKG", provider=self.signer)
        return json.dumps({"manifest": compact, "ciphertext": b64url_encode(ciphertext)})

    def mock_uploads(self, *, valid=True, lose_first=False, wrong_binding=False):
        def heartbeat(route):
            route.fulfill(json={"nonce": "synthetic-nonce"})

        def sync(route):
            batch = route.request.post_data_json
            self.uploads.append([row["sequence"] for row in batch["operations"]])
            if lose_first and len(self.uploads) == 1:
                route.abort()
                return
            acks = []
            for row in batch["operations"]:
                payload = {
                    "typ": "ASC-OACK",
                    "schema_version": 1,
                    "device": self.body["device"],
                    "store": batch["store"],
                    "operation_id": row["operation_id"],
                    "sequence": row["sequence"],
                    "payload_hash": hashlib.sha256(row["op"].encode()).hexdigest(),
                    "status": "APPLIED",
                    "outcome": "ADMISSION_APPLIED",
                    "conflict_type": "",
                    "processed_at": self.now,
                }
                if wrong_binding:
                    payload["store"] = "X" * 22
                ack, _kid = jws_sign(payload, typ="ASC-OACK", provider=self.signer)
                acks.append(
                    {
                        "operation_id": row["operation_id"],
                        "sequence": row["sequence"],
                        "durable": True,
                        "ack": ack if valid else "invalid",
                    }
                )
            route.fulfill(json={"acknowledgements": acks})

        self.page.route(ORIGIN + "/heartbeat", heartbeat)
        self.page.route(ORIGIN + "/sync", sync)
        self.page.evaluate(
            "() => {AscOffline.health.mode='recovering';AscOffline.health.unstable=false;}"
        )

    def record(self):
        self.page.evaluate("qr => AscOffline.verify(qr)", self.qr)
        self.page.evaluate("() => AscOffline.decide('ADMIT')")
        return self.page.evaluate("() => AscOffline.opstore.counts(AscOffline._db)")
