# Phase 3 — consolidated manual verification record

Scope: everything authorized in Phase 3 to date (Prompts 2–6): the Digital
Entry Pass lifecycle and signed QR contract, generic physical badge stock
and issuance, online entry/security, and the entry UI/observability/
performance layer. Verification-only; no offline verification, Offline
Packages, PWA behaviour, or Event Edge is implemented or claimed.

**Status of this document: NONE of the scenarios below has been executed by
a human.** Every scenario is written for a person to run locally with
synthetic data only (`example.test` addresses, invented identity values —
never a real NIN, passport number, photograph, or pass). Automated evidence
is cited per scenario from the real Phase 3 test suite; a scenario is
marked executed only when a person has actually run it by hand and recorded
the observed result and date here. Screenshots referenced anywhere in this
document (`var/test_artifacts/phase3/prompt5-ui/`, `var/screenshots/`) are
automatic Playwright captures — evidence of rendering only, never a claim
of human review or approval.

This document consolidates and supersedes the scenario numbering of the
four per-prompt manual test scripts, which remain in place unmodified as
detailed step-by-step references:

- [`phase_03_prompt_02_manual_test.md`](phase_03_prompt_02_manual_test.md) — Digital Entry Pass lifecycle, QR contract, key custody
- [`phase_03_prompt_03_manual_test.md`](phase_03_prompt_03_manual_test.md) — physical badge stock, allocation, issuance, reconciliation
- [`phase_03_prompt_04_manual_test.md`](phase_03_prompt_04_manual_test.md) — online entry, device enrollment, lookups, overrides, restrictions
- [`phase_03_prompt_05_manual_test.md`](phase_03_prompt_05_manual_test.md) — entry/pass/stock UI, localization/RTL, observability, performance

Each section below gives the evidence field for this consolidated pass and
points back to the exact per-prompt scenario numbers for full steps,
instead of duplicating ~750 lines of prior step text verbatim.

## 0. Preconditions

```bash
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py check
uv run --env-file .env python manage.py runserver
```

Requires: a published `ACTIVE` verification key (`QR_SIGNING_KEY_V1` and
related env vars, supplied by the developer, never generated or committed
here); one `EventEdition` with a venue, gate, and zones; an access profile
allowed at the zone; one approved, fully assigned registration with an
`ACTIVE` Digital Entry Pass; an enrolled entry device; operators in `Entry
Operators`, `Entry Supervisors`, `Entry Device Administrators`, `Badge Stock
Administrators`, `Badge Stock Issuers`, `Pass Administrators`, `Pass
Viewers`, and `Credential Key Custodians`, each correctly event/gate/
organization-scoped. See the per-prompt docs above for exact shell/admin
setup commands.

---

## A. QR contract: deterministic vectors and credential states

| Scenario | Ref | Evidence field |
| --- | --- | --- |
| Deterministic QR contract and public test vectors reproduce byte-for-byte | Prompt 2 doc §§1–8 combined with `apps/badges/tests/test_vectors.py` (9 tests: canonical header/payload bytes, signing input, committed signature verifies with the committed public key, every invalid vector fails, every vector produces its documented result) | **Automated: PASS** (see §H of the Prompt 6 report). **Human executed: NOT EXECUTED.** |
| Valid credential verifies | Prompt 2 doc §3; `apps/badges/tests/test_signing_and_verification.py::test_an_active_credential_verifies` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Expired credential (`EXPIRED`, fails closed without a worker) | Prompt 2 doc §7; `test_expiry_fails_closed_without_any_sweep` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Future / not-yet-valid credential | `test_a_not_yet_valid_credential_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Malformed token (truncated, padded base64url, tampered payload/signature, `alg:none`, HS256 substitution, unknown/extra header member, `crit` header) | Prompt 4 doc §3.5; `test_malformed_input_is_rejected`, `test_padded_base64url_is_rejected`, `test_alg_none_substitution_is_rejected`, `test_hs256_substitution_is_rejected`, `test_an_unknown_header_member_is_rejected`, `test_a_crit_header_is_rejected`, `test_a_tampered_payload_fails_the_signature`, `test_a_tampered_signature_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Wrong event code | `test_a_wrong_event_code_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Wrong / unknown key | `test_an_unknown_key_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Unsupported payload version | `test_an_unsupported_payload_version_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Revoked credential | Prompt 4 doc §3.4; credential-status mapping tests | Automated: PASS. **Executed: NOT EXECUTED.** |
| Suspended credential (never reported as revoked) | Prompt 2 doc §4; `test_suspended_is_not_reported_as_revoked` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Replaced credential (old code reports `REPLACED`, fallback reference unchanged) | Prompt 2 doc §5 | Automated: PASS. **Executed: NOT EXECUTED.** |
| Stale credential version | `test_a_stale_credential_version_is_rejected` | Automated: PASS. **Executed: NOT EXECUTED.** |

---

## B. Signing-key lifecycle

| Scenario | Ref | Evidence field |
| --- | --- | --- |
| Key rotation overlap: a credential issued under the retired key still verifies inside its retirement window, using the new active key for new issuance | `apps/badges/tests/test_signing_and_verification.py::test_a_retired_key_within_its_window_still_verifies` (promotes `v2`, confirms `v1` becomes `RETIRED`, and the pre-rotation token still verifies) | Automated: PASS. **Executed: NOT EXECUTED.** |
| Retired key outside its retirement window can no longer verify | `test_a_retired_key_outside_its_window_cannot_verify` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Emergency key revocation invalidates every credential immediately, regardless of the credential's own expiry | Prompt 2 doc §8; `test_emergency_key_revocation_fails_verification_immediately` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Unavailable signing provider fails closed (new-pass generation cannot proceed; existing verification is unaffected by a signing-side outage) | `apps/badges/tests/test_signing_and_verification.py::test_an_unavailable_signing_provider_fails_closed` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Only one key can be active at a time; a private-key PEM is refused at the key-publication surface | Prompt 2 doc §1; `test_only_one_key_can_be_active_at_a_time`, `test_a_private_key_pem_is_refused_by_the_key_service` | Automated: PASS. **Executed: NOT EXECUTED.** |

---

## C. QR payload privacy and log/audit redaction

| Scenario | Ref | Evidence field |
| --- | --- | --- |
| QR payload carries exactly the eleven allowed claims and no identifying or internal value | `test_the_qr_carries_exactly_the_eleven_allowed_claims`, `test_the_qr_contains_no_identifying_or_internal_value` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Identity values (NIN, passport, name) never reach logs, audit, or events | `apps/entry/tests/test_redaction.py::test_identity_values_never_reach_logs_audit_or_events` | Automated: PASS. **Executed: NOT EXECUTED.** |
| The QR/pass token itself never reaches logs or audit, even on a failed verification | `test_qr_token_never_reaches_logs_or_audit_even_on_failure` | Automated: PASS. **Executed: NOT EXECUTED.** |
| Device secret and activation code never reach logs or audit | `test_device_secret_and_activation_code_never_reach_logs_or_audit` | Automated: PASS. **Executed: NOT EXECUTED.** |
| An HTTP lookup never logs the submitted raw value (only a masked form) | `test_http_lookup_does_not_log_the_submitted_value`; Prompt 2 doc §9 (masked audit on the fallback-reference surface) | Automated: PASS. **Executed: NOT EXECUTED.** |

---

## D. Concurrent pass replacement

| Scenario | Ref | Evidence field |
| --- | --- | --- |
| Two concurrent replace/generate operations against the same registration never mint two live credentials; a repeated `operation_id` is a replay, not a second write | Prompt 2 doc §6; `tests/concurrency/test_pass_credential_concurrency.py` (all four tests, real multi-connection PostgreSQL) | Automated: PASS. **Executed: NOT EXECUTED.** |

---

## E. Physical badge stock: production, movement, and reconciliation

Full step-by-step scenarios: Prompt 3 doc §§1–10 (stock location/event
scoping, print-batch lifecycle and the damage split at receipt, transfer
between locations, issuance eligibility, replacement/return/loss/void,
allocation, reasoned adjustment and reconciliation, event and
handover-only boundaries, one-badge-per-registration across reassignment,
localization/RTL). All ten scenarios remain **NOT EXECUTED** by a human;
automated evidence is cited per scenario in that document and re-verified
in this pass (see the Prompt 6 report §H).

| Scenario | Evidence field |
| --- | --- |
| Negative-stock prevention (concurrent transfer/issuance never takes a balance below zero) | Automated: PASS, `tests/concurrency/test_badge_stock_concurrency.py`. **Executed: NOT EXECUTED.** |
| Double-issuance prevention (two simultaneous issuances against one unit, or against the same assignment, never both succeed) | Automated: PASS, same file. **Executed: NOT EXECUTED.** |
| Reconciliation: recomputing the balance from every ledger row for a location/Badge Type equals the projected balance | Automated: PASS, Prompt 3 doc §7. **Executed: NOT EXECUTED.** |

---

## F. Online entry, security, and observability

Full step-by-step scenarios: Prompt 4 doc §§1–8 (device enrollment,
checkpoint set-up and cross-checkpoint denial, online verification,
identity/reference lookup, supervisor override, restrictions and the
external-security view, localization, device lifecycle) and Prompt 5 doc
§§A–D (entry screen states, participant pass, badge operations/devices,
performance). All remain **NOT EXECUTED** by a human.

| Scenario | Evidence field |
| --- | --- |
| NIN lookup (masked hint only, full value never re-shown) | Automated: PASS (`apps/entry` lookup tests); Prompt 4 doc §4. **Executed: NOT EXECUTED.** |
| Passport-reference lookup | Automated: PASS; Prompt 4 doc §4. **Executed: NOT EXECUTED.** |
| Registration-reference lookup | Automated: PASS; Prompt 4 doc §4. **Executed: NOT EXECUTED.** |
| Controlled manual lookup (operator: absent/403; supervisor: short candidate list, never a free-text identity search) | Automated: PASS; Prompt 4 doc §4.4. **Executed: NOT EXECUTED.** |
| Device, event, Gate, Zone, checkpoint, and permitted-operation scope denial | Automated: PASS; Prompt 4 doc §2, §3.6, §8. **Executed: NOT EXECUTED.** |
| Entry Event idempotency and duplicate-scan handling (a second scan of an already-admitted pass reports "Previously admitted", not a second admission) | Automated: PASS; Prompt 4 doc §3.2–3.3. **Executed: NOT EXECUTED.** |
| Re-entry advisory wording distinct from a fresh admission | Automated: PASS; Prompt 4 doc §3.3. **Executed: NOT EXECUTED.** |
| External-security restricted view (neutral wording; no Badge Type, identity hint, or restriction category shown) | Automated: PASS; Prompt 4 doc §6.2. **Executed: NOT EXECUTED.** |
| Temporary (`EXTERNAL_SECURITY`) account expiry via `expire_entry_access` | Automated: PASS; Prompt 4 doc §6.3. **Executed: NOT EXECUTED.** |
| Supervisor override of an overrideable denial, tied to an explicit catalogue entry | Automated: PASS; Prompt 4 doc §5.1–5.2. **Executed: NOT EXECUTED.** |
| Non-overrideable restriction (e.g. a revoked pass) offers no override control even to a supervisor | Automated: PASS; Prompt 4 doc §5.3. **Executed: NOT EXECUTED.** |
| Rate limits and anomaly signals for repeated invalid scans and identity-reference lookups | Automated: PASS; Prompt 5 doc §A8. **Executed: NOT EXECUTED.** |
| Participant-detail timeout clearing | Automated: PASS; Prompt 4 doc §3.7; Prompt 5 doc §A6. **Executed: NOT EXECUTED.** |
| Degraded-online messages and the offline-readiness placeholder (no offline verification exists) | Automated: PASS; Prompt 5 doc §A9. **Executed: NOT EXECUTED.** |

---

## G. Scanner hardware and physical device compatibility

**These scenarios require physical hardware this session cannot access and
are marked `NOT EXECUTED` without qualification. No claim of camera,
handheld-scanner, or physical-device execution is made anywhere in this
document or the Prompt 6 report.**

| Scenario | What a human must verify locally | Evidence field |
| --- | --- | --- |
| G1 | A USB/Bluetooth handheld barcode/QR scanner, configured as a keyboard-wedge device, focused on the verify screen's QR field, produces the same result as manual paste for a valid and an invalid code. | **NOT EXECUTED** — no physical scanner available in this environment. |
| G2 | A device camera (phone/tablet) used with an OS- or browser-level QR-to-keyboard capture app feeds the verify screen correctly, including a low-light or angled scan. | **NOT EXECUTED** — no physical camera/device available in this environment. |
| G3 | Rapid sequential handheld scans (operator scanning a queue of people) do not drop, duplicate, or misattribute a scan when the previous result is still on screen. | **NOT EXECUTED** — requires physical hardware and a human operator. |

Automated coverage of the *software* side of scanner-friendly behaviour
(auto-focus and refocus of the QR field, keystroke capture without losing
characters, Enter submits, Esc clears) is real and passing —
`tests/browser/test_entry_ui_prompt5.py` — but a Playwright keyboard event
is not evidence that genuine scanner or camera hardware behaves the same
way. That gap is reported, not concealed.

---

## H. Localization, RTL, accessibility, responsive layout

Full scenarios: Prompt 5 doc §A11–A13, §B2, Prompt 3 doc §10, Prompt 4
doc §7.

| Scenario | Evidence field |
| --- | --- |
| English/French/Arabic parity across entry, pass, and stock screens | Automated: PASS (`tests/browser/test_entry_ui_prompt5.py`, `tests/browser/test_i18n_rtl_accessibility.py`, `tests/foundation/test_locale_catalogs.py` — 0 untranslated, 0 fuzzy). **Executed: NOT EXECUTED.** |
| Arabic RTL layout, navigation, forms, and bidi isolation of dynamic LTR values (codes, references, tokens) | Automated: PASS (post-correction browser assertions, Prompt 5 report §C). **Executed: NOT EXECUTED.** |
| Keyboard-only navigation and visible focus | Automated: PASS (structural assertions only — no screen-reader session was run). **Executed: NOT EXECUTED. No screen-reader session has ever been run against this application.** |
| Responsive layout at tablet/mobile widths and 200% zoom | Automated: PASS (`resize_window`-equivalent Playwright viewport assertions). **Executed: NOT EXECUTED.** |

---

## I. Django system checks, migrations, formatting, security scans, dependency audit

These are inherently automated and are reported with their exact commands
and results in the historical review record `phase_03_prompt_06_report.md` (kept outside the repository) §Verification,
not as manual scenarios here: `manage.py check`, `makemigrations
--check --dry-run`, migration reversibility, `ruff format`/`ruff check`,
the credential/secret scan, the vendored-asset checksum check,
`manage.py check --deploy`, and `pip-audit` (via `uv run
--env-file .env python scripts/check.py all`).

---

## Deployment-only boundaries (never claimed locally)

Unchanged from the Prompt 2/3 manual test docs: a real managed KMS/HSM
signing integration; production key custody, escrow, or recovery; real
Redis/Celery broker behaviour; real S3-compatible object storage; real
email or SMS delivery; production database-role isolation (ADR-0008 Fact
B); offline verification, Offline Packages, offline revocation
propagation, PWA behaviour, and Event Edge (all unimplemented and
unauthorized as of Phase 3 Prompt 6).

---

## Addendum — Phase 3 final closure (Prompt 9, 2026-09-23)

This addendum extends the record to the work approved after Prompt 6: the
UI/UX Completion Gate (Checkpoint 1 and Stage 3) and the Prompt 8
corrections. Sections A–I above are unchanged.

**Execution status at Phase 3 closure: still NONE of the scenarios in this
document, above or below, has been executed by a human.** No screen-reader
session, physical phone or tablet, camera, or handheld scanner has been
used against this application. The automated results cited here come from
the final Prompt 9 acceptance gate (the historical review record `phase_03_review_pack.md` (kept outside the repository)
§7); they are not a substitute for a human run.

### J. UI/UX Completion Gate (full steps: the historical review record `phase_03_ui_ux_completion_report.md` , kept outside the repository)

| Scenario | Automated evidence | Evidence field |
| --- | --- | --- |
| J1. A signed-in user without permission sees the localized 403 page (EN/FR/AR, RTL) with no redirect loop; an anonymous user is sent to sign-in with a safe `next` | `apps/accounts/tests/test_access_denied.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| J2. Localized 400, 403, 404, 500 and CSRF-failure pages; no diagnostic text; CSRF still refused | `apps/core/tests/test_error_pages.py`; browser `err-*` captures | Automated: PASS. **Executed: NOT EXECUTED.** |
| J3. Staff forms offer permission-scoped labelled choices, never raw database ids; a forged or out-of-scope id gives one generic error | `apps/reviews/tests/test_scoped_choices.py`, `apps/accreditation/tests/test_bulk_scoped_choices.py`, `apps/exports/tests/test_scope_choices.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| J4. Lists become stacked records below 768 px (360/390 px) with every reference, status and action inside the record, in EN/FR/AR | `tests/browser/test_ui_ux_stage3.py` | Automated: PASS (emulated widths in headless Chromium). **Executed: NOT EXECUTED on a physical device.** |
| J5. Destructive actions ask for confirmation in a dialog (focus trap, Escape, restore focus); without JavaScript a consent box is required | `tests/browser/test_ui_ux_stage3.py` dialog tests | Automated: PASS. **Executed: NOT EXECUTED.** |
| J6. Arabic pages download and render the self-hosted Thmanyah faces (400/500/700); English and French never load them | `test_thmanyah_webfont_is_served_loaded_and_used_for_arabic`, `test_blocked_font_request_is_detected_so_the_success_case_is_real`, `test_latin_interfaces_never_load_or_use_thmanyah` | Automated: PASS. **Executed: NOT EXECUTED by a human reader of Arabic.** |
| J7. French terminology « Examen des dossiers » / « File d’examen » | Locale catalogues, browser captures | Automated: PASS. **Executed: NOT EXECUTED.** |

### K. Prompt 8 corrections (full detail: the historical review record `phase_03_prompt_08_corrections_report.md` , kept outside the repository)

| Scenario | Automated evidence | Evidence field |
| --- | --- | --- |
| K1. Operational service failures appear as translated messages, never raw English service text (FR/AR) | `apps/badges/tests/test_prompt8_localized_service_errors.py`, `apps/entry/tests/test_prompt8_localized_service_errors.py`, `tests/browser/test_prompt8_localized_errors.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K2. A withdrawn or cancelled Registration Context cannot receive, or have replaced, a physical badge; stock is untouched and one denial audit is written | `apps/badges/tests/test_prompt8_badge_issuance_eligibility.py`, `tests/concurrency/test_prompt8_withdrawal_races.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K3. `QR_SIGNING_KEY_V<n>` values never appear in logs (raw or normalized PEM) | `tests/foundation/test_prompt8_qr_signing_key_redaction.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K4. A QR value with a trailing newline or control character is rejected | `apps/badges/tests/test_prompt8_canonical_strictness.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K5. Two venues with the same gate code never share monitor or metrics data | `apps/entry/tests/test_prompt8_gate_identity_isolation.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K6. A withdrawn or cancelled context's pass cannot be activated or resumed and shows no usable QR | `apps/badges/tests/test_prompt8_pass_registration_status.py`, `apps/entry/tests/test_prompt8_withdrawn_context_gate.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K7. Every identity/reference lookup outcome, including empty input, is audited and counted once; the issuing country is exactly two letters | `apps/entry/tests/test_prompt8_lookup_outcome_accounting.py` | Automated: PASS. **Executed: NOT EXECUTED.** |
| K8. An organization-scoped membership grants no checkpoint capability | `apps/entry/tests/test_prompt8_checkpoint_org_scope.py` | Automated: PASS. **Executed: NOT EXECUTED.** |

### Still awaiting genuine human execution at closure

* Every scenario in §§A–K.
* §G1–G3 (handheld scanner, camera, rapid sequential scans): requires
  physical hardware.
* A screen-reader session (NVDA/JAWS/VoiceOver/TalkBack) over the entry,
  pass, stock, registration and review screens in English, French and
  Arabic.
* A physical phone and tablet pass in portrait and landscape, and 200 %
  browser zoom on a real display.
* A native Arabic reader's review of Arabic wording and RTL reading order.

### Never provable locally (deployment facts)

Unchanged from the section above, restated at closure: managed KMS/HSM
signing and production key custody; production database-role isolation
(ADR-0008 Fact B, reported as unsatisfied locally); real Redis/Celery
broker integration; real S3-compatible storage; real email and SMS
delivery; production TLS, reverse-proxy and static hosting. Offline
verification, Offline Packages, synchronization, PWA behaviour and Event
Edge remain unimplemented and unauthorized at Phase 3 closure.
