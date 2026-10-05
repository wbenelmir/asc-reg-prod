# Offline synchronization contract

Version: 1. Status: READY_FOR_LOCAL_REVIEW, Phase 4 Prompt 3.
Authority: ADR-0024 and the preserved Prompt 3 authorization.
Normative machine allow-lists: `apps/entry/offline_contract.py`.
HTTP schema: `docs/api/entry_device_api_v1.openapi.json`.

## Endpoints and authorization

| Endpoint | Authorization | Effect |
| --- | --- | --- |
| `POST /entry/api/v1/offline/sync/` | Enrolled-device cookie, CSRF, single-use nonce and signed request proof | Intake and process bounded operations |
| `POST /entry/api/v1/offline/sync/quarantine/` | CSRF, revoked device identity and attributable registered device-key signatures | Quarantine only; never admission |

The request proof remains `ASC-DEVICE-PROOF-v1`, purpose `sync`, nonce and body
SHA-256 as defined in the package contract. Device calls serialize nonce use.
An operation signature does not replace the normal endpoint's proof or CSRF.

A batch is `{store, operations}`; quarantine also names `device`. At most the
configured 20 operations are accepted. The configured body limit and upload
budget also apply. Usable envelopes must have strictly increasing sequences
inside each batch; reversed/repeated order is refused as `UNORDERED_BATCH` before
intake. Missing sequence history is an explicit gap case, never silently repaired.

## Operation envelope and signed body

Envelope fields: `operation_id`, `sequence`, `op`, `signature`; optional `note`
is permitted only as bounded encrypted override evidence at intake.
`op` is exact canonical UTF-8 JSON: sorted keys, no whitespace, no floats. The
device signs its bytes with P-256 ECDSA/SHA-256, raw 64-byte `r || s`, base64url.
Maximum operation size is 8192 bytes. Unknown fields at any operation level fail
validation. Enums require strings; boolean schema versions and unrepresentable
timestamps are rejected. Integers that cross JavaScript use its safe range.

| Body field | Meaning |
| --- | --- |
| `schema_version`, `type` | Version 1; `ENTRY_DECISION` or `VERIFICATION_ATTEMPT` |
| `operation_id`, `device`, `event`, `store` | UUIDv4 plus device/event/local-store bindings |
| `sequence`, `prev` | Append-only order and prior SHA-256 hex digest |
| `occurred_at`, `device_time`, `server_offset` | Whole-second occurrence and clock evidence |
| `state` | State at recording; only authorized recording states |
| `package` | ID/version, delta version, band, package and critical cutoffs |
| `grant`, `gate`, `zone` | Operator grant and exact checkpoint bindings |
| `credential` | Verified opaque `jti` and `kid`, or null; never raw QR |
| `local` | Verification code/result/reason, bounded blockers/advisories, latency |
| `decision`, `decision_reason` | Existing entry vocabulary; no implicit admission |
| `override` | Existing configured reason code and optional bound note digest |

The chain digest is SHA-256 of the previous lowercase hex digest encoded as
UTF-8, followed by the exact operation bytes. The server never renumbers or
rewrites evidence. Unknown-field/invalid-signature input is retained as a digest
and controlled rejection metadata; this is a deliberate privacy boundary.

## Durable acknowledgements

The response contains `server_time` and `acknowledgements`. Each response entry
names the operation ID, sequence, status/outcome/conflict, durable/replayed flags
and a compact signed acknowledgement when durable. The signed payload has
exactly: `typ`, `schema_version`, `device`, `store`, `operation_id`, `sequence`,
`payload_hash`, `status`, `outcome`, `conflict_type`, `processed_at`.
`typ` is `ASC-OACK`, schema 1, signed by a pinned package-signing key.

Durable statuses: `APPLIED`, `CONFLICT`, `RECONCILIATION_REQUIRED`,
`SECURITY_CONFLICT`, `REJECTED`, `QUARANTINED`.
Non-durable statuses: `PENDING`, `NOT_PROCESSED`. Processing stops behind the
first non-durable result. A valid signed payload and all identity/digest bindings
must match before the device marks an operation acknowledged. HTTP 200, an
unsigned durable flag, wrong-store acknowledgement or response from an
untrusted signer cannot remove evidence.

The server commits before acknowledging. Retrying an already committed operation
creates no second Entry Event. Reusing an ID with different contents preserves
the original row and creates scoped rejection evidence. A sequence collision is
also retained as a case when a second inbox row cannot satisfy uniqueness.

## Local queue and recovery

Local states are `PENDING`, `IN_FLIGHT`, `LOCKED`, `ACKNOWLEDGED`. The encrypted
record and chain head commit in one strict IndexedDB transaction. Keys are
non-exportable CryptoKeys. Web Locks serialize append/upload across tabs. An
interrupted in-flight row returns to pending; locked evidence remains locked and
uploadable through the appropriate boundary. Retries are bounded exponential
backoff with jitter; manual retry bypasses delay, not authorization or locks.

Ordinary cleanup never deletes unacknowledged evidence. Acknowledged rows require
their stored signed acknowledgement to verify again, elapsed retention, and
preservation of local anti-passback knowledge. Corrupt/unreadable records remain
and block new admissions. Package refresh, sign-out, revocation and ordinary
cache cleanup do not substitute for acknowledgement. Signed emergency wipe is
the explicit exception documented in ADR-0023.

## Temporal rejection reasons

For an otherwise resolved ENTRY_DECISION, the existing classifier rejects hard
package expiry before the operator-grant time window, then rejects admission
on stale data. A stale package with an expired grant therefore produces
SECURITY_CONFLICT / POLICY_VIOLATION with GRANT_NOT_VALID. With a valid grant,
stale admission produces SECURITY_CONFLICT / PACKAGE_STALE. Neither creates an
Entry Event. The server-known evidence retains band_at_occurrence and
grant_expires_at even when only one primary reason is returned. Exact expiry is
already invalid; there is no extra grace interval introduced by correction 1.

## Reconciliation, audit and limitations

Cases keep device-known state, server evaluation and operational fact separate.
Scope is enforced in querysets and commands. Notes and closure are audited,
version-checked, encrypted where free text is retained, and cannot rewrite an
Entry Event. Unknown historical access becomes explicit review, as ADR-0024
describes. Successful synchronization does not imply admission: a durable
rejection/conflict/quarantine is also a synchronized outcome.

Browser tests using an acknowledgement double do not prove server idempotency.
PostgreSQL concurrency tests and the real API suites supply that evidence only
when actually executed. Hardware/browser limitations and manual scenarios are
recorded separately; no production validation is claimed here.
*(Correction 4 note: `tests/browser/test_offline_sync_live.py` now also covers
the complete browser-to-server path, with no endpoint or acknowledgement double.)*

## Override revalidation and quarantine bounds (correction 4, 2026-09-29)

**Override.** `override.code` names a reason from the catalogue of the package
the device used. The server checks it as follows:

1. **Catalogue-independent checks, first.** These reject:
   * an admittable local result;
   * a grant that does not permit overrides;
   * empty or never-overrideable blockers;
   * a note that does not match `override.note_digest`.

   Any of these is `POLICY_VIOLATION / OVERRIDE_NOT_PERMITTED`. A
   non-overrideable restriction is `NON_OVERRIDEABLE_RESTRICTION`. Neither
   creates an Entry Event.
2. **The reason unchanged in the journal since the package snapshot.** The
   current row is the package's catalogue entry. The reason must exist, be
   active, list every local blocker and satisfy its note rule. Otherwise the
   result is `POLICY_VIOLATION / OVERRIDE_NOT_PERMITTED`, with no Entry Event.
3. **The entry is unknown to the server.** This applies when:
   * the reason changed after the snapshot;
   * the code has no row and the catalogue changed;
   * the journal cannot attribute changes.

   The result is `CONFLICT / ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN`,
   unless an authority, pass, restriction or duplicate conflict applies
   first. The server evaluation records `override_catalogue_uncertain` and
   `historical_access_uncertain`. The offline Entry Event and Entry Override
   are recorded and marked as a conflict. If the reason no longer exists,
   neither can be recorded, because the schema requires a catalogue reason.
   The fact then stays in the operation and its case
   (`override_reason_unavailable`).

   *Correction 5 note: steps 2 and 3 are refined below. A reason inserted
   after the snapshot is checked against its only state, and a code without a
   row after insertions only is provably absent. An admission kept only in
   its operation now counts for later admission decisions.*

**Quarantine.** Until one operation signature verifies, the request is
unauthenticated. For such a request:

* shape refusals keep their response codes (`MALFORMED_BATCH`,
  `UNORDERED_BATCH`);
* a batch with no verifiable signature keeps `QUARANTINE_REFUSED`;
* an exhausted budget keeps HTTP 429 `RATE_LIMITED`;
* each refusal is audited at most once per device, reason and budget window;
* nothing counts against the device's upload budget, which counts only
  batches that ran.

Signature verification stops at the first operation no registered key
verifies. The response acknowledges only the verified prefix, and the device
keeps everything else pending. The authenticated endpoint is unchanged, and
still audits every refused request.

*Correction 5 note: the "at most once per device, reason and budget window"
bullet held only for sequential requests. Concurrent refusals could each
record a row. It is now guaranteed; see below.*

## Catalogue evidence, unlinked admissions and refusal serialization (correction 5, 2026-09-29)

The developer approved decisions D2 (option A2), D3-A, D3-B (option S1),
D3-C and R-02. ADR-0024's correction 5 addendum gives the reasoning. No
migration was added. The wire format, the acknowledgement and the HTTP codes
are unchanged.

**Catalogue evidence (D2).**

* **What the package holds.** A package holds its event's active reasons as
  its catalogue query read them. That query runs at READ COMMITTED after the
  watermark snapshot, so the state it reads can be newer than the snapshot.
  An uncovered INSERT therefore never proves that a reason was absent.
* **Recorded evidence.** Every checked override records
  `override_catalogue_evidence` in the server evaluation. The value is one of
  `UNCHANGED`, `ABSENT`, `INSERTED_ONLY`, `CHANGED` or `UNATTRIBUTABLE`.
* **Provable violation.** The override violates every state the package could
  have carried: the unchanged row, an absent code, or the only state of an
  INSERT-only row. The result is `SECURITY_CONFLICT / POLICY_VIOLATION /
  OVERRIDE_NOT_PERMITTED`, with no Entry Event.
* **When INSERT-only and absence are established.** Only while no uncovered
  UPDATE or DELETE touched any reason of the event, and while the journal can
  attribute every change.
* **Everything else.** A conforming override on an INSERT-only row, and any
  override after a rename, move, edit or deletion, or with an unattributable
  journal, is `CONFLICT / ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN`.

**Unlinked admission evidence (D3).** A durable, processed ENTRY_DECISION
operation can be "unlinked admission evidence". It must meet all of these
conditions:

* it reports ADMIT;
* its status is CONFLICT or RECONCILIATION_REQUIRED;
* it belongs to the registration's own event;
* it has no linked Entry Event.

Such an operation counts as a prior admission of that Registration Context:

* in the online evaluator: `ALREADY_ADMITTED` under `SINGLE_ENTRY`, otherwise
  `PRIOR_ENTRY` and `RECENT_REENTRY`;
* in the occurrence-time evaluation, but only for admissions strictly earlier;
* in `DUPLICATE_ENTRY` and the close-admission advisory at synchronization;
* in a later package's `last_admitted_at`.

The following rules apply:

* **Recorded keys.** The server evaluation records `prior_admission_at`, from
  either source, and `prior_unlinked_admission_at`.
* **Case links.** A case that relies on such an operation links it
  (`ReconciliationCaseOperation`). `related_entry_event` and
  `prior_entry_event` remain Entry Events, or null.
* **Exclusions.** PENDING, REJECTED, QUARANTINED and SECURITY_CONFLICT
  operations never count. An operation with an Entry Event counts once,
  through that event.
* **No new records.** No Entry Event, override reason or outcome is created or
  changed. Reporting still reads Entry Events only.

**Refusal serialization (R-02).**

* **One transaction.** An unauthenticated quarantine refusal checks for an
  existing audit row and inserts one in the same transaction.
* **Tried lock.** A transaction-scoped advisory lock on (device, reason)
  protects that check and insert. Its class id is `0x53515246`, and it is
  taken with `pg_try_advisory_xact_lock`.
* **Exactly one row.** At most one audit row and one WARNING line exist per
  device, reason and budget window, also under concurrency.
* **Contention.** A request that finds the lock held never waits and writes
  nothing. Its response is unchanged: 400 `MALFORMED_BATCH`,
  `UNORDERED_BATCH` or `QUARANTINE_REFUSED`, or 429 `RATE_LIMITED`.
* **Best effort.** The upload budget remains a best-effort count, as before.
