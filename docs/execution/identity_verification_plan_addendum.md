# ASC-REG — Identity Verification and Coordination Plan Addendum

Date: 2026-10-01
Document version: 2.1. This is the owner's version 2.0 (reconciled with the C3 independent review),
reconciled by PLAN-SYNC with the reviewed P4-4-C4 tree.
Language: English
Status: Owner decisions recorded; documentation integration done by PLAN-SYNC; **identity
implementation not started.**
*(Dated status, 2026-10-01, IDV-1 to IDV-4: implemented locally under the owner's later handoff,
recorded as amendment A-13; see §13. The line above is the PLAN-SYNC record.)*

**Baseline for this integration (PLAN-SYNC reconciliation).**

* Source tree: the P4-4-C4 review archive
  `asc-registration-platform-phase4-p4_4_c4-review.zip`, SHA-256
  `1ccb366eb6b9a89fe0702212fdd86fc3c0ab0a0360b16da83556790902e2903f`. At PLAN-SYNC start, all 910
  source and 12 evidence members of that archive matched the working tree byte for byte, so there
  was no drift.
* The independent review of P4-4-C3 returned CHANGES_REQUIRED for R-C3-01 (a PING falsely cleared
  write degradation) and R-C3-02 (an unavailable state persisted after shared recovery). See
  `P4_4_C3_INDEPENDENT_REVIEW.md`.
* P4-4-C4 corrected both findings. The owner reports that it **passed the bounded independent
  review** of those two corrections and of package integrity. That review is **not** staging
  acceptance and **not** production release approval.

The reconciled statements are marked *(PLAN-SYNC reconciliation)*. The owner's decisions, their
qualifications and the open questions are unchanged. The exact differences from version 2.0 are
listed in §12.

## 1. Authority and coordination

The owner requests that the decisions below be incorporated into project planning files and coordinated with the implementation team. This document records the visible conversation; it does not assert implementation or provider verification.

- The implementation team remains responsible for repository changes. This handoff does not replace the live tree with a C2 snapshot.
- Correct and review the two C3 findings before PLAN-SYNC. *(PLAN-SYNC reconciliation: done. P4-4-C4 corrected R-C3-01 and R-C3-02 and passed the bounded independent review; PLAN-SYNC started only afterwards.)* Integrate this addendum into the accepted corrected live tree as a separately identified documentation-only change; do not silently expand correction code scope.
- Record the exact baseline used for documentation integration and explain any drift from the reviewed C3 and subsequent accepted correction baseline. *(PLAN-SYNC reconciliation: the baseline is the P4-4-C4 review archive above, with no drift.)*
- Preserve earlier approved plans and historical evidence; add dated superseding decisions and references.
- Do not start identity implementation, P4-5, deployment or live provider calls as part of documentation integration.
- All development artifacts are in English; existing interface translations remain supported. Chat with the owner is in Arabic.
- The owner performs deployment manually. The implementation and review teams supply instructions and review evidence.

## 2. Decision register

| ID | Decision / requirement | Status and qualification |
| --- | --- | --- |
| AUTH-01 | Operational staff use email and password in staging AND production. | Explicit owner approval. Supersedes the temporary staging-only exception and mandatory sign-in MFA requirement. *(PLAN-SYNC reconciliation: recorded in P4-4-C3 as amendment A-11 to MFA-01; implementation unchanged and password-only; `OPERATIONAL_SIGN_IN_MFA_ENFORCED` remains False.)* |
| AUTH-02 | Participants continue authenticating with email OTP. | Explicitly retained. Real mail delivery requires separate configuration and evidence. |
| AUTH-03 | Preserve operational permissions, scopes, throttling, expiry, session controls and auditing. | Retained. Removing sign-in MFA does not authorize sensitive-operation step-up bypass. |
| AUTH-04 | Emergency-wipe step-up remains fail-closed; no fake MFA provider. | Retained boundary. Sensitive-operation provider availability remains separate from ordinary sign-in. *(PLAN-SYNC reconciliation: open as STEPUP-01; `release_readiness` reports `sensitive_operation_step_up` BLOCKED without a genuine provider.)* |
| CACHE-01 | Shared Redis issuance counter, stricter bounded process-local fallback, degradation/recovery alert. | C3 implements the direction. *(PLAN-SYNC reconciliation: the two counter-state findings were corrected in P4-4-C4 and passed the bounded independent review. Only a completed counter write now proves the shared counter; a PING never does. A working shared counter is no longer reported unavailable because of an earlier fallback failure. See ADR-0025.)* Defaults are normal 60, fallback 20 per process, 600-second window; real Redis tests remain NOT_PROVEN. Confirm process-count limits (CACHE-02) and alert routing (REDIS-01). |
| IDV-01 | Algerian NIN registration uses the ministry API for automated identity verification. | Owner approved workflow; actual integration remains to be implemented and proven. |
| IDV-02 | Confirm returned NIN belongs to the submitted civil identity; existence alone is insufficient. | Required correctness rule. Compare names and date of birth as defined in the contract. |
| IDV-03 | Minor name differences may be corrected using official data. Preserve submitted values and correction provenance. | Owner wants minor corrections. Exact safe normalization rules are still to be finalized; no unapproved fuzzy threshold. |
| IDV-04 | Major mismatch, NIN not found, ambiguous data and exhausted technical failures go to manual review with a clear reason. | Agreed workflow. These outcomes do not cause automatic registration rejection. |
| IDV-05 | Authorized staff may correct NIN based on identity evidence, with a reason, duplicate checks and a new API attempt. | Owner requested and agreed. Old verification must not authorize the new identifier. |
| IDV-06 | Staff may verify manually from the national identity card even when the API does not find the corrected NIN. | Owner requested. Requires documented evidence, permission and reason; must remain distinguishable from API verification. Never overrides unresolved cross-person duplicates. |
| IDV-07 | Check duplicates after corrections and again at final verification; enforce consistency under concurrency. | Explicit owner requirement. Consider pending and verified records; same-person repeat registration differs from cross-person conflict. |
| IDV-08 | Every foreign participant follows manual passport verification, without ministry API calls. | Explicit owner decision. Require the passport identity page for this workflow. |
| IDV-09 | A participant may correct and resubmit through the same account and existing registration. | Agreed workflow. Use return-for-correction for remediable issues; final rejection is a separate reasoned decision. Preserve history. |
| IDV-10 | Identity verification and participation/accreditation acceptance are separate states. | Agreed workflow. API success does not automatically approve participation. |
| IDV-11 | Nonuniform dates must be normalized explicitly; ambiguous dates must not be guessed. | Explicit owner concern. Supported formats and presumed-date semantics are unresolved. |
| DOC-01 | National identity card evidence is requested for Algerian manual review; foreign passport identity-page evidence is required. | Agreed plan. Current NIN path does not accept this evidence; new behavior requires implementation. Reuse private storage and scanning controls. |
| UAT-01 | Provide realistic manual tests for every role and account, including actual participant email OTP delivery. | Owner request retained in the plan; not satisfied by synthetic unit tests. |
| UI-01 | Keep the official shared footer empty. | Existing owner decision retained; no footer redesign in this work. *(PLAN-SYNC reconciliation: amendment A-10, decision gate §17.7, confirmed in §17.8.)* |

## 3. Observed provider contract

Evidence: owner-supplied request screenshots, a response example and explicit clarification. No external request has been executed by the reviewer. *(PLAN-SYNC reconciliation: none by the implementation team either. PLAN-SYNC made no provider call, and no screenshot, response example, credential or citizen record entered the repository.)*

| Operation | Observed contract | Still unknown |
| --- | --- | --- |
| Authentication | `POST https://miclat.mkesm.gov.dz/api/auth/`; JSON username and password. | Response schema, token field, lifetime, renewal, authentication error behavior. |
| Lookup | `GET https://miclat.mkesm.gov.dz/api/get/{nin}`; Bearer authorization. | Rate limits, access/network requirements, service availability commitments, broader error contract. |
| Found identity | JSON object under `identite`. | Completeness guarantees, official meanings of additional fields and date formats. |
| Not found | HTTP 200 with `identite: null`. Owner confirms HTTP 200 even when NIN is absent. | Does not distinguish mistyped NIN from genuinely absent NIN. |

Observed relevant fields:

| Field | Intended use |
| --- | --- |
| `nin` | Exact comparison with requested NIN; preserve as a string, including leading zeroes. |
| `nom_f`, `pren_f` | Official Latin-script family and given names. |
| `nom_a`, `pren_a` | Official Arabic-script family and given names; preserve separately if required by the approved model. |
| `d_nais` | Civil birth date; sample uses DD/MM/YYYY, but other formats are possible. |
| `presume` | Sample is the string `"False"`. Explicit parsing and official meaning are required. |

Do not collect parental names or marriage/divorce/death indicators for this matching workflow. Do not retain full upstream responses.

Response rules:
- HTTP 200 alone is not verification.
- HTTP 200 + explicit `identite: null` is NOT_FOUND and routes to manual review.
- A missing `identite`, wrong type, invalid JSON or structurally incomplete identity is INVALID_RESPONSE, not NOT_FOUND.
- An object with a different returned NIN is PROVIDER_IDENTITY_MISMATCH, never automatic verification.
- HTTP 401/403, timeouts, connection failures and service errors are technical/provider failures, never evidence that the NIN does not exist.
- The supplied JSON text has a trailing comma; confirm whether this is a copy artifact. Do not silently repair malformed production responses into successful verification.
- Request screenshot and response example use different NINs. Treat them as separate examples; do not use the pair as a valid matching fixture.

## 4. Matching and date rules

Store participant-submitted facts separately from the minimum official facts needed for matching. Preserve the source and history of every correction. Protect stored official identity data at least as strongly as the existing identity data.

Proposed conservative automatic rules, to finalize in the contract stage:
- Exact NIN match, unambiguous equal birth date and compatible given/family names are necessary for automatic verification.
- Whitespace and letter-case normalization may be automatic.
- Do not auto-correct substantive spelling, name order, Arabic/Latin transliteration or birth-date differences based solely on a fuzzy similarity score.
- Preserve the original date value only as restricted identity evidence where needed, not in logs or generic metadata. Store the normalized date separately; do not retain the entire provider payload.
- Define formats per documented provider contract, explicitly parse types and reject invalid/impossible dates.
- Do not use unrestricted date guessing. A date such as 03/04/1990 is unresolved unless the applicable contract establishes its order.
- Do not turn year-only, presumed or partial birth dates into an invented precise date.
- Do not extract or infer a birth date from NIN or unrelated civil-register fields as a replacement for an unresolved `d_nais`.
- Parse textual booleans explicitly; `"False"` must not become true because it is a nonempty string.

## 5. Target workflow

After final registration submission, enqueue verification in the background. Do not hold the public request or a long database transaction open during provider network calls.

Suggested domain states (names are design candidates, not existing model fields): PENDING, VERIFIED_API, MANUAL_REVIEW, VERIFIED_MANUAL, DUPLICATE_REVIEW, CORRECTION_REQUIRED. Use separate reason codes for NOT_FOUND, DATA_MISMATCH, AMBIGUOUS_DATE, PROVIDER_UNAVAILABLE, PROVIDER_AUTH_ERROR and INVALID_RESPONSE.

For Algerian NIN:
1. Validate local input shape and check existing identifiers.
2. Persist the submitted identity revision and schedule the provider attempt durably.
3. Fetch and validate the provider response, compare facts, and record a sanitized attempt.
4. Apply the result only to the same identity revision and intended registration/person. Ignore stale results after edits.
5. Verify automatically only when matching and duplicate rules pass; otherwise route to manual review.
6. Retry transient failures with bounded attempts and backoff. Do not repeatedly retry a definitive NOT_FOUND as an outage.

For foreign passport:
1. Require passport identity-page evidence through the approved document pipeline.
2. Set manual review pending; make zero ministry API calls.
3. Authorized staff inspect submitted names, birth date, passport number, issuing country and validity, then verify, return for correction or reject with a reason.

Staff workflow:
- Display identity status, verification source, reason, attempt history and permitted document access. Do not expose another person's details in duplicate messages.
- Use dedicated permissions and existing event/organization scopes.
- NIN correction requires evidence and a reason, invalidates previous applicable verification, checks conflicts and triggers a new API attempt.
- Manual confirmation must record actor, time, reason and evidence reference; preserve unsuccessful API outcomes.
- Duplicate checking must be repeated in the final database transaction and supported by locking/uniqueness guarantees. A preliminary query alone is insufficient.
- Cross-person duplicates require adjudication; manual confirmation cannot bypass them. Same-person repeats use the existing account and registration context.
- Returning for correction preserves the registration and all attempts; participant resubmission creates a new identity revision and verification attempt.
- Keep verification distinct from registration review, accreditation, badge issuance and entry permission.

**Current implementation, for contrast** *(PLAN-SYNC reconciliation: verified by reading the
P4-4-C4 tree; nothing was changed):*

* `save_identity_step` in `apps/registrations/services/__init__.py` is `@transaction.atomic`. It calls the NIN
  adapter synchronously during the **draft** identity step, before submission, inside that
  transaction. The target workflow above replaces this in IDV-2.
* The adapter interface `NinProvider.verify(nin, given_names, family_name, birth_date)` sends names
  and the date of birth to the provider and receives a result. The observed contract is a lookup by
  NIN that returns official facts, with the comparison done locally.
* The only providers are `LocalStubNinProvider` (deterministic: MATCH for an 18-digit NIN ending in
  an even digit) and `UnavailableNinProvider`. `NIN_PROVIDER_BACKEND` defaults to the stub in
  `config/settings/base.py`. Static validation does not refuse the stub in staging or production
  (finding F-PS-01 in the PLAN-SYNC report).
* A stub MATCH marks the `IdentityIdentifier` as `VERIFIED`, with only the attempt's `provider_code`
  (`LOCAL_STUB`) distinguishing it.
* The duplicate check runs only against `VERIFIED` identifiers of other persons.
* The NIN path is limited to Algerian nationals. The passport path is open to every nationality,
  including Algerians.
* The passport identity-page upload is optional, enabled per event by
  `passport_identity_page_upload_enabled`.
* There is no national-identity-card evidence path for Algerian manual review.

## 6. Security and operations

- Credentials and tokens belong in approved secret configuration; no real secrets or civil identities in code, fixtures, archives or reports.
- Use HTTPS with certificate validation, fixed configured provider hosts, bounded timeouts and controlled retries. Never accept a participant-supplied provider URL.
- Redact NIN from URL paths in application, HTTP-client, proxy/APM and exception logs.
- Keep tokens restricted; design renewal only from documented behavior, avoiding concurrent authentication storms.
- Private evidence storage, malware scanning, access auditing and existing file-validation controls apply to both identity cards and passports.
- Define evidence retention against the unresolved approved retention policy; do not invent a legal retention period.
- Staging synthetic tests must not label local stub output as official verification. Prohibit successful mock verification in production. Real provider unavailability routes to explicit review rather than synthetic success.
- Real-provider proof, mail proof and manual UAT remain distinct from implementation and mocked tests.

## 7. Work packages and gates

These are identity addendum stages; do not renumber the established Phase 4 P4-5 to P4-9 sequence.

| Stage | Scope | Exit evidence |
| --- | --- | --- |
| PLAN-SYNC | Integrate this addendum, decision register, dependencies and traceability into live planning files. Documentation only. | Updated plans, contradiction audit, exact file list and open-question register. *(PLAN-SYNC reconciliation: delivered; see the historical review record `phase_04_plan_sync_report.md` (kept outside the repository).)* |
| IDV-1 | Freeze API contract, date parsing, name normalization, states and data model proposal. | Sanitized fixtures and contract report; unresolved inputs remain explicit. |
| IDV-2 | Implement the real adapter and durable post-submission verification. | Success, null, mismatch, invalid-response, auth/outage/retry and stale-result tests; real-provider status stated honestly. |
| IDV-3 | Implement staff review, evidence, foreign passport flow, correction and resubmission. | Permission/scope, document, duplicate-concurrency and complete lifecycle tests. |
| IDV-4 | Integrate, rehearse and package guides/evidence. | Relevant full gate, manual role/account scenarios, authorized provider and mail tests, review/update archives and manifests. |

Current authorization is planning integration, not a blanket instruction to implement all stages. Subsequent implementation prompts define the authorized stage and baseline. C3 findings must be corrected and reviewed first. *(PLAN-SYNC reconciliation: satisfied by P4-4-C4.)* Insert the feature before the final system-test/review packages; reassess P4-5 documents if they have already been prepared. *(PLAN-SYNC reconciliation: P4-5 has not started, so no P4-5 document exists to reassess. Where IDV-1 to IDV-4 sit relative to P4-5 is open question SEQ-01.)*

## 8. Open questions — do not invent answers

| ID | Required input / decision | Dependency |
| --- | --- | --- |
| API-01 | Sanitized authentication response; token field, expiry and renewal. | Real adapter authentication. |
| API-02 | Actual nonuniform date examples and authoritative format order. | Safe automatic birth-date matching. |
| API-03 | Meaning and handling of presumed/partial birth dates and `presume`. | Automatic-versus-manual routing. |
| API-04 | Rate limits, access/network restrictions and error contract. | Retry and production configuration. |
| MATCH-01 | Exact safe name-correction policy. | Automatic correction beyond conservative normalization. |
| DOC-02 | Allowed card/passport formats, size limits, masking and retention mapping. | Reuse project defaults where applicable; report unresolved policy. |
| REVIEW-01 | Concrete permission mapping and required manual-confirmation reasons. | Review UI and services. |
| UAT-02 | Approved test environment, synthetic accounts and authorized provider/mail test method. | Real integration and operational acceptance. |

Missing inputs block their dependent behavior; they do not block documentation synchronization or designing synthetic tests. Do not claim live verification until authorized evidence exists.

*(PLAN-SYNC reconciliation: PLAN-SYNC found further open questions while synchronizing:*

* *MIN-01: which official facts are retained;*
* *SCOPE-01: which path an Algerian national may use;*
* *SEQ-01: sequencing against P4-5;*
* *STUB-01: the stub's staging and production guard.*

*They are recorded in `docs/execution/identity_open_questions_2026-10-01.md`, with every remaining
release prerequisite. None changes the owner's decisions.)*

## 9. Repository integration map

| File | Required integration |
| --- | --- |
| `docs/execution/identity_verification_plan_addendum.md` | Add this authoritative decision/plan record, reconciled with the actual live baseline. |
| the Phase 4 plan (historical, kept outside the repository) | Preserve historical approved text; append a dated addendum reference, new dependencies and stage ordering. |
| the completion playbook (historical, kept outside the repository) | Add the identity work packages and their stage boundaries; preserve P4-5..P4-9 meanings. |
| `docs/execution/UX_REMARKS_DECISION_GATE.md` | Record dated AUTH, CACHE and IDV decisions with unresolved details distinguished. |
| the project status record (historical, kept outside the repository) | Record C3 CHANGES_REQUIRED, subsequent correction acceptance and identity PLANNED status separately; do not assert execution. |
| `docs/specifications/01_PRD.md` | Reconcile staff sign-in and participant identity requirements. |
| `docs/specifications/02_TRD.md` | Reconcile sign-in versus sensitive step-up; adapter, asynchronous flow, date handling and failure semantics. |
| `docs/specifications/03_UI_UX_SPECIFICATION.md` | Planned staff status/reason display, evidence access and participant correction flow. |
| `docs/specifications/04_APPLICATION_FLOW.md` | Post-submit Algerian API path, foreign manual path and resubmission. |
| `docs/specifications/05_BACKEND_SCHEMA.md` | Clearly marked planned revisions, attempts, provenance, evidence and uniqueness requirements; no claim of applied schema. |
| `docs/specifications/06_IMPLEMENTATION_PLAN.md` | Work package dependencies, tests and integration gates. |
| the project handoff context (historical, removed from the specifications) | Latest decisions, implementation ownership, baseline and open questions. |

C3 uses amendment A-11 and keeps source specifications read-only. Respect any repository protection of originals: use a dated requirements overlay/amendment with precise section references rather than silently rewriting protected specifications. Update editable execution plans and handoff documents directly. The integration report must identify which records were edited and which are governed by the overlay.

*(PLAN-SYNC reconciliation: all seven specification files of that time, including
the project handoff context (historical), lived in `docs/specifications/`. The project engineering rules and
`docs/specifications/README.md` declared that folder read-only, so all seven were governed by the
dated overlay `docs/execution/requirements_overlay_2026-10-01_identity_and_auth.md` and none was
edited. The execution plans were edited directly. The report lists both groups.)*

For each affected requirement cite the decision ID. Produce a traceability table mapping decision → file/section → planned implementation → acceptance test. Mark older contradictory requirements superseded with a dated reference. Do not treat generic searches and replacements as a sufficient consistency review.

## 10. Acceptance scenarios to carry into the plan

1. Exact NIN/names/date match verifies through API without accepting participation automatically.
2. Approved minor normalization preserves submitted values and correction history.
3. Major mismatch routes to manual review without overwriting submitted identity.
4. HTTP 200 + null routes to NOT_FOUND; missing field/invalid JSON routes to INVALID_RESPONSE.
5. Auth errors and exhausted transient failures never become NOT_FOUND or VERIFIED.
6. Returned NIN differs from requested NIN: refuse automatic confirmation.
7. Known date formats normalize correctly; ambiguous, invalid, partial and presumed dates follow explicit rules.
8. Staff correction triggers duplicate checks and a new attempt; stale in-flight results cannot verify the changed identity.
9. Two concurrent confirmations of a cross-person duplicate cannot both succeed.
10. Pending duplicates are visible to authorized review without exposing another person's data to the applicant.
11. Manual confirmation after API NOT_FOUND records evidence and source, and does not bypass duplicates.
12. Every foreign applicant requires manual passport review and generates no ministry request.
13. Unauthorized/out-of-scope staff cannot view evidence, correct identifiers or confirm identity.
14. Return-for-correction and resubmission use the same account/registration with history preserved.
15. Real OTP email delivery and realistic role tests are recorded separately from synthetic tests.
16. No secrets, full upstream payloads or raw NIN URLs leak into logs or packaged evidence.

*(PLAN-SYNC reconciliation: the traceability file numbers these AS-01 to AS-16 and adds AS-17,
"staging and production refuse the local stub", for F-PS-01 / STUB-01.)*

## 11. Definition of completion for PLAN-SYNC

All applicable planning documents are updated in the latest implementation tree; every decision is traceable; contradictory requirements are resolved or explicitly flagged; open inputs are listed; no application code, setting, dependency or migration is changed; no deployment/provider call occurs. Deliver the documentation changes and a concise report for independent review.

## 12. Reconciliation record (PLAN-SYNC, 2026-10-01)

Differences from the owner's version 2.0. Every other word is unchanged.

| Location in v2.0 | v2.0 statement | Reconciled as |
| --- | --- | --- |
| Header, version | "Document version: 2.0 — reconciled with C3 independent review" | "2.1", described as v2.0 reconciled by PLAN-SYNC with the P4-4-C4 tree. |
| Header, status | "documentation integration authorized" | "documentation integration done by PLAN-SYNC"; "identity implementation not started" is kept. |
| Header, baseline | "Last independently examined source baseline: P4-4-C3 … Proposed bounded C4 corrections precede PLAN-SYNC; no corrected package is yet accepted." | Baseline = the P4-4-C4 review archive `1ccb366e…2903f`, with no drift. C4 passed the bounded independent review, which is neither staging acceptance nor release approval. |
| §1, bullets 2–3 | Correct and review C3 first; record the baseline. | Kept, with a note that both are done. |
| §2, AUTH-01, AUTH-04, UI-01 | (unchanged decisions) | Added references to the existing records A-11, STEPUP-01 and A-10. |
| §2, CACHE-01 | "with two counter-state findings pending correction" | Replaced by the C4 outcome; real Redis stays NOT_PROVEN, and CACHE-02 and REDIS-01 stay open. |
| §3, evidence line | "No external request has been executed by the reviewer." | Kept, with a note that PLAN-SYNC also made no provider call. |
| §5 | (target workflow) | Added a "Current implementation, for contrast" block. It describes the existing code and changes no decision. |
| §7, PLAN-SYNC row | (exit evidence) | Kept, with a note pointing to the delivered report. |
| §7, closing paragraph | "C3 findings must be corrected and reviewed first … reassess P4-5 documents" | Kept, with notes: satisfied by C4; P4-5 not started; sequencing is SEQ-01. |

A line-by-line comparison with the owner's file confirmed it: exactly 10 lines of v2.0 are not
reproduced verbatim (header lines 6–8; §1 line 15; §2 rows AUTH-01, AUTH-04, CACHE-01 and UI-01;
§7 PLAN-SYNC row and closing paragraph). Each one is listed above.
| §8, §9, §10 | (lists) | Added pointers to the open-question register, the overlay and the AS numbering. |

## 13. Implementation record (IDV-1 to IDV-4, 2026-10-01)

The owner's identity implementation handoff of 2026-10-01 authorized the
implementation of IDV-1 to IDV-4 in one assignment and settled the open choices of §8 (amendment
A-13, `requirements_overlay_2026-10-01_A13_identity_implementation.md`; decision gate §17.11).
Sections 1 to 12 are unchanged.

* **Delivered:** the strict contract and conservative matching (§3, §4), the official adapter with a
  configurable authentication mapping, the refusal of every stub in staging and production, the
  durable post-submission workflow (§5), duplicate locking, evidence, staff review, return for
  correction and resubmission, the staff-assisted exception, readiness and deploy reporting.
  Architecture: ADR-0026. Contract: `docs/security/identity_provider_contract.md`.
* **Still open:** API-01 (authentication response), API-02 (non-presumed formats beyond
  DD/MM/YYYY), the API-04 remainder (access, availability, errors), OD-007 (retention), UAT-02
  (authorized provider and mail test method). Real-provider and real-mail evidence: NOT_PROVEN.
* **Report:** the historical review record `phase_04_idv_report.md` (kept outside the repository).
