# Open questions and remaining prerequisites (identity addendum and release)

Date: 2026-10-01. Language: English. Prepared by PLAN-SYNC (documentation only). Baseline: the
P4-4-C4 review archive `1ccb366e…2903f`.

## 0. Status update after the owner's implementation handoff (2026-10-01)

Source of every resolution below: the owner's handoff
identity implementation handoff of 2026-10-01, recorded as amendment A-13
(`requirements_overlay_2026-10-01_A13_identity_implementation.md`, decision gate §17.11). The
original rows in §1 to §4 are kept unchanged as the PLAN-SYNC record.

| ID | Status now | How it is settled, or what is still missing |
| --- | --- | --- |
| API-01 | **OPEN** | The authentication response (token field, lifetime, renewal, errors) is still not supplied. The adapter takes a configurable, explicitly validated mapping (`MINISTRY_NIN_API_AUTH_TOKEN_PATH`, optional expiry path and format) and stays unavailable until it is configured. The exact input still needed is listed in the integration guide §3. |
| API-02 | **OPEN (narrowed)** | Only DD/MM/YYYY is parsed when `presume` is false. Any other format goes to manual review (`AMBIGUOUS_DATE`). Real examples of other formats are still needed before more formats are accepted. |
| API-03 | RESOLVED (A13-08, A13-09) | Owner rule: `presume` true ignores the ministry date; explicit flag parsing; unknown or absent flag goes to manual review. |
| API-04 | QUALIFIED (A13-11) | The owner reports no usage limits. Access and network requirements, availability and the error contract stay unknown; client timeouts, bounded concurrency and bounded retries apply regardless. |
| API-05 | RESOLVED (A13-10) | Copy artifact; fixtures corrected; malformed live responses are INVALID_RESPONSE. |
| MATCH-01 | RESOLVED (A13-04) | Case and whitespace only; no fuzzy threshold. |
| DOC-02 | RESOLVED for formats and limits (A13-05) | JPEG/PNG, the existing 8 MB identity-page limit, private storage and scanning are reused. Masking: no image masking is performed; access is restricted instead. Retention periods stay with OD-007. |
| REVIEW-01 | RESOLVED (A13-05) | Mapping in `docs/security/identity_review_permissions.md`; preset reasons in the review UI. |
| UAT-02 | **OPEN** | No approved environment or authorized provider and mail test method. |
| MIN-01 | RESOLVED (A13-03) | Retained facts listed in the A-13 overlay §2 (FR-IDV-005). |
| SCOPE-01 | RESOLVED (A13-01) | NIN route mandatory for Algerian nationals; staff-assisted exception only. |
| SEQ-01 | RESOLVED (A13-07) | IDV-1 to IDV-4 before P4-5, in one assignment. |
| STUB-01 / F-PS-01 | RESOLVED (A13-06) | Implemented in this assignment: static validation refuses every non-official backend except the disabled one in staging and production. |
| ASYNC-01 | RESOLVED for identity verification (A13-14) | Investigated: Django runs `on_commit` callbacks after commit, and an exception raised by a callback propagates to the request unless the callback is registered `robust=True`. The identity dispatch is robust and swallows broker errors; a sweeper recovers the durable job. The generic communication dispatch was made robust too (IDV-Q8); other `on_commit` users are unchanged (finding recorded in the report). |

Rules:

* No answer below is invented.
* A missing input blocks only the behaviour that depends on it; it does not block documentation or
  the design of synthetic tests (addendum §8).
* Nothing here is approved merely by being listed.

## 1. Identity inputs from the owner's addendum (§3, §8)

| ID | Required input or decision | Blocks | Owner |
| --- | --- | --- | --- |
| API-01 | A sanitized authentication response: token field, expiry, renewal and error behaviour | Real adapter authentication (IDV-2) | Owner / provider |
| API-02 | Real examples of nonuniform dates, and the authoritative format order | Safe automatic birth-date matching (IDV-1, IDV-2) | Owner / provider |
| API-03 | The meaning and handling of presumed or partial birth dates and of `presume` | Automatic-versus-manual routing | Owner / provider |
| API-04 | Rate limits, access and network restrictions, and the error contract | Retry policy and production configuration | Owner / provider |
| API-05 | Whether the trailing comma in the supplied response example is a copy artifact (addendum §3). A malformed production response is never repaired into success | The INVALID_RESPONSE contract and test fixtures | Owner |
| MATCH-01 | The exact safe name-correction policy | Automatic correction beyond whitespace and case normalization | Owner |
| DOC-02 | Allowed card and passport formats, size limits, masking and the retention mapping | The evidence upload (IDV-3); project defaults apply where they fit | Owner |
| REVIEW-01 | The concrete permission mapping and the required manual-confirmation reasons | The review UI and services (IDV-3) | Owner |
| UAT-02 | An approved test environment, synthetic accounts, and the authorized provider and mail test method | Real integration and operational acceptance (IDV-4, P4-6) | Owner |

## 2. New questions found by PLAN-SYNC

They do not change any owner decision.

| ID | Question | Why it is needed | Recommendation, not a decision | Blocks |
| --- | --- | --- | --- | --- |
| **MIN-01** | Which official facts are retained after a lookup: Latin names, Arabic names (`nom_a`, `pren_a`), the normalized birth date, and the original date string as restricted evidence? For how long? | PRD FR-IDV-005 and IR-NIN-002 allowed only "status, reference and comparison result". The addendum (§4) allows the "minimum official facts needed for matching". Official Arabic names also relate to decision D-02 (no Arabic-script name field in V1). | Retain only the facts that a correction can apply (Latin names, normalized date), plus a restricted original date string when a date is ambiguous. Store Arabic names only if staff review needs them, and never as a participant-editable field. | The IDV-1 data model |
| **SCOPE-01** | May an Algerian national use the passport path, and which verification applies then (manual passport review, as IDV-08, or the NIN path)? | Today the NIN path is limited to Algerian nationals, and the passport path is open to all, including Algerians. IDV-08 covers "every foreign participant". | Require the NIN path for Algerian nationals, with a staff-assisted exception route. Owner to decide. | IDV-1 path rules; IDV-3 |
| **SEQ-01** | Do IDV-1 to IDV-4 run before P4-5, or between P4-5 and P4-6? | The addendum requires only "before the final system-test/review packages" (P4-6 to P4-9). P4-5 (runbooks, recovery, load, rehearsals) would have to cover the new worker and provider. | Run IDV-1 to IDV-4 before P4-5, so that P4-5 documents the final topology once. | Sequencing of P4-5 |
| **STUB-01** (finding F-PS-01) | Authorize a bounded correction that refuses `LocalStubNinProvider` (and every stub) in staging and production at startup, and that keeps stub results from being presented as official verification. | `NIN_PROVIDER_BACKEND` defaults to the stub. `config/settings/validation.py` does not refuse it, and a stub MATCH marks the identifier `VERIFIED`. This contradicts addendum §6 ("prohibit successful mock verification in production"). | A small correction before any staging deployment that exposes registration (or at the start of IDV-2), following the existing pattern for `MALWARE_SCANNER_BACKEND`. | Staging registration testing; release |
| **ASYNC-01** (observation carried from P4-4-C3) | How should an on-commit Celery `.delay()` behave during a broker outage? It probably makes a web request fail after its data was saved (unverified). | Post-submission identity verification adds one more on-commit enqueue in the public path. | Investigate and decide in IDV-2 (for example, an outbox-only enqueue with a sweeper). | IDV-2 design |

## 3. Open items carried forward (unchanged by PLAN-SYNC)

| ID | Item | Source | Blocks |
| --- | --- | --- | --- |
| STEPUP-01 | A genuine step-up provider for the emergency wipe, or owner acceptance that it is unavailable | Brief §D; `release_readiness` | Release |
| CACHE-02 | Confirm or replace the counter figures against the real process count | Brief §D; ADR-0025 | Capacity acceptance |
| REDIS-01 | Provision the counter Redis (TLS, separate database, `noeviction`) and route the alerts | Brief §D; staging guide | Staging acceptance |
| OD-007 | Legally approved retention periods and the purge job (also needed for identity evidence) | Brief §B | Release |
| C-09 | Controller, rights contact, legal references and transfers | Brief §B | Public launch |
| C-01 | Official country catalog | Brief §B | Release |
| OD-006 | Hosting, data location, key custody, backup and PITR | Brief §B | Deployment |
| OD-001 / INFRA-001, OD-008 | Volumes, gates, operators, devices, network | Brief §B | Capacity and hardware acceptance |
| HSTS-01, COMM-01, SIGNIN-01, IDX-01, DRILL-01, FOOTER-02, C-08 | As listed in the P4-4-C1 owner brief §B | Brief §B | As listed there |
| OBS (P4-4-C2) | Malformed readiness `items` robustness in `scripts/check.py` | P4-4-C2 report §8 | Nothing now |
| OBS (P4-4-C3) | `docs/architecture/README.md` has no ADR-0024 row | P4-4-C3 report §10 | Nothing now |

## 4. Evidence still missing

**Before staging acceptance:**

* real Redis behaviour of the challenge counter: cross-process atomicity, expiry, outage and
  recovery, the probe key, TLS and `noeviction` (NOT_PROVEN);
* real participant OTP email delivery (provider, sending domain, mailbox);
* a staging account-provisioning procedure and the UAT-01 role and account scenarios;
* the STUB-01 correction;
* REDIS-01.

**Before production release (beyond the above):**

* the identity verification feature itself (IDV-1 to IDV-4), with authorized real-provider evidence
  (UAT-02);
* STEPUP-01, OD-007, C-09, C-01, OD-006 and the capacity figures;
* database-role isolation for the audit table;
* proxies, HSTS, the restore drill and rehearsals;
* P4-5 to P4-9;
* the separate go/no-go decision.

`manage.py release_readiness` currently reports **BLOCKED** (P4-4-C4 evidence). PLAN-SYNC does
not change that, and nothing here is a release approval.

## 5. Questions found during the implementation (IDV-1 to IDV-4)

Recommendations only; nothing below is decided. Each one blocks only what it names.

| ID | Question | Current behaviour | Recommendation |
| --- | --- | --- | --- |
| IDV-Q1 | Must an APPROVED participation decision require a verified identity? | Not enforced: identity and participation stay separate (IDV-10); the identity status is shown on the review case page. | Decide; if yes, a small gate in `record_approved_decision`. |
| IDV-Q2 | Should a final identity rejection notify the participant? | No message (public status never shows identity outcomes); managers decide participation separately. | Notify through the participation decision only. |
| IDV-Q3 | An Algerian national with no usable NIN cannot complete self-service registration. | The staff exception covers NIN-route cases in manual review only. | Decide whether staff may unlock the passport route for a specific draft. |
| IDV-Q4 | Registrations submitted before this change have no identity case. | Not forced (no backfill); no production data exists. | Confirm no backfill is needed. |
| IDV-Q5 | New interface strings are English in FR/AR (no translation pass, as instructed). | gettext-marked; catalogs unchanged. | Commission the FR/AR translation pass. |
| IDV-Q6 | Retention periods for identity evidence and retained official facts. | Existing document and identity lifecycle; no period invented. | Part of OD-007. |
| IDV-Q7 | Development databases may hold identifiers verified by the former stub. | Not rewritten by the migration. | Recreate or review development data; never applies to production. |
| IDV-Q8 | Communication dispatch: `messaging.queue_communication` registered a non-robust `on_commit` `.delay()`, so a broker outage after commit could turn a saved action into an error page (ASYNC-01 finding). | **Fixed in this assignment**, because the identity return and resubmission queue emails through it: the callback is robust, swallows the broker error and leaves the PENDING outbox event for the existing `dispatch_pending_communication_events` sweeper. Other `on_commit` users (OTP delivery, confirmation, offline package build, invitations) were not changed. | Review the remaining `on_commit` users in P4-5. |
| IDV-Q9 | Real SMTP: the settings read only `EMAIL_BACKEND` and `EMAIL_PROVIDER_API_KEY`. | A local relay, or a settings change, is needed for real delivery. | Decide with the mail provider choice (UAT-02). |

## 6. Status after the independent IDV review and IDV-C1 (2026-10-02)

Source: the independent review of the IDV package (verdict CHANGES_REQUIRED) and its bounded
correction IDV-C1, which fixes R-IDV-01 to R-IDV-06 only. The owner decisions below are **not**
part of IDV-C1: the developer's IDV-C1 instruction keeps IDV-Q1, IDV-Q2 and IDV-Q3 unresolved
unless they are explicitly approved. Nothing here is decided by being listed.

| ID | Status | Current behaviour (accurate) | Review recommendation, not a decision |
| --- | --- | --- | --- |
| IDV-Q1 | **OPEN** (owner decision) | A participation APPROVED decision does not require a verified identity. A pending, rejected or exhausted identity can still receive participation approval. Identity is shown on the participation review case page. | Keep the states separate, but require a valid verified identity for APPROVED, at the decision service and the relevant downstream eligibility, with explicit handling of old records and of simulated sources. |
| IDV-Q2 | **OPEN** (owner decision) | A final identity rejection sends no participant message and is not shown publicly; "return for correction" remains the remediable path on the same registration. The silence is current behaviour, not a settled decision. | A safe participant-visible final outcome and an idempotent notification, without ministry data or internal notes. |
| IDV-Q3 | **OPEN** (owner decision) | The staff exception handles an existing NIN-route case in manual review. It does not help an Algerian without a usable NIN, who cannot complete self-service registration. | Extending it needs a concrete owner-approved exception policy; no fabricated NIN and no unrestricted passport bypass. |
| IDV-Q4 | OPEN (owner input) | Registrations submitted before IDV have no identity case; nothing is backfilled. | The owner confirms the actual data situation. If records exist, design and test an explicit approach; never an automatic development-database migration. |
| IDV-Q5 | Unchanged | New strings, including the IDV-C1 ones, are English in FR and AR. | A separate translation pass; not a reason to fail this bounded assignment. |
| IDV-Q6 | Unchanged | Legal retention stays OD-007. | No invented approval. |
| IDV-Q7 | OPEN (procedure) | Identifiers that the former stub marked VERIFIED in a development database are not rewritten. Simulated results are labelled `SIMULATED_API`, including the official-name form decision (`reason_code = SIMULATED_API`). | Reset development data to synthetic seeds before any evidence run; never present stub- or simulation-verified data as official. |
| IDV-Q8 | Unchanged | The communication outbox dispatch is robust; other `on_commit` users remain a later-phase review. | Consistent with the identity scope. |
| IDV-Q9, API-01, API-02, API-04, UAT-02 | **OPEN** (external input) | Not proven by synthetic tests: the mail settings, the authentication response, expiry and errors, other non-presumed date formats, and authorized integration access. | No provider fact is invented. |

New questions found during IDV-C1:

| ID | Question | Current behaviour | Recommendation |
| --- | --- | --- | --- |
| IDV-Q10 | A participant who types a DIFFERENT NIN in a new draft for another event gets a second current NIN identifier (the draft step creates it without replacing the first). Should a new draft's different NIN replace the person's existing one (with the IDV-C1 rebinding) or be refused for review? | Unchanged by IDV-C1 (the draft step never marks an identifier REPLACED). Each case checks the identifier it was submitted with; duplicates across persons are still caught. | Decide; until then reviewers see each case's own NIN. |
| IDV-Q11 | Is the official-name form also wanted from the development simulation? | Applied locally and in tests, labelled as a simulation (`SIMULATED_API`). Staging and production refuse simulations at startup. | Keep (it lets local UAT show the behaviour), or limit it to the official adapter. |
| IDV-Q12 | Name resolution for the ministry host cannot be interrupted. At the deadline the request ends, but the resolver thread is abandoned until the system resolver gives up. | Bounded number of such threads (one per timed-out request, at most the provider concurrency at a time). | Accept, or require a resolver with short timeouts on the worker hosts (operations). |
| IDV-Q13 | A withdrawn or not-approved registration whose identity was already verified keeps that verification as a fact. | Decided identity cases are not changed by the closure; open ones go to manual review (`REGISTRATION_CLOSED`). | Confirm. |

## 7. Owner decisions IDV-Q1 to IDV-Q3 (approved 2026-10-02) and their implementation

Source: the owner's approval of 2026-10-02 in the developer's implementation instruction for
IDV-Q1, IDV-Q2 and IDV-Q3. The implementation and its evidence are in
the historical review record `phase_04_idv_q_owner_decisions_report.md` (kept outside the repository) (package IDV-Q), and ADR-0026
decisions 19 to 22. The rows of §5 and §6 are kept unchanged as history.

| ID | Status | Owner decision (as approved) | Implemented behaviour |
| --- | --- | --- | --- |
| IDV-Q1 | **OWNER-APPROVED 2026-10-02; implemented (IDV-Q), awaiting review** | A final participation APPROVED decision requires a current, valid, verified identity for that registration, enforced in the decision service; downstream eligibility guarded too; legacy registrations handled explicitly; a development simulation is never official evidence. | `record_approved_decision` refuses (before any write, audited) unless the identity is cleared: a verified case, a still-VERIFIED current identifier, an unexpired document, and no simulated source outside local and test. The same clearance is part of the shared eligibility predicate (passes, badges, entry, offline packages), of `evaluate_eligibility` and of the offline re-evaluation. A registration without an identity case is refused as `NO_IDENTITY_CASE` and is not eligible (no backfill: IDV-Q4 stays open). |
| IDV-Q2 | **OWNER-APPROVED 2026-10-02; implemented (IDV-Q), awaiting review** | A final identity rejection has a safe participant-visible outcome and an idempotent notification: the registration was rejected, and how to register again from the same account; no ministry data, full identifier, evidence or staff note; consistent registration lifecycle; return for correction stays distinct. | The rejection records a NOT_APPROVED participation decision (`IDENTITY_REJECTED` / `IDENTITY_NOT_VERIFIED`) in the same transaction, superseding an earlier approval; the context is released (non-current) so the participant can "Register again" (a new draft on the same account) while the public channel is open; one `IDENTITY_REJECTION` email (EN/FR/AR placeholder copy, communications review pending) keyed on the identity decision. The workspace, the identity review screen and the review case agree. A rejected registration cannot be reopened. |
| IDV-Q3 | **OWNER-APPROVED 2026-10-02; implemented (IDV-Q), awaiting review** | A narrow staff-authorized manual path for an Algerian participant without a usable NIN: completion of the registration, manual documentary verification, explicit reason, reviewable evidence, scoped permission, audit, clear queue source and status; never an invented NIN, never a NIN API call, no unrestricted passport bypass; the foreign-national route unchanged. | `people.NinExemption`, granted per draft by holders of `people.grant_nin_exemption` (Accreditation Managers) in exact scope, with reason, explanation and confirmation; revocable while a draft. The participant declares an Algerian identity card (new identifier type `NATIONAL_ID_CARD`) or an Algerian passport, with a photo; the case is `NIN_EXEMPTION` / `NIN_EXEMPTION_REVIEW`, verified only from that document (`MANUAL_NIN_EXEMPTION`); no job, no NIN. Procedure: §7.1. |

### 7.1 Operational procedure for the NIN exemption (IDV-Q3)

1. The participant starts a registration, cannot pass the NIN field, and contacts the
   registration team with the reference shown under the NIN field.
2. An Accreditation Manager finds the draft by its exact reference (Registration intake,
   "Find by registration reference"), checks that the person is an Algerian national who cannot
   supply a usable NIN (for example, an identity document that shows no NIN), and grants the
   exemption with a preset reason and an explanation.
3. The participant reopens the identity step, chooses "Another official Algerian document (no
   NIN)", declares an Algerian national identity card or passport and uploads its photo, then
   completes and submits the registration.
4. A reviewer opens the case from the identity queue (route filter "Algerian document without
   NIN") and verifies it from the declared document only, returns it for correction, or rejects
   it finally (IDV-Q2).
5. A grant given by mistake is revoked from the same intake page while the registration is a
   draft. A used grant is history and stays attached to the case.

### 7.2 Still open (not closed by this package)

* IDV-Q4 (backfill of registrations without an identity case): unchanged. Such registrations
  are now refused for approval and not eligible downstream; the owner still confirms whether any
  exist and how they are handled.
* IDV-Q5 to IDV-Q13: unchanged. The new interface strings are English in FR/AR (IDV-Q5); the
  identity-rejection email has EN/FR/AR placeholder copy for communications review.
* External inputs IDV-Q9, API-01, API-02, API-04, UAT-02: unchanged; nothing here is provider,
  mail or staging evidence.

**Update 2026-10-02:** IDV-Q17 and the placeholder status of the rejection email copy were
decided by the owner on 2026-10-02 (§9).

New questions found while implementing:

| ID | Question | Current behaviour | Recommendation |
| --- | --- | --- | --- |
| IDV-Q14 | Should an identity document expiring before or during the event block approval and entry? | Clearance requires an unexpired document at the time of the check (approval, and every downstream read). A passport valid at submission but expiring before the event stops eligibility on its expiry date. | Confirm, or define a reference date (for example the event end) and a re-verification path. |
| IDV-Q15 | Should the gate tell entry operators that the identity, not the approval, is the blocker? | The shared predicate fails, so entry reports the existing `REGISTRATION_NOT_APPROVED` reason (no new entry or offline-package code). | Keep, or add a dedicated entry reason (entry and offline contract change). |
| IDV-Q16 | May the NIN exemption accept a non-Algerian passport of an Algerian national (dual national)? | No: only an Algerian card or an Algerian passport (issuing country DZ). | Confirm. |
| IDV-Q17 | Should "Register again" also be offered for an invitation or on-behalf registration rejected on identity grounds? | The new registration is an open (public) registration, offered only while the public channel is open; an invitation context is not recreated. | Confirm, or route such participants to the inviting organization. |

## 8. Consolidated correction IDV-Q-C1 (2026-10-02)

IDV-Q1, IDV-Q2 and IDV-Q3 stay owner-approved; nothing here reopens them. IDV-Q-C1 corrects three
findings in their implementation (ADR-0026 decisions 23 to 25; report
the historical review record `phase_04_idv_q_c1_report.md` , kept outside the repository):

* **Lock order.** Review commands lock the Registration before their own case or request, so a
  final identity rejection can no longer deadlock with them.
* **Bound identifiers.** A draft or correction never changes the validity of an identifier that
  a submitted case uses. Another registration's eligibility changes only through an authorized
  verification.
* **Register again by origin.** An invitation registration registers again through its own,
  still valid invitation, or is told that a new invitation is needed. The public channel is never
  used as a way around the invitation.

### 8.1 Operational procedure: a rejected participant who was invited

1. The workspace shows the participant's next step.
2. If their invitation is still valid, they use **Register again with your invitation**. Capacity
   is checked again at the new submission.
3. Otherwise the workspace says that a new invitation is needed. The inviting organization (or
   the registration team, for the organization) issues one, from the campaign's link management:
   * rotate or reissue the campaign link;
   * check that the campaign is ACTIVE, valid and has a place.
4. The participant opens the new link while signed in to the same account, then uses **Register
   with this invitation** on the workspace.
5. A registration created on behalf or by delegation has no self-service restart. The workspace
   tells the participant to contact the organization or the registration team.

### 8.2 Status of the rejection email copy

The `IDENTITY_REJECTION` template (EN/FR/AR) is `v1-draft`, PUBLISHED only so the local pipeline
renders it. When this section was written it was not approved for real delivery; **the owner
approved the exact wording on 2026-10-02 (COMM-IDV-01, §9)**. No real mail was sent and no
staging delivery was tested; real delivery stays IDV-Q9 / UAT-02.

### 8.3 Items for the owner (recorded separately; nothing is decided by being listed)

**Update 2026-10-02:** all four items were decided by the owner; see §9. The rows below are kept
as the question record.

| ID | Question | Current behaviour | Recommendation |
| --- | --- | --- | --- |
| COMM-IDV-01 | Approve the identity-rejection message copy (EN/FR/AR) for real delivery | Placeholder copy (`v1-draft`): it points to the workspace and mentions that an invitation may need renewing | Communications review of the three texts before any real sending |
| IDV-Q17 | (from §7.2) Register again for invitation and on-behalf registrations | **Addressed by IDV-Q-C1:** the same invitation while still valid, otherwise a new invitation from the organization; on-behalf and delegation: contact the team | Confirm |
| IDV-Q18 | When staff verify a newer declaration of the same document (another expiry) in one registration, the person's older verified registration returns to manual review and needs its own verification | Conservative: the older case is rebound with a recorded decision; its eligibility returns after re-verification | Confirm, or allow a reviewed carry-over of the new expiry |
| IDV-Q19 | Should a participant opening a new invitation while signed in be taken straight into the invited registration? | The workspace offers **Register with this invitation** (one more click, explicit) | Confirm |

## 9. Owner decisions of 2026-10-02 on IDV-Q-C1 (COMM-IDV-01, IDV-Q17, IDV-Q18, IDV-Q19)

Source: the owner's decisions dated 2026-10-02, relayed in the developer's staging-preparation
instruction. They accept the behavior implemented by IDV-Q-C1 (ADR-0026 decisions 23 to 25;
the historical review record `phase_04_idv_q_c1_report.md` , kept outside the repository). Recording them changes no behavior, migration or
setting. The rows of §7.2 and §8.3 are kept unchanged as the question record.

| ID | Status | Owner decision |
| --- | --- | --- |
| COMM-IDV-01 | **DECIDED 2026-10-02: approved** | The exact EN/FR/AR `IDENTITY_REJECTION` copy in `communications.0007` is approved for real delivery, subject to normal staging configuration, delivery testing and release gates. Approval of the wording is not evidence that delivery works: real delivery stays NOT_PROVEN until the staging UAT (`docs/testing/phase_04_staging_uat_handoff.md`, check INT-09) records it. The version label `v1-draft` is unchanged; it is a label the earlier seeds share, not a status. |
| IDV-Q17 | **DECIDED 2026-10-02: accepted as implemented** | Origin-aware register again: a usable invitation is revalidated under lock; an unusable invitation requires the inviting organization to issue a new one; on-behalf and delegation registrations direct the participant to the organization or team. This supersedes the earlier §7.2 row (public registration only). |
| IDV-Q18 | **DECIDED 2026-10-02: accepted** | The conservative rule stays: verifying a newer declaration of the same document returns the person's older linked verified cases to manual review, and their eligibility returns only after re-verification. A reviewed carry-over of the new expiry is not adopted. |
| IDV-Q19 | **DECIDED 2026-10-02: accepted** | A signed-in participant who opens a new invitation uses it with one explicit click in the workspace (**Register with this invitation**); there is no automatic jump into the invited registration. |

Still open and not touched by these decisions: IDV-Q4 to IDV-Q16, IDV-Q9, API-01, API-02, API-04
and UAT-02, and every external input and release-readiness blocker (see the handoff guide §6).
