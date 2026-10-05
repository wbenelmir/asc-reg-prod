# Traceability and contradiction audit: AUTH, CACHE, IDV, DOC, UAT and UI decisions

Date: 2026-10-01. Language: English.

* **Prepared by:** PLAN-SYNC (documentation only).
* **Baseline:** the P4-4-C4 review archive, SHA-256
  `1ccb366eb6b9a89fe0702212fdd86fc3c0ab0a0360b16da83556790902e2903f`.
* **Sources:**
  * `identity_verification_plan_addendum.md` (decisions; "addendum");
  * `requirements_overlay_2026-10-01_identity_and_auth.md` ("overlay");
  * `UX_REMARKS_DECISION_GATE.md` §17.9 and §17.10 ("gate");
  * the completion playbook (historical, kept outside the repository) §7a ("playbook").

**Evidence labels.**

* **IMPLEMENTED (P4-4-C3/C4)**: code and named tests exist in the baseline and ran in the
  P4-4-C3 and P4-4-C4 gates. Those runs are earlier evidence; PLAN-SYNC did not rerun them.
* **PLANNED**: no code exists. The stage named is where it will be built and tested, after
  separate authorization.
* **NOT_PROVEN**: needs real-service, real-mail or human evidence that does not exist yet.

## 1. Decision → document and section → implementation → acceptance

| Decision | Governing document and section | Planned or actual implementation | Acceptance test |
| --- | --- | --- | --- |
| AUTH-01, password-only operational sign-in in staging and production | Gate §17.9 (A-11); overlay: PRD FR-AUTH-009, TRD §16.1 (staff, delegate and external-security rows), AF-AUTH-03, IMP §29 OD-IMP-07, handoff §5.1, §15 and §20 | **IMPLEMENTED (P4-4-C3):** unchanged password sign-in; static validation no longer requires `MFA_BACKEND`; `release_readiness` item `operational_sign_in` READY with `operational_sign_in_mfa_enforced: false` | `apps/accounts/tests/test_p4_4_c3_password_sign_in.py`; `tests/foundation/test_p4_4_c3_settings_and_deploy.py::test_staging_and_production_start_without_a_sign_in_mfa_provider`; `apps/core/tests/test_p4_4_c1_release_readiness.py::test_the_mfa_fact_agrees_with_the_actual_sign_in_behaviour`. **NOT_PROVEN:** real staging sign-in (staging guide §4) |
| AUTH-02, participant email OTP unchanged | Overlay: PRD FR-AUTH-001 to FR-AUTH-003, TRD §16.1 applicant row | **IMPLEMENTED** (unchanged) | `apps/accounts/tests/test_otp.py`; `test_p4_4_c3_password_sign_in.py::test_the_participant_otp_is_unchanged_and_unaffected_by_staff_passwords`. **NOT_PROVEN:** real email delivery (AS-15, UAT-01) |
| AUTH-03, permissions, scopes, throttling, expiry, sessions and audit preserved | Overlay: PRD FR-AUTH-010 to FR-AUTH-012 | **IMPLEMENTED** (unchanged) | `apps/accounts/tests/test_p4_4_operational_sign_in.py`; `apps/accounts/tests/test_session_expiry.py`; `apps/accounts/tests/test_temporary_account_expiry.py` |
| AUTH-04, emergency-wipe step-up fails closed; no fake provider | Overlay: TRD §16.1 (all rows), AF-AUTH-03 high-risk actions; ADR-0023; P2-F | **IMPLEMENTED (unchanged; warning `entry.W001` and item `sensitive_operation_step_up` added in P4-4-C3).** The provider is open (STEPUP-01) | `apps/entry/tests/test_offline_devices.py::test_wipe_fails_closed_without_a_valid_mfa_step_up`; `apps/entry/tests/test_offline_views.py::test_emergency_wipe_without_mfa_is_refused`; `apps/entry/tests/test_p4_4_c3_step_up_after_password_sign_in.py` |
| CACHE-01, shared Redis issuance counter with a bounded fallback and alerts | Gate §17.9; ADR-0025 (including the P4-4-C4 section); overlay: TRD §6.1 | **IMPLEMENTED (P4-4-C3; state evidence corrected by P4-4-C4, which passed the bounded independent review)** | `apps/core/tests/test_p4_4_c3_issuance_counter.py`, `test_p4_4_c4_counter_state.py`, `test_p4_4_c3_readiness.py`, `test_p4_4_c3_release_readiness.py`. **NOT_PROVEN:** real Redis (`test_p4_4_c3_redis_integration.py`, 5 opt-in tests skipped; staging guide §7); CACHE-02, REDIS-01 |
| IDV-01, NIN registration uses the ministry API | Gate §17.10 (A-12); overlay: PRD FR-IDV-001, FR-IDV-004, IR-NIN-001, TRD §15.3, AF-REG-03 (timing), AF-REG-06, IMP §9.1, handoff §5.3 | **PLANNED.** IDV-1: contract and states. IDV-2: real adapter (auth POST, Bearer lookup GET), durable post-submission job, sanitized attempts, no provider call inside a transaction | AS-01, AS-04, AS-05, AS-16, AS-17; provider proof NOT_PROVEN until UAT-02 |
| IDV-02, the returned identity must belong to the submitted person | Overlay: PRD FR-IDV-004, FR-IDV-007; addendum §3 and §4 | **PLANNED.** IDV-1 matching rules; IDV-2 local comparison | AS-01, AS-06 |
| IDV-03, minor name corrections from official data, with provenance | Overlay: PRD FR-IDV-005, IR-NIN-002, DR-QLT-003, DR-QLT-004; Schema §4.3 | **PLANNED.** IDV-1 normalization policy (MATCH-01) and retained-fact list (MIN-01); IDV-2 storage | AS-02, AS-03 |
| IDV-04, mismatch, not found, ambiguous and exhausted failures go to manual review with a reason | Overlay: PRD FR-IDV-007, BR-IDV-003, IR-NIN-004 | **PLANNED.** IDV-2 outcome routing and bounded retries | AS-03, AS-04, AS-05 |
| IDV-05, staff NIN correction with evidence, reason, duplicate check and a new attempt | Overlay: PRD FR-IDV-012; Schema §4.6, §7.1; UI/UX §8.4 | **PLANNED.** IDV-3 correction service and UI; IDV-2 recheck | AS-08, AS-13 |
| IDV-06, manual verification from the identity card | Overlay: PRD FR-IDV-013; AF-REV-04 | **PLANNED.** IDV-3 manual confirmation with source, actor, reason and evidence | AS-11, AS-13 |
| IDV-07, duplicates rechecked after correction and at final verification, under concurrency | Overlay: PRD BR-DUP-001 to BR-DUP-003; TRD §14.4; AF-REG-05; Schema §4.6, §16 | **PLANNED.** IDV-1 lock or uniqueness design; IDV-3 implementation and concurrency tests | AS-09, AS-10, AS-11 |
| IDV-08, every foreign participant: manual passport verification and identity page, no ministry calls | Overlay: PRD FR-IDV-008, FR-IDV-010, DR-COL-004; AF-REG-04; OD-AF-04; UI/UX §6.2, §6.4; handoff §5.4 | **PLANNED.** IDV-3 foreign flow; the identity page becomes required (today it is optional per event) | AS-12 |
| IDV-09, same-account return for correction and resubmission | Overlay: AF-INFO-01 to AF-INFO-04; AF §12.3; TRD §8.4 | **PLANNED.** IDV-3 | AS-14 |
| IDV-10, identity verification is separate from participation acceptance | Overlay: PRD BR-ACC-001, BR-ACC-004; AF-REV-03; TRD §8.4 | **PLANNED.** IDV-1 state separation; IDV-3 | AS-01 |
| IDV-11, explicit date normalization; no guessing | Addendum §4; API-02, API-03 | **PLANNED.** IDV-1 parser contract with sanitized fixtures | AS-07 |
| DOC-01, identity card for Algerian manual review; passport identity page required | Overlay: PRD DR-COL-002, DR-COL-004, DR-COL-006, DR-COL-007; Schema §7.3; UI/UX §6.4 | **PLANNED.** IDV-3, reusing private storage, scanning and access audit | AS-12, AS-13, AS-16 |
| UAT-01, realistic manual tests for every role and account, with real OTP email | Overlay: IMP §19.1; playbook IDV-4 and P4-6 | **PLANNED.** IDV-4 manual scenarios; P4-6 final checklist | AS-15; **NOT_PROVEN** until it is executed |
| UI-01, the official footer stays empty | Gate §17.7 (A-10), §17.8 | **IMPLEMENTED** (P4-4) | `apps/core/tests/test_ux1_shared_controls.py::test_the_public_shell_footer_carries_no_link_under_amendment_a10`; `tests/browser/test_ux1_shared_controls.py::test_arabic_pages_name_the_event_in_full_and_the_footer_is_empty_under_a10` |

## 2. Planned acceptance scenarios (AS-01 to AS-17; none executed)

AS-01 to AS-16 are the owner's scenarios (addendum §10). AS-17 was added by PLAN-SYNC for finding
F-PS-01.

**Test kinds:**

* **A** = automated, with synthetic data and a contract stand-in;
* **C** = concurrency test on PostgreSQL;
* **P** = an authorized real-provider test, which needs UAT-02;
* **M** = a manual or human scenario.

| ID | Scenario | Decisions | Stage | Kind |
| --- | --- | --- | --- | --- |
| AS-01 | Exact NIN, name and date match verifies through the API without accepting participation | IDV-01, IDV-02, IDV-10 | IDV-2 | A, then P |
| AS-02 | Approved minor normalization preserves the submitted values and the correction history | IDV-03 | IDV-2 | A |
| AS-03 | A major mismatch routes to manual review without overwriting the submitted identity | IDV-03, IDV-04 | IDV-2 | A |
| AS-04 | HTTP 200 with null gives NOT_FOUND; a missing field or invalid JSON gives INVALID_RESPONSE | IDV-01, IDV-04 | IDV-2 | A, then P |
| AS-05 | Authentication errors and exhausted transient failures never become NOT_FOUND or VERIFIED | IDV-01, IDV-04 | IDV-2 | A |
| AS-06 | A returned NIN that differs from the requested one refuses automatic confirmation | IDV-02 | IDV-2 | A |
| AS-07 | Known date formats normalize; ambiguous, invalid, partial and presumed dates follow explicit rules | IDV-11 | IDV-1 (contract), IDV-2 | A |
| AS-08 | A staff correction triggers duplicate checks and a new attempt; stale results cannot verify the changed identity | IDV-05, IDV-07 | IDV-3 | A, C |
| AS-09 | Two concurrent confirmations of a cross-person duplicate cannot both succeed | IDV-07 | IDV-3 | C |
| AS-10 | Pending duplicates are visible to authorized review, without exposing another person to the applicant | IDV-07 | IDV-3 | A |
| AS-11 | Manual confirmation after NOT_FOUND records evidence and source, and does not bypass duplicates | IDV-06, IDV-07 | IDV-3 | A |
| AS-12 | Every foreign applicant needs manual passport review and generates no ministry request | IDV-08, DOC-01 | IDV-3 | A |
| AS-13 | Unauthorized or out-of-scope staff cannot view evidence, correct identifiers or confirm identity | IDV-05, IDV-06, DOC-01 | IDV-3 | A |
| AS-14 | Return for correction and resubmission use the same account and Registration, with history preserved | IDV-09 | IDV-3 | A, then M |
| AS-15 | Real OTP email delivery and realistic role tests are recorded separately from synthetic tests | UAT-01, AUTH-02 | IDV-4, P4-6 | M, then P |
| AS-16 | No secrets, full upstream payloads or raw NIN URLs leak into logs or packaged evidence | IDV-01, DOC-01 | IDV-2, IDV-4 | A |
| AS-17 | Staging and production refuse the local NIN stub at startup; staging synthetic results are never presented as official verification | addendum §6, STUB-01 | IDV-2 (or an earlier bounded correction) | A |

## 3. Contradiction audit

The method was to read every specification section listed in the overlay and every editable plan,
not a text search. Results:

| # | Topic | Where | Finding | Resolution |
| --- | --- | --- | --- | --- |
| 1 | **API existence versus identity match** | PRD FR-IDV-004; the current `NinProvider.verify(nin, names, birth_date)`; addendum §3 and §4 | The specification and the code assume a provider that compares submitted values. The observed provider returns an identity, or null, by NIN. | Resolved by IDV-02: existence is not verification, and HTTP 200 alone is not verification. The comparison is done locally, null is NOT_FOUND, and only the NIN is sent. The adapter change is PLANNED (IDV-1, IDV-2). |
| 2 | **Mock versus official verification** | IMP §9.1 and handoff §5.3 ("mock … for CI and Staging"); the code | Not a contradiction in the text: the overlay refines it (a labelled mock in staging, prohibited in production). **It is a real gap in the code:** `NIN_PROVIDER_BACKEND` defaults to `LocalStubNinProvider`, static validation does not refuse it in staging or production, and a stub MATCH marks the identifier `VERIFIED`. | Open: finding **F-PS-01 / STUB-01**. Documentation cannot fix it; it needs a bounded code correction (refusing the stub outside local and test) before staging exposes registration, or IDV-2 at the latest. Not changed by PLAN-SYNC. |
| 3 | **Staff sign-in MFA versus sensitive-operation step-up** | PRD FR-AUTH-009; TRD §16.1; AF-AUTH-03; IMP OD-IMP-07; handoff §5.1, §15, §20; playbook §9; phase 4 plan R8 | The older texts require sign-in MFA. | Resolved: sign-in MFA is superseded by A-11 (AUTH-01). The step-up is retained (AUTH-04), and `release_readiness` reports the two separately. No current text still requires sign-in MFA once the overlay applies. |
| 4 | Data minimization versus retained official facts | PRD FR-IDV-005, IR-NIN-002; handoff §5.3; decision D-02 | Storing official facts for corrections goes beyond "status, reference and comparison result". Official Arabic names touch D-02 (no Arabic-script name field in V1). | REFINED by the owner (addendum §4: minimum official facts may be stored, never the full payload). The exact field list is open as **MIN-01**, to settle in IDV-1. |
| 5 | Verification timing | AF-REG-03; the current synchronous call in the draft step, inside a transaction | The flow and the code verify during registration. | SUPERSEDED (timing): post-submission and asynchronous (addendum §5). The code change is PLANNED (IDV-2). |
| 6 | Passport identity page conditional versus required | PRD FR-IDV-010, DR-COL-004; OD-AF-04; UI/UX §6.2, §6.4; handoff §5.4; Schema §7.3 note | No contradiction: these anticipate an "approved policy", and IDV-08 is that policy. The code makes the upload optional per event. | POLICY SET; implementation PLANNED (IDV-3). The schema stays conditional, and the service enforces the policy. |
| 7 | Duplicates: verified-only versus pending and verified | Schema §4.6 (partial index on verified rows); the code (checks `VERIFIED` only) | IDV-07 also requires pending records. | PLANNED REVISION (IDV-1, IDV-3); the partial index can stay and is complemented by locking. |
| 8 | Which participants are "foreign" | PRD FR-IDV-001; the code (passport path open to Algerians) | IDV-08 covers foreign participants. An Algerian national who chooses the passport path is not defined. | Open: **SCOPE-01**. |
| 9 | Sequencing against P4-5 | Playbook §4 (P4-5 follows P4-4); addendum §7 ("before the final system-test/review packages") | The hard constraint is before P4-6. P4-5's position is not stated. | Open: **SEQ-01**, with a recommendation in the playbook (§7a). |
| 10 | Identity states versus public status | TRD §8.4 ("MUST NOT be collapsed"); UI/UX §10.1 | The addendum's candidate states must not become participant statuses. | Resolved in the overlay: a separate state machine; return for correction uses Additional Information Required (candidate, IDV-1). |
| 11 | International mobile optional | TRD REG-010; AF-REG-04; handoff §5.2 | Older text. | PRE-EXISTING: superseded by A-01 (D-01). Nothing new. |
| 12 | Stale status table | the project status record (historical) header table (still shows Phase 4 Prompt 3 as current) | Older packages appended history without refreshing the table. | A dated "status as of 2026-10-01" row is added at the top. The older rows are kept. |

## 4. Implementation status after IDV-1 to IDV-4 (2026-10-01)

Authority: the owner's implementation handoff, recorded as amendment A-13
(`requirements_overlay_2026-10-01_A13_identity_implementation.md`, decision gate §17.11). Sections
1 to 3 above are kept unchanged as the PLAN-SYNC record. Evidence labels as in the header, plus
**IMPLEMENTED (IDV)**: code and named tests exist in this assignment's tree; the gate result is in
the historical review record `phase_04_idv_report.md` (kept outside the repository). "Synthetic" means a test double or the development
simulation; it is never provider evidence.

| Decision | Status now | Implementation | Tests (synthetic unless stated) |
| --- | --- | --- | --- |
| AUTH-01..AUTH-04, CACHE-01, UI-01 | Unchanged (A13-15) | No change to sign-in, OTP, step-up, counter or footer | Existing suites in the gate |
| IDV-01 | IMPLEMENTED (IDV); real provider **NOT_PROVEN** | `apps/people/nin_provider.py` (`MinistryNinProvider`), `apps/people/services/identity_verification.py` | `apps/people/tests/test_idv_ministry_adapter.py`, `test_idv_worker.py` |
| IDV-02 | IMPLEMENTED (IDV) | `apps/people/identity_contract.py::evaluate_identity_match` | `test_idv_contract.py` |
| IDV-03 (MATCH-01, MIN-01) | IMPLEMENTED (IDV): case/whitespace only, no rewrite; minimum retained facts | `identity_contract.py`; attempt fields (people.0003) | `test_idv_contract.py`, `test_idv_worker.py::test_attempts_store_no_clear_identity_outside_encrypted_official_facts` |
| IDV-04 | IMPLEMENTED (IDV) | Worker routing, bounded retries | `test_idv_worker.py::test_unverifiable_outcomes_route_to_manual_review_once`, `::test_transient_failures_retry_with_backoff_then_go_to_manual_review` |
| IDV-05 | IMPLEMENTED (IDV) | `identity_review.correct_nin_and_recheck` | `test_idv_review_services.py` (NIN correction tests) |
| IDV-06 | IMPLEMENTED (IDV) | `identity_review.verify_identity_manually` | `test_idv_review_services.py::test_manual_verification_after_not_found_records_source_actor_and_evidence` |
| IDV-07 | IMPLEMENTED (IDV), PostgreSQL concurrency tested | `find_cross_person_conflicts`, `lock_identifier_values`, final recheck | `test_idv_review_services.py` (duplicates), `tests/concurrency/test_idv_concurrency.py` |
| IDV-08 (A13-02) | IMPLEMENTED (IDV) | Passport route, identity page required, no job | `test_idv_worker.py::test_a_foreign_participant_goes_to_manual_review_with_no_ministry_job`, `apps/registrations/tests/test_passport_identity_page.py` |
| IDV-09 | IMPLEMENTED (IDV) | `return_identity_for_correction`, `resubmit_identity_correction`, participant page | `test_idv_review_services.py`, `test_idv_views.py`, `tests/browser/test_idv_review.py` |
| IDV-10 | IMPLEMENTED (IDV) | Separate state machine; participation untouched | `test_idv_worker.py::test_a_simulated_match_verifies_without_approving_participation` |
| IDV-11 (API-03 resolved, API-02 narrowed) | IMPLEMENTED (IDV) | DD/MM/YYYY only; presumed-date owner rule | `test_idv_contract.py` |
| DOC-01 (DOC-02 resolved for formats) | IMPLEMENTED (IDV); retention periods OPEN (OD-007) | `NATIONAL_ID_CARD` type, `save_national_id_card`, evidence preview | `test_idv_views.py` (evidence tests), `test_idv_review_services.py::test_manual_verification_needs_reviewable_evidence` |
| SCOPE-01 (A13-01) | IMPLEMENTED (IDV) | NIN route mandatory for Algerians; staff exception | `apps/registrations/tests/test_identity_and_phone_validation.py::test_algerian_nationals_cannot_use_the_passport_path`, `test_idv_review_services.py` (exception tests) |
| STUB-01 / F-PS-01 (A13-06) | IMPLEMENTED (IDV) | `config/settings/validation.py` check 11; worker guard; readiness item | `tests/foundation/test_idv_settings_and_redaction.py` |
| UAT-01 | Guide written; **NOT EXECUTED** | `docs/testing/phase_04_idv_manual_uat_guide.md` | Human scenarios pending |

| Scenario | Status | Kind executed | Where |
| --- | --- | --- | --- |
| AS-01 | Passed with the simulation; real provider NOT_PROVEN | A | `test_idv_worker.py::test_a_simulated_match_verifies_without_approving_participation` |
| AS-02 | Passed | A | `test_idv_contract.py::test_case_and_whitespace_only_differences_verify_without_rewriting` |
| AS-03 | Passed | A | `test_idv_worker.py::test_a_name_mismatch_goes_to_manual_review_without_overwriting` |
| AS-04 | Passed (synthetic); P pending | A | `test_idv_ministry_adapter.py`, `test_idv_contract.py` |
| AS-05 | Passed | A | `test_idv_ministry_adapter.py` (auth), `test_idv_worker.py` (retries) |
| AS-06 | Passed | A | `test_idv_contract.py::test_a_different_returned_nin_never_verifies_and_retains_nothing` |
| AS-07 | Passed | A | `test_idv_contract.py` (date and flag tests) |
| AS-08 | Passed | A, C | `test_idv_worker.py::test_a_result_for_a_superseded_revision_is_discarded`, `test_idv_concurrency.py::test_a_result_racing_a_nin_correction_elsewhere_never_verifies` |
| AS-09 | Passed | C | `test_idv_concurrency.py::test_two_cross_person_duplicate_cases_are_never_both_verified` |
| AS-10 | Passed | A | `test_idv_views.py::test_duplicate_conflicts_are_shown_before_confirmation` |
| AS-11 | Passed | A | `test_idv_review_services.py::test_manual_verification_cannot_override_a_cross_person_duplicate` |
| AS-12 | Passed | A | `test_idv_worker.py::test_a_foreign_participant_goes_to_manual_review_with_no_ministry_job` |
| AS-13 | Passed | A | `test_idv_review_services.py` (roles and scope), `test_idv_views.py` |
| AS-14 | Passed (A); M pending | A, browser | `test_idv_review_services.py`, `tests/browser/test_idv_review.py::test_the_participant_corrects_on_the_same_registration` |
| AS-15 | **NOT EXECUTED** (M, P) | — | UAT guide §5 |
| AS-16 | Passed | A | `tests/foundation/test_idv_settings_and_redaction.py`, `test_idv_worker.py::test_audit_events_carry_no_civil_data` |
| AS-17 | Passed | A | `tests/foundation/test_idv_settings_and_redaction.py` |

## 5. IDV-C1: corrections after the independent review (2026-10-02)

Authority: the developer's IDV-C1 instruction with the independent review (CHANGES_REQUIRED,
R-IDV-01 to R-IDV-06). Sections 1 to 4 above are kept; this section supersedes the rows it names.
Every regression below was run first against the unfixed IDV tree, where it failed for the
defect, and then against the fixed tree, where it passed. The evidence is in
the IDV-C1 reproduction evidence (historical) and the historical review record `phase_04_idv_c1_report.md` (kept outside the repository).
Kinds: S = service or view test (PostgreSQL), C = multi-connection PostgreSQL concurrency, B =
real Chromium, T = real TLS against a synthetic local peer. None is provider evidence.

| Finding | Fix (where) | Regressions (kind) |
| --- | --- | --- |
| R-IDV-01: a stale confirmation could resurrect a replaced NIN | Person lock first; the person's other cases sharing the identifier are locked and rebound (`LINKED_IDENTITY_CHANGE`); the final write re-reads and locks the identifier, checks the fingerprint against it and updates only from DECLARED or VERIFIED (`identity_verification.py`, `identity_review.py`) | `apps/people/tests/test_idv_c1_shared_identifier.py` (S, 9); `tests/concurrency/test_idv_c1_concurrency.py`: the forced interleaving, the racing stale confirmation, and two corrections at once (C); `tests/browser/test_idv_c1_corrections.py::test_a_stale_confirmation_after_a_linked_correction_shows_a_conflict` (B) |
| R-IDV-02: the official name form was not applied | `_apply_official_name_form`: profile names from the official form for `normalized_match` fields only, recorded as an `OFFICIAL_NAME_NORMALIZATION` revision with an `OFFICIAL_NAME_APPLIED` decision; originals stay in the submission snapshot | `apps/people/tests/test_idv_c1_official_names.py` (S, 6); `tests/browser/test_idv_c1_corrections.py::test_the_review_screen_shows_the_official_name_form` (B) |
| R-IDV-03: the total deadline was not enforced during blocking calls | `HttpsTransport`: every socket call gets only the time left; resolution, connect, handshake, request, headers and body are bounded; no success after the deadline (`nin_provider.py`) | `apps/people/tests/test_idv_c1_transport_deadline.py` (T, 7, including the worker releasing its provider slot) |
| R-IDV-04: expired-lease recovery bypassed the attempt budget | `_claim` stops at `max_attempts()`; `_finalize_without_attempt` exhausts the job and routes the case in one transaction, in the lock order | `apps/people/tests/test_idv_c1_attempt_budget.py` (S, 5) |
| R-IDV-05: a full NIN in search and navigation URLs | POST search resolved into case ids in a server-side, user- and session-bound, expiring context (`apps/people/search_context.py`); URLs carry only `ctx`; `?q=` is redirected | `apps/people/tests/test_idv_c1_search_context.py` (S, 9); `tests/browser/test_idv_c1_corrections.py::test_a_nin_search_keeps_the_nin_out_of_every_url` (B, every request URL checked) |
| R-IDV-06: resubmission reopened a withdrawn or cancelled registration | `close_identity_work_for_registration` from withdrawal, cancellation and NOT_APPROVED; the resubmission requires Additional Information Required under the lock; every staff command refuses a closed registration | `apps/people/tests/test_idv_c1_closed_registration.py` (S, 6, including the authorized reopening); `tests/concurrency/test_idv_c1_concurrency.py`: withdrawal racing a resubmission, and withdrawal racing a ministry result (C); `tests/browser/test_idv_c1_corrections.py::test_a_withdrawn_registration_offers_no_identity_correction` (B) |

Rows superseded:

* **IDV-03 / AS-02.** "case/whitespace only, no rewrite" now reads: a case- or spacing-only
  difference verifies, and the official form becomes the current name (recorded revision,
  originals in the submission snapshot). Substantive differences are still never rewritten. The
  pure contract test `test_idv_contract.py::test_case_and_whitespace_only_differences_verify_without_rewriting`
  still holds for the contract function; the application is covered by
  `test_idv_c1_official_names.py`.
* **IDV-05, IDV-07, AS-08.** A correction also rebinds the person's other cases, and the duplicate
  and freshness checks of the final write use a freshly locked identifier row.
* **IDV-09 / AS-14.** Correction and resubmission only while the registration awaits the
  participant; closure ends them.
* **IDV-04.** The bounded retries include abandoned (expired-lease) attempts.

Not changed by IDV-C1: the owner decisions IDV-Q1, IDV-Q2 and IDV-Q3 (open-question register §6);
AS-15 and UAT-01 (still NOT EXECUTED); every NOT_PROVEN item (ministry service, real mail, real
Redis).
