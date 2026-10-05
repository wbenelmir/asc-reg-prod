# Requirements overlay A-13 (2026-10-01): identity implementation decisions after PLAN-SYNC

Date: 2026-10-01. Language: English.

* **Prepared by:** the unified identity implementation assignment (IDV-1 to IDV-4).
* **Authority:** the owner's identity implementation handoff of 2026-10-01
  ("Owner-approved scope, including decisions made after PLAN-SYNC"), supplied by the developer on
  2026-10-01 together with the instruction to implement IDV-1 to IDV-4 in one assignment. The
  handoff states that it supersedes the documentation-only restriction of PLAN-SYNC for this
  assignment.
* **Baseline:** the PLAN-SYNC review archive
  the historical review archive, SHA-256
  `5342cea6badd89a3c6f21626bad3e7a1d02b1d7d92ab1ca11cc37da9886cbf86` (915 source members, no drift
  from the working tree at the start of this assignment).
* **Relationship to A-12.** `requirements_overlay_2026-10-01_identity_and_auth.md` (A-12) stays in
  force. This overlay answers the questions A-12 left open and adds the owner's later decisions.
  Where a row below and an A-12 row govern the same requirement, this row wins. The seven
  specification files in `docs/specifications/` remain read-only and unedited.

## 1. Decisions recorded by this overlay

| ID | Decision (from the handoff) | Resolves or refines | Status |
| --- | --- | --- | --- |
| A13-01 | Algerian nationals must use the NIN route. An explicit staff-assisted exception exists; there is no ordinary self-service passport bypass. | SCOPE-01 (open in A-12) | ACCEPTED |
| A13-02 | Foreign nationals always follow manual passport verification, with no ministry lookup. | IDV-08 (confirmed) | ACCEPTED |
| A13-03 | Retain only the minimum official Latin given and family names and birth-date facts needed for matching, source and correction provenance. No Arabic name storage; no parental names, marriage, divorce or death data; no full API bodies. For presumed dates, keep the flag and the comparison outcome only; an unused official date is not retained. | MIN-01 (open in A-12); D-02 unchanged | ACCEPTED |
| A13-04 | Automatic name correction is limited to case and whitespace normalization. Substantive spelling, accents or transliteration, reordered tokens, missing names or uncertain equivalence go to manual review. No fuzzy-match threshold. | MATCH-01 (open in A-12) | ACCEPTED |
| A13-05 | Reuse the existing evidence formats, size limits, private storage and malware scanning. Map the dedicated identity permissions onto existing scoped staff roles and document the mapping; never grant them to every staff account. | DOC-02, REVIEW-01 (open in A-12) | ACCEPTED (legal retention periods stay open, OD-007) |
| A13-06 | Refuse every stub or mock official-verification backend in staging and production; distinguish development simulations from official verification. | STUB-01 / F-PS-01 | ACCEPTED |
| A13-07 | Implement the feature and its staff interface in one assignment with one final package (IDV-1 to IDV-4 run internally, before P4-5). | SEQ-01; addendum §7 per-stage authorization | ACCEPTED |
| A13-08 | `presume` means that one or both of the birth day and month are unknown. **When `presume` is true, the ministry birth date is ignored entirely for matching and correction; the participant's entered date is kept and relied on; if NIN and names pass, automatic identity verification may succeed.** Record `birth_date_comparison = skipped_presumed`, the flag and the owner-policy basis; never label the entered date as ministry-confirmed. | API-03 (open in A-12) | ACCEPTED (owner rule) |
| A13-09 | `presume` is parsed explicitly: real booleans, and the strings `"True"` / `"False"` with surrounding whitespace and case normalized. Any other encoding, an absent flag or an invalid flag routes to manual review with a precise reason. | API-03, IDV-11 | ACCEPTED |
| A13-10 | The supplied malformed sample was a copy artifact. Synthetic fixtures are corrected; a malformed actual response is INVALID_RESPONSE and is never repaired into success. | API-05 | ACCEPTED |
| A13-11 | The owner reports no service usage limits. This does not remove client timeouts, bounded concurrency and retries, and it does not establish network access, availability or the wider error contract. | API-04 (qualified, not closed) | QUALIFIED |
| A13-12 | The authentication response contract (token field, lifetime, renewal, errors) is NOT supplied. It is a configurable, explicitly validated mapping with synthetic tests; a conservative configurable local token-cache lifetime applies when no expiry is configured; re-authenticate once on a confirmed rejection; the real backend stays unavailable until its settings are valid. | API-01 (stays OPEN) | ACCEPTED as design; the input stays open |
| A13-13 | When `presume` is false, the official date is parsed only from documented unambiguous formats (DD/MM/YYYY observed). Ambiguous order, impossible dates, missing facts or unrecognized formats go to manual review. Never guess the order, never invent 1 January, never change the birth date automatically. | API-02 (the non-presumed format evidence stays OPEN) | ACCEPTED as rule |
| A13-14 | Verification runs after final submission, asynchronously, from a durable job persisted atomically with the submission; a broker outage never turns a saved submission into a failure. Identity status stays separate from participation status. Each result binds to the exact identity revision; stale results are discarded. | ASYNC-01; AF-REG-03 timing (A-12) | ACCEPTED |
| A13-15 | Operational staff sign in with email and password (no ordinary sign-in MFA) in staging and production; participants keep email OTP; the emergency-wipe step-up stays fail-closed; the official shared footer stays empty; the P4-4-C4 Redis counter behaviour is preserved. | AUTH-01..04 (A-11), UI-01 (A-10), CACHE-01 | CONFIRMED (unchanged) |

## 2. Specification sections governed (read-only files, not edited)

| File and section | Effect of this overlay | Decision |
| --- | --- | --- |
| `01_PRD.md` FR-IDV-001 | The NIN path is mandatory for Algerian nationals; the passport path is for foreign nationals only; the staff-assisted exception is staff-only. | A13-01, A13-02 |
| `01_PRD.md` FR-IDV-005, IR-NIN-002 | The retained official facts are exactly: the official Latin family and given names, the normalized official birth date when it was compared (`presume` false), the raw official date text only when it could not be parsed (restricted, for the reviewer), the `presume` flag as parsed, and the comparison outcomes. Nothing else from the response is stored. | A13-03 |
| `01_PRD.md` FR-IDV-007, IR-NIN-004 | Add the routing for `presume` (A13-08, A13-09) and for unparseable official dates (A13-13). | A13-08, A13-09, A13-13 |
| `01_PRD.md` DR-QLT-003, DR-QLT-004 | A case- or whitespace-only difference is accepted as a match (`normalized_match`); the submitted value is never rewritten automatically; the official form is retained for provenance. | A13-04 |
| `02_TRD.md` §15.3 Integration adapters | The adapter contract, the authentication mapping and the client policies are those of `docs/security/identity_provider_contract.md`. | A13-10..A13-12 |
| `02_TRD.md` §8.4 State model | Identity verification is its own state machine (`people.IdentityVerification`): PENDING, API_VERIFIED, MANUAL_REVIEW, MANUALLY_VERIFIED, RETURNED_FOR_CORRECTION, REJECTED. Return for correction reaches the participant through the existing Additional Information Required public status. | A13-14 |
| `03_UI_UX_SPECIFICATION.md` §6.2, §6.4 | The passport identity-page upload is shown and required on every passport path; the national identity card is requested only for Algerian manual review. | A13-02, A13-05 |
| `03_UI_UX_SPECIFICATION.md` §8.4, §19.1 | The identity review queue and review screen, and the permission mapping of `docs/security/identity_review_permissions.md`. | A13-05 |
| `04_APPLICATION_FLOW.md` §4.4 AF-REG-03, §4.7 AF-REG-06 | Submission persists a durable verification job atomically; the provider is called only by the worker, never in the request or inside a transaction. | A13-14 |
| `04_APPLICATION_FLOW.md` §4.5 AF-REG-04 | Foreign participants only; identity page required; zero ministry calls. | A13-02 |
| `05_BACKEND_SCHEMA.md` §4.6, §7.1, §7.3, §16 | Implemented as additive migrations `people.0003` and `documents.0002` (see ADR-0026). | A13-03, A13-14 |
| `06_IMPLEMENTATION_PLAN.md` §9.1 REG-03 "Mock service for CI and Staging" | The local simulation is refused in staging and production at startup; staging uses the official adapter or the disabled backend (manual review only). | A13-06 |
| the project handoff context (historical) §5.3, §5.4 | As above. | A13-01..A13-06 |

## 3. What this overlay does not decide

* The real authentication response (API-01), real non-presumed date examples (API-02), access and
  network requirements and the error contract (API-04 remainder), legal retention periods
  (OD-007), and the authorized real-provider and mail test method (UAT-02) stay open. They are
  listed in `docs/execution/identity_open_questions_2026-10-01.md`.
* No production readiness, staging acceptance or real-provider proof is claimed.
