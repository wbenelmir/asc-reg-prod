# P4-4 threat, permission and private-data-flow matrix

Package: Phase 4 Prompt 4 (whole-platform security and privacy hardening). Date: 2026-10-01.
Scope: TRD §16–§17, approved plan §8, and the playbook's P4-4 block. This is an engineering
record, not a security certification or a penetration test. Synthetic data only.

**Revised by P4-4-C1 (2026-10-01)** after the independent review (R-01 to R-05): the OTP-abuse
row, the cache, storage and MFA rows of §5, and the P44-F06, F07, F09 and OBS-01 rows of §6 were
updated, and rows R-02 and R-03 were added. Earlier dispositions are kept in the rows' text where
they changed, so the history stays visible.

**Revised by P4-4-C3 (2026-10-01)** after owner decisions MFA-01 (revised requirement, amendment
A-11) and CACHE-01: the account-takeover, OTP-abuse and device-theft rows, the cache and MFA rows
of §5, and the P44-F06 and P44-F09 rows of §6 carry a "P4-4-C3" note. Their earlier text is kept.

Evidence labels: **TEST** means a named automated test that ran in this package; **STATIC** means
code inspection only; **NOT PROVEN** means that no local evidence can exist (a real provider or a
deployment is needed).

## 1. Method

* The route inventory comes from Django's resolver. `apps/core/tests/test_p4_4_route_access.py`
  assigns every route an access class, and it fails when a new route has none. 146 routes were
  found: 21 public, 15 participant, 1 private document, 14 checkpoint, 9 device API,
  78 operational with a decorator permission, and 8 operational with a scoped policy in the view.
  The Django admin is not routed in the tested settings.
* Behaviour is tested, not only declared. For every operational route, GET and POST are refused to
  an anonymous visitor and to a signed-in user with no scoped membership. Every participant route
  sends an anonymous visitor to the start page. That makes 364 parametrized cases.
* The threats are the TRD §17.4 list. For each threat, §3 names the control and the tests.
* The data flows (§4) were traced from the models, services, the audit and outbox writers, the
  log redaction, and the cache and session configuration. They are checked by the whole-journey
  persistence sweep (`apps/registrations/tests/test_p4_4_persistence_leak_sweep.py`).

## 2. Access classes and permission matrix

| Access class | Routes | Enforcement | Evidence |
| --- | ---: | --- | --- |
| Public | 21 | Nothing personal is rendered without a token or a session. Token routes (`invite/`, `claim/`) resolve a 256-bit token that is stored only as a hash, and answer generically. `robots.txt` allows only the start page. | TEST: `test_resolve_invitation_link_rejects_an_unknown_token`, `test_claim_fails_for_an_unknown_token`, `test_token_urls_are_private_noindex_and_do_not_leak_through_referrers`, `test_robots_txt_allows_only_the_start_page` |
| Participant | 15 | `participant_required`, then a lookup scoped by ownership (`person=`) | TEST: `test_anonymous_visitors_are_sent_to_the_start_page_on_participant_routes`, `test_participant_cannot_view_another_persons_request`, `test_another_person_cannot_withdraw` |
| Private document | 1 | Operational scope and permission, or participant ownership; anything else is a generic 404 | TEST: `test_other_participant_cannot_access_someone_elses_document`, `test_operational_permission_from_one_group_never_combines_with_scope_from_another` |
| Checkpoint | 14 | `checkpoint_required`: operational sign-in, an enrolled device cookie and an open checkpoint session | TEST: `test_forged_device_cookie_is_refused`, `test_prompt8_checkpoint_org_scope.py` |
| Device API | 9 | DRF, deny by default: an operational session plus a device proof with a one-time nonce. The quarantine endpoint accepts a signature only, by design (ADR-0024). | TEST: `test_a_nonce_is_bound_to_its_device`, `test_one_signed_request_replayed_concurrently_is_accepted_exactly_once` |
| Operational, decorator | 78 | `operational_permission_required(<codename>)` plus a queryset scoped by membership | TEST: the route guard, plus the per-module scope tests below |
| Operational, policy in the view | 8 | A scoped policy in the view, applied before any object lookup | TEST: route guard; `test_an_unknown_assignment_kind_is_a_404_before_any_state_change` (P44-F02) |

Operational permissions per module (generated from the inventory):

| Module | Permission (codename) | Routes |
| --- | --- | ---: |
| `ops/accommodation-requests` | `registrations.coordinate_accommodation_support` | 1 |
| `ops/accreditation` | `(scoped policy in the view)` | 3 |
| `ops/accreditation` | `accreditation.add_bulkassignmentoperation` | 2 |
| `ops/accreditation` | `registrations.view_registration` | 1 |
| `ops/badges` | `badges.activate_digitalentrypass` | 1 |
| `ops/badges` | `badges.add_digitalentrypass` | 1 |
| `ops/badges` | `badges.allocate_badgestock` | 2 |
| `ops/badges` | `badges.issue_badgeissuance` | 2 |
| `ops/badges` | `badges.manage_printbatch` | 2 |
| `ops/badges` | `badges.manage_stocklocation` | 2 |
| `ops/badges` | `badges.manage_verificationkey` | 4 |
| `ops/badges` | `badges.receive_printbatch` | 1 |
| `ops/badges` | `badges.reconcile_badgestock` | 1 |
| `ops/badges` | `badges.resume_digitalentrypass` | 1 |
| `ops/badges` | `badges.return_badgeissuance` | 3 |
| `ops/badges` | `badges.revoke_digitalentrypass` | 2 |
| `ops/badges` | `badges.suspend_digitalentrypass` | 1 |
| `ops/badges` | `badges.transfer_badgestock` | 1 |
| `ops/badges` | `badges.view_badgeissuance` | 1 |
| `ops/badges` | `badges.view_digitalentrypass` | 3 |
| `ops/badges` | `badges.view_printbatch` | 1 |
| `ops/badges` | `badges.view_stocklocation` | 1 |
| `ops/communications` | `communications.view_communicationmessage` | 1 |
| `ops/entry` | `(scoped policy in the view)` | 4 |
| `ops/entry` | `entry.enable_offline_entry` | 1 |
| `ops/entry` | `entry.manage_entrydevice` | 6 |
| `ops/entry` | `entry.view_entry_observability` | 1 |
| `ops/entry` | `entry.view_entrydevice` | 2 |
| `ops/events` | `events.manage_registration_channels` | 2 |
| `ops/exports` | `(scoped policy in the view)` | 1 |
| `ops/exports` | `exports.add_exportrequest` | 1 |
| `ops/registrations` | `registrations.view_registration` | 2 |
| `ops/reviews` | `reviews.add_checklistresult` | 1 |
| `ops/reviews` | `reviews.add_duplicatecandidateresolution` | 1 |
| `ops/reviews` | `reviews.add_informationrequest` | 1 |
| `ops/reviews` | `reviews.add_internalreviewnote` | 1 |
| `ops/reviews` | `reviews.add_registrationdecision` | 2 |
| `ops/reviews` | `reviews.assign_reviewcase` | 1 |
| `ops/reviews` | `reviews.cancel_registration` | 1 |
| `ops/reviews` | `reviews.change_informationrequest` | 3 |
| `ops/reviews` | `reviews.change_reviewcase_status` | 1 |
| `ops/reviews` | `reviews.reopen_reviewcase` | 1 |
| `ops/reviews` | `reviews.view_informationrequest` | 1 |
| `ops/reviews` | `reviews.view_reviewcase` | 2 |
| `organizations` | `invitations.add_delegationbatch` | 2 |
| `organizations` | `invitations.add_invitationcampaign` | 2 |
| `organizations` | `invitations.add_invitationlink` | 1 |
| `organizations` | `invitations.change_invitationcampaign` | 1 |
| `organizations` | `invitations.change_invitationlink` | 1 |
| `organizations` | `invitations.view_delegationbatch` | 1 |
| `organizations` | `invitations.view_invitationcampaign` | 2 |
| `organizations` | `registrations.register_on_behalf` | 1 |
| `organizations` | `registrations.view_registration` | 1 |

Queryset scope per module. A decorator permission is never sufficient on its own: each module
also filters by the actor's scoped memberships.

| Module | Scope rule | Evidence |
| --- | --- | --- |
| Registrations, reviews | Event and organization scope from memberships; out-of-scope objects are a 404 | TEST: `test_case_detail_returns_404_for_an_out_of_scope_case`, `test_case_page_never_lists_an_out_of_scope_person`, `test_forged_assignee_ids_are_refused_identically` |
| Accreditation | Same, and forged reference ids are refused identically | TEST: `test_cross_organization_denial`, `test_direct_url_access_denied_for_out_of_scope_registration`, `test_forged_reference_ids_are_refused_identically` |
| Badges and stock | Event scope; foreign locations and badge types are refused before any side effect | TEST: `test_an_out_of_scope_event_is_a_404_not_a_403`, `test_a_forged_cross_event_issue_post_is_refused_with_no_side_effect`, `test_stock_corrections.py` (the foreign-* tests) |
| Pass lookup | Out of scope is indistinguishable from unknown, and is audited without the target | TEST: `test_an_out_of_scope_match_is_indistinguishable_from_unknown`, `test_an_out_of_scope_lookup_is_audited_without_leaking_the_target` |
| Invitations, organizations | Organization scope; writes to another organization are refused | TEST: `test_campaign_detail_returns_404_for_an_out_of_scope_campaign`, `test_campaign_rotate_link_denies_write_scope_for_a_different_organization`, `test_expired_membership_sees_nothing` |
| Exports | Scope re-derived at request and at download; the raw id list is never trusted | TEST: `test_export_scope_is_re_derived_and_never_trusts_the_raw_id_list`, `test_direct_download_url_denied_for_out_of_scope_export`, `test_service_rejects_out_of_scope_retrieval` |
| Entry, reconciliation, devices | Event, gate and device scope. External security users have no reconciliation or package access. | TEST: `test_prompt8_checkpoint_org_scope.py`, `test_prompt8_gate_identity_isolation.py`, `test_emergency_wipe_page_is_hidden_from_device_administrators` |
| Accommodation support | A dedicated permission, event scope, explicit consent, and hidden after withdrawal or cancellation | TEST: `test_ux3_accommodation_and_consent.py`, `test_uxrc1_corrections.py` |
| External security accounts | Named, time-bounded, gate-scoped, zero permissions by default | TEST: `test_creation_produces_a_scoped_zero_permission_account`, `test_account_denied_server_side_once_expired` |

## 3. Threat matrix (TRD §17.4)

| Threat | Controls | Named tests | Residual risk / status |
| --- | --- | --- | --- |
| Invitation-link guessing and leakage | 256-bit tokens, stored hashed; generic refusal and audit (`INV_LINK_REJECTED`); rotation and revocation; token URLs `noindex`, `private, no-store`, same-origin referrer | `test_resolve_invitation_link_rejects_an_unknown_token`, `test_rotate_link_disables_old_link_and_new_one_works`, `test_consumption_rejects_a_link_revoked_after_initial_resolution`, `test_token_urls_are_private_noindex_and_do_not_leak_through_referrers` | No per-network limit on `invite/`; guessing a 256-bit token is not feasible. A link shared by its holder is out of the platform's control. |
| Account takeover | Participants: email OTP, hashed, bounded attempts, database throttles, ALTCHA; session rotation on sign-in; inactivity and absolute expiry. Operational users: password (minimum 12 characters, validators), **new per-email and per-network attempt limits and sign-in audit (P44-F03)**, session expiry, temporary-account expiry. | `test_max_attempts_locks_the_challenge`, `test_recipient_issuance_rate_limit_locks_after_the_configured_ceiling`, `test_the_email_budget_refuses_without_checking_the_password`, `test_the_email_budget_is_exact_under_concurrency`, `test_operational_inactivity_expiry_forces_sign_in_again`, `test_account_denied_server_side_once_expired` | **Operational MFA is not enforced at sign-in** (pre-existing; P44-F06, decision MFA-01). This is a release blocker. **P4-4-C3:** owner decision MFA-01 revised (A-11): email and password is the approved operational sign-in in staging and production, so this is no longer a release blocker. MFA is still not enforced and is not reported as implemented; the password limits, account expiry, sessions and audit are unchanged (TEST: `test_p4_4_c3_password_sign_in.py`, `test_staging_and_production_start_without_a_sign_in_mfa_provider`). |
| OTP abuse | Recipient and network issuance limits (database, advisory locks), resend cooldown, attempt lock, proof-of-work before issuance, **challenge-endpoint limit per network (P4-4)** | `test_network_issuance_rate_limit_applies_across_different_recipients`, `test_resend_before_cooldown_elapses_is_throttled`, `test_the_otp_throttle_still_applies_after_the_check`, `test_a_replayed_solution_is_refused`, `test_p4_4_challenge_and_cache.py` | **P4-4-C1:** the OTP action is protected by database-backed, exact controls (ALTCHA verification, binding, expiry, atomic one-time use, OTP limits). Challenge *issuance* is bounded only per process (staging and production inherit `LocMemCache`) and fails open on a cache error, so production issuance protection is **PARTIAL** (P44-F09, R-05, decision CACHE-01). **P4-4-C3 (CACHE-01, ADR-0025):** staging and production count issuance in a shared Redis counter (atomic MULTI/EXEC of INCR and EXPIRE); on a Redis failure a stricter, bounded per-process fallback applies (no fail-open) with sanitized, rate-limited alerts; issuance is refused if no counter can count. TEST: `test_p4_4_c3_issuance_counter.py`. Against a real Redis: NOT PROVEN (opt-in `test_p4_4_c3_redis_integration.py` skipped; staging guide). |
| Authorization bypass | Server-side permission plus scope on every route; the route inventory guard | `test_every_route_has_an_access_class`, `test_signed_in_users_without_scope_are_refused_on_every_operational_route`, `test_operational_permission_from_one_group_never_combines_with_scope_from_another` | None known. |
| IDOR | Scoped querysets; out of scope is a generic 404; forged ids are refused identically | §2 scope table; `test_participant_cannot_withdraw_someone_elses_registration`, `test_cross_event_id_reuse_never_links_foreign_evidence` | None known. |
| Export abuse | Permission, purpose and reason; scope re-derived; minimized columns; formula neutralization; expiry; retrieval audited and counted | `test_generated_csv_contains_only_the_minimized_field_schema`, `test_formula_shaped_organization_name_is_neutralized`, `test_expired_export_cannot_be_retrieved_and_records_a_denial`, `test_retrieval_is_audited_and_counted` | Export retention periods are not approved (OD-007). |
| Malicious upload | Allow-listed type, size and dimensions; malware adapter fails closed; private storage; no bytes in PostgreSQL; safe response headers | `test_save_profile_photo_rejects_content_scanner_flags_as_infected`, `test_failed_scan_does_not_leave_orphaned_storage_bytes`, `test_private_storage_object_is_never_publicly_addressable`, `test_response_headers_are_safe` | A real scanner provider: NOT PROVEN. |
| QR tampering | ES256 signature with versioned keys; strict canonical form; forged claims rejected | `test_a_tampered_payload_fails_the_signature`, `test_a_tampered_signature_is_rejected`, `test_a_forged_pid_claim_is_rejected`, `test_tampered_or_malformed_tokens_are_invalid` | Key custody in production: NOT PROVEN (HSM or KMS decision). |
| QR replay | Server-side state (a replaced credential is stale); duplicate-scan handling; Entry Events are append-only | `test_replaced_credential_version_is_stale`, `test_duplicate_scan_second_admission_becomes_stale_then_advisory`, `test_entry_events_are_append_only` | A screenshot of a still-valid pass remains usable until revocation: an accepted design property. |
| Device theft | Non-exportable device keys; revocation; quarantine boundary; operator inactivity lock; emergency wipe behind a separate permission and MFA step-up (fail closed) | `test_a_revoked_device_uploads_through_the_quarantine_boundary`, `test_wipe_fails_closed_without_a_valid_mfa_step_up`, `test_device_administrators_cannot_order_a_wipe` | The wipe cannot be ordered until a real MFA provider exists (MFA-01). **P4-4-C3:** unchanged by the MFA-01 revision, which covers sign-in only; a password session never replaces the step-up (TEST: `test_a_password_session_never_replaces_the_wipe_step_up`). Without a provider, `entry.W001` warns at `check --deploy` and `release_readiness` reports `sensitive_operation_step_up` BLOCKED (open question STEPUP-01). |
| Stale offline data | Validity windows; the Stale state permits only manual review or do-not-admit; delta journal; clock-discontinuity block | `test_stale_permits_only_manual_review_or_do_not_admit`, `test_an_admission_on_stale_data_is_a_security_conflict`, `test_an_old_package_is_replaced_by_a_new_version` | Real-clock tests may fail near 23:54 UTC (known test-only issue). |
| Sync forgery | Signed proofs, one-time nonces, idempotent operations, cost-bounded quarantine | `test_jws_tampering_is_detected`, `test_an_operation_id_reused_with_other_content_is_rejected_and_kept`, `test_a_forged_quarantine_batch_costs_at_most_one_pass_over_the_device_keys`, `test_one_operation_sent_concurrently_with_fresh_nonces_is_applied_once` (G-04) | None known. |
| Audit tampering | Append-only service; PostgreSQL `BEFORE UPDATE OR DELETE` trigger; redacted summaries | `test_trigger_blocks_update`, `test_trigger_blocks_delete`, `test_trigger_is_present_and_enabled_in_the_catalog`, `test_persistent_recorder_redacts_sensitive_summary_values` | Database-role isolation (audit-isolation Fact B) is NOT PROVEN locally; it is a deployment requirement. |
| Privileged-insider misuse | Least privilege by group and scope; the superuser is break-glass only; sensitive views, lookups and exports audited; lookup budgets with anomaly signals | `test_successful_access_records_a_bounded_audit_event`, `test_an_out_of_scope_lookup_is_audited_without_leaking_the_target`, `test_p4_4_operational_sign_in.py`, `apps/entry/services/limits.py` tests | An audit review process and alerting are P4-5 material. |

## 4. Private-data flow matrix

| Data | Stored as | Who reads it | Audit, outbox, logs | Cache and session | Retention |
| --- | --- | --- | --- | --- | --- |
| Email address (participant) | Encrypted, plus a blind index (ADR-0006) | Owner; scoped operators | Not in audit or logs (sweep: TEST); the communication destination is encrypted | Pending OTP email in the session (DB-backed session) | OD-007 not approved |
| Mobile number | Encrypted | Owner; scoped operators | Not in audit, outbox or logs (sweep: TEST) | — | OD-007 |
| NIN, passport number | Encrypted at rest, plus a versioned HMAC blind index | Scoped reviewers (masked); checkpoint lookup by blind index | Never in audit, logs or outbox (sweep: TEST) | — | OD-007 |
| Photo, passport page | Private storage (filesystem locally, private S3 in deployment); metadata only in PostgreSQL | Owner; scoped operators; streamed and audited | `DOCUMENT_STREAMED`, no URL | Never cached (`private, no-store`) | OD-007 |
| Accommodation categories and note | The note is encrypted (`note_encrypted`); categories are relational; explicit sensitive consent | Coordinators with `coordinate_accommodation_support`, scoped, hidden after withdrawal or cancellation | Views audited without the category or the note (sweep: TEST) | — | OD-007; legal hold honoured |
| Consents and legal acceptance | Registration-scoped consent facts with the document version | Owner; scoped operators | Audit carries the fact, not the text | — | Legal facts C-09 open |
| OTP code | Django password hash only | Nobody | Never logged or audited (sweep: TEST) | — | Expired rows are cleaned up |
| Operational password | Django password hash | Nobody | Never in audit (TEST: `test_success_and_failure_are_audited_without_the_email_or_password`) | — | — |
| Typed operational email (sign-in) | Only a keyed fingerprint, as the audit target | Auditors (`email_target_uuids` recomputes it) | `ACC_OPERATIONAL_SIGN_IN_*` | — | Audit retention (OD-007) |
| Client IP address | Never stored; keyed fingerprints only | — | `network_fingerprint` (HMAC) | Challenge counter under an HMAC key | Window-bounded |
| Invitation and claim tokens | Hash only | — | Never raw | Claim hash in the session | Delegation purge command (manual) |
| QR payload | Signed; pseudonymous identifiers, no personal data | Checkpoint verification | Verification audited without the token | — | Pass lifecycle |
| Offline package | Encrypted ciphertext for the device; content verified; delivered for 1 h | Enrolled, ready devices only | Build and download events carry no content | Service-worker cache of static assets only | Ciphertext retained 1 h; journal 24 h |
| Sync operations and cases | Append-only rows | Reconciliation authority, scoped | Redacted | — | OD-007 |
| Exports (CSV) | Private storage, minimized columns | The requester in scope; retrieval audited | Counted | — | Expiry job (idempotent) |
| Communications | Destination and body encrypted | `view_communicationmessage`, scoped | Outbox payload carries ids only (sweep: TEST) | — | OD-007 |
| Logs | Redaction filter (`apps/core/redaction.py`) for credentials, tokens, NIN and passport values, and document URLs | Operators of the host | — | — | Deployment policy |
| Error reporting, telemetry, analytics | **None integrated** (no SDK or tracker found; STATIC) | — | — | — | — |

## 5. External dependencies and outage behaviour

| Dependency | Locally | Outage behaviour | Evidence |
| --- | --- | --- | --- |
| PostgreSQL 17 | Real | readyz reports 503 with a generic body; requests fail | TEST: `test_readiness_reports_unavailable_without_leaking_the_driver_error` |
| Cache | LocMemCache (also inherited by staging and production) | readyz reports 503; the challenge limiter fails open, logged without the address | TEST: `test_a_failing_probe_is_unavailable_without_detail`, `test_a_cache_failure_allows_the_request_and_logs_no_address`. Shared counter and degraded mode: CACHE-01, not implemented. **P4-4-C3:** the counter no longer uses this cache (ADR-0025); `/readyz` reports `challenge_counter` (`shared`, `local`, `degraded` or `unavailable`) instead of the process-local `cache` probe, which proved nothing about Redis; the fail-open test was replaced by `test_a_counter_failure_applies_the_stricter_fallback_and_logs_no_address` (TEST: `test_p4_4_c3_readiness.py`) |
| Private storage (S3) | Filesystem | readyz reports 503 when the backend cannot serve requests: **since P4-4-C1** a missing or unusable local root, or an absent or refused S3 bucket, is unavailable, while a healthy absent object is not (R-02); uploads fail closed without orphans; stream returns a generic 404 | TEST: `test_p4_4_c1_storage_readiness.py` (filesystem, and S3 through botocore's offline `Stubber`), `test_missing_storage_object_is_a_generic_404_not_a_500`; real S3 NOT PROVEN |
| Celery and Redis broker | Eager, no broker | Outbox rows persist; recovery sweeps for pending outbox events and **overdue DEFERRED messages (P44-F05)** | TEST: `test_dispatch_pending_communication_events_recovers_a_crashed_dispatch`, `test_p4_4_overdue_retry.py`; real broker NOT PROVEN |
| Mail provider | Local backend | Transient failures retried (bounded); permanent failures are UNDELIVERABLE | TEST: `test_transient_failure_retries_up_to_the_bound_then_fails`; real provider NOT PROVEN |
| Malware scanner | Deterministic adapter | Fails closed | TEST (adapter); real scanner NOT PROVEN |
| NIN verification provider | Synthetic provider | Evidence recorded; no silent pass | TEST: `apps/people/tests/test_nin_provider.py`; real provider NOT PROVEN |
| MFA provider | None | Step-up fails closed (`MfaStepUpUnavailable`) | TEST: `test_emergency_wipe_without_mfa_is_refused`; sign-in enforcement missing (P44-F06), reported BLOCKED by `manage.py release_readiness` (TEST: `test_missing_mandatory_mfa_enforcement_blocks_release`). **P4-4-C3:** sign-in MFA is no longer required (A-11); `MFA_BACKEND` is optional at startup and is the step-up provider only; the release item is now `operational_sign_in` READY with MFA not enforced (TEST: `test_password_only_sign_in_is_the_revised_requirement_and_not_reported_as_mfa`), plus `sensitive_operation_step_up` |
| Signing keys (QR, offline package) | Environment keys | Signing fails closed | TEST: `test_an_unavailable_signing_provider_fails_closed`; HSM or KMS NOT PROVEN |

## 6. Findings disposition

| ID | Severity | Finding | Disposition |
| --- | --- | --- | --- |
| P44-F01 | Medium | No Content-Security-Policy or Permissions-Policy (SEC-004); 4 inline scripts and 2 inline handlers | **Fixed**: an enforced same-origin policy with no inline code; a browser collector fails any CSP violation |
| P44-F02 | Low | `assignment_assign` and `assignment_revoke` stored a flash cookie and redirected on an unknown `kind` before authorization | **Fixed**: generic 404 |
| P44-F03 | Medium | Operational password sign-in had no attempt limit and no audit (AF-AUTH-03) | **Fixed**: per-email and per-network budgets counted from the audit trail, exact per email under concurrency, enumeration-safe |
| P44-F04 | Low (performance) | The badges fallback-reference **network** budget has no suitable index; the count walks an unrelated audit index (6.2 ms at 200k rows, linear growth) | **Open**: decision IDX-01 (an additive partial index needs a migration) |
| P44-F05 | Medium (operational) | A DEFERRED message whose countdown task was lost was never attempted again; a message stuck in SENDING has no recovery | **Fixed for DEFERRED**: an idempotent sweep in the beat schedule. **SENDING**: decision COMM-01 |
| P44-F06 (R-01) | High (pre-existing) | Operational MFA is mandatory (TRD §16.1, PRD FR-AUTH-009) but is enforced only for the emergency wipe | **Open, release blocker**: decision MFA-01 (the provider family). No handwritten MFA. **P4-4-C1:** `/accounts/ops/sign-in/` confirmed as the only staging and production entry point; the blocker is now reported by `manage.py release_readiness`; options, required approvals and the enforcement contract are in `p4_4_c1_mfa_options_comparison.md`. Institutional SSO availability is UNKNOWN. No deferral to P4-8 is approved. **P4-4-C3: resolved by an owner requirement change, not by an implementation.** MFA-01 revised (A-11): email and password sign-in in staging and production for every operational user; no longer a release blocker. MFA is not enforced and not claimed. The step-up of sensitive operations is unchanged (STEPUP-01 open). |
| P44-F07 | Low | readyz probed only the database (DEP-005) | **Partly fixed**: database, cache and storage. Broker, mail and signing alignment are P4-5 material. The P4-4 storage probe had a false positive (R-02), corrected in P4-4-C1. |
| P44-F08 | Low | `SESSION_COOKIE_AGE` was Django's 14-day default | **Fixed**: 10 h, the longest approved lifetime |
| P44-F09 (R-05) | Medium | Unthrottled ALTCHA challenge endpoint and anonymous session growth | P4-4 recorded this as Fixed. **P4-4-C1 correction: PARTIAL, open for production.** Fixed: a per-network limit before any session write, and short-lived challenge sessions. Not established: a global bound, because staging and production inherit the per-process `LocMemCache`; and the limiter fails open on a cache error. Release prerequisite: decision CACHE-01 (a shared counter and a degraded mode), then its own concurrency and outage tests. The OTP action's database-backed protections are unaffected. **P4-4-C3: implemented (CACHE-01, ADR-0025)** — shared Redis counter, stricter bounded fallback, alerts, readiness states; local tests pass. Status: fixed in code, **NOT PROVEN against a real Redis** until the staging verification runs. **P4-4-C4:** two counter-state defects found by the independent review are fixed. R-C3-01: a `PING` no longer counts as recovery; only a completed counter write does, and probes keep the retry schedule. R-C3-02: a working shared counter is no longer reported unavailable because of an earlier fallback failure. TEST: `test_p4_4_c4_counter_state.py`. |
| P44-F10 | Low | The indexable start page could be shared-cached, including a bound error that repeats the email | **Fixed**: `private, no-store` |
| G-04 | Coverage | HTTP-level nonce race untested | **Closed**: PostgreSQL race tests; no defect |
| OBS-01 (R-04) | Test quality | Running `test_ux1_shared_controls.py` immediately before `test_ux2_identity_contact_professional.py` fails two UX-2 tests: a request leaves Arabic active in the test thread. This is pre-existing, and does not occur in the canonical order. | P4-4 recorded it unchanged. **Fixed in P4-4-C1**: a project-wide autouse fixture (root `conftest.py`) restores the thread's active language after every test. Reproduced first (2 failed, 163 passed), then forward 165 passed, reverse 165 passed, the affected tests by node id 3 passed. No assertion changed. |
| R-02 | Medium | The storage readiness probe ignored `exists()` and reported a missing local root as reachable | **Fixed in P4-4-C1**: a backend-aware check (`check_private_storage_ready`). It proves, for the filesystem, root existence, type, permission bits and listing; for S3, endpoint, credentials, bucket and `HeadBucket` permission. It does not prove upload, read of an existing object or delete. TEST: `test_p4_4_c1_storage_readiness.py`. Real S3 NOT PROVEN. |
| R-03 | Medium | The local gate ran `manage.py check` without a database, and the privacy checks returned nothing when their assessment failed, so release blockers were invisible and an unassessable state looked clean | **Fixed in P4-4-C1**: `manage.py release_readiness` (READY / BLOCKED / NOT_ASSESSED; database-dependent facts are NOT_ASSESSED on an unavailable or outdated schema); `scripts/check.py release-readiness`; `all` runs `check --database=default` and prints release readiness beside, never inside, the local result; `privacy.W003` / `W004` for a failed assessment. TEST: `test_p4_4_c1_release_readiness.py`, `test_p4_4_c1_release_readiness_gate.py`. |
