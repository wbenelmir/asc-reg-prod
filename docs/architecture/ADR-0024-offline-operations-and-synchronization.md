# ADR-0024: Offline operations, synchronization and reconciliation

Status: IMPLEMENTED, READY_FOR_LOCAL_REVIEW; not independently approved.
Date: 2026-09-28. Scope: Phase 4 Prompt 3 only. Amended on 2026-09-29 by the
correction 4 and correction 5 addenda below.

## Context and authority

The authoritative scope is the Phase 4 Prompt 3 work-package brief (historical),
an explicitly authoritative preserved authorization message. The original
work-package directory was not supplied. The approved Phase 4 plan and
ADR-0023 remain binding. Event Edge and Prompt 4 are outside this decision.

The uploaded working tree already contained a partial Prompt 3 implementation,
including migration `entry.0005`; this continuation preserves its migration bytes
and completes the runtime, tests, translations and documentation around it.

## Decision

1. Only signed QR input is available offline. A successfully verified, encrypted,
   device-bound package and checkpoint-bound version-2 grant establish local
   authority. P-256 trust roots remain pinned at authorized provisioning. QR and
   package signing use separate key families. No offline identity or directory
   search is introduced.
2. Application heartbeat responses, not `navigator.onLine`, determine reachability.
   Three failed checks spanning 20 seconds enter offline mode. Two successes
   separated by at least 10 seconds permit recovery. Recovery uploads evidence,
   obtains critical data, then restores online mode. A failed critical refresh
   retains recovery state.
3. Each operation gets a random UUIDv4 and a random store identity, monotonically
   increasing sequence, previous digest, and SHA-256 chain digest over the
   previous digest text plus canonical operation bytes. The device signs those
   bytes with its non-exportable ECDSA key, then encrypts the envelope with a
   non-exportable local AES-GCM key. A strict-durability IndexedDB transaction
   commits the operation and chain head together before local success.
4. Web Locks serialize append and upload across tabs. Missing Web Locks blocks
   work; there is no process-local fallback. An admission revalidates the QR,
   authority and decision immediately before recording. Under the append lock,
   local admission evidence is reloaded so another tab's acceptance cannot be
   missed. Unreadable local evidence blocks subsequent admissions.
5. The server has two transactions: immutable intake, then idempotent processing.
   PostgreSQL advisory locks serialize device and operation identity; row locks
   protect current device state, inbox rows and registration admission decisions.
   Unique operation IDs, unique device/store/sequence and one-to-one event links
   are database constraints. Device state is re-read under lock in both stages.
   A known lower pending sequence prevents processing a later one.
6. A signed `ASC-OACK` identifies the exact device, store, operation, sequence,
   payload digest and durable outcome. Losing a response causes a retry of the
   same evidence. Acknowledgement delivery is not an application transaction;
   a repeated submission receives the original durable outcome. Re-signing may
   produce different JWS bytes, but the outcome and processed timestamp do not
   change. No response alone authorizes deleting local evidence.
7. Cleanup re-verifies the stored signed acknowledgement. Pending, in-flight,
   locked, unacknowledged or unreadable records remain. Acknowledged admissions
   remain until the active package covers their local anti-passback evidence.
   Emergency wipe remains the separately signed destructive path of ADR-0023.
8. Explicit reconciliation cases distinguish conflicts, security violations and
   advisories. Original Entry Events and inbox evidence are immutable. A scoped
   supervisor can add an encrypted note or close a case with a required note;
   neither action changes an admission. Case scope is event/venue/gate, with no
   organization-scoped grant of checkpoint authority.

## State and conflict interpretation

Fresh/Aging data and a valid grant allow Offline Active. Stale data permits only
manual review or refusal. Expired data permits an unresolved manual referral,
never a resolved verification or admission. Blocked means no local admission.
Any known rebuild-required delta forbids admission, including override.

Existing temporal rejection precedence is: hard package expiry for any resolved
decision; invalid operator-grant window; then a stale-package admission. If both
a stale package and an expired grant exist, the primary reason is
POLICY_VIOLATION / GRANT_NOT_VALID. The case still records both the package band
and grant expiry. A stale-package-only test must retain a valid grant, obtained
through normal online issuance; changing this test must not relax grant expiry.

Operations from a device that is suspended, revoked, expired or awaiting wipe
are quarantined, never applied. The revoked-device quarantine endpoint accepts
only operations signed by a registered historical device key. It does not
restore authorization. Quarantine cases are separated by device and checkpoint.

The server records actual offline admission facts with explicit conflicts for
revocation/replacement, changed access, duplicate single entry or withdrawn
authority. Policy violations create a security case without an Entry Event.
Non-admissions within the permitted state/grant window remain non-admissions.

## Historical uncertainty

Pass/assignment/restriction timestamps reconstruct facts only to the extent the
source schema preserves them. Mutable rules and registration state are not a
complete event-sourced history. A later relaxation must not validate an earlier
admission silently. Relevant changes in the trigger-written package journal,
rebuild conditions, repeated suspension/resumption evidence, or elapsed journal
retention therefore cause `ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN` when an
admission would otherwise apply normally. This can require review for a benign
later edit. It is a conservative classification, not a new admission outcome.

QR verification-key revocation/window changes are evaluated explicitly. A key
revoked only after the admission does not retroactively prove it invalid then.
Clock anomalies and sequence/chain gaps remain explicit evidence, never repaired
by changing an operation timestamp or sequence.

## Privacy and scope

Only contract-validated signed fields enter inbox JSON. Arbitrary malformed or
unsupported payloads and invalid signatures retain digest/size/outcome evidence,
not their submitted fields. Override notes are separate and encrypted. Logging
reports failure class names without exception text or database payloads.

Cross-event credential claims create a security case without a foreign pass or
registration relationship. Cross-device or cross-checkpoint ID reuse creates
local rejection evidence without linking the original operation or disclosing
its device/status/digest. No raw QR, identity number, lookup digest, contact data
or document enters the local operation contract.

## Migration and verification boundary

`entry.0005_offline_sync_and_reconciliation` introduces inbox, cases, actions and
links, offline Entry Event/override coherence and telemetry mode. Triggers keep
evidence append-only. Its reversal guard runs before dropping evidence and
refuses reversal once any synchronized evidence exists.

*Historical (2026-09-28, continuation host). The next paragraph is superseded by
the verification notes at the end of this ADR; it is kept unchanged.*

The migration was present in the upload and has not been rewritten. Source SQL
was inspected. Live SQL generation, forward/reverse/forward and concurrency
proofs remain blocked by the absence of PostgreSQL in this execution environment.
Added tests are not substitutes for running them. See the Prompt 3 report for
actual check results and the manual record for required Windows verification.

## Correction 4 addendum (2026-09-29)

These notes record refinements made under the developer's correction-4
authorization (findings R-01 and R-02 of the final independent review). They do
not change decisions 1 to 8.

**Override revalidation against the device's own package (R-01).** An offline
override is checked against the override catalogue of the package the device
used, as the server can establish it from the trigger-written journal:

* The catalogue-independent checks come first and are unchanged. The local
  result must be non-admittable. The grant must permit overrides. The blockers
  must be present and must not include a never-overrideable code. A supplied
  note must match its signed digest. A non-overrideable restriction remains a
  security conflict.
* If the journal shows no change to the named reason since the package
  snapshot, the current row is exactly what the package carried. The reason
  must then exist, be active, cover every local blocker and satisfy its note
  rule. Otherwise the result is `SECURITY_CONFLICT / POLICY_VIOLATION /
  OVERRIDE_NOT_PERMITTED`, with no Entry Event. A reason that was inactive or
  absent when the package was built can never produce an admission, even
  after an unrelated reason changes.
* In some cases the server cannot know that catalogue entry:
  * the named reason changed after the snapshot (added, narrowed,
    note-tightened, deactivated or removed);
  * the code has no row and the catalogue changed;
  * the journal can no longer attribute changes (a fail-closed rebuild
    reason, or elapsed retention).

  The operation then follows the changed-rule path, `CONFLICT / ACCESS_CHANGED
  / HISTORICAL_ACCESS_UNCERTAIN`, unless a more specific conflict applies
  first. That path records an offline Entry Event marked `offline_conflict`,
  its Entry Override and an open case. It is never applied as a clean
  admission and never recorded as a device policy violation.

  *Correction 5 note: refined by developer decision D2 (option A2). A reason
  added after the snapshot and never changed is now checked against its only
  state. See the correction 5 addendum.*
* A reason deleted after the snapshot cannot be linked. An Entry Override must
  name a catalogue reason, and an admission on a non-admittable result must name
  its override (`entry_event_admit_requires_allowed_or_override`). Without a
  schema change, that conflict keeps the physical fact in the immutable
  operation and its open case (`override_reason_unavailable`). The approved
  mapping already does this for an override that cannot be linked. The server's
  Entry Event history does not show that admission, but the case does. This is
  a documented limitation.

  *Correction 5 note: this bullet is inaccurate in three respects, and the
  text is kept only as history:*
  * *The approved mapping (plan §6) records an Entry Event for a changed-rule
    CONFLICT. It keeps the fact only in SyncOperation for SECURITY_CONFLICT
    rows, so it does not "already do this". This bullet also contradicted
    "State and conflict interpretation" above.*
  * *A renamed or moved reason leads to the same path.*
  * *The consequence was not only documentary. No later admission decision
    read the operation or its case (review probes P1, P2 and P4).*

  *See the correction 5 addendum.*

**Unauthenticated quarantine traffic (R-02).** A quarantine request stays
unauthenticated until one operation signature verifies. Until then it:

* stores nothing;
* never spends the device's upload budget;
* is audited at most once per device, reason and budget window;
* stops signature verification at the first operation no registered key
  verifies, so no operation is processed after one without a durable outcome.

The authenticated synchronization endpoint keeps its per-request refusal audit.

*Correction 5 note: the third point held only for sequential requests.
Concurrent requests could each record a row (review probes P9 and P10). It is
now guaranteed; see the correction 5 addendum.*

## Correction 5 addendum (2026-09-29)

These notes record the developer's decisions on the focused independent review
of correction 4 (the historical review record `phase_04_prompt_03_correction_04_independent_review.md` , kept outside the repository)
and the resulting refinements. Decisions 1 to 8 are unchanged. No migration was
added or applied.

| Developer decision | Choice |
| --- | --- |
| D2 | Option A2, the INSERT-only refinement |
| D3-A | A qualifying synchronized admission without an Entry Event counts for later admission decisions |
| D3-B | Option S1: one service-level definition, no migration |
| D3-C | SECURITY_CONFLICT operations keep their approved behaviour and stay outside that definition |
| R-02 | A nonblocking PostgreSQL advisory-lock guarantee for unauthenticated refusal suppression |

### Package catalogue evidence (D2)

**Projection timing is not snapshot contents.**

* The watermark snapshot is captured before the first projection query. The
  projection then runs at PostgreSQL's default READ COMMITTED level, so each
  query reads a state at least as new as the snapshot.
* A package therefore carries its event's active reasons as its catalogue
  query read them, not as they stood at the snapshot.
* A reason committed between the watermark and that query is in the package
  while its INSERT is still uncovered (probe P8). An uncovered INSERT is
  therefore never proof of absence.
* Codes are mutable: a rename keeps the row.
* The journal keeps only the operation kind (`I`, `U` or `D`) of each changed
  row, never previous values.

The server records what it can establish as `override_catalogue_evidence`:

| Evidence | Condition | What it establishes |
| --- | --- | --- |
| `UNCHANGED` | No uncovered change to the named row | Exact: the current row is the package's entry |
| `ABSENT` | No row has the code, and every uncovered reason change is an INSERT | Exact: the code was never in the package |
| `INSERTED_ONLY` | The named row's uncovered history is INSERT-only, and no uncovered UPDATE or DELETE touched any reason of the event | Its only state is known; package membership is uncertain |
| `CHANGED` | Any uncovered UPDATE or DELETE: a rename, move, deactivation, edit or deletion | Unknown |
| `UNATTRIBUTABLE` | Elapsed retention, or `SNAPSHOT_MISSING`, `BULK_CHANGE` or `UNTRACKED_CHANGE` | Unknown |

**Provable violations versus conservative uncertainty.**

* **Provable violation.** No state the package could have carried permits the
  override: the `UNCHANGED` row, an `ABSENT` code, or the only state of an
  `INSERTED_ONLY` row. The result is `SECURITY_CONFLICT / POLICY_VIOLATION /
  OVERRIDE_NOT_PERMITTED`, with no Entry Event.
* **Conservative uncertainty.** A conforming override on an `INSERTED_ONLY`
  row, or any override when the evidence is `CHANGED` or `UNATTRIBUTABLE`.
  The result is `CONFLICT / ACCESS_CHANGED / HISTORICAL_ACCESS_UNCERTAIN`,
  unless a more specific conflict applies first. It is never a clean admission
  and never a device policy violation.
* **Unchanged checks.** The catalogue-independent checks, the
  non-overrideable restriction check and every other security check keep their
  order and outcome.

**Validation of the review's A2 wording.** Checking an INSERT-only row against
its only state is sound only when no other row could have carried the same
code in the package. Such a row would need an uncovered rename, move or
deletion before the new row took its code. The inference is therefore used only
when no uncovered UPDATE or DELETE touched any reason of the event. That is the
same condition as for a missing code. Without it, a conforming device could be
accused falsely; the regression `code_reassigned_to_a_new_row` covers this case.

**Remaining D2 limits.**

* A renamed, moved, edited or deleted reason stays unknown. Full precision
  would need the reason code in the journal references. That is a trigger
  change and therefore a migration, which was not authorized.
* A benign edit, such as a label, still makes every later override with that
  reason a reviewable conflict. So does an override with a code that has no
  row after such an edit.
* When a code was reassigned to a new row, a conforming override's Entry
  Override links the row that holds the code now. The case records the
  uncertainty as `CHANGED`.

### Unlinked admission evidence (D3, option S1)

**The schema consequence.** Some changed-rule conflicts have no Entry Event:
those whose override reason can no longer be linked because it was deleted,
renamed or moved after the snapshot. The constraint
`entry_event_admit_requires_allowed_or_override` and the NOT NULL
`EntryOverride.reason` forbid a faithful Entry Event. This is a schema
consequence, not what the approved mapping prescribes for a changed-rule
CONFLICT.

**One definition.** `apps.entry.selectors.admissions` defines the qualifying
"unlinked admission evidence" once. It is a durable, processed ENTRY_DECISION
operation that:

* reports ADMIT;
* is in status CONFLICT or RECONCILIATION_REQUIRED;
* belongs to the registration's own event;
* has no linked Entry Event, checked through the `EntryEvent.sync_operation`
  link itself.

Further rules:

* PENDING, REJECTED, QUARANTINED and SECURITY_CONFLICT operations never
  qualify.
* An operation with a linked Entry Event is counted through that event only.
* Reconciliation cases are not consulted, so closing a case does not erase the
  admission.

**Four consumers.** Each keeps its scope, cutoffs and ordering.

* **Online `assess_context`, step 8.** The rule uses the later of the last
  admission Entry Event and the last unlinked admission:
  * under `SINGLE_ENTRY`, it is the `ALREADY_ADMITTED` blocker;
  * otherwise, it gives the `PRIOR_ENTRY` and `RECENT_REENTRY` advisories.

  `prior_entry` stays an Entry Event or None. The assessment signature
  includes the unlinked admission, so a decision verified before such an
  admission committed is refused as changed.
* **`evaluate_at`, step 8.** Only admissions strictly before the occurrence
  count, never the operation being assessed. `prior_admission` stays an Entry
  Event, because it becomes the new event's `prior_entry_event`. The server
  evaluation records `prior_admission_at` (from either source) and
  `prior_unlinked_admission_at`.
* **The synchronization classifier.** `DUPLICATE_ENTRY` under `SINGLE_ENTRY`,
  and the close-admission advisory, now also consider these operations and
  link them to the case (`ReconciliationCaseOperation`).
  `related_entry_event` stays an Entry Event or None.
* **The Offline Package projection.** `last_admitted_at` is the later of both
  sources.

**Serialization.** Online decisions and synchronization processing both lock
the Registration row, and an operation reaches CONFLICT only inside that lock.
An online decision, or another device's operation, that waits behind such a
commit reads it. Multi-connection regressions prove this.

**Remaining D3 limits.**

* S1 protects admission decisions only. It creates no Entry Event, no Entry
  Override and no attendance or report entry.
* The Entry Event history, unique attendance, exports and the supervisor
  monitor still omit that physical admission; the operation and its case keep
  it. S1 does not claim complete compliance with the Entry Event mapping.
* The online result for such an admission shows the `ALREADY_ADMITTED` reason
  or the advisory. It cannot show "previously admitted at … through …", which
  needs an Entry Event.
* Independent offline devices prepared before the synchronization still admit
  while disconnected (Flow §11.9). The conflict is then classified at their own
  synchronization.
* SECURITY_CONFLICT admissions remain outside the evidence (D3-C, unchanged).

### Unauthenticated refusal suppression (R-02)

**Mechanism.** The existence check and the insert of an unauthenticated
refusal audit run in one transaction. A transaction-scoped advisory lock keyed
by (device, refusal reason) protects them:

* its own class id, `0x53515246` ("SQRF");
* taken with `pg_try_advisory_xact_lock`, so it is only ever tried.

**Guaranteed.**

* At most one audit row, and one WARNING line, per device, reason and budget
  window, also under concurrency.
* A request that finds the lock held never waits and writes nothing. It is
  refused exactly as before.

**Unchanged.** The authenticated endpoint's per-request audit, the upload
budget (which counts only batches that ran) and the verified-prefix handling.

**Remaining best-effort limits.**

* The upload budget itself counts committed batches without a lock.
  Concurrent genuine batches can overshoot it by their concurrency. This is
  pre-existing.
* A 32-bit object-id collision can skip an optional audit. This happens only
  with another (device, reason) pair that is recording at that moment; a later
  refusal then records it.
* A replayed, captured genuine operation still spends that revoked device's
  budget. This is pre-existing.
* A forged tail after a verified prefix still leaves no audit. This is
  pre-existing.

## Verification notes (updated 2026-09-29)

On the developer's Windows host, with PostgreSQL 17 and Chromium, the local
acceptance gate of the correction-3 local verification passed. It included:

* the 0005 forward/reverse/forward tests with no evidence present;
* the explicit refusal once evidence exists;
* the multi-connection concurrency proofs.

Correction 4 added:

* live `sqlmigrate entry 0005` output, forward and backwards, which is
  generated and not applied;
* a live-server Chromium test of the complete browser-to-server
  synchronization path.

It then ran the gate again. Current results and the location of every required
report item are in the historical review record `phase_04_prompt_03_evidence_index.md` (kept outside the repository).
Migration `entry.0005` has still not been applied to the development database.

Correction 5 added the D2, D3 and R-02 regressions described in its addendum.
They include real multi-connection proofs of the watermark window, the
Registration-row serialization and the refusal lock. It then ran the gate
again. See the historical review record `phase_04_prompt_03_correction_05_report.md` (kept outside the repository). No
migration was added, and `entry.0005` is still not applied to the development
database.
