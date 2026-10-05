# Requirements overlay 2026-10-01: operational sign-in, challenge counter and identity verification

Date: 2026-10-01. Language: English.

* **Prepared by:** PLAN-SYNC (documentation only).
* **Baseline:** the P4-4-C4 review archive, SHA-256
  `1ccb366eb6b9a89fe0702212fdd86fc3c0ab0a0360b16da83556790902e2903f`.
* **Authority:**
  * the owner decisions in `docs/execution/identity_verification_plan_addendum.md` (v2.1);
  * the decision gate records `docs/execution/UX_REMARKS_DECISION_GATE.md` §17.9 (amendment A-11,
    CACHE-01) and §17.10 (amendment A-12, the identity decisions).

## How this overlay works

* The specification files in `docs/specifications/` are read-only (the project engineering rules;
  `docs/specifications/README.md`: "Do not edit these files during implementation"). **None of
  them was edited.** This overlay is the dated amendment that governs the listed sections. A
  section that is not listed here is unchanged.
* For a listed requirement, the overlay wins over the original text, in the same way that the
  earlier amendments A-01 to A-11 do. The specification precedence of the project engineering rules (PRD, then TRD,
  UI/UX, Application Flow, Schema and Implementation Plan) still applies to everything else.
* Nothing here claims an implementation, a schema change or provider proof. PLANNED REVISION means
  the change is to be designed in IDV-1 and implemented in IDV-2 to IDV-4, under separate
  authorization.

**Status vocabulary**

| Status | Meaning |
| --- | --- |
| SUPERSEDED | The original requirement no longer applies, as of the date and decision cited. The original text stays in the file for history. |
| REFINED | The requirement stands; the decision narrows it or adds conditions. |
| POLICY SET | The original deliberately left a policy to a later approval; the decision is that approval. |
| CONFIRMED | Unchanged. Listed because a decision relies on it. |
| PLANNED REVISION | A design or schema change that a later IDV stage will specify and implement; nothing is applied. |
| PRE-EXISTING | Already superseded by an earlier amendment; listed only to avoid a false contradiction. |
| HISTORICAL | Describes an earlier project state; the current state is in the project status record (historical). |

## 1. `01_PRD.md`

| Section and ID | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| Authentication and Account Management, FR-AUTH-001 to FR-AUTH-003 | Applicant email OTP, expiry, limits | CONFIRMED | Unchanged. Real mail delivery needs separate evidence. | AUTH-02 |
| FR-AUTH-008 | Separate administrative authentication flow | CONFIRMED | Unchanged. | AUTH-03 |
| FR-AUTH-009 | "shall enforce MFA for privileged operational accounts and support future SSO integration" | SUPERSEDED (MFA part), 2026-10-01 | Operational accounts sign in with email and password in staging and production. Future SSO integration remains supported. A sensitive operation that requires an MFA step-up (the emergency wipe) keeps that requirement and fails closed without a genuine provider. | AUTH-01 (A-11), AUTH-04 |
| FR-AUTH-010 to FR-AUTH-012 | Account dates, denial, audit | CONFIRMED | Unchanged. | AUTH-03 |
| Identity Verification, FR-IDV-001 | Apply the NIN flow when the approved rule requires it | REFINED | The NIN path applies to Algerian nationals. Whether an Algerian national may instead use the passport path is open question SCOPE-01. | IDV-01, IDV-08 |
| FR-IDV-004 | Submit the minimum required values to the approved service | REFINED | Per the observed contract, only the NIN is transmitted (in the lookup path). Names and date of birth are compared locally and never sent. | IDV-01, IDV-02 |
| FR-IDV-005 | Retain only status, reference, timestamp and approved comparison result | REFINED | The minimum official facts needed for matching and approved corrections may be retained, separately from the submitted facts, with source and history, and protected at least like the existing identity data. The complete provider response is never retained. The exact retained field list is MIN-01 (IDV-1). | IDV-03, addendum §4 |
| FR-IDV-006 | Successful verification removes the default identity-card copy | CONFIRMED | Unchanged. A card is requested only for manual review (DOC-01). | DOC-01 |
| FR-IDV-007 | Unavailable, unidentified, invalid or inconclusive results go to manual examination | REFINED | Also routed to manual review with a reason, never to automatic approval or rejection: NOT_FOUND (HTTP 200 with `identite: null`), INVALID_RESPONSE, PROVIDER_IDENTITY_MISMATCH, DATA_MISMATCH, AMBIGUOUS_DATE, authentication errors and exhausted transient failures. A technical failure is never NOT_FOUND. | IDV-02, IDV-04 |
| FR-IDV-008 | Passport flow for international participants per the approved rule | REFINED | Every foreign participant follows manual passport verification, with zero ministry API calls. | IDV-08 |
| FR-IDV-010 | Passport identity-page copy only under an approved policy or a targeted request | POLICY SET | The approved policy requires the identity page from every foreign participant. | IDV-08, DOC-01 |
| FR-IDV-012 | A change invalidates the prior verification and starts reverification | REFINED | A change creates a new identity revision. A result for an older revision is ignored (stale-result protection). A staff NIN correction needs evidence, a reason, a duplicate check and a new API attempt. | IDV-05, IDV-09 |
| FR-IDV-013 | Manual result, reason and evidence without overwriting external history | REFINED | Manual verification from the national identity card is allowed even after API NOT_FOUND. It must stay distinguishable from API verification, and it never overrides an unresolved cross-person duplicate. | IDV-06 |
| FR-IDV-014, FR-IDV-015 | Document permission; no identity values in logs | CONFIRMED | Unchanged. NIN redaction also covers URL paths (lookup by NIN). | addendum §6 |
| Business Rules, BR-IDV-003 | An inconclusive result is neither success nor rejection | CONFIRMED | Unchanged. | IDV-04 |
| BR-DUP-001 to BR-DUP-003 | Matching, no automatic merge, contexts | REFINED | Duplicates are checked after a correction and again at final verification, against pending and verified records, under locking or uniqueness. A same-person repeat is not a cross-person conflict. | IDV-07 |
| BR-ACC-001, BR-ACC-004 | Human decision; applicant data never becomes classification | CONFIRMED | Identity verification and participation acceptance are separate; API success approves nothing. | IDV-10 |
| Collection and Document Minimization, DR-COL-002, DR-COL-004 | Conditional documents; only under an approved rule | POLICY SET | Approved rules: the national identity card for Algerian manual review; the passport identity page for every foreign participant. | DOC-01, IDV-08 |
| DR-COL-006, DR-COL-007 | Document purpose, context and validation | CONFIRMED | They apply to both identity cards and passports (private storage, scanning, access audit). | DOC-01 |
| Data Quality and Provenance, DR-QLT-003, DR-QLT-004 | Distinguish verified, declared and corrected values | CONFIRMED | The basis of IDV-03 provenance. | IDV-03 |
| NIN Verification Integration, IR-NIN-001 | Transmit only the contract's values | REFINED | Only the NIN, over HTTPS with certificate validation, to a fixed configured host. | IDV-01 |
| IR-NIN-002 | Store only the minimal result | REFINED | As FR-IDV-005 (MIN-01). | IDV-03 |
| IR-NIN-004 | Timeout, outage, unidentified and inconclusive go to manual examination | REFINED | As FR-IDV-007. Transient failures are retried with bounded attempts and backoff; a definitive NOT_FOUND is not retried as an outage. | IDV-04 |
| IR-NIN-005 | Non-sensitive correlation identifiers in logs | REFINED | The NIN is also redacted from request URL paths in application, HTTP-client, proxy, APM and exception logs. | addendum §6 |
| External Dependencies | The NIN verification service | REFINED | The contract is partly observed (addendum §3). Its unknowns are API-01 to API-04. | IDV-01 |

## 2. `02_TRD.md`

| Section and ID | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| §8.2 REG-007 | Identity documents not requested by default | REFINED | Not by default, except under the approved rules of DOC-01 and IDV-08. | DOC-01, IDV-08 |
| §8.2 REG-010 | Mobile optional for international participants | PRE-EXISTING | Already superseded by amendment A-01 (decision D-01). Nothing new. | D-01 |
| §8.4 State model | Participant status separate from internal statuses | CONFIRMED | Identity-verification state is a further separate state machine. Return-for-correction reaches the participant through the existing Additional Information Required status (a design candidate, IDV-1). Public status never shows identity outcomes. | IDV-09, IDV-10 |
| §14.4 Matching and duplicate handling | Candidates; never auto-merge | REFINED | As BR-DUP (IDV-07). The final recheck runs in the verifying transaction with locking or uniqueness guarantees. | IDV-07 |
| §15.3 Integration adapters | Adapter interfaces; bounded timeouts, retries, reconciliation | REFINED | **PLANNED REVISION** (IDV-1 and IDV-2) of the identity adapter: authentication POST plus Bearer lookup GET by NIN, returning sanitized official facts. The comparison happens in the domain service. Provider payloads never leak into domain models. Token renewal follows only documented behaviour (API-01) and avoids authentication storms. | IDV-01, IDV-02 |
| §16.1 Authentication methods, row "ASC staff" | "strong password or SSO, mandatory MFA" | SUPERSEDED (MFA), 2026-10-01 | Named account, password (or a future SSO); no sign-in MFA. | AUTH-01 (A-11) |
| §16.1, row "Organization delegate" | "MFA when elevated" | SUPERSEDED (sign-in MFA), 2026-10-01 | Named account, verified email, password sign-in. | AUTH-01 (A-11) |
| §16.1, row "External security user" | "time-bounded account with MFA" | SUPERSEDED (sign-in MFA), 2026-10-01 | Named, time-bounded, scope-limited account, password sign-in. | AUTH-01 (A-11) |
| §16.1, row "Applicant/participant" | Email OTP; step-up for sensitive changes | CONFIRMED | Unchanged. | AUTH-02 |
| §16.1 (all rows) | — | CONFIRMED | A sensitive-operation step-up stays separate from sign-in and fails closed without a genuine provider. No fake provider is allowed. | AUTH-04 |
| §17.3 Privacy engineering | Document upload exceptional, through InformationRequest | REFINED | The approved document rules are DOC-01 and IDV-08. Evidence retention follows the unresolved retention policy (OD-007); no period is invented. | DOC-01 |
| §24.2 Critical acceptance scenarios | 12 scenarios | REFINED | Add AS-01 to AS-17 (`identity_traceability_2026-10-01.md`). | IDV-* |
| §25.2 Assumptions | "MFA-capable contact methods are available" | SUPERSEDED (for sign-in) | It still applies to a future step-up provider (STEPUP-01). | AUTH-01, AUTH-04 |
| §25.3 OD-005 | NIN service availability and contract | REFINED | Partly observed (addendum §3). Still open: API-01 to API-04 and UAT-02. | IDV-01 |
| §6.1 Redis row; challenge counter | Cache and rate-limit state | CONFIRMED | Implemented as CACHE-01 (ADR-0025, P4-4-C3, corrected by P4-4-C4). Real Redis is NOT_PROVEN. | CACHE-01 |

## 3. `03_UI_UX_SPECIFICATION.md`

| Section | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| §6.2 Identity method behavior, row "Approved Algerian NIN path" | Success removes the card copy | REFINED | After submission, the participant sees a neutral "being verified" state. The card is requested only for manual review. | IDV-01, DOC-01 |
| §6.2, row "International passport path" | Identity-page upload conditional | POLICY SET | The identity-page upload is required for every foreign participant. | IDV-08 |
| §6.2, row "Inconclusive or unavailable verification" | Manual examination; no automatic rejection | CONFIRMED | No public message reveals another person or a provider detail. | IDV-04 |
| §6.4 File upload pattern | Passport identity page only when the policy requires it | POLICY SET | The policy now requires it for every foreign participant. The same component serves the national identity card for Algerian manual review. Never invite a full passport or a visa. | DOC-01, IDV-08 |
| §6.6 Duplicate handling | Never reveal another person | CONFIRMED | Pending duplicates are visible only to authorized review. | IDV-07 |
| §8.4 Registration detail, tab "Identity" | (tab named) | PLANNED REVISION | Identity status, verification source, reason, attempt history and permitted document access; correction and manual-confirmation actions behind dedicated permissions and scopes. | IDV-05, IDV-06, REVIEW-01 |
| §10.1 Applicant-facing status | Seven statuses | CONFIRMED | No new public status. Return-for-correction uses Additional Information Required (candidate). | IDV-09, IDV-10 |
| §19.1 Role and visibility matrix | — | PLANNED REVISION | Identity-review, correction, manual-confirmation and evidence-access permissions (REVIEW-01). | REVIEW-01 |

## 4. `04_APPLICATION_FLOW.md`

| Section and ID | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| §3.4 AF-AUTH-03, "MFA is mandatory for operational users." | — | SUPERSEDED, 2026-10-01 | Email and password sign-in in staging and production. | AUTH-01 (A-11) |
| §3.4 AF-AUTH-03, "High-risk actions MAY require recent re-authentication or a new MFA challenge." | — | CONFIRMED | The emergency wipe requires an MFA step-up and fails closed without a provider. | AUTH-04 |
| §3.4 AF-AUTH-03, audit events, "MFA enrollment and challenge" | — | REFINED | Applies to step-up only. | AUTH-04 |
| §4.4 AF-REG-03, step 2: "Call the approved identity verification service when available." | Verification during registration | SUPERSEDED (timing), 2026-10-01 | After final submission, verification is scheduled durably and runs in the background. No provider call happens in the public request or inside a long database transaction. Steps per addendum §5 (local shape check; persist the identity revision; fetch, validate and compare; apply only to the same revision; verify only when matching and duplicate rules pass; bounded retries). | IDV-01, addendum §5 |
| §4.4 AF-REG-03, steps 3 to 5 | Minimum result; no card after success; manual review | REFINED | As PRD FR-IDV-005 and FR-IDV-007. | IDV-03, IDV-04 |
| §4.5 AF-REG-04 | Identity page only when the policy requires it | POLICY SET | Required for every foreign participant; manual review pending; zero ministry calls. | IDV-08 |
| §4.5 AF-REG-04, "Mobile is optional for international participants." | — | PRE-EXISTING | Superseded by A-01 (D-01). | D-01 |
| §4.6 AF-REG-05 Duplicate handling | Situations table | REFINED | Plus pending records, the final-transaction recheck and cross-person adjudication. | IDV-07 |
| §4.7 AF-REG-06 Submission precondition, "required verification or manual-review flags are recorded" | — | REFINED | At submission, identity verification is recorded as pending and scheduled. Provider availability never blocks submission. | IDV-01, IDV-04 |
| §6 Additional Information Flows, AF-INFO-01 to AF-INFO-04 | Request and response | REFINED | Return-for-correction of identity uses this flow on the same account and Registration. A resubmission creates a new identity revision and attempt. A final rejection is a separate reasoned decision. | IDV-09 |
| §7.3 AF-REV-03 | "Automated verification is evidence only." | CONFIRMED | | IDV-10 |
| §7.4 AF-REV-04 | Duplicate review outcomes | REFINED | Manual identity confirmation cannot bypass an unresolved cross-person duplicate. | IDV-06, IDV-07 |
| §12.3 Editing rules | Identity changes need a correction flow | CONFIRMED | | IDV-09 |
| §16 Policy register, OD-AF-04 | Passport identity-page upload scope: "Targeted or policy-driven only" | POLICY SET | Policy-driven: every foreign participant. | IDV-08 |

## 5. `05_BACKEND_SCHEMA.md` (all PLANNED REVISION; no schema is applied)

| Section | Original (short) | Status | Planned revision (IDV-1 proposal, IDV-2/3 implementation) | Decision |
| --- | --- | --- | --- | --- |
| §4.3 `PersonName` | `source` includes verification service | CONFIRMED + PLANNED | Official Latin names may be stored with source "verification service", next to the applicant's names. Whether official Arabic-script names are stored is MIN-01, to be reconciled with decision D-02 (no Arabic-script name field in V1). | IDV-03, MIN-01 |
| §4.6 `IdentityIdentifier`, uniqueness | Verified active NIN unique (partial index on verified rows) | PLANNED REVISION | Duplicate detection also considers pending (declared) identifiers, at correction and at final verification, under a lock or a uniqueness guarantee. A correction marks the old identifier `REPLACED`, and an old verification never authorizes the new identifier. | IDV-05, IDV-07 |
| §7.1 `IdentityVerificationAttempt` | `result`: MATCH, NO_MATCH, INCONCLUSIVE, UNAVAILABLE, ERROR | PLANNED REVISION | Reason codes NOT_FOUND, DATA_MISMATCH, AMBIGUOUS_DATE, PROVIDER_UNAVAILABLE, PROVIDER_AUTH_ERROR, INVALID_RESPONSE and PROVIDER_IDENTITY_MISMATCH (mapping or extension to be decided in IDV-1); a link to the identity revision that was checked; `method` values for manual document and supervised correction (already present). Full payloads stay unretained. | IDV-02, IDV-04, IDV-05 |
| §7.3 `Document`, `document_type` | Profile photo, passport identity page, requested evidence | PLANNED REVISION | Add national identity card evidence (Algerian manual review). The note "passport identity-page storage is conditional, never universally required by schema" stays true at the schema level; the IDV-08 requirement is enforced as policy by the service, not by a schema constraint. | DOC-01, IDV-08 |
| New candidate entity | — | PLANNED REVISION | An identity revision (a submitted identity snapshot per revision), to support stale-result protection and the resubmission history. Name and shape decided in IDV-1. | IDV-05, IDV-09 |
| §16.1, §16.4, §16.5 Constraints, locking, transactions | — | PLANNED REVISION | Duplicate recheck in the final verification transaction; no provider call inside a database transaction. | IDV-07, addendum §5 |
| §17.1, §17.2 Classification | NIN and passport are Restricted | CONFIRMED + PLANNED | Retained official identity facts and identity-card evidence are Restricted. | IDV-03, DOC-01 |

## 6. `06_IMPLEMENTATION_PLAN.md`

| Section | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| §9.1 Slice REG-03, "Mock service for CI and Staging." | — | REFINED | A mock is allowed in local development and CI tests. In staging, synthetic results must be labelled as synthetic and must never be presented as official verification. In production, a mock is prohibited and must be refused at startup (open STUB-01 / finding F-PS-01: today nothing refuses it). | addendum §6 |
| §9.1 Slice REG-03, other bullets | Adapter, outcomes, storage, passport path | REFINED | Delivered by IDV-1 to IDV-4 (see the playbook). | IDV-* |
| §9.2 Gate G2, "Identity-service outage routes to manual review…" | — | CONFIRMED | Plus AS-04, AS-05 and AS-06. | IDV-04 |
| §18.2 Critical E2E scenarios 5 and 6 | NIN outage; passport path | REFINED | Scenario 6 now requires the identity page for every foreign participant (AS-12). Add AS-01 to AS-17. | IDV-* |
| §19.1 UAT groups | — | REFINED | UAT-01: realistic manual tests for every role and account, including real participant OTP email delivery, recorded separately from synthetic tests. | UAT-01 |
| §29, OD-IMP-07, default "Secure local accounts with MFA" | — | SUPERSEDED, 2026-10-01 | Secure local accounts with password sign-in; MFA only for a sensitive-operation step-up. | AUTH-01 (A-11) |
| §1.2 Change control | Impact analysis, owner, record, tests | CONFIRMED | Change record: decision gate §17.9 and §17.10, this overlay, and the PLAN-SYNC report (impact and contradictions). | — |

## 7. Project handoff context (historical)

| Section | Original (short) | Status | Overlay | Decision |
| --- | --- | --- | --- | --- |
| §1 Continuation instructions | Work in confirmed stages | CONFIRMED | The implementation team owns repository changes (addendum §1). The owner deploys manually. | addendum §1 |
| §5.1, "Operational users use a separate sign-in path with MFA." | — | SUPERSEDED, 2026-10-01 | A separate sign-in path with email and password. | AUTH-01 (A-11) |
| §5.1, "SSO may be added through an adapter; secure local operational accounts remain the fallback." | — | CONFIRMED | | AUTH-01 |
| §5.2 Phone policy | Mobile optional for international participants | PRE-EXISTING | Superseded by A-01 (D-01). | D-01 |
| §5.3 Algerian NIN path, "Provide a mock adapter for CI and Staging." | — | REFINED | As IMP §9.1. | addendum §6 |
| §5.3, "Store only the minimal verification result…" | — | REFINED | As PRD FR-IDV-005 (MIN-01). | IDV-03 |
| §5.3, other bullets | 18-character string; card copy; manual examination | CONFIRMED / REFINED | As PRD FR-IDV-002, FR-IDV-006 and FR-IDV-007. Verification is post-submission and asynchronous. | IDV-01, IDV-04 |
| §5.4 International passport path, "A passport identity-page copy is conditional…" | — | POLICY SET | Required for every foreign participant. | IDV-08 |
| §8.2 Internal processing status | Includes `VERIFICATION_PENDING`, `DUPLICATE_REVIEW` | CONFIRMED + PLANNED | Identity-verification states are tracked separately; how they map onto the internal status is decided in IDV-1. | IDV-10 |
| §15 Roles, "External security users are named, MFA-protected, time-bounded, Gate-scoped and easy to disable." | — | SUPERSEDED (MFA), 2026-10-01 | Named, time-bounded, Gate-scoped and easy to disable; password sign-in. | AUTH-01 (A-11) |
| §20 Remaining decisions, "External NIN verification service availability and contract." | — | REFINED | Partly observed; open API-01 to API-04. | IDV-01 |
| §20, "Operational SSO readiness; local MFA accounts are the fallback." | — | SUPERSEDED, 2026-10-01 | Local password accounts are the fallback. | AUTH-01 (A-11) |
| §22 Current Completion State; §23 Immediate Next Stage; §24 | Pre-implementation state | HISTORICAL | The current state and next step are in the project status record (historical, kept outside the repository) and the playbook. | — |

## 8. Superseded statements outside `docs/specifications/`

* the completion playbook (historical, kept outside the repository) §9, item 1, lists "MFA" among the
  release blockers. It is dated-annotated in place: sign-in MFA is no longer a blocker; the
  step-up provider (STEPUP-01) still is.
* the Phase 4 plan (historical, kept outside the repository) §8 R8, "MFA readiness boundary … operational sign-in …". The
  sign-in part is superseded by A-11; the step-up boundary stays. The plan's historical text is
  unchanged, and the dated addendum at its end records this.
* The "temporary staging-only exception" mentioned in the owner's addendum was never recorded in
  the repository as a requirement. The repository refers to it only as superseded (§17.9, A-11).
  There is therefore no repository text that still states it as current.
