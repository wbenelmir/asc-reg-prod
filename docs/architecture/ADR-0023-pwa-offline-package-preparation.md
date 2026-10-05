# ADR-0023: Enrolled-device PWA shell and Offline Package preparation

| Field | Value |
| --- | --- |
| Status | Proposed (Phase 4 Prompt 2 implementation; READY_FOR_LOCAL_REVIEW) |
| Date | 2026-09-24 |
| Authority | the Phase 4 Prompt 2 work-package brief (binding decisions P2-A to P2-G) and the Phase 4 plan, both historical and kept outside the repository |
| Related requirements | PRD NFR-CON-001…006, NFR-PRI-003, NFR-COMP-003; TRD §7.3, §11.3–11.4, §12.2–12.3, §15, §17.4, §18.1–18.6, OFF-001…004; Flow §11.1–11.6, §11.9, §14.6, §14.10; UI/UX §9.5–9.7, §12.2, §19.3; Schema §11.1–11.3, §17.3; ADR-0006, ADR-0013, ADR-0019, ADR-0021, ADR-0022 |

## Context

Plan B, the enrolled-device PWA fallback, is mandatory for V1. Phase 4 Prompt 2 builds only
the **preparation** half:

* the shell;
* the device keys;
* the signed, encrypted, scope-bound Offline Package;
* the local stores;
* the seven operational states;
* evidence-preserving clean-up.

It builds nothing that decides an admission offline. Offline QR decisions, queue
population, synchronization, acknowledgement and reconciliation belong to Prompt 3, and
Event Edge is not authorized.

## Decisions

### 1. Boundaries (P2-A, P2-G)

* **HTML stays ordinary Django views.** The shell (`/entry/offline/`), the service worker
  (`/entry/sw.js`), the manifest, and the device-administration pages in `offline_views.py`
  are all plain Django views.
* **DRF only at the device API boundary.** DRF 3.18.1 serves `/entry/api/v1/offline/*`
  (`apps/entry/api/`).
  * It denies by default (`DenyAll`), speaks JSON only, and has no browsable API.
  * Every error uses one envelope: `{"error": {"code", "message", "correlation_id"}}`.
* **OpenAPI is hand-maintained.** The contract is
  `docs/api/entry_device_api_v1.openapi.json`, contract-tested by `test_offline_api.py`.
  There is no schema generator.
* **Deviation — API namespace.** The API lives under `/entry/api/v1/`, not under TRD §15.1's
  example `/api/v1/entry/`.
  * The approved Phase 3 device credential is an HttpOnly cookie scoped to `Path=/entry/`
    (ADR-0021).
  * Under `/api/…` the cookie would never be sent, and widening it to `/` would send the
    device credential with every public request.
  * The service worker scope is `/entry/` for the same reason.
  * Reported for review; no other behaviour depends on the prefix.
* **Exclusions.**
  * Event Edge: none.
  * Camera decoding: not added. It remains a requirement gap (PRD FR-ENT-001), and
    `BarcodeDetector` may only ever be a progressive enhancement.
  * Background Sync and Push: not used.
  * Offline NIN/passport/reference lookup: not implemented (deferred D4).

### 2. Availability is layered and off by default (P2-E)

A device may hold offline data only when **all** of these hold (`offline_unavailability`):

* the global `ENTRY_OFFLINE_ENABLED` flag is set (default `False` in every environment);
* the event has an `OfflineEventSetting` enabled row (`entry.enable_offline_entry`, granted
  to Entry Device Administrators);
* the event is open (not COMPLETED or ARCHIVED);
* the device is operational and not under a wipe order or an offline block;
* the current scope version has `offline_capable = True`.

A scope that includes a RESTRICTED or HIGH_SECURITY zone can only be offline-capable under
the Sensitive validity values. This reuses the existing `Zone.sensitivity`; it is a
conservative addition, never a relaxation.

`OFFLINE_READY` is set only by a passed self-test on the current package. A database CHECK
also requires preparation and a self-test, and forbids an offline block.

**Gate offline policy (resolved; independent re-review correction D).** `Gate.offline_policy`
is a Phase 3 placeholder, pinned to `NOT_PERMITTED` by the `events_gate_offline_not_permitted`
constraint before offline continuity existed. It is now formally **deprecated and
non-authoritative**:
* the only authoritative controls are the global `ENTRY_OFFLINE_ENABLED` flag, the per-event
  `OfflineEventSetting` and the device's CURRENT versioned `DeviceScope` (`offline_capable`,
  `offline_sensitivity`), exactly as listed above;
* no code reads the field, and it grants or denies nothing. A test
  (`test_no_code_reads_the_deprecated_gate_offline_policy`) fails if any application module,
  template or script starts referencing it;
* the model documents the deprecation in code. Its value, choices and constraint are
  unchanged, so this needs no migration;
* **follow-up, not authorized here:** a separate migration to remove the field and its
  constraint. It needs its own explicit authorization, and until then the field stays as it
  is.

### 3. Key and trust model (P2-D)

**Device keys.**
* Provisioning happens online, by a device administrator signed in on the device.
* The browser generates two **non-extractable** WebCrypto P-256 key pairs:
  * ECDSA, for request proofs and, in Prompt 3, operations;
  * ECDH, for package unwrapping.
* A non-extractable AES-GCM key seals the local store.
* The server stores public SPKI only (`EntryDeviceKey`: versioned; RETIRED, never deleted;
  guarded by a trigger).
* Possession of the new signing key is proven by a signature over the provisioning request.

**Request proofs.**
* Every signed call carries a signature over
  `ASC-DEVICE-PROOF-v1\n<purpose>\n<nonce>\n<hex SHA-256(body)>`.
* The nonce is device-bound, short-lived and **single-use**: its digest is inserted into
  `entry_offline_nonce` in the same request.
* The purpose is part of what is signed, so a proof for one endpoint is useless at another.

**Package signing.**
* Manifests, critical deltas, operator grants and wipe orders are signed with a **fifth
  key family**, `OFFLINE_PACKAGE_SIGNING_KEY_V<n>`, key identifiers `p<n>`.
* It sits behind the existing `SigningKeyProvider` interface
  (`apps/core/crypto/package_signing.py`) and never uses the QR keys (`v<n>`).
* Redaction and the credential scanner cover the new family, including its normalized PEM
  form.

**Trust pinning.**
* The provisioning response returns the active package-verification keys, and the device
  **pins** them.
* A key carried inside a package — the QR `qr_keys` list — is never accepted as a signer
  of packages.
* Rotating the package key family therefore requires re-provisioning. This is deliberate.

**Composition only.** There is no custom primitive; everything is composed from
`cryptography` on the server and WebCrypto in the browser:
* body: AES-256-GCM with a random per-package data key;
* wrap: an ephemeral P-256 ECDH → HKDF-SHA256 (random salt; `info` binds kind, device and
  object) → AES-256-GCM of the data key;
* signature: ES256 compact JWS with a distinct `typ` per object type (`ASC-OPKG`,
  `ASC-ODELTA`, `ASC-OGRANT`, `ASC-OWIPE`).

The server discards the data key, so it cannot decrypt a package it built.

### 4. Offline Package: minimum, immutable, retrievable (P2-C, P2-E)

**Contents.**
* Only approved, current contexts are included, each with an ACTIVE pass and at least one
  reachable ALLOW rule at this device's gate and zones. There is no participant directory.
* Per entry:
  * `jti`, `pid`, `bai`, `cv`, `apc`, `btc`, pass window;
  * display name;
  * Badge Type labels;
  * assignment end;
  * profile window;
  * re-entry policy;
  * per-zone rule windows;
  * restriction windows with severity and overrideability — never category or reason;
  * last admission time.
* Package-level: revoked-pass list, QR public keys, the override catalogue subset, and the
  bands.

**Allow-list enforcement.** `apps/entry/offline_contract.py` holds exact key sets at every
level. The builder validates every body against them before encrypting, and tests prove
any undeclared field fails the build.

**Never included:** NIN or passport values, digests, fingerprints or lookup keys;
registration reference; masked hints; email; phone; documents; notes; restriction reason
or category; Participant Role; photos.

**Build lifecycle (independent re-review correction B).**
* The download request **never builds**. It records, or finds, the device's single open
  `OfflinePackageBuild` and answers `202 BUILDING` with `Retry-After` until bytes exist.
  A valid package that is merely due for refresh is still served while its successor
  builds.
* The package version is **reserved on the build row**. A partial unique index allows at
  most one PENDING or BUILDING build per device, and the device row lock serializes
  requests. Concurrent requests and Celery redeliveries therefore converge on one build
  and one version; they never create successive immutable versions.
* `run_package_build`, dispatched by `build_offline_package_task`, claims the build with a
  **lease token**. Any other worker, or a redelivery, leaves a live lease alone, and an
  expired lease is reclaimed for the **same** version. After
  `ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS` attempts the build fails; a later request starts a new
  build with a new version.
* The builder projects, encrypts and signs **without holding any lock**, then stores the
  bytes. It records the object key on the build (`pending_object_key`) and commits under
  the device and build row locks, always taken in that order. Before committing it
  **re-validates** availability, the current scope and the recipient key. A build
  overtaken by a block, revocation, re-scope, re-provisioning or event disable is
  cancelled, never committed.
* **Stored ciphertext that is not committed is deleted whatever fails, including the
  database COMMIT itself**: a deferred foreign-key failure at commit is tested on real
  PostgreSQL. `cleanup_offline_packages` retries any deletion that failed, using
  `pending_object_key`, and releases abandoned leases.
* With the local eager broker substitute (ADR-0009), Celery runs the dispatched task
  in-process right after the enqueuing transaction commits. With a real broker a worker
  runs it. The request's own code path never builds in either case.

**Immutability and retrieval.**
* Each version is built once. Its response bytes (manifest + ciphertext) are stored through
  the existing private-storage adapter; no production provider is chosen.
* A retry of the same READY version returns **byte-identical** bytes.
* A missing or corrupted stored object is never served. A **new** version is built instead,
  and a version number is never reused.
* Every download and retry is proof-authorized, rate-limited per device
  (`ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW`) and audited with versions and hashes only.
* Metadata rows are never deleted. A trigger keeps the content columns immutable, and a
  terminal status cannot be revived.
* Ciphertext is purged after a configurable retention
  (`ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS`) once the package is superseded, expired or
  revoked (`cleanup_offline_packages`).

### 5. Validity and freshness (P2-B)

**Approved values** (`settings.ENTRY_OFFLINE_VALIDITY`). They are not read from the
environment, and the system check `entry.E006` refuses any value above them.

| | Aging | Stale | Hard expiry | Grant |
| --- | --- | --- | --- | --- |
| Standard | 15 min | 2 h | 8 h | ≤ 2 h after contact |
| Sensitive | 5 min | 30 min | 2 h | ≤ 1 h |

* Every value is measured from `package_data_cutoff_at`. "Aging to Stale: 2 hours" is read
  conservatively, from the cutoff.
* Hard expiry is the earliest of the maximum, the **end of the event day in the event
  timezone**, and the device expiry.
* A grant never outlives the operator session's absolute expiry.
* Health, inactivity-lock and clear values are pinned by check `entry.E007`.

**Two timestamps, never merged.**
* `package_data_cutoff_at` belongs to the full projection.
* `critical_delta_cutoff_at` belongs to the last accepted signed delta.
* A delta can never move the package cutoff. The device verifies the delta's signature,
  schema, device, package, scope and **monotonic version** before recording it.
* The package band alone governs the state; a delta narrows the revocation gap but never
  extends freshness. No separate threshold for delta age was approved, so the delta age is
  displayed only.

**Durable invalidation, not `updated_at` (independent re-review correction C).**
* PostgreSQL triggers (`entry.0004`) append one `OfflineChangeJournal` row for **every**
  INSERT, UPDATE and DELETE on every table that can change a package or a delta
  (`offline_journal.TRACKED_TABLES`), whoever issues it. That includes `QuerySet.update()`,
  bulk operations and raw SQL, none of which touch `updated_at`. A statement-level trigger
  records a TRUNCATE. No authoritative mutation service can bypass the journal or forget
  to write to it.
* Every package records the PostgreSQL **snapshot** taken in the builder's transaction
  before the first projection query (`data_snapshot`, `journal_xmin`, `build_txid`,
  `journal_high_id`). A journal row counts as covered only if its transaction is visible
  in that snapshot. Each delta re-sends every row that is not covered. The result is
  correct in **commit order**: a change committed after the build by a transaction that
  was still open, carrying a timestamp older than the cutoff, still reaches the next
  delta. This is proven by a multi-connection test. No lock is taken, so there is no
  deadlock with writers.
* **Fail closed.** The delta requires a full rebuild, and the device is **Blocked** offline
  (`REBUILD_BLOCKING_REASONS`) rather than merely Stale, when:
  * a TRUNCATE, a deleted row the delta would need, or an unknown table is journaled
    (`UNTRACKED_CHANGE`);
  * there are more uncovered rows than `ENTRY_OFFLINE_DELTA_MAX_CHANGES` (`BULK_CHANGE`);
  * the package has no watermark (`SNAPSHOT_MISSING`: a pre-0004 package is never current
    again);
  * the scope changed (`SCOPE_CHANGED`).

  Such a package is also withheld from download: the device receives BUILDING until the
  rebuild exists.
* **Re-validated under the row lock.** A delta takes the device lock, then the package lock,
  and only then checks availability, READY status, expiry, the current scope and the
  recipient key. A package revoked, superseded, expired, blocked, re-scoped or
  re-provisioned concurrently never receives a delta. Multi-connection tests cover a block,
  a re-scope and a re-provisioning that each win the lock.
* The journal holds internal ids only and is pruned after
  `ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS`, except rows a READY package can still need.
  System check `entry.E011` keeps the retention longer than any package lifetime plus the
  refresh interval.

**The critical delta covers exactly the following** (`delta_body`), and
`validate_delta_body` enforces that closed set:
* pass revocation, replacement, suspension and expiry (every non-active pass of the event),
  and any other change to an active pass (withdrawn: `PASS_CHANGED`);
* the eligibility of every active pass: a context no longer approved or current;
* restriction changes: the current restriction set of every affected context, which the
  device merges monotonically;
* verification-key revocation;
* access that could be removed:
  * a changed assignment;
  * a changed direct rule, per context;
  * a changed profile or profile rule, as whole Access Profiles by code.

**Anything else a delta cannot represent** sets `rebuild_required` with a reason:
* changed gates, zones or venues;
* a changed override catalogue;
* a changed QR key set other than revocation.

These three make the device Stale until a rebuild; the fail-closed reasons above make it
Blocked. New passes and newly granted access stay unavailable until a rebuild; a local
miss routes to Manual Review.

**Operational tuning, not security values:** refresh cadence, health retry interval,
nonce lifetime, download budget, retention, minimum free storage. None of these can
extend a band.

### 6. The seven required states

`computeState` in `static/js/entry-offline.js` is a pure function evaluated in this order:

1. **Blocked.** An emergency wipe in progress, blocked, failed or completed; not enrolled; a
   server directive; storage unavailable; or a clock discontinuity over 5 min (Standard) or
   2 min (Sensitive). While offline, also: no valid package, integrity failure, a rebuild
   for a blocking reason (scope change, untracked or bulk change, missing watermark), a
   locked grant, or no grant.
2. **Expired.**
3. **Stale.** This includes "rebuild required" for other reasons.
4. **Offline Active.**
5. **Syncing.** Recovering, or unacknowledged records exist while online.
6. **Offline Ready.** Provisioned, server-ready, a Fresh package, and no rebuild pending.
   When no valid operator grant exists it carries the `NO_GRANT` sub-state, because a loss
   of connection would then end in Blocked.
7. **Online.**

The permitted-action matrix is `offline_contract.PERMITTED_ACTIONS`, sent to the device as
data. Admission actions exist only in Offline Active. Stale permits only Manual Review or
Do Not Admit, with no override. Expired permits neither verification nor admission.
Blocked permits only the manual procedure.

**Health transitions (OFF-001, approved values).**
* Offline begins after 3 consecutive failures spanning at least 20 s.
* Syncing begins after 2 consecutive successes spanning at least 10 s.
* A structured API answer — even a 401 — counts as the server being reachable.

The checkpoint page's connection indicator (`entry.js`) now also needs repeated failures
before it reports "offline". A browser `offline` event still switches it at once.

**Inactivity.** Participant data clears after 45 s (existing). Operator actions lock after
10 minutes of offline inactivity, with **no local re-authentication**. Only an online
re-issue unlocks them, and another operator needs their own online-issued grant.

### 7. Local storage and evidence preservation (P2-F)

**Stores.** One IndexedDB database, `asc-entry-offline`, holds four stores:
* `keys`: non-extractable CryptoKeys;
* `packages`: the `staging`, `active`, `previous` and `delta` slots, as signed and
  encrypted envelopes only;
* `ops`: the append-only operation-store foundation. Each record is AES-GCM sealed and
  hash-chained, with a sequence counter and chain head; appends are serialized with Web
  Locks;
* `meta`: trust anchors, sealed grants, the chain head.

Decrypted package data exists in memory only.

**Atomic activation.**
1. Stage the new envelope.
2. Verify the signature against pinned keys, then the device binding, schema, scope,
   expiry, ciphertext length and hash, decryption, and allow-list.
3. Swap `active` → `previous` in one transaction.

A failed refresh keeps the last valid package. Refresh accepts only a **newer** version.

**Load and rollback.** Start-up re-verifies the active package at rest and rolls back only
to a `previous` package that is still valid. If neither verifies, package data is purged
and the integrity failure is shown.

**Cleanup is split.**
* `purgePackageData` never touches operations, keys or the chain.
* `purgeAcknowledgedOperations` deletes only records in state `ACKNOWLEDGED`, which only
  Prompt 3 will set.
* Expiry, event closure, package replacement, rollback, a service-worker update (the worker
  never opens IndexedDB), ordinary revocation, suspension and "Block offline use" never
  delete an unacknowledged record.
* The server directives `PURGE_PACKAGE` and `LOCK_OPERATIONS` purge package data and turn
  `PENDING` records into `LOCKED`: kept, still encrypted, upload-only. A locked store
  refuses new records.
* A revoked device's keys are retired, not deleted, so the future quarantine boundary can
  verify locked records. The quarantine processor itself is Prompt 3.

**Destructive emergency wipe.**
* It is a distinct permission, `entry.emergency_wipe_device`, held only by Security
  Restriction Managers.
* It requires an MFA step-up through the `apps.accounts.mfa` boundary, an explicit
  confirmation, a reason code, and an optional bounded, encrypted note.
* The order (`DeviceWipeOrder`, append-only) records the last reported pending and locked
  counts, sequence, chain head, and the expected evidence-loss class.
* At the device's next contact it receives the order signed with the package family. It
  verifies it against the pinned keys and reports its counts, sequence range and chain head
  (`DeviceEvidenceReport`, append-only). Only then does it delete its IndexedDB database.
* **Deletion is confirmed, never assumed (independent re-review correction A).**
  * The wipe counts as done only after `indexedDB.deleteDatabase()` fires `onsuccess`.
  * `onblocked` shows a safe Blocked state ("waiting for the other entry tabs") and never
    reports WIPED. The request stays queued and completes once the other connections
    close.
  * `onerror` shows a failure state, and the loop retries.
  * Before deleting, the wiping tab asks the other tabs of the application to close their
    connections (`BroadcastChannel`). Every shell connection also closes itself on
    `versionchange`.
  * Only after the database is gone does it clear this application's caches and unregister
    **only its own** registration: scope exactly `/entry/`. Any other service worker on the
    same origin is left alone.
  * Playwright regressions cover:
    * a blocking connection in a second tab: never reported as wiped, then completes;
    * a second shell tab that steps aside;
    * an unrelated worker that survives the wipe.
* An unverifiable order destroys nothing.

**MFA.** No authenticator is implemented. Where `MFA_BACKEND` is unset — local
development — the wipe fails closed. Staging and production already require
`MFA_BACKEND`.

### 8. Service worker (TRD §7.3)

* Served from `/entry/sw.js`, so its scope can never exceed `/entry/`.
* It precaches the **static** shell (identical for every visitor: no user, device,
  participant value or CSRF token in its HTML) and the shell's static assets.
* Every other request goes to the network and is never cached.
* It never opens IndexedDB and never calls `skipWaiting` automatically, so an update never
  interrupts an operation.
* With the flag off, the same URL serves a self-removing worker. It clears only this app's
  caches and unregisters itself.

### 9. Migration

`entry/0003_offline_preparation` is additive:
* new tables and nullable columns;
* a new OFFLINE_READY twin constraint, leaving the approved `entry_device_enrolled_has_credential`
  untouched;
* the one relaxation, dropping `entry_scope_online_only`;
* triggers.

`sqlmigrate` shows only `ADD COLUMN … DEFAULT … / DROP DEFAULT` on existing tables: no
`UPDATE` and no rewrite.

`entry_event_online_only` is **kept**, so no offline admission can be recorded before
Prompt 3.

Forward, reverse and forward again are proven on PostgreSQL. Reversal is **deliberately
refused** once any offline record exists: a guarded reverse step runs first.

`entry/0004_offline_build_lifecycle_and_journal` (independent re-review corrections B and C) is
also additive:
* the package watermark columns, which join the immutable content columns of the package
  guard;
* `entry_offline_package_build`, never deleted, with a final terminal status;
* `entry_offline_change_journal`;
* the journal triggers on every tracked table.

`sqlmigrate` shows `ADD COLUMN` (with `DEFAULT '' / DROP DEFAULT` for `data_snapshot`),
`CREATE TABLE`, indexes and constraints: no `UPDATE`. Forward, reverse (which restores the
0003 guard body) and forward again are tested. Reversal is refused once a build or a
watermarked package exists. Journal rows alone do not block reversal: they are derived
data.

## Browser-security limitations (documented, not overclaimed)

* A non-extractable key cannot be exported, but any script running in the origin can
  **use** it: XSS, a malicious extension, devtools on an unlocked device, or OS compromise.
  CSP (Phase 4 Prompt 4, not yet present), managed devices and kiosk mode reduce this risk
  but do not remove it.
* A stolen, unlocked, logged-in device can verify locally until its package expires or its
  grant lapses.
* Browser or user destruction of site data, Safari/ITP eviction and quota exhaustion are
  outside application control and can destroy unacknowledged records. Mitigations:
  * `storage.persist()` is requested (reported by the self-test, not required);
  * the self-test enforces a free-space minimum;
  * pending and locked counts are always visible and reported to the server;
  * sequence and chain gaps will be detected at synchronization (Prompt 3).
* Device clocks are untrusted. The server offset and the monotonic clock are compared, and
  a jump over the approved limit is Blocked. A deliberately rolled-back clock beyond that
  heuristic cannot be fully prevented, and a device resuming from sleep may be Blocked
  conservatively.
* Independent devices cannot enforce a global anti-passback rule (Flow §11.9).
* Plaintext package data lives in page memory while the shell is open. JavaScript and
  Python cannot guarantee that memory is zeroed.
* A database superuser can disable triggers (`session_replication_role = replica`) and
  thereby bypass the change journal. Database-role separation is the control (ADR-0008);
  an application cannot enforce it.
* If the build process is killed between writing the ciphertext and recording its key on
  the build row (the very next statement), the object is left unreferenced. It stays
  unreadable (encrypted to a device key, with the data key discarded), but the sweeper
  cannot find it without listing storage.
* A connection that ignores `versionchange` (an outdated tab, a debugging session) keeps an
  emergency wipe waiting until it closes. The device shows this and never claims the wipe
  is done.

## Consequences

* The new permissions `entry.enable_offline_entry` (Entry Device Administrators) and
  `entry.emergency_wipe_device` (Security Restriction Managers) are granted through groups.
  Security Restriction Managers also gain `entry.view_entrydevice`, so they can find the
  device to wipe.
* `OFFLINE_READY` operates online exactly like `ENROLLED`. Every lifecycle change
  withdraws offline readiness in the same transaction: suspend, revoke, expire, rescope,
  re-enroll, event disable, event closure, and block.
* A new dependency, `djangorestframework>=3.16.1,<4` (resolved to 3.18.1; depends only on
  Django).
* A new fail-closed system-check family, `entry.E005`–`E011`.
