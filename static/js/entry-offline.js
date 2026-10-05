/*
 * ASC 2026 enrolled-device offline runtime (Phase 4 Prompt 2, ADR-0023,
 * docs/security/offline_package_contract.md; Phase 4 Prompt 3, ADR-0024,
 * docs/security/offline_sync_contract.md).
 * Project-owned. No dependency: WebCrypto, IndexedDB and the Service Worker
 * API only. No custom cryptographic primitive.
 *
 * Phase 4 Prompt 3 adds, on top of the preparation runtime below:
 *   8. offline verification of SIGNED QR passes only, against the active,
 *      verified, device- and scope-bound package and a checkpoint-bound
 *      operator grant (the QR contract rules, then the admission evaluator
 *      of apps.entry.services.access mirrored on package data);
 *   9. the durable operation queue: every decision and every unresolved
 *      scan is a canonical, signed, hash-chained operation, written to
 *      IndexedDB (committed) BEFORE the operator sees "recorded";
 *  10. ordered, bounded, idempotent synchronization once the application
 *      health check is stable: one upload at a time across every tab (Web
 *      Locks), records marked in flight, and a record acknowledged ONLY on
 *      a signed durable acknowledgement verified against the pinned keys --
 *      never merely because a response arrived;
 *  11. the offline verification, synchronization and incident views.
 * navigator.onLine is never taken as proof that the server is reachable.
 *
 * What this script does:
 *   1. generates the device's two NON-EXTRACTABLE P-256 key pairs (ECDSA for
 *      signing requests and future operations, ECDH for unwrapping packages)
 *      and a non-extractable AES-GCM key for the local store;
 *   2. pins the package-verification keys returned by authorized
 *      provisioning -- the ONLY trust roots; a key carried inside a package
 *      is never trusted as a signer of packages;
 *   3. downloads, verifies (signature, device binding, schema, scope,
 *      monotonic version, expiry, ciphertext hash), decrypts and validates
 *      Offline Packages, and activates them ATOMICALLY: a failed refresh
 *      keeps the last valid package; rollback only to a still-valid one;
 *   4. tracks package freshness (package_data_cutoff_at) and critical-delta
 *      freshness (critical_delta_cutoff_at) SEPARATELY -- a delta never
 *      moves the package cutoff;
 *   5. computes the seven required operational states and the permitted
 *      actions of each, from the server-provided matrix;
 *   6. carries out a SIGNED emergency wipe only when the database deletion
 *      really succeeds (never on a blocked or failed request), coordinating
 *      with other tabs of this app, and removes only its own /entry/
 *      service-worker registration -- never another worker on the origin;
 *   7. keeps the encrypted append-only local operation store (store id,
 *      sequence counter, chain head). Package cleanup and operation
 *      cleanup are separate; no ordinary path deletes an unacknowledged
 *      operation (only a durably acknowledged one, after its retention).
 *
 * What it never does: store package plaintext at rest, use localStorage or
 * sessionStorage for entry data, log a key, a nonce or a package value,
 * rely on Background Sync or Push, or decode a camera image.
 */
(function () {
  "use strict";

  var doc = document;
  var configEl = doc.getElementById("asc-offline-config");
  var stringsEl = doc.getElementById("asc-offline-strings");
  var CONFIG = configEl ? JSON.parse(configEl.textContent) : null;
  var STR = stringsEl ? JSON.parse(stringsEl.textContent) : {};

  var DB_NAME = "asc-entry-offline";
  var DB_VERSION = 1;
  var CACHE_PREFIX = "asc-entry-shell-";
  var PROOF_PREFIX = "ASC-DEVICE-PROOF-v1";
  var ZERO_HASH = "0000000000000000000000000000000000000000000000000000000000000000";

  function t(key, params) {
    var text = STR[key] || "";
    if (params) {
      Object.keys(params).forEach(function (name) {
        text = text.split("%(" + name + ")s").join(String(params[name]));
      });
    }
    return text;
  }

  function codedError(code) {
    var error = new Error(code);
    error.code = code;
    return error;
  }

  // -------------------------------------------------------------------
  // Encoding helpers
  // -------------------------------------------------------------------

  function utf8(text) {
    return new TextEncoder().encode(text);
  }

  function fromUtf8(bytes) {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  }

  function b64u(bytes) {
    var view = new Uint8Array(bytes);
    var binary = "";
    for (var i = 0; i < view.length; i += 1) binary += String.fromCharCode(view[i]);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function unb64u(text) {
    if (typeof text !== "string" || !/^[A-Za-z0-9_-]*$/.test(text)) throw codedError("ENCODING");
    var padded = text.replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4) padded += "=";
    var binary;
    try {
      binary = atob(padded);
    } catch (e) {
      throw codedError("ENCODING");
    }
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    if (b64u(bytes) !== text) throw codedError("ENCODING"); // canonical only
    return bytes;
  }

  function toHex(buffer) {
    var view = new Uint8Array(buffer);
    var out = "";
    for (var i = 0; i < view.length; i += 1) out += ("0" + view[i].toString(16)).slice(-2);
    return out;
  }

  /** Canonical JSON: sorted keys, no whitespace, integers only. */
  function canonical(value) {
    if (value === null || typeof value === "boolean" || typeof value === "string") {
      return JSON.stringify(value);
    }
    if (typeof value === "number") {
      if (!Number.isInteger(value)) throw codedError("CANONICAL");
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (typeof value === "object") {
      return (
        "{" +
        Object.keys(value)
          .sort()
          .map(function (key) {
            return JSON.stringify(key) + ":" + canonical(value[key]);
          })
          .join(",") +
        "}"
      );
    }
    throw codedError("CANONICAL");
  }

  function sha256Hex(bytes) {
    return crypto.subtle.digest("SHA-256", bytes).then(toHex);
  }

  function concatBytes(a, b) {
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  // -------------------------------------------------------------------
  // IndexedDB (the only persistent store; every entry value is encrypted
  // or is public key material / metadata)
  // -------------------------------------------------------------------

  function openDb() {
    return new Promise(function (resolve, reject) {
      var request;
      try {
        request = indexedDB.open(DB_NAME, DB_VERSION);
      } catch (e) {
        reject(codedError("STORAGE_UNAVAILABLE"));
        return;
      }
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains("keys")) db.createObjectStore("keys", { keyPath: "name" });
        if (!db.objectStoreNames.contains("packages")) {
          db.createObjectStore("packages", { keyPath: "slot" });
        }
        if (!db.objectStoreNames.contains("ops")) db.createObjectStore("ops", { keyPath: "seq" });
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "name" });
      };
      request.onsuccess = function () {
        var db = request.result;
        // Another tab (an emergency wipe, an upgrade) needs the database:
        // close at once rather than block it; this tab is then Blocked.
        db.onversionchange = function () {
          try {
            db.close();
          } catch (e) {
            /* already closed */
          }
          state.storageError = true;
        };
        resolve(db);
      };
      request.onerror = function () {
        reject(codedError("STORAGE_UNAVAILABLE"));
      };
      request.onblocked = function () {
        reject(codedError("STORAGE_UNAVAILABLE"));
      };
    });
  }

  /** A result filled in by request callbacks, read once the transaction
   *  commits. Explicitly tagged, so a missing record (value undefined) is
   *  never mistaken for a present one. */
  function holder(value) {
    return { __ascHolder: true, value: value };
  }

  /** Run `work(stores)` in one transaction; resolve with its result once
   *  the transaction COMMITS (durable), reject on error or abort. */
  function tx(db, names, mode, work) {
    return new Promise(function (resolve, reject) {
      var transaction;
      try {
        transaction = mode === "readwrite"
          ? db.transaction(names, mode, { durability: "strict" })
          : db.transaction(names, mode);
      } catch (e) {
        reject(codedError("STORAGE_FAILED"));
        return;
      }
      var stores = {};
      names.forEach(function (name) {
        stores[name] = transaction.objectStore(name);
      });
      var result;
      try {
        result = work(stores, transaction);
      } catch (e) {
        try {
          transaction.abort();
        } catch (ignored) {
          /* already finished */
        }
        reject(e && e.code ? e : codedError("STORAGE_FAILED"));
        return;
      }
      transaction.oncomplete = function () {
        resolve(result && result.__ascHolder ? result.value : result);
      };
      function fail() {
        var err = transaction.error;
        reject(codedError(err && err.name === "QuotaExceededError" ? "QUOTA" : "STORAGE_FAILED"));
      }
      transaction.onerror = fail;
      transaction.onabort = fail;
    });
  }

  function getOne(db, store, key) {
    return tx(db, [store], "readonly", function (s) {
      var result = holder(undefined);
      s[store].get(key).onsuccess = function (event) {
        result.value = event.target.result;
      };
      return result;
    });
  }

  function getAll(db, store) {
    return tx(db, [store], "readonly", function (s) {
      var result = holder([]);
      s[store].getAll().onsuccess = function (event) {
        result.value = event.target.result || [];
      };
      return result;
    });
  }

  function putOne(db, store, value) {
    return tx(db, [store], "readwrite", function (s) {
      s[store].put(value);
    });
  }

  function deleteOne(db, store, key) {
    return tx(db, [store], "readwrite", function (s) {
      s[store].delete(key);
    });
  }

  // -------------------------------------------------------------------
  // Keys (non-extractable) and local-store encryption
  // -------------------------------------------------------------------

  function localKey(db) {
    return getOne(db, "keys", "local-store").then(function (row) {
      if (row && row.key) return row.key;
      return crypto.subtle
        .generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"])
        .then(function (key) {
          return putOne(db, "keys", { name: "local-store", key: key }).then(function () {
            return key;
          });
        });
    });
  }

  function sealLocal(db, value) {
    return localKey(db).then(function (key) {
      var iv = crypto.getRandomValues(new Uint8Array(12));
      return crypto.subtle
        .encrypt({ name: "AES-GCM", iv: iv }, key, utf8(canonical(value)))
        .then(function (ct) {
          return { iv: b64u(iv), ct: b64u(ct) };
        });
    });
  }

  function openLocal(db, sealed) {
    return localKey(db).then(function (key) {
      return crypto.subtle
        .decrypt({ name: "AES-GCM", iv: unb64u(sealed.iv) }, key, unb64u(sealed.ct))
        .then(function (plain) {
          return JSON.parse(fromUtf8(new Uint8Array(plain)));
        });
    });
  }

  function generateDeviceKeys() {
    var sign = crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, [
      "sign",
      "verify",
    ]);
    var unwrap = crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, [
      "deriveBits",
    ]);
    return Promise.all([sign, unwrap]).then(function (pairs) {
      return Promise.all([
        crypto.subtle.exportKey("spki", pairs[0].publicKey),
        crypto.subtle.exportKey("spki", pairs[1].publicKey),
      ]).then(function (spkis) {
        return {
          sign: pairs[0],
          unwrap: pairs[1],
          signSpki: b64u(spkis[0]),
          unwrapSpki: b64u(spkis[1]),
        };
      });
    });
  }

  function proofMessage(purpose, nonce, body) {
    return sha256Hex(utf8(body)).then(function (digest) {
      return utf8(PROOF_PREFIX + "\n" + purpose + "\n" + nonce + "\n" + digest);
    });
  }

  function signWith(privateKey, bytes) {
    return crypto.subtle
      .sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, bytes)
      .then(b64u); // WebCrypto returns raw r || s, exactly the JWS form
  }

  // -------------------------------------------------------------------
  // Pinned trust roots and ES256 compact JWS verification
  // -------------------------------------------------------------------

  function trustAnchors(db) {
    return getOne(db, "meta", "trust").then(function (row) {
      return (row && row.anchors) || [];
    });
  }

  function verifyJws(compact, typ, anchors) {
    if (typeof compact !== "string" || compact.split(".").length !== 3) {
      return Promise.reject(codedError("SIGNATURE"));
    }
    var parts = compact.split(".");
    var header;
    try {
      header = JSON.parse(fromUtf8(unb64u(parts[0])));
    } catch (e) {
      return Promise.reject(codedError("SIGNATURE"));
    }
    var expected = canonical({ alg: "ES256", kid: header.kid, typ: typ });
    if (
      typeof header.kid !== "string" ||
      Object.keys(header).length !== 3 ||
      fromUtf8(unb64u(parts[0])) !== expected
    ) {
      return Promise.reject(codedError("SIGNATURE"));
    }
    var anchor = anchors.filter(function (a) {
      return a.kid === header.kid;
    })[0];
    if (!anchor) return Promise.reject(codedError("UNTRUSTED_KEY"));
    return crypto.subtle
      .importKey("spki", unb64u(anchor.spki), { name: "ECDSA", namedCurve: "P-256" }, false, [
        "verify",
      ])
      .then(function (key) {
        var signature = unb64u(parts[2]);
        if (signature.length !== 64) throw codedError("SIGNATURE");
        return crypto.subtle.verify(
          { name: "ECDSA", hash: "SHA-256" },
          key,
          signature,
          utf8(parts[0] + "." + parts[1])
        );
      })
      .then(function (ok) {
        if (!ok) throw codedError("SIGNATURE");
        return JSON.parse(fromUtf8(unb64u(parts[1])));
      });
  }

  // -------------------------------------------------------------------
  // Envelope decryption (ECDH P-256 -> HKDF-SHA256 -> AES-256-GCM)
  // -------------------------------------------------------------------

  function context(kind, part, device, objectId) {
    return "ASC-" + kind + "-" + part + "-v1|" + device + "|" + objectId;
  }

  function decryptEnvelope(db, manifest, ciphertext, kind, objectId) {
    var wrap = manifest.wrap;
    var enc = manifest.enc;
    if (enc.aad !== context(kind, "BODY", manifest.device, objectId)) {
      return Promise.reject(codedError("INTEGRITY"));
    }
    return getOne(db, "keys", "device-unwrap").then(function (row) {
      if (!row || !row.privateKey) throw codedError("NOT_PREPARED");
      return crypto.subtle
        .importKey("spki", unb64u(wrap.epk), { name: "ECDH", namedCurve: "P-256" }, false, [])
        .then(function (ephemeral) {
          return crypto.subtle.deriveBits({ name: "ECDH", public: ephemeral }, row.privateKey, 256);
        })
        .then(function (shared) {
          return crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
        })
        .then(function (hkdfKey) {
          return crypto.subtle.deriveKey(
            {
              name: "HKDF",
              hash: "SHA-256",
              salt: unb64u(wrap.salt),
              info: utf8(context(kind, "WRAP", manifest.device, objectId)),
            },
            hkdfKey,
            { name: "AES-GCM", length: 256 },
            false,
            ["decrypt"]
          );
        })
        .then(function (wrappingKey) {
          return crypto.subtle.decrypt(
            {
              name: "AES-GCM",
              iv: unb64u(wrap.iv),
              additionalData: utf8(context(kind, "DEK", manifest.device, objectId)),
            },
            wrappingKey,
            unb64u(wrap.wrapped_key)
          );
        })
        .then(function (rawKey) {
          return crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["decrypt"]);
        })
        .then(function (dataKey) {
          return crypto.subtle.decrypt(
            { name: "AES-GCM", iv: unb64u(enc.iv), additionalData: utf8(enc.aad) },
            dataKey,
            ciphertext
          );
        })
        .then(function (plain) {
          return JSON.parse(fromUtf8(new Uint8Array(plain)));
        })
        .catch(function (error) {
          throw error && error.code === "NOT_PREPARED" ? error : codedError("INTEGRITY");
        });
    });
  }

  function exactKeys(obj, allowed) {
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return false;
    var keys = Object.keys(obj).sort();
    var want = allowed.slice().sort();
    return keys.length === want.length && keys.every(function (k, i) {
      return k === want[i];
    });
  }

  // -------------------------------------------------------------------
  // Runtime state (memory only; package plaintext never leaves memory)
  // -------------------------------------------------------------------

  var state = {
    body: null, // decrypted active package body (memory only)
    activeMeta: null,
    deltaMeta: null,
    deltaBody: null,
    integrityFailure: false,
    storageError: false,
    notEnrolled: false,
    directives: { block: false, blockReason: "" },
    rebuildRequired: false,
    rebuildReasons: [],
    nonce: null,
    serverOffsetSeconds: 0,
    device: null,
    offline: null,
    grants: [],
    counts: {
      pending: 0,
      inFlight: 0,
      locked: 0,
      acknowledged: 0,
      conflicted: 0,
      sequenceLow: null,
      sequenceHigh: null,
      chainHead: "",
    },
    lastState: "",
    wiped: false,
    // Emergency wipe: NONE | IN_PROGRESS | BLOCKED | FAILED | WIPED. WIPED is
    // set only after indexedDB.deleteDatabase() fires onsuccess.
    wipe: { status: "NONE", reportedOrder: "", promise: null, handling: null },
    building: false,
    // Phase 4 Prompt 3: offline verification and synchronization.
    index: null, // in-memory lookup built from the active package (jti -> entry)
    qrKeys: {}, // kid -> imported CryptoKey (memory only)
    localAdmissions: {}, // jti -> latest offline admission time on this device
    result: null, // the verification on screen, bound to the state it was made in
    sync: { running: false, lastSuccessAt: null, lastAttemptAt: null, failures: 0, nextAt: 0, error: "" },
    lockDirective: false,
  };

  var channel = null;
  try {
    if (window.BroadcastChannel) channel = new BroadcastChannel("asc-entry-offline");
  } catch (e) {
    channel = null;
  }

  function broadcast(type) {
    if (!channel) return;
    try {
      channel.postMessage({ type: type });
    } catch (e) {
      /* best effort; onversionchange still closes other connections */
    }
  }

  var health = {
    mode: "online", // "online" | "offline" | "recovering"
    consecutiveFailures: 0,
    firstFailureAt: null,
    consecutiveSuccesses: 0,
    firstSuccessAt: null,
    unstable: false,
    wallAnchor: null,
    monoAnchor: null,
    clockDiscontinuity: false,
    // Monotonic instant at which the current discontinuity was detected
    // (UX-C2, item D): only a contact STARTED after it may clear the block.
    discontinuityAtMono: null,
    lastActivity: Date.now(),
  };

  function nowSeconds() {
    return Math.floor(Date.now() / 1000 + state.serverOffsetSeconds);
  }

  // -------------------------------------------------------------------
  // Health transitions (OFF-001: repeated checks, never one failure)
  // -------------------------------------------------------------------

  function recordHealth(ok, atMs) {
    var H = CONFIG.health;
    var now = typeof atMs === "number" ? atMs : Date.now();
    if (ok) {
      health.consecutiveFailures = 0;
      health.firstFailureAt = null;
      health.unstable = false;
      if (health.mode === "offline") {
        health.consecutiveSuccesses += 1;
        if (health.firstSuccessAt === null) health.firstSuccessAt = now;
        if (
          health.consecutiveSuccesses >= H.successes_to_sync &&
          now - health.firstSuccessAt >= H.success_spacing_seconds * 1000
        ) {
          health.mode = "recovering";
          health.consecutiveSuccesses = 0;
          health.firstSuccessAt = null;
        }
      }
    } else {
      health.consecutiveSuccesses = 0;
      health.firstSuccessAt = null;
      health.consecutiveFailures += 1;
      if (health.firstFailureAt === null) health.firstFailureAt = now;
      health.unstable = true;
      if (
        health.mode !== "offline" &&
        health.consecutiveFailures >= H.failures_to_offline &&
        now - health.firstFailureAt >= H.failure_span_seconds * 1000
      ) {
        health.mode = "offline";
      }
    }
    return health.mode;
  }

  /** Detect a wall-clock jump against the monotonic clock. */
  function checkClock() {
    var wall = Date.now();
    var mono = performance.now();
    if (health.wallAnchor === null) {
      health.wallAnchor = wall;
      health.monoAnchor = mono;
      return false;
    }
    var drift = Math.abs(wall - health.wallAnchor - (mono - health.monoAnchor)) / 1000;
    var sensitivity = (state.activeMeta && state.activeMeta.sensitivity) || "STANDARD";
    var limit = CONFIG.validity[sensitivity].clock_discontinuity_seconds;
    if (drift > limit && !health.clockDiscontinuity) {
      health.clockDiscontinuity = true;
      health.discontinuityAtMono = mono;
    }
    return health.clockDiscontinuity;
  }

  function reanchorClock() {
    health.wallAnchor = Date.now();
    health.monoAnchor = performance.now();
    health.clockDiscontinuity = false;
    health.discontinuityAtMono = null;
  }

  //: Largest wall-versus-monotonic difference over ONE request that still
  //: counts as a steady clock (UX-C2, item D).
  var CLOCK_REQUEST_TOLERANCE_MS = 2000;

  /** True when a heartbeat response may re-anchor the clock and set the
   *  server offset: its request was sent after the current discontinuity
   *  was detected (the block ends only with the NEXT successful contact),
   *  and the local clock did not jump while it was in flight (otherwise the
   *  offset would mix readings from both sides of the jump). */
  function clockTrustworthyFor(sentAt, sentMono) {
    var drift = Math.abs(Date.now() - sentAt - (performance.now() - sentMono));
    if (drift > CLOCK_REQUEST_TOLERANCE_MS) return false;
    return health.discontinuityAtMono === null || sentMono > health.discontinuityAtMono;
  }

  // -------------------------------------------------------------------
  // The seven required states (pure) and the permitted actions
  // -------------------------------------------------------------------

  function bandAt(nowSec, meta) {
    if (!meta) return "NONE";
    if (nowSec >= meta.expires_at) return "EXPIRED";
    if (nowSec >= meta.stale_at) return "STALE";
    if (nowSec >= meta.aging_at) return "AGING";
    return "FRESH";
  }

  /**
   * ctx: {enabled, notEnrolled, directiveBlock, storageError, clockDiscontinuity,
   *       integrityFailure, connectivity ("online"|"offline"|"recovering"), unstable,
   *       prepared, serverReady, band ("NONE"|"FRESH"|"AGING"|"STALE"|"EXPIRED"),
   *       grant ("NONE"|"VALID"|"LOCKED"), unacknowledged, rebuildBlocking, rebuildRequired,
   *       wipe ("NONE"|"IN_PROGRESS"|"BLOCKED"|"FAILED"|"WIPED")}
   * Returns {state, sub: [...]}; precedence Blocked > Expired > Stale >
   * Offline Active > Syncing > Offline Ready > Online.
   */
  var WIPE_SUB = { IN_PROGRESS: "WIPE_PENDING", BLOCKED: "WIPE_BLOCKED", FAILED: "WIPE_FAILED", WIPED: "WIPED" };

  function computeState(ctx) {
    var sub = [];
    if (ctx.wipe && ctx.wipe !== "NONE") return { state: "BLOCKED", sub: [WIPE_SUB[ctx.wipe]] };
    if (ctx.unstable && ctx.connectivity === "online") sub.push("UNSTABLE");
    if (ctx.notEnrolled) return { state: "BLOCKED", sub: ["NOT_ENROLLED"] };
    if (ctx.directiveBlock) return { state: "BLOCKED", sub: ["DIRECTIVE"] };
    if (ctx.storageError) return { state: "BLOCKED", sub: ["STORAGE"] };
    if (ctx.clockDiscontinuity) return { state: "BLOCKED", sub: ["CLOCK"] };
    if (ctx.connectivity === "offline") {
      if (!ctx.enabled) return { state: "BLOCKED", sub: ["DISABLED"] };
      if (ctx.band === "NONE") {
        return { state: "BLOCKED", sub: [ctx.integrityFailure ? "INTEGRITY" : "NO_PACKAGE"] };
      }
      if (ctx.band === "EXPIRED") return { state: "EXPIRED", sub: [] };
      if (ctx.rebuildBlocking) return { state: "BLOCKED", sub: ["REBUILD"] };
      if (ctx.grant === "LOCKED") return { state: "BLOCKED", sub: ["OPERATOR_LOCKED"] };
      if (ctx.grant !== "VALID") return { state: "BLOCKED", sub: ["NO_GRANT"] };
      if (ctx.band === "STALE") return { state: "STALE", sub: [] };
      if (ctx.rebuildRequired) return { state: "STALE", sub: ["REBUILD"] };
      return { state: "OFFLINE_ACTIVE", sub: ctx.band === "AGING" ? ["AGING"] : [] };
    }
    if (ctx.connectivity === "recovering" || ctx.unacknowledged > 0) {
      return { state: "SYNCING", sub: sub };
    }
    if (!ctx.enabled) return { state: "ONLINE", sub: sub.concat(["DISABLED"]) };
    if (ctx.prepared && ctx.serverReady && ctx.band === "FRESH" && !ctx.rebuildRequired && !ctx.rebuildBlocking) {
      // Offline Ready describes the DEVICE. Without a valid operator grant a
      // loss of connection still ends in Blocked, so say so.
      if (ctx.grant !== "VALID") sub.push("NO_GRANT");
      return { state: "OFFLINE_READY", sub: sub };
    }
    if (ctx.band === "AGING") sub.push("AGING");
    return { state: "ONLINE", sub: sub };
  }

  function permitted(stateName, action) {
    var list = (CONFIG.permitted && CONFIG.permitted[stateName]) || [];
    return list.indexOf(action) !== -1;
  }

  /** A grant is usable offline only if it is unexpired AND bound to this
   *  device's checkpoint as the active package describes it: same event,
   *  gate and scope version, and a zone of the package (grant schema 2). */
  function grantMatchesPackage(grant) {
    var body = state.body;
    if (!body || grant.schema !== 2) return false;
    return (
      grant.event === body.event &&
      grant.gate === body.gate &&
      grant.scope_version === body.scope_version &&
      body.zones.indexOf(grant.zone) !== -1
    );
  }

  function usableGrants() {
    var now = nowSeconds();
    return state.grants.filter(function (grant) {
      return grant.expires_at > now && grant.issued_at <= now + 300 && grantMatchesPackage(grant);
    });
  }

  /** The operator working offline: the most recently issued usable,
   *  unlocked grant (no local sign-in exists; ADR-0024 §3). */
  function activeGrant() {
    var list = usableGrants().filter(function (grant) {
      return !grant.locked;
    });
    list.sort(function (a, b) {
      return b.issued_at - a.issued_at;
    });
    return list[0] || null;
  }

  function currentGrantState() {
    var valid = usableGrants();
    if (!valid.length) return "NONE";
    if (health.mode === "offline") {
      var idle = (Date.now() - health.lastActivity) / 1000;
      if (idle >= CONFIG.inactivity_lock_seconds) {
        valid.forEach(function (grant) {
          grant.locked = true; // no local re-authentication: only an online re-issue unlocks
        });
      }
    }
    return valid.every(function (grant) {
      return grant.locked;
    })
      ? "LOCKED"
      : "VALID";
  }

  function currentContext() {
    var offline = state.offline || {};
    return {
      enabled: !!(CONFIG.enabled && offline.available !== false),
      notEnrolled: state.notEnrolled,
      directiveBlock: state.directives.block,
      storageError: state.storageError,
      clockDiscontinuity: health.clockDiscontinuity,
      integrityFailure: state.integrityFailure,
      connectivity: health.mode,
      unstable: health.unstable,
      prepared: !!offline.prepared,
      serverReady: !!offline.ready,
      band: bandAt(nowSeconds(), state.activeMeta),
      grant: currentGrantState(),
      unacknowledged:
        typeof state.counts.unacknowledged === "number"
          ? state.counts.unacknowledged
          : state.counts.pending + state.counts.locked,
      rebuildBlocking: state.rebuildReasons.some(function (reason) {
        return (CONFIG.rebuild_blocking_reasons || ["SCOPE_CHANGED"]).indexOf(reason) !== -1;
      }),
      rebuildRequired: state.rebuildRequired,
      wipe: state.wipe.status,
    };
  }

  // -------------------------------------------------------------------
  // Local operation store (append-only, encrypted, signed, hash-chained)
  // -------------------------------------------------------------------
  //
  // Record states, always distinguishable:
  //   PENDING       recorded, not yet sent (or sent without a durable ack);
  //   IN_FLIGHT     included in an upload whose answer has not arrived --
  //                 back to PENDING on any failure and at every start-up;
  //   LOCKED        frozen by a server directive: upload only (`in_flight`
  //                 flags one being uploaded), refuses new records;
  //   ACKNOWLEDGED  the server's SIGNED durable acknowledgement was verified;
  //                 deleted only after the configured retention.

  function withLock(name, work) {
    if (navigator.locks && navigator.locks.request) return navigator.locks.request(name, work);
    return Promise.reject(codedError("STORAGE_UNAVAILABLE"));
  }

  function isConflictedOutcome(outcome) {
    if (!outcome) return false;
    var conflicted = (CONFIG.sync && CONFIG.sync.conflicted_statuses) || [];
    return conflicted.indexOf(outcome.status) !== -1 || !!outcome.conflict_type;
  }

  function opCounts(db) {
    return getAll(db, "ops").then(function (rows) {
      var counts = {
        pending: 0,
        inFlight: 0,
        locked: 0,
        acknowledged: 0,
        conflicted: 0,
        unacknowledged: 0,
        sequenceLow: null,
        sequenceHigh: null,
        chainHead: "",
      };
      rows.forEach(function (row) {
        if (row.state === "PENDING") counts.pending += 1;
        else if (row.state === "IN_FLIGHT") counts.inFlight += 1;
        else if (row.state === "LOCKED") {
          counts.locked += 1;
          if (row.in_flight) counts.inFlight += 1;
        } else if (row.state === "ACKNOWLEDGED") {
          counts.acknowledged += 1;
          if (isConflictedOutcome(row.outcome)) counts.conflicted += 1;
        }
        if (row.state !== "ACKNOWLEDGED") {
          counts.unacknowledged += 1;
          if (counts.sequenceLow === null || row.seq < counts.sequenceLow) counts.sequenceLow = row.seq;
        }
        if (counts.sequenceHigh === null || row.seq > counts.sequenceHigh) counts.sequenceHigh = row.seq;
      });
      return getOne(db, "meta", "chain").then(function (chain) {
        counts.chainHead = (chain && chain.head) || "";
        if (chain && chain.sequence && (counts.sequenceHigh === null || chain.sequence > counts.sequenceHigh)) {
          counts.sequenceHigh = chain.sequence;
        }
        return counts;
      });
    });
  }

  function newStoreId() {
    return b64u(crypto.getRandomValues(new Uint8Array(16)));
  }

  /**
   * Append one operation. Inside ONE exclusive lock (every tab of the app):
   * assign the next sequence and the chain's previous hash, write the
   * canonical operation, sign it with the device's non-extractable key,
   * chain it (SHA-256(prev || operation)), seal it with the local store key
   * and commit it in ONE IndexedDB transaction together with the new chain
   * head. Resolves only once that transaction COMMITTED (durable). Refused
   * while the store is locked. Never overwrites a record (`add`).
   */
  function appendOperation(db, draft, note, guard) {
    return withLock("asc-entry-opstore", function () {
      // Other tabs may have committed an admission since this tab scanned.
      // Reload under the same origin-wide lock before checking the decision.
      return (guard ? loadLocalAdmissions(db) : Promise.resolve()).then(function () {
      if (guard && !guard()) throw codedError("STATE_CHANGED");
      return Promise.all([getOne(db, "meta", "chain"), getOne(db, "keys", "device-sign")]).then(
        function (found) {
          var chain = found[0];
          var signer = found[1];
          if (chain && chain.locked) throw codedError("STORE_LOCKED");
          if (!signer || !signer.privateKey) throw codedError("NOT_PREPARED");
          var store = (chain && chain.store) || newStoreId();
          var seq = ((chain && chain.sequence) || 0) + 1;
          var previous = (chain && chain.head) || ZERO_HASH;
          var operation = Object.assign({}, draft, { store: store, sequence: seq, prev: previous });
          var text = canonical(operation);
          return Promise.all([
            signWith(signer.privateKey, utf8(text)),
            sha256Hex(concatBytes(utf8(previous), utf8(text))),
          ]).then(function (done) {
            var signature = done[0];
            var head = done[1];
            return sealLocal(db, { op: text, signature: signature, note: note || "" }).then(function (sealed) {
              if (guard && !guard()) throw codedError("STATE_CHANGED");
              return tx(db, ["ops", "meta"], "readwrite", function (s) {
                s.ops.add({
                  seq: seq,
                  operation_id: String(operation.operation_id || ""),
                  state: "PENDING",
                  sealed: sealed,
                  chain: head,
                  created_at: Date.now(),
                });
                s.meta.put({
                  name: "chain",
                  store: store,
                  sequence: seq,
                  head: head,
                  locked: false,
                  lockedReason: "",
                });
                return holder({ seq: seq, chain: head, operation: operation });
              });
            });
          });
        }
      );
      });
    });
  }

  /** PENDING / IN_FLIGHT -> LOCKED: kept, upload-only, never usable for a new
   *  admission. */
  function lockOperations(db, reason) {
    return withLock("asc-entry-opstore", function () {
      return getAll(db, "ops").then(function (rows) {
        return getOne(db, "meta", "chain").then(function (chain) {
          return tx(db, ["ops", "meta"], "readwrite", function (s) {
            rows.forEach(function (row) {
              if (row.state === "PENDING" || row.state === "IN_FLIGHT") {
                row.state = "LOCKED";
                row.in_flight = false;
                row.locked_reason = String(reason || "");
                s.ops.put(row);
              }
            });
            s.meta.put(
              Object.assign({}, chain || { name: "chain", sequence: 0, head: "" }, {
                name: "chain",
                locked: true,
                lockedReason: String(reason || ""),
              })
            );
          });
        });
      });
    });
  }

  /** Once every locked record is durably acknowledged and the server no
   *  longer directs locking, the store may record again. */
  function maybeUnlockStore(db) {
    if (state.lockDirective) return Promise.resolve(false);
    return withLock("asc-entry-opstore", function () {
      return getAll(db, "ops").then(function (rows) {
        var waiting = rows.some(function (row) {
          return row.state === "LOCKED" || row.state === "PENDING" || row.state === "IN_FLIGHT";
        });
        return getOne(db, "meta", "chain").then(function (chain) {
          if (!chain || !chain.locked || waiting) return false;
          return putOne(db, "meta", Object.assign({}, chain, { locked: false, lockedReason: "" })).then(function () {
            return true;
          });
        });
      });
    });
  }

  /** At start-up an upload can never still be in flight: whatever was sent
   *  without a durable acknowledgement is pending again (the server
   *  deduplicates a repeat by operation id). */
  function revertInFlight(db) {
    return withLock("asc-entry-opstore", function () {
      return getAll(db, "ops").then(function (rows) {
        return tx(db, ["ops"], "readwrite", function (s) {
          rows.forEach(function (row) {
            if (row.state === "IN_FLIGHT") {
              row.state = "PENDING";
              s.ops.put(row);
            } else if (row.in_flight) {
              row.in_flight = false;
              s.ops.put(row);
            }
          });
        });
      });
    });
  }

  /** Deletes ONLY operations the server durably acknowledged, and only once
   *  their retention has passed. Pending, in-flight and locked operations are
   *  never touched. Missing or unverifiable acknowledgements retain evidence. */
  function purgeAcknowledgedOperations(db, retentionMs) {
    var keepFor = typeof retentionMs === "number" ? retentionMs : 0;
    var cutoff = Date.now() - keepFor;
    return withLock("asc-entry-opstore", function () {
      return Promise.all([getAll(db, "ops"), trustAnchors(db), getOne(db, "meta", "chain")]).then(function (found) {
        var candidates = found[0].filter(function (row) {
          return row.state === "ACKNOWLEDGED" && row.acknowledged_at && row.acknowledged_at <= cutoff && row.outcome && row.outcome.ack;
        });
        var doomed = [];
        return candidates.reduce(function (pending, row) {
          return pending.then(function () {
            return openLocal(db, row.sealed).then(function (value) {
              var op = JSON.parse(value.op);
              return verifyAck(row.outcome.ack, { operation_id: row.operation_id, seq: row.seq, text: value.op }, found[1], found[2] && found[2].store).then(function (ack) {
                if (!ack) return;
                // Retain local anti-passback evidence until the active package
                // itself covers it (or cannot resolve that pass at all).
                if (op.decision === "ADMIT") {
                  if (!state.body) return;
                  var entry = currentIndex().entries.get(op.credential.jti);
                  if (entry && (!entry.last_admitted_at || entry.last_admitted_at < op.occurred_at)) return;
                }
                doomed.push(row.seq);
              });
            }).catch(function () { return null; }); // unreadable evidence is retained
          });
        }, Promise.resolve()).then(function () {
          return tx(db, ["ops"], "readwrite", function (s) {
            doomed.forEach(function (seq) { s.ops.delete(seq); });
            return holder(doomed.length);
          });
        });
      });
    });
  }

  /** The device's own offline admissions (memory only), for the re-entry
   *  advisory and the single-entry rule while disconnected (Flow §11.5:
   *  "local prior operations"). */
  function loadLocalAdmissions(db) {
    return getAll(db, "ops").then(function (rows) {
      var admissions = {};
      return rows
        .reduce(function (chain, row) {
          return chain.then(function () {
            return openLocal(db, row.sealed)
              .then(function (value) {
                var op = JSON.parse(value.op);
                if (op.decision === "ADMIT" && op.credential) {
                  var previous = admissions[op.credential.jti] || 0;
                  admissions[op.credential.jti] = Math.max(previous, op.occurred_at);
                }
              })
              .catch(function () {
                state.storageError = true;
                throw codedError("STORAGE_FAILED"); // never ignore lost anti-passback evidence
              });
          });
        }, Promise.resolve())
        .then(function () {
          state.localAdmissions = admissions;
          return admissions;
        });
    });
  }

  /** Package cleanup: package slots and in-memory data only. Never touches
   *  operations, keys, or the chain -- operation cleanup is separate. */
  function purgePackageData(db) {
    state.body = null;
    state.activeMeta = null;
    state.deltaMeta = null;
    state.deltaBody = null;
    // In-memory participant data goes with the package (Prompt 3).
    state.index = null;
    state.deltaIndex = null;
    state.qrKeys = {};
    state.result = null;
    return tx(db, ["packages"], "readwrite", function (s) {
      ["staging", "active", "previous", "delta"].forEach(function (slot) {
        s.packages.delete(slot);
      });
    });
  }

  // -------------------------------------------------------------------
  // Package activation (atomic) and rollback (only to a still-valid package)
  // -------------------------------------------------------------------

  function metaFrom(manifest) {
    return {
      package_id: manifest.package_id,
      package_version: manifest.package_version,
      device: manifest.device,
      scope_version: manifest.scope_version,
      sensitivity: manifest.sensitivity,
      issued_at: manifest.issued_at,
      data_cutoff_at: manifest.data_cutoff_at,
      aging_at: manifest.aging_at,
      stale_at: manifest.stale_at,
      expires_at: manifest.expires_at,
      entry_count: manifest.entry_count,
    };
  }

  /** Verify and decrypt one stored package response. Resolves {manifest, body}. */
  function packageShapeValid(body) {
    function list(items, name, extra) {
      return Array.isArray(items) && items.every(function (item) {
        return exactKeys(item, CONFIG.allow[name]) && (!extra || extra(item));
      });
    }
    function labels(value) {
      return exactKeys(value, CONFIG.allow.labels) && CONFIG.allow.labels.every(function (key) {
        return typeof value[key] === "string";
      });
    }
    var seen = new Set();
    return list(body.entries, "entry", function (entry) {
      if (seen.has(entry.jti)) return false;
      seen.add(entry.jti);
      return labels(entry.badge_label) && exactKeys(entry.profile_window, CONFIG.allow.window) &&
        list(entry.zone_rules, "zone_rules", function (zone) {
          return list(zone.rules, "rule", function (rule) {
            return rule.effect === "ALLOW" || rule.effect === "DENY";
          });
        }) && list(entry.restrictions, "restriction");
    }) && list(body.revoked_passes, "revoked_pass") && list(body.qr_keys, "qr_key") &&
      list(body.override_reasons, "override_reason", function (reason) { return labels(reason.names); });
  }

  function verifyPackage(db, raw, expect) {
    var envelope;
    try {
      envelope = JSON.parse(raw);
    } catch (e) {
      return Promise.reject(codedError("INTEGRITY"));
    }
    if (!exactKeys(envelope, ["ciphertext", "manifest"])) {
      return Promise.reject(codedError("INTEGRITY"));
    }
    return trustAnchors(db).then(function (anchors) {
      return verifyJws(envelope.manifest, "ASC-OPKG", anchors).then(function (manifest) {
        if (!exactKeys(manifest, CONFIG.allow.manifest)) throw codedError("INTEGRITY");
        if (manifest.typ !== "ASC-OPKG") throw codedError("INTEGRITY");
        if (manifest.schema_version !== CONFIG.schema.package) throw codedError("SCHEMA");
        if (expect.device && manifest.device !== expect.device) throw codedError("BINDING");
        if (expect.event && manifest.event !== expect.event) throw codedError("BINDING");
        if (typeof expect.scopeVersion === "number" && manifest.scope_version !== expect.scopeVersion) {
          throw codedError("SCOPE");
        }
        if (!["package_version", "scope_version", "issued_at", "data_cutoff_at", "aging_at", "stale_at", "expires_at", "entry_count"].every(function (key) {
          return Number.isSafeInteger(manifest[key]) && manifest[key] >= 0;
        }) || manifest.package_version < 1 || manifest.scope_version < 1 ||
            manifest.issued_at > nowSeconds() + CONFIG.qr.clock_skew_seconds ||
            manifest.data_cutoff_at > manifest.issued_at ||
            manifest.data_cutoff_at > manifest.aging_at || manifest.aging_at > manifest.stale_at ||
            manifest.stale_at > manifest.expires_at) throw codedError("INTEGRITY");
        if (nowSeconds() >= manifest.expires_at) throw codedError("EXPIRED");
        return getOne(db, "keys", "device-unwrap").then(function (row) {
          if (!row) throw codedError("NOT_PREPARED");
          return sha256Hex(unb64u(row.spki)).then(function (fingerprint) {
            if (manifest.wrap.recipient !== fingerprint) throw codedError("BINDING");
            var ciphertext = unb64u(envelope.ciphertext);
            if (ciphertext.length !== manifest.ciphertext_length) throw codedError("INTEGRITY");
            return sha256Hex(ciphertext).then(function (digest) {
              if (digest !== manifest.ciphertext_sha256) throw codedError("INTEGRITY");
              return decryptEnvelope(db, manifest, ciphertext, "OPKG", manifest.package_id);
            });
          });
        }).then(function (body) {
          if (!exactKeys(body, CONFIG.allow.package)) throw codedError("ALLOW_LIST");
          if (!packageShapeValid(body)) throw codedError("ALLOW_LIST");
          ["schema_version", "package_id", "package_version", "device", "event", "scope_version", "issued_at", "data_cutoff_at",
            "aging_at", "stale_at", "expires_at", "sensitivity"].forEach(function (key) {
            if (body[key] !== manifest[key]) throw codedError("INTEGRITY");
          });
          if (body.entries.length !== manifest.entry_count || !Array.isArray(body.zones) ||
              typeof body.gate !== "string" || !body.gate) throw codedError("INTEGRITY");
          return { manifest: manifest, body: body };
        });
      });
    });
  }

  /**
   * Stage -> verify -> decrypt -> validate -> swap `active`/`previous` in ONE
   * transaction. Any failure before the swap leaves the active package as it
   * was. Resolves "ACTIVATED" or "UP_TO_DATE"; rejects with a code otherwise.
   */
  function activatePackage(db, raw, expect) {
    return putOne(db, "packages", { slot: "staging", raw: raw })
      .then(function () {
        return verifyPackage(db, raw, expect || {});
      })
      .then(function (result) {
        return getOne(db, "packages", "active").then(function (active) {
          var version = result.manifest.package_version;
          if (active && active.meta && version <= active.meta.package_version) {
            if (version === active.meta.package_version) return "UP_TO_DATE";
            throw codedError("NOT_NEWER"); // refresh only to a NEWER immutable version
          }
          var meta = metaFrom(result.manifest);
          return tx(db, ["packages"], "readwrite", function (s) {
            if (active) s.packages.put({ slot: "previous", raw: active.raw, meta: active.meta });
            s.packages.put({ slot: "active", raw: raw, meta: meta });
            s.packages.delete("staging");
            s.packages.delete("delta"); // a delta belongs to the package it was issued for
          }).then(function () {
            state.body = result.body;
            state.activeMeta = meta;
            state.deltaMeta = null;
            state.deltaBody = null;
            state.rebuildRequired = false;
            state.rebuildReasons = [];
            state.integrityFailure = false;
            return "ACTIVATED";
          });
        });
      })
      .catch(function (error) {
        return deleteOne(db, "packages", "staging")
          .catch(function () {
            return null;
          })
          .then(function () {
            throw error && error.code ? error : codedError("INTEGRITY");
          });
      });
  }

  /** Load the active package at start-up, re-verifying it at rest. Falls
   *  back to `previous` ONLY if that one is still valid and verifies. */
  function loadActive(db, expect) {
    return getOne(db, "packages", "active").then(function (active) {
      if (!active) return "NONE";
      return verifyPackage(db, active.raw, expect || {})
        .then(function (result) {
          state.body = result.body;
          state.activeMeta = metaFrom(result.manifest);
          return loadDelta(db).then(function () {
            return "ACTIVE";
          });
        })
        .catch(function (error) {
          // Diagnostic only (a code, never a value); shown to nobody.
          state.lastIntegrityCode = (error && error.code) || "UNEXPECTED";
          return getOne(db, "packages", "previous").then(function (previous) {
            if (!previous || !previous.meta || nowSeconds() >= previous.meta.expires_at) {
              state.integrityFailure = true;
              return purgePackageData(db).then(function () {
                return "NONE";
              });
            }
            return verifyPackage(db, previous.raw, expect || {})
              .then(function (result) {
                return tx(db, ["packages"], "readwrite", function (s) {
                  s.packages.put({ slot: "active", raw: previous.raw, meta: previous.meta });
                  s.packages.delete("previous");
                  s.packages.delete("delta");
                }).then(function () {
                  state.body = result.body;
                  state.activeMeta = metaFrom(result.manifest);
                  return "ROLLED_BACK";
                });
              })
              .catch(function () {
                state.integrityFailure = true;
                return purgePackageData(db).then(function () {
                  return "NONE";
                });
              });
          });
        });
    });
  }

  // -------------------------------------------------------------------
  // Critical delta: advances ONLY critical_delta_cutoff_at
  // -------------------------------------------------------------------

  function verifyDelta(db, raw) {
    var envelope;
    try {
      envelope = JSON.parse(raw);
    } catch (e) {
      return Promise.reject(codedError("INTEGRITY"));
    }
    if (!state.activeMeta) return Promise.reject(codedError("NO_PACKAGE"));
    return trustAnchors(db).then(function (anchors) {
      return verifyJws(envelope.manifest, "ASC-ODELTA", anchors).then(function (manifest) {
        var active = state.activeMeta;
        if (!exactKeys(manifest, CONFIG.allow.delta_manifest)) throw codedError("INTEGRITY");
        if (manifest.schema_version !== CONFIG.schema.delta) throw codedError("SCHEMA");
        if (manifest.device !== active.device || manifest.package_id !== active.package_id) {
          throw codedError("BINDING");
        }
        if (manifest.package_version !== active.package_version) throw codedError("BINDING");
        if (manifest.scope_version !== active.scope_version) throw codedError("SCOPE");
        var lastVersion = (state.deltaMeta && state.deltaMeta.delta_version) || 0;
        var lastCutoff = (state.deltaMeta && state.deltaMeta.critical_delta_cutoff_at) || active.data_cutoff_at;
        if (manifest.delta_version <= lastVersion) throw codedError("NOT_NEWER");
        if (manifest.critical_delta_cutoff_at < lastCutoff) throw codedError("NOT_NEWER");
        var ciphertext = unb64u(envelope.ciphertext);
        return sha256Hex(ciphertext).then(function (digest) {
          if (digest !== manifest.ciphertext_sha256) throw codedError("INTEGRITY");
          var objectId = manifest.package_id + ".d" + manifest.delta_version;
          return decryptEnvelope(db, manifest, ciphertext, "ODELTA", objectId).then(function (body) {
            if (!exactKeys(body, CONFIG.allow.delta)) throw codedError("ALLOW_LIST");
            if (body.delta_version !== manifest.delta_version) throw codedError("INTEGRITY");
            return { manifest: manifest, body: body };
          });
        });
      });
    });
  }

  function applyDelta(db, raw) {
    return verifyDelta(db, raw).then(function (result) {
      var meta = {
        delta_version: result.manifest.delta_version,
        critical_delta_cutoff_at: result.manifest.critical_delta_cutoff_at,
        package_id: result.manifest.package_id,
      };
      return putOne(db, "packages", { slot: "delta", raw: raw, meta: meta }).then(function () {
        state.deltaMeta = meta;
        state.deltaBody = result.body;
        state.rebuildRequired = !!result.body.rebuild_required;
        state.rebuildReasons = result.body.rebuild_reasons || [];
        return "APPLIED"; // the package cutoff is deliberately untouched
      });
    });
  }

  function loadDelta(db) {
    return getOne(db, "packages", "delta").then(function (row) {
      if (!row || !state.activeMeta || row.meta.package_id !== state.activeMeta.package_id) {
        return null;
      }
      var previous = state.deltaMeta;
      state.deltaMeta = null;
      return verifyDelta(db, row.raw)
        .then(function (result) {
          state.deltaMeta = row.meta;
          state.deltaBody = result.body;
          state.rebuildRequired = !!result.body.rebuild_required;
          state.rebuildReasons = result.body.rebuild_reasons || [];
        })
        .catch(function () {
          state.deltaMeta = previous;
          return deleteOne(db, "packages", "delta");
        });
    });
  }

  // -------------------------------------------------------------------
  // Offline verification of SIGNED QR passes (Phase 4 Prompt 3)
  // -------------------------------------------------------------------
  //
  // Only the active package -- verified at activation and again at every
  // start-up (signature under a PINNED key, device and scope binding,
  // schema, expiry, integrity, allow-list) -- is ever consulted, and only
  // by the pass's signed `jti`. There is no other offline lookup: no
  // identity number, passport, registration reference, name, e-mail or
  // phone search exists in this runtime.

  var QR_KID = /^v[1-9][0-9]{0,3}$/;
  var OPAQUE_ID = /^[A-Za-z0-9_-]{22}$/;
  var QR_NONCE = /^[A-Za-z0-9_-]{16}$/;
  var CODE_TEXT = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
  var QR_CLAIMS = ["apc", "bai", "btc", "cv", "eid", "exp", "jti", "n", "nbf", "pid", "v"];
  // Codes that mean "this is not a credential we can trust" -- collapsed
  // into ONE result, exactly as the online verifier does.
  var UNTRUSTED_CODES = ["MALFORMED", "UNKNOWN_KEY", "INVALID_KEY", "INVALID_SIGNATURE", "CREDENTIAL_MISMATCH"];
  var RESULT_SEVERITY = ["DENIED", "STALE", "MANUAL_REVIEW"];
  var ADMITTABLE = ["ALLOWED", "ALLOWED_WITH_ADVISORY"];
  var EMPTY_DELTA = { passes: {}, restrictions: {}, withdrawn: {}, profiles: {}, kids: {} };

  function buildIndex(body) {
    var entries = new Map();
    var listed = new Map();
    body.entries.forEach(function (entry) {
      entries.set(entry.jti, entry);
    });
    body.revoked_passes.forEach(function (item) {
      listed.set(item.jti, item.status);
    });
    var keys = {};
    body.qr_keys.forEach(function (key) {
      keys[key.kid] = key;
    });
    var reasons = body.override_reasons.slice();
    return { entries: entries, listed: listed, keys: keys, reasons: reasons };
  }

  function buildDeltaIndex(delta) {
    if (!delta) return EMPTY_DELTA;
    var view = { passes: {}, restrictions: {}, withdrawn: {}, profiles: {}, kids: {} };
    delta.passes.forEach(function (item) {
      view.passes[item.jti] = item.status;
    });
    delta.restrictions.forEach(function (item) {
      view.restrictions[item.jti] = item.restrictions;
    });
    delta.access_withdrawn.forEach(function (item) {
      view.withdrawn[item.jti] = item.reason;
    });
    delta.withdrawn_access_profiles.forEach(function (code) {
      view.profiles[code] = true;
    });
    delta.revoked_kids.forEach(function (kid) {
      view.kids[kid] = true;
    });
    return view;
  }

  function currentIndex() {
    if (!state.body) return null;
    if (!state.index || state.index.packageId !== state.body.package_id) {
      state.index = buildIndex(state.body);
      state.index.packageId = state.body.package_id;
      state.qrKeys = {};
    }
    return state.index;
  }

  function currentDeltaIndex() {
    if (!state.deltaBody) return EMPTY_DELTA;
    if (!state.deltaIndex || state.deltaIndex.source !== state.deltaBody) {
      state.deltaIndex = buildDeltaIndex(state.deltaBody);
      state.deltaIndex.source = state.deltaBody;
    }
    return state.deltaIndex;
  }

  function inWindow(window_, now) {
    return (window_.from === null || now >= window_.from) && (window_.until === null || now < window_.until);
  }

  function qrKeyUsable(key, now) {
    if (key.status !== "ACTIVE" && key.status !== "RETIRED") return false;
    if (currentDeltaIndex().kids[key.kid]) return false; // revoked by a critical delta
    if (key.not_before !== null && now < key.not_before) return false;
    if (key.not_after !== null && now >= key.not_after) return false;
    return true;
  }

  function importQrKey(key) {
    if (!state.qrKeys[key.kid]) {
      state.qrKeys[key.kid] = crypto.subtle.importKey(
        "spki",
        unb64u(key.spki),
        { name: "ECDSA", namedCurve: "P-256" },
        false,
        ["verify"]
      );
    }
    return state.qrKeys[key.kid];
  }

  function validClaims(claims) {
    if (!exactKeys(claims, QR_CLAIMS)) return false;
    function code(value, max) {
      return typeof value === "string" && value.length <= max && CODE_TEXT.test(value);
    }
    function opaque(value) {
      return typeof value === "string" && OPAQUE_ID.test(value);
    }
    function integer(value) {
      return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
    }
    return (
      code(claims.apc, 64) &&
      code(claims.btc, 64) &&
      code(claims.eid, 32) &&
      opaque(claims.bai) &&
      opaque(claims.jti) &&
      opaque(claims.pid) &&
      typeof claims.n === "string" &&
      QR_NONCE.test(claims.n) &&
      integer(claims.v) &&
      integer(claims.cv) &&
      integer(claims.nbf) &&
      integer(claims.exp) &&
      claims.exp > claims.nbf
    );
  }

  /** The QR contract (docs/security/qr_contract.md §5), tier 1-2, against the
   *  package: structure, canonical header, key resolved BEFORE verifying,
   *  ES256 (algorithm from the stored key, never the header), payload,
   *  version, event. Resolves {code, claims?, kid?}. Never throws. */
  function checkQr(raw, body, now) {
    function fail(code, extra) {
      return Promise.resolve(Object.assign({ code: code }, extra || {}));
    }
    if (typeof raw !== "string") return fail("MALFORMED");
    var token = raw.trim();
    if (utf8(token).length > CONFIG.qr.max_bytes) return fail("MALFORMED");
    var parts = token.split(".");
    if (parts.length !== 3 || !parts[0] || !parts[1] || !parts[2]) return fail("MALFORMED");
    var headerBytes, payloadBytes, signature, headerText, header;
    try {
      headerBytes = unb64u(parts[0]);
      payloadBytes = unb64u(parts[1]);
      signature = unb64u(parts[2]);
      headerText = fromUtf8(headerBytes);
      header = JSON.parse(headerText);
    } catch (e) {
      return fail("MALFORMED");
    }
    if (signature.length !== 64) return fail("MALFORMED");
    if (
      !exactKeys(header, ["alg", "kid", "typ"]) ||
      header.alg !== "ES256" ||
      header.typ !== "ASC-PASS" ||
      typeof header.kid !== "string" ||
      header.kid.length > 8 ||
      !QR_KID.test(header.kid)
    ) {
      return fail("MALFORMED");
    }
    try {
      if (canonical(header) !== headerText) return fail("MALFORMED"); // duplicates, whitespace
    } catch (e) {
      return fail("MALFORMED");
    }
    var key = currentIndex().keys[header.kid];
    if (!key) return fail("UNKNOWN_KEY");
    if (!qrKeyUsable(key, now)) return fail("INVALID_KEY", { kid: header.kid });
    return importQrKey(key)
      .then(function (cryptoKey) {
        return crypto.subtle.verify(
          { name: "ECDSA", hash: "SHA-256" },
          cryptoKey,
          signature,
          utf8(parts[0] + "." + parts[1])
        );
      })
      .then(
        function (ok) {
          if (!ok) return { code: "INVALID_SIGNATURE", kid: header.kid };
          var claims;
          var payloadText;
          try {
            payloadText = fromUtf8(payloadBytes);
            claims = JSON.parse(payloadText);
            if (!validClaims(claims) || canonical(claims) !== payloadText) return { code: "MALFORMED" };
          } catch (e) {
            return { code: "MALFORMED" };
          }
          if (CONFIG.qr.supported_versions.indexOf(claims.v) === -1) {
            return { code: "UNSUPPORTED_VERSION", kid: header.kid };
          }
          if (claims.eid !== body.event) return { code: "WRONG_EVENT", claims: claims, kid: header.kid };
          return { code: "SIGNED", claims: claims, kid: header.kid };
        },
        function () {
          return { code: "INVALID_SIGNATURE", kid: header.kid };
        }
      );
  }

  function blocker(result, reason) {
    return { result: result, reason: reason };
  }

  function statusBlocker(status) {
    return (
      {
        REVOKED: blocker("DENIED", "PASS_REVOKED"),
        REPLACED: blocker("STALE", "PASS_REPLACED"),
        SUSPENDED: blocker("DENIED", "PASS_SUSPENDED"),
        INACTIVE: blocker("MANUAL_REVIEW", "PASS_INACTIVE"),
        EXPIRED: blocker("DENIED", "PASS_EXPIRED"),
      }[status] || null
    );
  }

  /** Primary result = the most severe blocker (DENIED, then STALE, then
   *  MANUAL_REVIEW), exactly as `apps.entry.services.access._finish`. */
  function finishLocal(blockers, advisories) {
    if (blockers.length) {
      var primary = null;
      RESULT_SEVERITY.some(function (severity) {
        primary = blockers.filter(function (b) {
          return b.result === severity;
        })[0];
        return !!primary;
      });
      return { result: primary.result, reason: primary.reason, blockers: blockers, advisories: advisories };
    }
    return {
      result: advisories.length ? "ALLOWED_WITH_ADVISORY" : "ALLOWED",
      reason: advisories.length ? advisories[0] : "",
      blockers: [],
      advisories: advisories,
    };
  }

  /**
   * The admission evaluator (apps.entry.services.access.assess_context,
   * steps 2-9; step 1 -- the event -- is the signed `eid`), on package data
   * merged with the last accepted critical delta. Pure.
   */
  function evaluateEntry(entry, listed, now, zone) {
    var blockers = [];
    var advisories = [];
    var delta = currentDeltaIndex();
    var withdrawn = delta.withdrawn[entry.jti] || "";
    // 2. Registration no longer approved (critical delta).
    if (withdrawn === "REGISTRATION_NOT_APPROVED") {
      return Object.assign(finishLocal([blocker("DENIED", "REGISTRATION_NOT_APPROVED")], []), {
        restrictionBlocksOverride: false,
        prior: null,
      });
    }
    // 3. Restrictions in force now: package set merged MONOTONICALLY with the delta.
    var restrictions = (entry.restrictions || [])
      .concat(delta.restrictions[entry.jti] || [])
      .filter(function (r) {
        return inWindow({ from: r.from, until: r.until }, now);
      });
    if (restrictions.some(function (r) {
      return r.severity === "DENY_ENTRY";
    })) {
      blockers.push(blocker("DENIED", "SECURITY_RESTRICTION"));
    }
    var restrictionBlocksOverride = restrictions.some(function (r) {
      return !r.overrideable;
    });
    // 4. Pass status (package + delta) and validity window (same skew as online).
    var problem = statusBlocker(listed);
    if (!problem) {
      var skew = CONFIG.qr.clock_skew_seconds;
      if (now < entry.valid_from - skew) problem = blocker("DENIED", "PASS_NOT_YET_VALID");
      else if (now >= entry.valid_until + skew) problem = blocker("DENIED", "PASS_EXPIRED");
    }
    if (problem) blockers.push(problem);
    // 5. Assignments.
    if (entry.assignment_until !== null && now >= entry.assignment_until) {
      blockers.push(blocker("DENIED", "NO_ACCESS_ASSIGNMENT"));
    }
    if (withdrawn === "ASSIGNMENT_CHANGED") blockers.push(blocker("STALE", "ASSIGNMENT_CHANGED"));
    else if (withdrawn === "PASS_CHANGED" || withdrawn === "ACCESS_RULE_CHANGED") {
      blockers.push(blocker("MANUAL_REVIEW", "OFFLINE_DATA_CHANGED"));
    }
    // 6. Access Profile (withdrawn by a delta, or outside its window).
    if (delta.profiles[entry.apc]) blockers.push(blocker("MANUAL_REVIEW", "OFFLINE_DATA_CHANGED"));
    else if (!inWindow(entry.profile_window, now)) blockers.push(blocker("DENIED", "OUTSIDE_TIME_WINDOW"));
    // 7. Rules at THIS zone (the grant's checkpoint), incl. gate-specific ones.
    var zoneRules = entry.zone_rules.filter(function (z) {
      return z.zone === zone;
    })[0];
    var rules = zoneRules
      ? zoneRules.rules.filter(function (rule) {
          return inWindow(rule, now);
        })
      : [];
    if (rules.some(function (rule) {
      return rule.effect === "DENY";
    })) {
      blockers.push(blocker("DENIED", "ACCESS_RULE_DENY"));
    } else if (!rules.some(function (rule) {
      return rule.effect === "ALLOW";
    })) {
      blockers.push(blocker("DENIED", "WRONG_ZONE"));
    }
    // 8. Prior admission: the package's, or this device's own offline one.
    var prior = Math.max(entry.last_admitted_at || 0, state.localAdmissions[entry.jti] || 0) || null;
    if (prior) {
      if (entry.reentry === "SINGLE_ENTRY") blockers.push(blocker("DENIED", "ALREADY_ADMITTED"));
      else {
        advisories.push("PRIOR_ENTRY");
        if (now - prior <= CONFIG.recent_reentry_seconds) advisories.push("RECENT_REENTRY");
      }
    }
    // 9. Review-level restriction.
    if (restrictions.some(function (r) {
      return r.severity === "MANUAL_REVIEW";
    })) {
      blockers.push(blocker("MANUAL_REVIEW", "RESTRICTION_REVIEW"));
    }
    return Object.assign(finishLocal(blockers, advisories), {
      restrictionBlocksOverride: restrictionBlocksOverride,
      prior: prior,
    });
  }

  function listedStatus(jti) {
    return currentDeltaIndex().passes[jti] || currentIndex().listed.get(jti) || null;
  }

  function snapshotMatches(entry, claims) {
    return (
      entry.cv === claims.cv &&
      entry.pid === claims.pid &&
      entry.bai === claims.bai &&
      entry.apc === claims.apc &&
      entry.btc === claims.btc &&
      entry.valid_from === claims.nbf &&
      entry.valid_until === claims.exp
    );
  }

  /**
   * Verify one scanned value offline. Resolves a verification object:
   * {type: ENTRY_DECISION|VERIFICATION_ATTEMPT, code, result, reason,
   *  blockers, advisories, entry (display only), credential {jti, kid},
   *  prior, restrictionBlocksOverride, state, latency_ms}.
   * Every failure fails closed; a local miss routes to Manual Review.
   */
  function verifyOfflineQr(raw, opts) {
    var started = performance.now();
    var body = state.body;
    var now = nowSeconds();
    var grant = activeGrant();
    var zone = grant ? grant.zone : null;
    var stateName = (opts && opts.state) || computeState(currentContext()).state;
    var authority = verificationAuthority();
    if (!body || !permitted(stateName, "VERIFY_OFFLINE_QR")) {
      return Promise.reject(codedError("STATE_CHANGED"));
    }
    return checkQr(raw, body, now).then(function (checked) {
      var v = {
        code: checked.code,
        credential: null,
        entry: null,
        prior: null,
        restrictionBlocksOverride: false,
        state: stateName,
        type: "VERIFICATION_ATTEMPT",
        referral: false,
        authority: authority,
      };
      if (UNTRUSTED_CODES.indexOf(checked.code) !== -1) {
        Object.assign(v, finishLocal([], []), { result: "DENIED", reason: "INVALID_CREDENTIAL" });
      } else if (checked.code === "UNSUPPORTED_VERSION") {
        Object.assign(v, finishLocal([], []), { result: "UNSUPPORTED", reason: "UNSUPPORTED_CREDENTIAL" });
      } else if (checked.code === "WRONG_EVENT") {
        v.credential = { jti: checked.claims.jti, kid: checked.kid };
        Object.assign(v, finishLocal([blocker("DENIED", "WRONG_EVENT")], []));
      } else {
        var claims = checked.claims;
        var entry = currentIndex().entries.get(claims.jti) || null;
        var listed = listedStatus(claims.jti);
        v.credential = { jti: claims.jti, kid: checked.kid };
        if (entry && !snapshotMatches(entry, claims)) {
          v.code = "CREDENTIAL_MISMATCH";
          v.credential = null;
          Object.assign(v, finishLocal([], []), { result: "DENIED", reason: "INVALID_CREDENTIAL" });
        } else if (entry) {
          var evaluation = evaluateEntry(entry, listed, now, zone);
          v.entry = entry;
          v.type = "ENTRY_DECISION";
          v.prior = evaluation.prior;
          v.restrictionBlocksOverride = evaluation.restrictionBlocksOverride;
          Object.assign(v, evaluation);
          v.code = listed || windowCode(entry, now) || "VALID";
        } else if (listed) {
          v.type = "ENTRY_DECISION";
          v.code = listed;
          Object.assign(v, finishLocal([statusBlocker(listed)], []));
        } else {
          v.code = "NOT_IN_PACKAGE";
          v.referral = true; // a local miss is not proof of anything: Manual Review
          Object.assign(v, finishLocal([blocker("MANUAL_REVIEW", "OFFLINE_LOCAL_MISS")], []));
        }
      }
      // Stale offline data: never present an admittable result (UI/UX §9.7).
      if (stateName === "STALE" && ADMITTABLE.indexOf(v.result) !== -1) {
        v.blockers = v.blockers.concat([blocker("MANUAL_REVIEW", "OFFLINE_DATA_STALE")]);
        v.result = "MANUAL_REVIEW";
        v.reason = "OFFLINE_DATA_STALE";
      }
      v.latency_ms = Math.max(0, Math.round(performance.now() - started));
      return v;
    });
  }

  function windowCode(entry, now) {
    var skew = CONFIG.qr.clock_skew_seconds;
    if (now < entry.valid_from - skew) return "NOT_YET_VALID";
    if (now >= entry.valid_until + skew) return "EXPIRED";
    return "";
  }

  function isAdmittable(v) {
    return ADMITTABLE.indexOf(v.result) !== -1;
  }

  function blockerCodes(v) {
    return (v.blockers || []).map(function (b) {
      return b.reason;
    });
  }

  /** Overrides are possible IN PRINCIPLE only (the fixed exclusions of
   *  `Assessment.may_be_overridden`); the catalogue must cover every blocker. */
  function overrideReasonsFor(v) {
    if (isAdmittable(v) || !v.blockers || !v.blockers.length || v.restrictionBlocksOverride) return [];
    var codes = blockerCodes(v);
    var never = CONFIG.never_overrideable || [];
    if (codes.some(function (code) {
      return never.indexOf(code) !== -1;
    })) {
      return [];
    }
    return currentIndex().reasons.filter(function (reason) {
      return codes.every(function (code) {
        return reason.overridable_reason_codes.indexOf(code) !== -1;
      });
    });
  }

  // -------------------------------------------------------------------
  // Recording operations (durable BEFORE any "recorded" message)
  // -------------------------------------------------------------------

  function uuid4() {
    if (crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var h = toHex(b);
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function draftOperation(v, decision, decisionReason, override) {
    var grant = activeGrant();
    var meta = state.activeMeta;
    var delta = state.deltaMeta;
    var now = nowSeconds();
    return {
      schema_version: CONFIG.schema.operation,
      operation_id: uuid4(),
      type: v.type,
      device: state.device.public_id,
      event: state.body.event,
      occurred_at: now,
      device_time: Math.floor(Date.now() / 1000),
      server_offset: Math.round(state.serverOffsetSeconds),
      state: v.state,
      package: {
        id: meta.package_id,
        version: meta.package_version,
        delta_version: delta ? delta.delta_version : null,
        band: bandAt(now, meta),
        data_cutoff_at: meta.data_cutoff_at,
        critical_delta_cutoff_at: delta ? delta.critical_delta_cutoff_at : meta.data_cutoff_at,
      },
      grant: grant ? grant.grant_id : null,
      gate: state.body.gate,
      zone: grant ? grant.zone : null,
      credential: v.credential,
      local: {
        code: v.code,
        result: v.result,
        reason: v.reason || "",
        blockers: blockerCodes(v),
        advisories: (v.advisories || []).slice(),
        latency_ms: Math.min(v.latency_ms || 0, 600000),
      },
      decision: decision,
      decision_reason: decisionReason || "",
      override: override || null,
    };
  }

  function recordOperation(db, draft, note, guard) {
    return appendOperation(db, draft, note, guard).then(function (appended) {
      if (draft.decision === "ADMIT" && draft.credential) {
        var previous = state.localAdmissions[draft.credential.jti] || 0;
        state.localAdmissions[draft.credential.jti] = Math.max(previous, draft.occurred_at);
      }
      return refreshCounts(db).then(function () {
        return appended;
      });
    });
  }

  function noteDigest(operationId, note) {
    return sha256Hex(utf8(operationId + "\n" + note));
  }

  // -------------------------------------------------------------------
  // Device API
  // -------------------------------------------------------------------

  function csrfToken() {
    var name = CONFIG.csrf_cookie + "=";
    var parts = doc.cookie ? doc.cookie.split("; ") : [];
    for (var i = 0; i < parts.length; i += 1) {
      if (parts[i].indexOf(name) === 0) return decodeURIComponent(parts[i].slice(name.length));
    }
    return "";
  }

  // Signed calls share ONE single-use server nonce. They run one at a time,
  // and a call that finds no nonce first fetches one with an unsigned
  // heartbeat -- so a concurrent heartbeat, build poll or report can never
  // leave another call without its proof.
  var signedQueue = Promise.resolve();

  function withSignedSlot(work) {
    var run = signedQueue.then(work, work);
    signedQueue = run.then(
      function () {
        return null;
      },
      function () {
        return null;
      }
    );
    return run;
  }

  function freshNonce(db) {
    if (state.nonce) return Promise.resolve();
    return send(db, CONFIG.api.heartbeat, { report: null }, "").then(function (response) {
      if (!state.nonce && response.json && response.json.nonce) state.nonce = response.json.nonce;
    });
  }

  function api(db, url, payload, purpose) {
    if (!purpose) return send(db, url, payload, "");
    return withSignedSlot(function () {
      return freshNonce(db).then(function () {
        return send(db, url, payload, purpose);
      });
    });
  }

  function send(db, url, payload, purpose) {
    var body = JSON.stringify(payload || {});
    var headers = { "Content-Type": "application/json", "X-CSRFToken": csrfToken() };
    var signing = Promise.resolve(null);
    if (purpose) {
      signing = getOne(db, "keys", "device-sign").then(function (row) {
        if (!row || !state.nonce) return null;
        var nonce = state.nonce;
        state.nonce = null; // single use
        return proofMessage(purpose, nonce, body).then(function (message) {
          return signWith(row.privateKey, message).then(function (signature) {
            headers["X-ASC-Device-Nonce"] = nonce;
            headers["X-ASC-Device-Signature"] = signature;
          });
        });
      });
    }
    return signing.then(function () {
      var controller = window.AbortController ? new AbortController() : null;
      var timer = setTimeout(function () {
        if (controller) controller.abort();
      }, 8000);
      return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "manual",
        headers: headers,
        body: body,
        signal: controller ? controller.signal : undefined,
      }).then(
        function (response) {
          clearTimeout(timer);
          var next = response.headers.get("X-ASC-Next-Nonce");
          if (next) state.nonce = next;
          return response.text().then(function (text) {
            var json = null;
            try {
              json = text ? JSON.parse(text) : null;
            } catch (e) {
              json = null;
            }
            return { status: response.status, text: text, json: json, ok: response.ok };
          });
        },
        function () {
          clearTimeout(timer);
          throw codedError("NETWORK");
        }
      );
    });
  }

  function errorCode(response) {
    return (response && response.json && response.json.error && response.json.error.code) || "ERROR";
  }

  function report(db) {
    var name = health.mode === "offline" ? "OFFLINE_ACTIVE" : (state.lastState || "ONLINE");
    return {
      state: name,
      pending: state.counts.pending,
      locked: state.counts.locked,
      sequence_low: state.counts.sequenceLow,
      sequence_high: state.counts.sequenceHigh,
      chain_head: state.counts.chainHead,
      package_version: state.activeMeta ? state.activeMeta.package_version : null,
      delta_version: state.deltaMeta ? state.deltaMeta.delta_version : null,
    };
  }

  /** One health check. A structured API answer (even 401) means the server
   *  is reachable; only a network failure, a timeout or a 5xx counts as a
   *  failure (OFF-001). */
  function heartbeat(db) {
    var sentAt = Date.now();
    var sentMono = performance.now();
    var signed = !!state.nonce;
    return api(db, CONFIG.api.heartbeat, { report: signed ? report(db) : null }, signed ? "heartbeat" : "")
      .then(function (response) {
        var body = response.json;
        var validHealth = response.status === 200 && body && Number.isSafeInteger(body.server_time) &&
          body.device && typeof body.device.public_id === "string" && body.offline && Array.isArray(body.directives);
        var structuredDenial = response.status >= 400 && response.status < 500 && body &&
          body.error && typeof body.error.code === "string";
        if (!validHealth && !structuredDenial) {
          recordHealth(false);
          return null;
        }
        recordHealth(true);
        var clockTrusted = validHealth && clockTrustworthyFor(sentAt, sentMono);
        if (clockTrusted) reanchorClock();
        if (response.status === 401) {
          state.notEnrolled = true;
          state.lockDirective = true;
          return purgePackageData(db).then(function () {
            return lockOperations(db, "DEVICE_NOT_AUTHENTICATED");
          });
        }
        if (!response.ok || !response.json) return null;
        var data = response.json;
        state.notEnrolled = false;
        state.nonce = data.nonce || state.nonce;
        if (clockTrusted) {
          state.serverOffsetSeconds = data.server_time - (sentAt + Date.now()) / 2000;
        }
        state.device = data.device;
        state.offline = data.offline;
        // Kept for a start-up without a network: the device's own public
        // identity (non-secret) and the last measured server clock offset.
        putOne(db, "meta", {
          name: "device",
          device: data.device,
          offset: state.serverOffsetSeconds,
          at: Date.now(),
        }).catch(function () {
          return null;
        });
        return applyDirectives(db, data.directives || []);
      })
      .catch(function () {
        recordHealth(false);
        return null;
      });
  }

  function applyDirectives(db, directives) {
    var block = false;
    var reason = "";
    var chain = Promise.resolve();
    state.lockDirective = directives.some(function (directive) {
      return directive.type === "LOCK_OPERATIONS";
    });
    directives.forEach(function (directive) {
      if (directive.type === "BLOCK") {
        block = true;
        reason = directive.reason;
      } else if (directive.type === "PURGE_PACKAGE") {
        chain = chain.then(function () {
          return purgePackageData(db);
        });
      } else if (directive.type === "LOCK_OPERATIONS") {
        chain = chain.then(function () {
          return lockOperations(db, directive.reason);
        });
      } else if (directive.type === "REVOKE_GRANTS") {
        chain = chain.then(function () {
          return removeGrants(db, directive.grants || []);
        });
      } else if (directive.type === "EMERGENCY_WIPE") {
        chain = chain.then(function () {
          return executeEmergencyWipe(db, directive.order);
        });
      }
    });
    state.directives = { block: block, blockReason: reason };
    return chain;
  }

  // -------------------------------------------------------------------
  // Synchronization (Phase 4 Prompt 3): ordered, bounded, idempotent
  // -------------------------------------------------------------------

  /** Upload is allowed only while the APPLICATION health check is stable:
   *  never while offline, never right after a failed check. */
  function syncAllowed() {
    return health.mode !== "offline" && !health.unstable && !state.storageError && state.wipe.status === "NONE";
  }

  function lockIfAvailable(name, work) {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request(name, { ifAvailable: true }, function (lock) {
        if (!lock) return "BUSY"; // another tab or loop is uploading: never in parallel
        return work();
      });
    }
    return Promise.reject(codedError("STORAGE_UNAVAILABLE"));
  }

  function scheduleRetry() {
    var S = CONFIG.sync;
    state.sync.failures += 1;
    var wait = Math.min(S.retry_max_seconds, S.retry_base_seconds * Math.pow(2, state.sync.failures - 1));
    var jitter = 0.5 + Math.random() * 0.5;
    state.sync.nextAt = Date.now() + Math.round(wait * jitter * 1000);
  }

  function markUploading(db, seqs) {
    return withLock("asc-entry-opstore", function () {
      return tx(db, ["ops"], "readwrite", function (s) {
        seqs.forEach(function (seq) {
          s.ops.get(seq).onsuccess = function (event) {
            var row = event.target.result;
            if (!row) return;
            if (row.state === "PENDING") row.state = "IN_FLIGHT";
            else if (row.state === "LOCKED") row.in_flight = true;
            else return;
            s.ops.put(row);
          };
        });
      });
    });
  }

  function settleUploads(db, outcomes) {
    // outcomes: seq -> {outcome} for durable acks; missing seq = back to pending.
    return withLock("asc-entry-opstore", function () {
      return tx(db, ["ops"], "readwrite", function (s) {
        Object.keys(outcomes.touched).forEach(function (key) {
          var seq = Number(key);
          s.ops.get(seq).onsuccess = function (event) {
            var row = event.target.result;
            if (!row) return;
            var durable = outcomes.durable[seq];
            if (durable) {
              row.state = "ACKNOWLEDGED";
              row.in_flight = false;
              row.outcome = durable;
              row.acknowledged_at = Date.now();
            } else if (row.state === "IN_FLIGHT") {
              row.state = "PENDING";
            } else {
              row.in_flight = false;
            }
            s.ops.put(row);
          };
        });
      });
    });
  }

  /** A durable acknowledgement is accepted ONLY when its signature verifies
   *  under a PINNED key and it names exactly this device, store, operation,
   *  sequence and operation bytes, with a durable status. */
  function verifyAck(ackText, record, anchors, store) {
    if (typeof ackText !== "string" || !ackText) return Promise.resolve(null);
    return verifyJws(ackText, "ASC-OACK", anchors)
      .then(function (payload) {
        if (!exactKeys(payload, CONFIG.allow.ack)) return null;
        if (payload.typ !== "ASC-OACK" || payload.schema_version !== CONFIG.schema.ack) return null;
        if (!state.device || payload.device !== state.device.public_id || payload.store !== store) return null;
        if (payload.operation_id !== record.operation_id || payload.sequence !== record.seq) return null;
        if ((CONFIG.sync.durable_statuses || []).indexOf(payload.status) === -1) return null;
        return sha256Hex(utf8(record.text)).then(function (digest) {
          if (digest !== payload.payload_hash) return null;
          return {
            ack: ackText,
            status: payload.status,
            outcome: payload.outcome,
            conflict_type: payload.conflict_type,
            processed_at: payload.processed_at,
          };
        });
      })
      .catch(function () {
        return null;
      });
  }

  function uploadBatch(db) {
    return Promise.all([getAll(db, "ops"), getOne(db, "meta", "chain"), trustAnchors(db)]).then(function (found) {
      var rows = found[0];
      var chain = found[1];
      var anchors = found[2];
      var store = chain && chain.store;
      var candidates = rows
        .filter(function (row) {
          return row.state === "PENDING" || (row.state === "LOCKED" && !row.in_flight);
        })
        .sort(function (a, b) {
          return a.seq - b.seq;
        });
      if (!candidates.length || !store || !anchors.length) return "IDLE";
      var S = CONFIG.sync;
      var batch = [];
      var bytes = 0;
      return candidates
        .slice(0, S.batch_size)
        .reduce(function (chainPromise, row) {
          return chainPromise.then(function (stop) {
            if (stop) return true;
            return openLocal(db, row.sealed).then(function (value) {
              var envelope = { operation_id: row.operation_id, sequence: row.seq, op: value.op, signature: value.signature };
              if (value.note) envelope.note = value.note;
              var size = utf8(JSON.stringify(envelope)).length;
              if (batch.length && bytes + size > S.batch_bytes) return true; // bounded batch
              bytes += size;
              batch.push({ row: row, envelope: envelope, text: value.op });
              return false;
            });
          });
        }, Promise.resolve(false))
        .then(function () {
          if (!batch.length) return "IDLE";
          var seqs = batch.map(function (item) {
            return item.row.seq;
          });
          var payload = { store: store, operations: batch.map(function (item) {
            return item.envelope;
          }) };
          var request;
          if (state.notEnrolled) {
            // Revoked device: the quarantine boundary, authenticated by the
            // operations' own signatures (never applied by the server).
            payload.device = state.device ? state.device.public_id : "";
            request = function () {
              return send(db, CONFIG.api["sync-quarantine"], payload, "");
            };
          } else {
            request = function () {
              return api(db, CONFIG.api.sync, payload, "sync");
            };
          }
          state.sync.lastAttemptAt = Date.now();
          return markUploading(db, seqs)
            .then(request)
            .then(
              function (response) {
                var touched = {};
                seqs.forEach(function (seq) {
                  touched[seq] = true;
                });
                if (response.status !== 200 || !response.json || !Array.isArray(response.json.acknowledgements)) {
                  return settleUploads(db, { touched: touched, durable: {} }).then(function () {
                    throw codedError(errorCode(response));
                  });
                }
                var acks = response.json.acknowledgements;
                var durable = {};
                return batch
                  .reduce(function (p, item) {
                    return p.then(function () {
                      var ack = acks.filter(function (a) {
                        return a && a.operation_id === item.row.operation_id && a.sequence === item.row.seq;
                      })[0];
                      if (!ack || !ack.durable) return null;
                      return verifyAck(ack.ack, { operation_id: item.row.operation_id, seq: item.row.seq, text: item.text }, anchors, store).then(
                        function (outcome) {
                          if (outcome) durable[item.row.seq] = outcome;
                        }
                      );
                    });
                  }, Promise.resolve())
                  .then(function () {
                    return settleUploads(db, { touched: touched, durable: durable });
                  })
                  .then(function () {
                    var accepted = Object.keys(durable).length;
                    if (accepted) {
                      state.sync.lastSuccessAt = Date.now();
                      state.sync.failures = 0;
                      state.sync.nextAt = 0;
                      state.sync.error = "";
                      putOne(db, "meta", { name: "sync", last_success_at: state.sync.lastSuccessAt }).catch(function () {
                        return null;
                      });
                    }
                    // A batch that did not durably settle every record stops
                    // here; the next attempt resends from the first pending one.
                    return accepted === batch.length ? "PROGRESS" : "PARTIAL";
                  });
              },
              function (error) {
                var touched = {};
                seqs.forEach(function (seq) {
                  touched[seq] = true;
                });
                return settleUploads(db, { touched: touched, durable: {} }).then(function () {
                  throw error && error.code ? error : codedError("NETWORK");
                });
              }
            );
        });
    });
  }

  /** Upload everything unacknowledged, in order, one batch at a time, in ONE
   *  place across every tab of the app. Resolves "IDLE", "DONE", "BUSY",
   *  "WAIT" (backing off) or rejects with the failure. */
  function syncNow(db, opts) {
    if (!syncAllowed()) return Promise.resolve("WAIT");
    if (!(opts && opts.force) && Date.now() < state.sync.nextAt) return Promise.resolve("WAIT");
    return lockIfAvailable("asc-entry-sync", function () {
      var rounds = 0;
      function next() {
        if (!syncAllowed() || rounds >= 20) return Promise.resolve("DONE");
        rounds += 1;
        return uploadBatch(db).then(function (outcome) {
          if (outcome === "PROGRESS") return next();
          if (outcome === "PARTIAL") {
            scheduleRetry();
            return "WAIT";
          }
          return rounds > 1 ? "DONE" : "IDLE";
        });
      }
      return next().then(
        function (outcome) {
          return refreshCounts(db)
            .then(function () {
              return maybeUnlockStore(db);
            })
            .then(function () {
              return outcome;
            });
        },
        function (error) {
          state.sync.error = (error && error.code) || "NETWORK";
          scheduleRetry();
          return refreshCounts(db).then(function () {
            throw error;
          });
        }
      );
    });
  }

  // -------------------------------------------------------------------
  // Emergency wipe: only on a SIGNED order verified against pinned keys
  // -------------------------------------------------------------------

  function executeEmergencyWipe(db, orderCompact) {
    if (state.wipe.status !== "NONE") return Promise.resolve(null); // one wipe at a time
    if (state.wipe.handling) return state.wipe.handling; // this order is already being handled
    var done = function () {
      state.wipe.handling = null;
      return null;
    };
    state.wipe.handling = handleWipeOrder(db, orderCompact).then(done, done);
    return state.wipe.handling;
  }

  function handleWipeOrder(db, orderCompact) {
    return trustAnchors(db)
      .then(function (anchors) {
        if (!anchors.length) return null; // never prepared: nothing verifiable to wipe for
        return verifyJws(orderCompact, "ASC-OWIPE", anchors).then(function (order) {
          if (!state.device || order.device !== state.device.public_id) {
            throw codedError("BINDING");
          }
          return opCounts(db).then(function (counts) {
            // Record the evidence-loss scope wherever technically possible.
            return api(
              db,
              CONFIG.api["wipe-report"],
              {
                order_id: order.order_id,
                pending: counts.pending,
                locked: counts.locked,
                sequence_low: counts.sequenceLow,
                sequence_high: counts.sequenceHigh,
                chain_head: counts.chainHead,
              },
              "wipe-report"
            )
              .catch(function () {
                return null; // the order itself is the authority for the loss
              })
              .then(function () {
                state.wipe.reportedOrder = order.order_id;
                // Not awaited: a deletion blocked by another tab must not
                // stall the heartbeat; the loop follows it in wipe mode.
                destroyLocalData(db).catch(function () {
                  return null;
                });
                return null;
              });
          });
        });
      })
      .catch(function () {
        return null; // an unverifiable order destroys nothing
      });
  }

  /** Resolves ONLY on deleteDatabase().onsuccess. `onblocked` means another
   *  open connection holds the database: the request stays queued and
   *  succeeds once that connection closes, and until then nothing is
   *  reported as wiped. `onerror` rejects (the loop retries). */
  function deleteDatabase() {
    return new Promise(function (resolve, reject) {
      var request;
      try {
        request = indexedDB.deleteDatabase(DB_NAME);
      } catch (e) {
        reject(codedError("WIPE_FAILED"));
        return;
      }
      request.onsuccess = function () {
        resolve();
      };
      request.onerror = function () {
        reject(codedError("WIPE_FAILED"));
      };
      request.onblocked = function () {
        state.wipe.status = "BLOCKED";
        broadcast("wipe"); // ask the other tabs of this app to close again
        notify(t("msg.wipe_blocked"), "danger");
        render(null);
      };
    });
  }

  /** Unregister ONLY this application's worker (scope exactly /entry/);
   *  any other registration on the origin is left alone. */
  function unregisterOwnWorker() {
    if (!navigator.serviceWorker || !navigator.serviceWorker.getRegistrations) return Promise.resolve();
    var scope = new URL(CONFIG.scope, window.location.href).href;
    return navigator.serviceWorker.getRegistrations().then(function (registrations) {
      return Promise.all(
        registrations
          .filter(function (registration) {
            return registration.scope === scope;
          })
          .map(function (registration) {
            return registration.unregister();
          })
      );
    });
  }

  function clearOwnCaches() {
    if (!window.caches) return Promise.resolve();
    return caches.keys().then(function (names) {
      return Promise.all(
        names
          .filter(function (name) {
            return name.indexOf(CACHE_PREFIX) === 0;
          })
          .map(function (name) {
            return caches.delete(name);
          })
      );
    });
  }

  function destroyLocalData(db) {
    if (state.wipe.promise) return state.wipe.promise;
    state.body = null;
    state.activeMeta = null;
    state.grants = [];
    state.wipe.status = "IN_PROGRESS";
    broadcast("wipe"); // other tabs of this app close their connections
    try {
      if (db) db.close();
    } catch (e) {
      /* already closed */
    }
    var promise = deleteDatabase()
      .then(function () {
        // The database is gone. Only now clear this app's caches and worker.
        return Promise.all([
          clearOwnCaches().catch(function () {
            return null;
          }),
          unregisterOwnWorker().catch(function () {
            return null;
          }),
        ]);
      })
      .then(
        function () {
          state.wipe.promise = null;
          state.wipe.status = "WIPED";
          state.wiped = true;
          return "WIPED";
        },
        function (error) {
          state.wipe.promise = null;
          state.wipe.status = "FAILED";
          notify(t("msg.wipe_failed"), "danger");
          render(null);
          throw error;
        }
      );
    state.wipe.promise = promise;
    return promise;
  }

  // -------------------------------------------------------------------
  // Operator grants (issued online only; stored sealed)
  // -------------------------------------------------------------------

  function loadGrants(db) {
    return getOne(db, "meta", "grants").then(function (row) {
      if (!row || !row.sealed) {
        state.grants = [];
        return [];
      }
      return openLocal(db, row.sealed).then(function (grants) {
        state.grants = grants || [];
        return state.grants;
      });
    });
  }

  function saveGrants(db) {
    return sealLocal(db, state.grants).then(function (sealed) {
      return putOne(db, "meta", { name: "grants", sealed: sealed });
    });
  }

  function removeGrants(db, ids) {
    state.grants = state.grants.filter(function (grant) {
      return ids.indexOf(grant.grant_id) === -1;
    });
    return saveGrants(db);
  }

  function requestGrant(db) {
    return api(db, CONFIG.api.grant, {}, "grant").then(function (response) {
      if (response.status !== 201) throw codedError(errorCode(response));
      return trustAnchors(db).then(function (anchors) {
        return verifyJws(response.json.grant, "ASC-OGRANT", anchors).then(function (grant) {
          // Grant schema 2 binds the checkpoint (Prompt 3); anything else is refused.
          if (!exactKeys(grant, CONFIG.allow.grant) || grant.typ !== "ASC-OGRANT") {
            throw codedError("INTEGRITY");
          }
          if (grant.schema_version !== CONFIG.schema.grant) throw codedError("SCHEMA");
          if (!state.device || grant.device !== state.device.public_id) throw codedError("BINDING");
          state.grants = state.grants
            .filter(function (existing) {
              return existing.display_name !== grant.display_name;
            })
            .concat([
              {
                schema: grant.schema_version,
                grant_id: grant.grant_id,
                display_name: grant.display_name,
                permissions: grant.permissions,
                event: grant.event,
                gate: grant.gate,
                zone: grant.zone,
                scope_version: grant.scope_version,
                sensitivity: grant.sensitivity,
                issued_at: grant.issued_at,
                expires_at: grant.expires_at,
                locked: false,
              },
            ]);
          return saveGrants(db).then(function () {
            return grant;
          });
        });
      });
    });
  }

  // -------------------------------------------------------------------
  // Preparation, refresh, self-test
  // -------------------------------------------------------------------

  function ensureNonce(db) {
    if (state.nonce) return Promise.resolve();
    return heartbeat(db);
  }

  function expectations() {
    return {
      device: state.device ? state.device.public_id : "",
      scopeVersion: state.offline ? state.offline.scope_version : null,
    };
  }

  /** Deliberate preparation by a signed-in device administrator. The new
   *  keys are stored ONLY after the server accepted them. */
  function prepare(db) {
    return ensureNonce(db)
      .then(function () {
        return generateDeviceKeys();
      })
      .then(function (keys) {
        return withSignedSlot(function () {
          return freshNonce(db).then(function () {
            return provisionWith(db, keys);
          });
        });
      })
      .then(function () {
        return purgePackageData(db); // packages encrypted to the old key are useless now
      })
      .then(function () {
        return heartbeat(db);
      });
  }

  function provisionWith(db, keys) {
    var payload = { signing_key: keys.signSpki, unwrap_key: keys.unwrapSpki };
    var body = JSON.stringify(payload);
    var nonce = state.nonce;
    state.nonce = null;
    if (!nonce) return Promise.reject(codedError("NONCE_INVALID"));
    return proofMessage("provision", nonce, body)
      .then(function (message) {
        return signWith(keys.sign.privateKey, message);
      })
      .then(function (signature) {
        return fetch(CONFIG.api.provision, {
          method: "POST",
          credentials: "same-origin",
          cache: "no-store",
          redirect: "manual",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken(),
            "X-ASC-Device-Nonce": nonce,
            "X-ASC-Device-Signature": signature,
          },
          body: body,
        });
      })
      .then(function (response) {
        var next = response.headers.get("X-ASC-Next-Nonce");
        if (next) state.nonce = next;
        return response.text().then(function (text) {
          var json = null;
          try {
            json = JSON.parse(text);
          } catch (e) {
            json = null;
          }
          if (response.status !== 201 || !json) {
            throw codedError(errorCode({ json: json }));
          }
          return tx(db, ["keys", "meta"], "readwrite", function (s) {
            s.keys.put({ name: "device-sign", privateKey: keys.sign.privateKey, spki: keys.signSpki });
            s.keys.put({ name: "device-unwrap", privateKey: keys.unwrap.privateKey, spki: keys.unwrapSpki });
            s.meta.put({ name: "trust", anchors: json.trust_anchors, pinned_at: Date.now() });
          });
        });
      });
  }

  function delay(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  /** The server never builds in the request: 202 BUILDING means "ask again
   *  after retry_after_seconds" (bounded by CONFIG.build_poll_limit). */
  function refreshPackage(db, attempt) {
    var tries = attempt || 0;
    var have = state.activeMeta ? state.activeMeta.package_version : null;
    return ensureNonce(db)
      .then(function () {
        return api(db, CONFIG.api.package, { have_version: have }, "package");
      })
      .then(function (response) {
        if (response.status === 202) {
          state.building = true;
          if (tries + 1 >= (CONFIG.build_poll_limit || 30)) {
            state.building = false;
            throw codedError("BUILD_PENDING");
          }
          notify(t("msg.building"), "neutral");
          var wait = (response.json && response.json.retry_after_seconds) || 2;
          return delay(Math.min(Math.max(wait, 1), 30) * 1000).then(function () {
            return refreshPackage(db, tries + 1);
          });
        }
        state.building = false;
        if (response.status === 204) return "UP_TO_DATE";
        if (response.status !== 200) throw codedError(errorCode(response));
        return activatePackage(db, response.text, expectations());
      });
  }

  function refreshDelta(db) {
    if (!state.activeMeta) return Promise.resolve("NO_PACKAGE");
    return ensureNonce(db)
      .then(function () {
        return api(db, CONFIG.api.delta, { package_id: state.activeMeta.package_id }, "delta");
      })
      .then(function (response) {
        if (response.status !== 200) throw codedError(errorCode(response));
        return applyDelta(db, response.text);
      });
  }

  function verifyVector() {
    var vector = CONFIG.vector || {};
    if (!vector.spki || !vector.message || !vector.signature) return Promise.resolve(false);
    return crypto.subtle
      .importKey("spki", unb64u(vector.spki), { name: "ECDSA", namedCurve: "P-256" }, false, [
        "verify",
      ])
      .then(function (key) {
        var signature = unb64u(vector.signature);
        var tampered = signature.slice();
        tampered[10] ^= 0x01;
        return Promise.all([
          crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, signature, utf8(vector.message)),
          crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, key, tampered, utf8(vector.message)),
        ]);
      })
      .then(function (results) {
        return results[0] === true && results[1] === false;
      })
      .catch(function () {
        return false;
      });
  }

  function runSelfTest(db) {
    var checks = {};
    var guard = function (name, promise) {
      return Promise.resolve(promise)
        .then(function (value) {
          checks[name] = value === true;
        })
        .catch(function () {
          checks[name] = false;
        });
    };
    return Promise.all([
      guard("webcrypto_es256", verifyVector()),
      guard(
        "trust_pinned",
        trustAnchors(db).then(function (a) {
          return a.length > 0;
        })
      ),
      guard(
        "package_signature",
        getOne(db, "packages", "active").then(function (row) {
          if (!row) return false;
          return verifyPackage(db, row.raw, expectations()).then(function () {
            checks.package_decrypt = true;
            checks.package_allow_list = true;
            return true;
          });
        })
      ),
      guard(
        "storage_roundtrip",
        putOne(db, "meta", { name: "self-test", value: Date.now() })
          .then(function () {
            return getOne(db, "meta", "self-test");
          })
          .then(function (row) {
            return deleteOne(db, "meta", "self-test").then(function () {
              return !!row;
            });
          })
      ),
      guard(
        "opstore_encryption",
        // A synthetic value through the SAME sealing used by the operation
        // store -- never written to the operation store itself.
        sealLocal(db, { probe: "self-test" }).then(function (sealed) {
          return openLocal(db, sealed).then(function (value) {
            return value.probe === "self-test";
          });
        })
      ),
      guard(
        "storage_quota",
        navigator.storage && navigator.storage.estimate
          ? navigator.storage.estimate().then(function (estimate) {
              return estimate.quota - estimate.usage >= CONFIG.min_free_storage_bytes;
            })
          : false
      ),
      guard(
        "service_worker",
        navigator.serviceWorker
          ? navigator.serviceWorker.getRegistration(CONFIG.scope).then(function (registration) {
              return !!(registration && (registration.active || registration.waiting || registration.installing));
            })
          : false
      ),
      guard(
        "storage_persisted",
        navigator.storage && navigator.storage.persist ? navigator.storage.persist() : false
      ),
    ]).then(function () {
      if (checks.package_signature !== true) {
        checks.package_decrypt = false;
        checks.package_allow_list = false;
      }
      var packageId = state.activeMeta ? state.activeMeta.package_id : "";
      return api(db, CONFIG.api["self-test"], { package_id: packageId, checks: checks }, "self-test").then(
        function (response) {
          if (response.status !== 200) throw codedError(errorCode(response));
          return heartbeat(db).then(function () {
            return { ready: !!response.json.ready, checks: checks };
          });
        }
      );
    });
  }

  // -------------------------------------------------------------------
  // Shell UI (progressive; every string comes from the server catalogue)
  // -------------------------------------------------------------------

  var ui = {};
  var STATE_TONES = {
    ONLINE: ["info", "online"],
    OFFLINE_READY: ["success", "check-circle"],
    OFFLINE_ACTIVE: ["warning", "cloud-off"],
    SYNCING: ["info", "refresh"],
    STALE: ["warning", "alert-triangle"],
    EXPIRED: ["danger", "x-circle"],
    BLOCKED: ["danger", "lock"],
  };

  function duration(seconds) {
    var s = Math.max(0, Math.round(seconds));
    var minutes = Math.floor(s / 60);
    if (minutes < 60) return t("value.minutes", { n: minutes });
    return t("value.hours", { h: Math.floor(minutes / 60), m: minutes % 60 });
  }

  function setField(name, text) {
    var node = doc.querySelector("[data-offline-field='" + name + "']");
    if (node) node.textContent = text === null || text === undefined || text === "" ? t("value.none") : String(text);
  }

  function announce(text) {
    if (ui.announcer) ui.announcer.textContent = text;
  }

  function setBusy(busy) {
    Array.prototype.forEach.call(doc.querySelectorAll("[data-offline-action]"), function (button) {
      button.disabled = busy || button.getAttribute("data-offline-forbidden") === "true";
    });
  }

  function notify(text, tone) {
    if (!ui.message) return;
    ui.message.hidden = !text;
    ui.message.className = "asc-notice asc-notice-" + (tone || "neutral");
    var paragraph = ui.message.querySelector("p");
    if (paragraph) paragraph.textContent = text || "";
    if (text) announce(text);
  }

  // --- Offline verification view (Phase 4 Prompt 3) -------------------

  var RESULT_TONES = {
    ALLOWED: ["success", "check-circle"],
    ALLOWED_WITH_ADVISORY: ["info", "info"],
    MANUAL_REVIEW: ["warning", "user-check"],
    STALE: ["warning", "refresh"],
    DENIED: ["danger", "x-circle"],
    UNSUPPORTED: ["danger", "ban"],
  };
  var VERIFYING_STATES = ["OFFLINE_ACTIVE", "STALE"];
  var clearTimer = null;

  function byId(id) {
    return doc.getElementById(id);
  }

  function show(node, visible) {
    if (node) node.hidden = !visible;
  }

  function alertText(text) {
    var node = byId("offline-alert");
    if (node) node.textContent = text;
  }

  function badgeLabel(entry) {
    var labels = entry.badge_label || {};
    return labels[CONFIG.language] || labels.en || "";
  }

  function formatTime(epoch) {
    if (!epoch) return "";
    try {
      return new Date(epoch * 1000).toLocaleString(CONFIG.language || undefined);
    } catch (e) {
      return new Date(epoch * 1000).toLocaleString();
    }
  }

  function clearResult(noticeKey) {
    state.result = null;
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = null;
    var panel = byId("offline-result");
    if (panel) {
      show(panel, false);
      panel.removeAttribute("data-result");
    }
    ["offline-result-name", "offline-result-badge", "offline-result-reason", "offline-result-prior"].forEach(
      function (id) {
        var node = byId(id);
        if (node) node.textContent = "";
      }
    );
    var list = byId("offline-result-advisories");
    if (list) list.textContent = "";
    if (noticeKey) notify(t(noticeKey), "neutral");
  }

  function armClear() {
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = setTimeout(function () {
      if (state.result) clearResult("msg.result_cleared");
      var field = byId("offline-qr");
      if (field) field.focus();
    }, (CONFIG.result_clear_seconds || 45) * 1000);
  }

  function renderResult(stateName) {
    var panel = byId("offline-result");
    var v = state.result;
    if (!panel) return;
    if (!v) {
      show(panel, false);
      return;
    }
    // A verification belongs to the state it was made in (TRD OFF-002): if
    // the state changed, it is withdrawn and the operator verifies again.
    if (v.state !== stateName) {
      clearResult("msg.result_state_changed");
      return;
    }
    var tone = RESULT_TONES[v.result] || ["neutral", "info"];
    panel.className = "asc-offline-result asc-result asc-result-" + tone[0];
    panel.setAttribute("data-result", v.result);
    panel.setAttribute("data-code", v.code);
    var icon = panel.querySelector("[data-result-icon] use");
    if (icon && ui.iconBase) icon.setAttribute("href", ui.iconBase + "#i-" + tone[1]);
    byId("offline-result-heading").textContent = t("result." + v.result);
    byId("offline-result-reason").textContent = v.reason ? t("reason." + v.reason) : "";
    var person = byId("offline-result-person");
    show(person, !!v.entry);
    if (v.entry) {
      byId("offline-result-name").textContent = v.entry.display_name;
      byId("offline-result-badge").textContent = badgeLabel(v.entry);
    }
    var list = byId("offline-result-advisories");
    list.textContent = "";
    (v.advisories || []).forEach(function (code) {
      var item = doc.createElement("li");
      item.textContent = t("reason." + code);
      list.appendChild(item);
    });
    show(list, !!(v.advisories && v.advisories.length));
    byId("offline-result-prior").textContent = v.prior ? t("msg.prior_admission", { t: formatTime(v.prior) }) : "";
    var meta = state.activeMeta;
    byId("offline-result-meta").textContent = t("msg.offline_verified_meta", {
      v: meta ? meta.package_version : "",
      t: state.sync.lastSuccessAt ? new Date(state.sync.lastSuccessAt).toLocaleString() : t("value.none"),
    });
    renderDecisions(stateName, v);
    show(panel, true);
  }

  function renderDecisions(stateName, v) {
    var recorded = !!v.recorded;
    var grant = activeGrant();
    var canDecide = v.type === "ENTRY_DECISION" && !!grant && !recorded;
    function setButton(decision, allowed) {
      var button = doc.querySelector("[data-offline-decision='" + decision + "']");
      if (!button) return;
      show(button, allowed);
      button.disabled = !allowed;
    }
    setButton("ADMIT", canDecide && permitted(stateName, "ADMIT_OFFLINE") && isAdmittable(v));
    setButton("DO_NOT_ADMIT", canDecide && permitted(stateName, "DO_NOT_ADMIT_OFFLINE"));
    setButton("REDIRECTED", canDecide && permitted(stateName, "MANUAL_REVIEW_OFFLINE"));
    show(byId("offline-dna-reason-wrap"), canDecide && permitted(stateName, "DO_NOT_ADMIT_OFFLINE"));
    var reasons =
      canDecide && permitted(stateName, "OVERRIDE_OFFLINE") && grant.permissions.indexOf("OVERRIDE_OFFLINE") !== -1
        ? overrideReasonsFor(v)
        : [];
    var block = byId("offline-override");
    show(block, reasons.length > 0);
    var select = byId("offline-override-reason");
    if (select && block && !block.hidden) {
      select.textContent = "";
      reasons.forEach(function (reason) {
        var option = doc.createElement("option");
        option.value = reason.code;
        option.textContent = reason.names[CONFIG.language] || reason.names.en;
        select.appendChild(option);
      });
    }
    show(byId("offline-decisions"), canDecide);
  }

  function renderVerify(stateName) {
    var verifying = VERIFYING_STATES.indexOf(stateName) !== -1 && !!state.body;
    show(byId("offline-verify-online"), ["ONLINE", "OFFLINE_READY", "SYNCING"].indexOf(stateName) !== -1);
    show(byId("offline-verify-form"), verifying && permitted(stateName, "VERIFY_OFFLINE_QR"));
    show(byId("offline-verify-stale"), stateName === "STALE");
    show(byId("offline-expired"), stateName === "EXPIRED");
    var referral = doc.querySelector("[data-offline-decision='REFERRAL']");
    if (referral) referral.disabled = stateName !== "EXPIRED" || !permitted(stateName, "MANUAL_REVIEW_OFFLINE");
    var operator = activeGrant();
    setField("operator", operator ? operator.display_name : "");
    renderResult(stateName);
  }

  function renderIncident(stateName) {
    // The documented incident path whenever verification cannot proceed
    // safely: no valid package, expired data, a blocked device or operator.
    show(byId("offline-incident"), stateName === "BLOCKED" || stateName === "EXPIRED");
  }

  function renderSync(stateName) {
    var c = state.counts;
    setField("in_flight", c.inFlight || 0);
    setField("acknowledged", c.acknowledged || 0);
    setField("conflicted", c.conflicted || 0);
    setField("last_sync", state.sync.lastSuccessAt ? new Date(state.sync.lastSuccessAt).toLocaleString() : "");
    var status = byId("offline-sync-status");
    if (status) {
      var key = "sync.idle";
      if (c.unacknowledged) {
        if (health.mode === "offline") key = "sync.waiting_offline";
        else if (state.sync.error) key = "sync.retrying";
        else key = "sync.pending";
      } else if (c.acknowledged) key = "sync.done";
      status.textContent = t(key);
      status.setAttribute("data-sync-status", key);
    }
    show(byId("offline-conflicts-notice"), (c.conflicted || 0) > 0);
    var body = state.body;
    setField("event", body ? body.event : state.device ? state.device.event : "");
    setField("scope_version", body ? body.scope_version : "");
    setField("zones", body ? body.zones.join(", ") : "");
    var grant = activeGrant();
    setField("zone", grant ? grant.zone : "");
    setField("sensitivity", body ? t("sensitivity." + body.sensitivity) : "");
  }

  function render(db) {
    var result = computeState(currentContext());
    var name = result.state;
    var tone = STATE_TONES[name] || ["neutral", "info"];
    if (ui.chip) {
      ui.chip.className = "asc-chip asc-chip-" + tone[0];
      ui.chip.setAttribute("data-offline-state", name);
      ui.chipText.textContent = t("state." + name);
      if (ui.chipIcon) ui.chipIcon.setAttribute("href", ui.iconBase + "#i-" + tone[1]);
    }
    if (ui.root) ui.root.setAttribute("data-offline-state", name);
    if (ui.explain) ui.explain.textContent = t("explain." + name);
    if (ui.sub) {
      ui.sub.textContent = "";
      result.sub.forEach(function (code) {
        var item = doc.createElement("li");
        item.setAttribute("data-offline-sub", code);
        item.textContent = t("sub." + code);
        ui.sub.appendChild(item);
      });
    }
    if (ui.actions) {
      ui.actions.textContent = "";
      CONFIG.actions.forEach(function (action) {
        var allowed = permitted(name, action);
        var item = doc.createElement("li");
        item.setAttribute("data-action", action);
        item.setAttribute("data-permitted", allowed ? "true" : "false");
        var label = doc.createElement("span");
        label.textContent = t("action." + action);
        var verdict = doc.createElement("span");
        verdict.className = "asc-chip " + (allowed ? "asc-chip-success" : "asc-chip-outline");
        verdict.textContent = allowed ? t("action.permitted") : t("action.forbidden");
        item.appendChild(label);
        item.appendChild(verdict);
        ui.actions.appendChild(item);
      });
    }
    Array.prototype.forEach.call(doc.querySelectorAll("[data-offline-action]"), function (button) {
      var action = button.getAttribute("data-offline-requires");
      var forbidden = action ? !permitted(name, action) : false;
      button.setAttribute("data-offline-forbidden", forbidden ? "true" : "false");
      button.disabled = forbidden;
    });
    var now = nowSeconds();
    var meta = state.activeMeta;
    setField("device", state.device ? state.device.name : "");
    setField("gate", state.device ? state.device.gate : "");
    var offline = state.offline || {};
    setField("prepared", offline.prepared_at ? new Date(offline.prepared_at * 1000).toLocaleString() : "");
    setField("self_test", offline.self_tested_at ? new Date(offline.self_tested_at * 1000).toLocaleString() : "");
    setField("package_version", meta ? meta.package_version : "");
    setField("band", t("band." + bandAt(now, meta)));
    setField("package_age", meta ? t("value.ago", { t: duration(now - meta.data_cutoff_at) }) : "");
    var criticalCutoff = state.deltaMeta ? state.deltaMeta.critical_delta_cutoff_at : meta ? meta.data_cutoff_at : null;
    setField("critical_age", criticalCutoff ? t("value.ago", { t: duration(now - criticalCutoff) }) : "");
    setField("expires_in", meta ? (now >= meta.expires_at ? t("band.EXPIRED") : t("value.in", { t: duration(meta.expires_at - now) })) : "");
    setField("pending", state.counts.pending);
    setField("locked", state.counts.locked);
    setField("sequence", state.counts.sequenceHigh);
    setField("chain_head", state.counts.chainHead ? state.counts.chainHead.slice(0, 16) : "");
    setField("grant", state.grants.length ? state.grants.map(function (g) { return g.display_name; }).join(", ") : "");
    renderVerify(name);
    renderIncident(name);
    renderSync(name);
    if (state.lastState && state.lastState !== name) {
      announce(t("msg.state_changed", { state: t("state." + name) }));
    }
    state.lastState = name;
    return result;
  }

  // --- Verification and decision commands ------------------------------

  function presentVerification(db, v) {
    state.result = v;
    render(db);
    var stop = v.result === "DENIED" || v.result === "UNSUPPORTED" || v.result === "STALE";
    var text = t("result." + v.result) + (v.reason ? ". " + t("reason." + v.reason) : "");
    if (stop) alertText(text);
    else announce(text);
    armClear();
  }

  var scanGeneration = 0;

  function onVerify(db, raw) {
    var generation = ++scanGeneration;
    var stateName = computeState(currentContext()).state;
    if (!permitted(stateName, "VERIFY_OFFLINE_QR") || !state.body) return Promise.resolve(null);
    notify("", "neutral");
    return verifyOfflineQr(raw, { state: stateName }).then(function (v) {
      if (generation !== scanGeneration || v.authority !== verificationAuthority()) return null;
      state.lastVerification = { code: v.code, latency_ms: v.latency_ms };
      if (v.type === "ENTRY_DECISION") {
        v.raw = raw; // memory only; never part of an operation or storage
        presentVerification(db, v);
        return v;
      }
      // An unresolved scan is recorded as an attempt (codes only) -- a
      // local miss as a referral to the review desk.
      var draft = draftOperation(
        v,
        v.referral ? "REDIRECTED" : null,
        v.referral ? "SENT_TO_REVIEW_DESK" : "",
        null
      );
      return recordOperation(db, draft)
        .then(
          function () {
            v.recorded = true;
          },
          function () {
            v.recordFailed = true;
          }
        )
        .then(function () {
          if (generation !== scanGeneration || v.authority !== verificationAuthority()) return null;
          presentVerification(db, v);
          if (v.recordFailed) notify(t("error.RECORD_FAILED"), "danger");
          return v;
        });
    });
  }

  function verificationAuthority() {
    var grant = activeGrant();
    return canonical({
      package: state.activeMeta ? state.activeMeta.package_id : null,
      delta: state.deltaMeta ? state.deltaMeta.delta_version : null,
      grant: grant ? grant.grant_id : null,
      state: computeState(currentContext()).state,
    });
  }

  function decisionSnapshot(v) {
    return canonical({
      type: v.type, code: v.code, result: v.result, reason: v.reason,
      blockers: blockerCodes(v), advisories: v.advisories,
      restrictionBlocksOverride: v.restrictionBlocksOverride,
    });
  }

  function decisionStillCurrent(v) {
    if (state.result !== v || v.authority !== verificationAuthority()) return false;
    if (!v.entry) return true;
    var grant = activeGrant();
    if (!grant) return false;
    var now = nowSeconds();
    var listed = listedStatus(v.entry.jti);
    var evaluation = evaluateEntry(v.entry, listed, now, grant.zone);
    // A stale result may still record a non-admission, but never an admission.
    if (v.state === "STALE" && ADMITTABLE.indexOf(evaluation.result) !== -1) {
      evaluation.blockers = evaluation.blockers.concat([blocker("MANUAL_REVIEW", "OFFLINE_DATA_STALE")]);
      evaluation.result = "MANUAL_REVIEW";
      evaluation.reason = "OFFLINE_DATA_STALE";
    }
    return decisionSnapshot(v) === decisionSnapshot(Object.assign({}, v, evaluation, {
      code: listed || windowCode(v.entry, now) || "VALID",
    }));
  }

  function onDecision(db, decision) {
    if (decision === "REFERRAL") return commitDecision(db, decision);
    var previous = state.result;
    if (!previous || previous.recorded || previous.recording || !previous.raw) return Promise.resolve(null);
    previous.recording = true;
    setDecisionBusy(true);
    return verifyOfflineQr(previous.raw).then(function (fresh) {
      if (state.result !== previous || previous.authority !== fresh.authority ||
          fresh.authority !== verificationAuthority() || decisionSnapshot(previous) !== decisionSnapshot(fresh)) {
        clearResult("msg.result_state_changed");
        return null;
      }
      fresh.raw = previous.raw;
      fresh.recording = true;
      state.result = fresh;
      return commitDecision(db, decision);
    }).finally(function () {
      previous.recording = false;
      if (state.result) state.result.recording = false;
      setDecisionBusy(false);
    });
  }

  function commitDecision(db, decision) {
    var v = state.result;
    var stateName = computeState(currentContext()).state;
    if (decision === "REFERRAL") {
      if (stateName !== "EXPIRED" || !state.body) return Promise.resolve(null);
      var referral = {
        type: "VERIFICATION_ATTEMPT",
        code: "PACKAGE_EXPIRED",
        result: "MANUAL_REVIEW",
        reason: "OFFLINE_PACKAGE_EXPIRED",
        blockers: [blocker("MANUAL_REVIEW", "OFFLINE_PACKAGE_EXPIRED")],
        advisories: [],
        credential: null,
        state: stateName,
        latency_ms: 0,
      };
      return recordOperation(db, draftOperation(referral, "REDIRECTED", "SENT_TO_REVIEW_DESK", null)).then(
        function () {
          notify(t("msg.referral_recorded"), "warning");
        },
        function (error) {
          notify(t(error && error.code === "STORE_LOCKED" ? "error.STORE_LOCKED" : "error.RECORD_FAILED"), "danger");
        }
      );
    }
    if (!v || v.recorded || v.type !== "ENTRY_DECISION" || v.state !== stateName) {
      clearResult("msg.result_state_changed");
      return Promise.resolve(null);
    }
    var action = {
      ADMIT: "ADMIT_OFFLINE",
      DO_NOT_ADMIT: "DO_NOT_ADMIT_OFFLINE",
      REDIRECTED: "MANUAL_REVIEW_OFFLINE",
      OVERRIDE: "OVERRIDE_OFFLINE",
    }[decision];
    if (!action || !permitted(stateName, action) || !activeGrant()) return Promise.resolve(null);
    if (decision === "ADMIT" && !isAdmittable(v)) return Promise.resolve(null);
    var reason = "";
    var override = null;
    var note = "";
    var finalDecision = decision;
    if (decision === "DO_NOT_ADMIT") {
      var select = byId("offline-dna-reason");
      reason = (select && select.value) || "FOLLOWS_RESULT";
    } else if (decision === "REDIRECTED") {
      reason = "SENT_TO_REVIEW_DESK";
    } else if (decision === "OVERRIDE") {
      var code = (byId("offline-override-reason") || {}).value || "";
      var chosen = overrideReasonsFor(v).filter(function (r) {
        return r.code === code;
      })[0];
      note = ((byId("offline-override-note") || {}).value || "").trim().slice(0, 500);
      if (!chosen) return Promise.resolve(null);
      if (chosen.requires_note && !note) {
        notify(t("msg.override_note_required"), "danger");
        return Promise.resolve(null);
      }
      finalDecision = "ADMIT";
      override = { code: chosen.code, note_digest: "" };
    }
    var draft = draftOperation(v, finalDecision, reason, override);
    var ready = Promise.resolve();
    if (override && note) {
      ready = noteDigest(draft.operation_id, note).then(function (digest) {
        draft.override.note_digest = digest;
      });
    }
    setDecisionBusy(true);
    return ready
      .then(function () {
        return recordOperation(db, draft, note, function () {
          return decisionStillCurrent(v);
        });
      })
      .then(
        function () {
          // Durable in IndexedDB now -- and only now is it "recorded". It is
          // NOT synchronized until the server's signed acknowledgement.
          v.recorded = true;
          var key = finalDecision === "ADMIT" ? "msg.admit_recorded" : "msg.decision_recorded";
          clearResult(null);
          notify(t(key), finalDecision === "ADMIT" ? "success" : "warning");
          var field = byId("offline-qr");
          if (field) field.focus();
        },
        function (error) {
          notify(t(error && error.code === "STORE_LOCKED" ? "error.STORE_LOCKED" : "error.RECORD_FAILED"), "danger");
          alertText(t("error.RECORD_FAILED"));
        }
      )
      .then(function () {
        setDecisionBusy(false);
        render(db);
      });
  }

  function setDecisionBusy(busy) {
    Array.prototype.forEach.call(doc.querySelectorAll("[data-offline-decision]"), function (button) {
      if (busy) button.disabled = true;
    });
  }

  function wireVerification(db) {
    var form = byId("offline-verify-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var field = byId("offline-qr");
        var raw = field ? field.value : "";
        if (field) field.value = ""; // the token is never kept on screen or stored
        onVerify(db, raw).catch(function () {
          notify(t("msg.error"), "danger");
        });
      });
    }
    Array.prototype.forEach.call(doc.querySelectorAll("[data-offline-decision]"), function (button) {
      button.addEventListener("click", function () {
        onDecision(db, button.getAttribute("data-offline-decision")).catch(function () {
          notify(t("msg.error"), "danger");
        });
      });
    });
    var clear = byId("offline-result-clear");
    if (clear) {
      clear.addEventListener("click", function () {
        clearResult(null);
        var field = byId("offline-qr");
        if (field) field.focus();
      });
    }
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && state.result) {
        clearResult(null);
        var field = byId("offline-qr");
        if (field) field.focus();
      }
    });
    ["pointerdown", "keydown", "input"].forEach(function (name) {
      doc.addEventListener(
        name,
        function () {
          if (state.result) armClear();
        },
        true
      );
    });
  }

  function refreshCounts(db) {
    return opCounts(db).then(function (counts) {
      state.counts = counts;
    });
  }

  function onlineCycle(db) {
    // Critical delta first, then a newer package when due (Flow §11.7 order).
    var due = !state.activeMeta || nowSeconds() - state.activeMeta.data_cutoff_at >= CONFIG.package_refresh_seconds;
    if (state.counts.unacknowledged || !syncAllowed()) return Promise.resolve();
    var work = Promise.resolve();
    if (state.offline && state.offline.available && state.offline.prepared) {
      work = state.activeMeta ? refreshDelta(db).catch(function () {
        // A verified replacement package can also establish current authority.
        return refreshPackage(db);
      }) : refreshPackage(db);
      if (due) work = work.then(function () { return refreshPackage(db); });
    }
    return work.then(function () {
      // Flow §11.7: back to Online only once the critical work is complete --
      // every local record durably acknowledged, then the delta/package.
      if (health.mode === "recovering" && state.counts.unacknowledged === 0) {
        health.mode = "online";
      }
    }).catch(function () { return null; }); // keep recovering until refresh succeeds
  }

  /** One synchronization attempt inside the loop: never while offline or
   *  unstable, never in parallel, backing off after a failure. */
  function syncCycle(db) {
    if (!state.counts.unacknowledged || !syncAllowed()) return Promise.resolve(null);
    return syncNow(db).catch(function () {
      return null; // recorded in state.sync; the records stay pending
    });
  }

  function restoreDeviceMeta(db) {
    return Promise.all([getOne(db, "meta", "device"), getOne(db, "meta", "sync")]).then(function (found) {
      var known = found[0];
      var sync = found[1];
      if (known && known.device && !state.device) state.device = known.device;
      if (known && typeof known.offset === "number") state.serverOffsetSeconds = known.offset;
      if (sync && sync.last_success_at) state.sync.lastSuccessAt = sync.last_success_at;
    });
  }

  function start() {
    ui.root = doc.getElementById("offline-shell");
    if (!ui.root || !CONFIG) return;
    ui.chip = doc.getElementById("offline-state-chip");
    ui.chipText = ui.chip ? ui.chip.querySelector("[data-offline-state-text]") : null;
    var use = ui.chip ? ui.chip.querySelector("use") : null;
    ui.chipIcon = use;
    ui.iconBase = use ? use.getAttribute("href").split("#")[0] : "";
    ui.explain = doc.getElementById("offline-state-explain");
    ui.sub = doc.getElementById("offline-state-sub");
    ui.actions = doc.getElementById("offline-actions");
    ui.announcer = doc.getElementById("offline-announcer");
    ui.message = doc.getElementById("offline-message");

    var supported = window.crypto && crypto.subtle && window.indexedDB && window.TextEncoder &&
      navigator.locks && navigator.locks.request;
    if (!supported) {
      state.storageError = true;
      notify(t("msg.unsupported"), "danger");
      render(null);
      return;
    }
    ["pointerdown", "keydown"].forEach(function (name) {
      doc.addEventListener(name, function () {
        health.lastActivity = Date.now();
      }, true);
    });
    if (navigator.serviceWorker && CONFIG.enabled) {
      navigator.serviceWorker.register(CONFIG.service_worker, { scope: CONFIG.scope }).catch(function () {
        return null;
      });
    }
    if (channel) {
      channel.onmessage = function (event) {
        if (!event.data || event.data.type !== "wipe" || state.wipe.status !== "NONE") return;
        state.body = null;
        state.activeMeta = null;
        state.grants = [];
        state.storageError = true;
        try {
          if (window.AscOffline._db) window.AscOffline._db.close();
        } catch (e) {
          /* already closed */
        }
        notify(t("msg.wiped_elsewhere"), "danger");
        render(null);
      };
    }
    openDb()
      .then(function (db) {
        window.AscOffline._db = db;
        return revertInFlight(db)
          .then(function () {
            return restoreDeviceMeta(db);
          })
          .then(function () {
            return loadGrants(db);
          })
          .catch(function () {
            return null;
          })
          .then(function () {
            return refreshCounts(db);
          })
          .then(function () {
            return heartbeat(db);
          })
          .then(function () {
            return loadActive(db, expectations()).catch(function () {
              return null;
            });
          })
          .then(function () {
            return loadLocalAdmissions(db).catch(function () {
              return null;
            });
          })
          .then(function () {
            wireButtons(db);
            wireVerification(db);
            render(db);
            loop(db);
          });
      })
      .catch(function () {
        state.storageError = true;
        render(null);
      });
  }

  function loop(db) {
    if (state.wiped) {
      notify(t("msg.wiped"), "danger");
      render(db);
      return;
    }
    if (state.wipe.status !== "NONE") {
      // Wipe mode: no heartbeat and no data work; retry a failed deletion
      // (a blocked one is still queued) until it really succeeds.
      var retry =
        state.wipe.status === "FAILED"
          ? destroyLocalData(null).catch(function () {
              return null;
            })
          : Promise.resolve();
      retry.then(function () {
        render(null);
        if (state.wiped) {
          notify(t("msg.wiped"), "danger");
          return;
        }
        setTimeout(function () {
          loop(db);
        }, CONFIG.health.retry_seconds * 1000);
      });
      return;
    }
    checkClock();
    heartbeat(db)
      .then(function () {
        if (state.wipe.status !== "NONE") return null;
        return refreshCounts(db).catch(function () {
          state.storageError = true;
        });
      })
      .then(function () {
        // Flow §11.7: upload the local records FIRST (in any state in which
        // the server is reachable -- a blocked or revoked device still
        // hands over its evidence), then the delta and package refresh.
        if (state.wipe.status === "NONE") return syncCycle(db);
        return null;
      })
      .then(function () {
        if (state.wipe.status === "NONE" && health.mode !== "offline" && !state.notEnrolled) {
          return onlineCycle(db);
        }
        return null;
      })
      .then(function () {
        if (state.wipe.status !== "NONE") return null;
        return purgeAcknowledgedOperations(db, CONFIG.sync.ack_retention_seconds * 1000)
          .then(function () {
            return refreshCounts(db);
          })
          .catch(function () {
            return null;
          });
      })
      .then(function () {
        if (state.wiped) notify(t("msg.wiped"), "danger");
        render(db);
        var seconds = health.unstable || health.mode !== "online" ? CONFIG.health.retry_seconds : CONFIG.health.poll_seconds;
        setTimeout(function () {
          loop(db);
        }, seconds * 1000);
      });
  }

  function wireButtons(db) {
    Array.prototype.forEach.call(doc.querySelectorAll("[data-offline-action]"), function (button) {
      button.addEventListener("click", function () {
        var action = button.getAttribute("data-offline-action");
        setBusy(true);
        var work;
        if (action === "prepare") {
          notify(t("msg.preparing"), "neutral");
          work = prepare(db)
            .then(function () {
              notify(t("msg.prepared"), "neutral");
              return refreshPackage(db);
            })
            .then(function () {
              notify(t("msg.downloaded"), "neutral");
              return runSelfTest(db);
            })
            .then(function (result) {
              notify(result.ready ? t("msg.self_test_passed") : t("msg.self_test_failed"), result.ready ? "success" : "danger");
            });
        } else if (action === "refresh") {
          work = refreshPackage(db).then(
            function (outcome) {
              notify(outcome === "UP_TO_DATE" ? t("msg.up_to_date") : t("msg.refreshed"), "success");
            },
            function (error) {
              notify(error && error.code === "INTEGRITY" ? t("msg.rolled_back") : t("msg.refresh_failed"), "warning");
            }
          );
        } else if (action === "self-test") {
          work = runSelfTest(db).then(function (result) {
            notify(result.ready ? t("msg.self_test_passed") : t("msg.self_test_failed"), result.ready ? "success" : "danger");
          });
        } else if (action === "grant") {
          work = requestGrant(db);
        } else if (action === "upload") {
          // Authorized manual retry: immediately, whatever the backoff says.
          notify(t("msg.uploading"), "neutral");
          work = syncNow(db, { force: true }).then(
            function (outcome) {
              if (outcome === "BUSY") notify(t("msg.upload_busy"), "neutral");
              else if (state.counts.unacknowledged) notify(t("msg.upload_partial"), "warning");
              else notify(t("msg.upload_done"), "success");
            },
            function () {
              notify(t("error.UPLOAD_FAILED"), "warning");
            }
          );
        } else {
          work = Promise.resolve();
        }
        work
          .catch(function (error) {
            var message = (STR["error." + (error && error.code)] || "") || t("msg.error");
            notify(message, "danger");
          })
          .then(function () {
            return refreshCounts(db).catch(function () {
              return null;
            });
          })
          .then(function () {
            setBusy(false);
            render(db);
          });
      });
    });
  }

  window.AscOffline = {
    DB_NAME: DB_NAME,
    CACHE_PREFIX: CACHE_PREFIX,
    config: CONFIG,
    state: state,
    health: health,
    canonical: canonical,
    b64u: b64u,
    unb64u: unb64u,
    bandAt: bandAt,
    computeState: computeState,
    permitted: permitted,
    currentContext: currentContext,
    recordHealth: recordHealth,
    checkClock: checkClock,
    openDb: openDb,
    verifyJws: verifyJws,
    activatePackage: activatePackage,
    loadActive: loadActive,
    applyDelta: applyDelta,
    purgePackageData: purgePackageData,
    executeEmergencyWipe: executeEmergencyWipe,
    opstore: {
      append: appendOperation,
      counts: opCounts,
      lock: lockOperations,
      purgeAcknowledged: purgeAcknowledgedOperations,
      revertInFlight: revertInFlight,
    },
    heartbeat: heartbeat,
    prepare: prepare,
    refreshPackage: refreshPackage,
    refreshDelta: refreshDelta,
    runSelfTest: runSelfTest,
    requestGrant: requestGrant,
    render: render,
    // Phase 4 Prompt 3 (the verifier is pure; nothing here bypasses the
    // state matrix -- the UI commands check it again on every action).
    activeGrant: activeGrant,
    verifyOfflineQr: verifyOfflineQr,
    evaluateEntry: evaluateEntry,
    verify: function (raw) {
      return onVerify(window.AscOffline._db, raw);
    },
    decide: function (decision) {
      return onDecision(window.AscOffline._db, decision);
    },
    syncNow: function (opts) {
      return syncNow(window.AscOffline._db, opts);
    },
    refreshCounts: function () {
      return refreshCounts(window.AscOffline._db);
    },
  };

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start);
  else start();
})();
