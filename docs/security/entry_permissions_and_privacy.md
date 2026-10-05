# Online entry — permissions, audit, and privacy classification

Phase 3 Prompt 4 (ADR-0021). Covers `events.Venue`/`Zone`/`Gate`,
`entry.EntryDevice`, `DeviceScope`, `EntryDeviceSession`,
`EntryOperatorSession`, `SecurityRestriction`, `EntryOverrideReason`,
`EntryOverride`, `EntryEvent`, and the checkpoint interfaces. Online
(Plan A) only.

## 1. Permissions

| Codename | Meaning |
| --- | --- |
| `entry.verify_entry` | Operate a checkpoint: scan QR, record Admit / Do not admit / Redirect |
| `entry.lookup_registration_reference` | Look up by registration reference or pass fallback reference |
| `entry.lookup_identity_reference` | Look up by NIN or passport number (blind index only) |
| `entry.manual_search_entry` | Controlled manual name search (bounded, audited) |
| `entry.override_entry` | Record a reason-coded supervisor override |
| `entry.view_entryevent` | Checkpoint monitor: recent entries and denied attempts at this gate |
| `entry.view_entrydevice` | See devices of an event edition |
| `entry.manage_entrydevice` | Register, enroll, rescope, suspend, resume, revoke devices |
| `entry.manage_securityrestriction` | Create and revoke security restrictions |
| `entry.view_restriction_reason` | See a restriction's **category** at a checkpoint (never its free-text reason) |
| `entry.manage_entryoverridereason` | Configure the override catalogue (no UI in this prompt) |

A checkpoint action requires BOTH the user permission scoped to the exact
(event, venue, gate) of the operator's current device session AND the
device scope listing the method (`apps.entry.policies.method_is_permitted`).

### 1.1 Groups (`apps.entry.apps`, idempotent `post_migrate`)

* **Entry Operators** — `verify_entry`, `lookup_registration_reference`,
  `lookup_identity_reference`.
* **Entry Supervisors** — the above plus `manual_search_entry`,
  `override_entry`, `view_entryevent`, `view_entrydevice`.
* **Entry Device Administrators** — `manage_entrydevice`,
  `view_entrydevice`. No verification permission.
* **Security Restriction Managers** — `manage_securityrestriction`,
  `view_restriction_reason`.

No entry group holds `registrations.view_registration`,
`view_registrationprofile`, any document, export, review, or accreditation
permission (regression-tested): **entry users never gain general
participant browsing.** The Phase 2 `External Security (Temporary)` group
still carries zero permissions.

### 1.2 Gate-scoped memberships

`ScopedGroupMembership.venue` / `.gate` (added by `accounts.0004`) narrow a
membership to a checkpoint. Such a membership authorizes only a check that
names that exact venue/gate; every non-checkpoint check ignores it
entirely. A gate-scoped grant can therefore never open event-wide screens,
even if misconfigured with a back-office permission (regression-tested in
`apps/accounts/tests/test_temporary_account_expiry.py`).

## 2. Device credential handling

| Material | Where it exists | Stored as |
| --- | --- | --- |
| Activation code (12 chars, ~59 bits, 15 min, single use) | Shown once to the enrolling administrator | SHA-256 digest; cleared on use |
| Device secret (256 bits) | `HttpOnly; SameSite=Strict; Path=/entry/` cookie on the device (`Secure` in production) | SHA-256 digest |
| `device_key_id` | Audit trail, device detail page | Plain (non-secret, rotated per enrollment) |

Neither the code nor the secret is ever audited or logged
(`apps/entry/tests/test_redaction.py`). Enrollment requires the code AND a
signed-in administrator on the device. Revocation clears the digest.

## 3. What a checkpoint screen may show (FR-ENT-009, ENTRY-001/002)

| Viewer | Shown |
| --- | --- |
| Entry operator / supervisor | Result heading and concise reason, display name, registration reference, profile photo (if on file), Badge Type, masked identity hint (identity lookups only), prior admission time and gate |
| External security account | Result heading, display name, registration reference, profile photo, prior admission. **No** Badge Type, **no** identity hint |
| Anyone, restriction result, without `view_restriction_reason` | Operators: "Security restriction — follow the restricted procedure". External security: neutral "call the supervisor", no restriction wording |
| Anyone, wrong-event / not-approved / unresolved | Result only, **no participant field at all** |

Never shown: full NIN or passport values, documents other than the profile
photo, internal notes, restriction free text, contact data, other
contexts' data, exports. Result pages are `Cache-Control: no-store`.

Participant details clear after `ENTRY_RESULT_CLEAR_SECONDS` of operator
inactivity (Flow §15.3); the result heading stays until the operator
acknowledges it (UI/UX §12.2). Details are also dropped on every decision,
operator sign-out, and session end (`apps.entry.session_state`).

## 4. Audit action codes (no token, identity value, search string, secret, or code)

| Code | When |
| --- | --- |
| `EVT_VENUE_CREATED`, `EVT_ZONE_CREATED`, `EVT_GATE_CREATED` | Venue structure configured |
| `ENT_DEVICE_REGISTERED`, `ENT_DEVICE_ACTIVATION_CODE_ISSUED`, `ENT_DEVICE_ENROLLED`, `ENT_DEVICE_ENROLLMENT_REJECTED` | Registration and enrollment |
| `ENT_DEVICE_SCOPE_CHANGED`, `ENT_DEVICE_SUSPENDED`, `ENT_DEVICE_RESUMED`, `ENT_DEVICE_REVOKED`, `ENT_DEVICE_EXPIRED` | Device lifecycle (AUTHZ-002) |
| `ENT_DEVICE_SESSION_STARTED/ENDED/REJECTED`, `ENT_OPERATOR_SESSION_STARTED/ENDED` | Checkpoint sessions |
| `ENT_VERIFICATION_PERFORMED`, `ENT_IDENTITY_LOOKUP`, `ENT_REFERENCE_LOOKUP`, `ENT_MANUAL_SEARCH`, `ENT_LOOKUP_DENIED` | Every lookup: method, result, reason, gate, zone, device, counts only |
| `ENT_PHOTO_VIEWED` | Profile photo streamed at a checkpoint |
| `ENT_EVENT_RECORDED`, `ENT_DECISION_REJECTED` | Entry Event written / decision refused (stale, not admittable) |
| `ENT_OVERRIDE_RECORDED`, `ENT_OVERRIDE_REJECTED` | Override (original result, blockers, reason code; note presence only) |
| `ENT_SECURITY_RESTRICTION_CREATED/REVOKED` | Restriction lifecycle (severity, category, target kind; never the reason) |
| `ACC_EXTERNAL_SECURITY_ACCOUNT_EXPIRED` | Automatic temporary-account expiry |
| `ENT_LOOKUP_THROTTLED` | Phase 3 Prompt 5: a lookup refused by the per-operator budget (kind, method, window, limit, gate, zone, device) |
| `ENT_ANOMALY_SIGNAL` | Phase 3 Prompt 5: repeated invalid scans or identity-reference lookups crossed the anomaly threshold (kind, count, window, threshold, gate, zone, device); deduplicated per operator, kind and window |

## 5. Data classification

| Data | Class | Protection |
| --- | --- | --- |
| `SecurityRestriction.reason_encrypted` | Restricted, security-restricted | AES-GCM field encryption; never projected to a checkpoint |
| `EntryOverride.note_encrypted` | Restricted | AES-GCM field encryption; audit records presence only |
| `EntryEvent` | Personal, audit-protected | Append-only trigger; no identity value, token, or name columns |
| `EntryDevice.credential_hash`, `activation_code_hash` | Secret-derived | Digest only |
| NIN / passport input at a checkpoint | Restricted | Matched through the versioned HMAC blind index; never stored, logged, audited, or echoed -- including on a validation re-render (`NonEchoTextInput`, Prompt 5) |
| `VerificationSample` (Prompt 5) | Operational telemetry, no personal data | Checkpoint coordinates, method, codes, latency only; pruned after `ENTRY_METRICS_RETENTION_DAYS`; visible only with `entry.view_entry_observability` |

## 6. Known limits (not claimed)

* Operational MFA is not implemented in the repository yet (pre-existing;
  TRD §16.1 requires it for staff and external security accounts).
* Checkpoint lookup limits (Prompt 5, ADR-0022 §6) are best-effort under
  concurrent requests by the same operator, exactly like the approved
  fallback-reference budget.
* Offline verification, Offline Packages, synchronization, and PWA
  behaviour are not implemented.

## 7. Phase 3 Prompt 5 additions (ADR-0022)

| Change | Effect |
| --- | --- |
| New permission `entry.view_entry_observability` | Opens `/ops/entry/events/<event>/observability/` for an event-scoped holder. Granted to **Entry Device Administrators** only. Shows aggregates and checkpoint coordinates; no participant, credential, token, identity value or operator. |
| Passive `/entry/status/` | Validates the checkpoint chain without refreshing any inactivity clock (`OPERATIONAL_PASSIVE_PATHS`, `resolve_checkpoint(touch=False)`); returns a state word only. An idle session still expires while a page polls (tested). |
| Lookup budgets and anomaly signals | Per operator: identity-reference lookups and invalid QR scans. Refusal = HTTP 429, no result, audited; valid QR scans never count toward the invalid-scan budget, but an operator who exhausts it has QR verification paused for the window; the permission check always runs first. |
| Gate monitor | Adds anomaly signals at the gate (kind, count, time); denied attempts now show localized labels instead of raw codes. Still no participant data. |
| Validation re-render | Checkpoint forms re-display field errors without echoing the submitted token, number, reference or search text. |
| Participant pass page | Now also shows the participant's own display name and the context's Registration Reference (UI/UX §7.3) to the authenticated owner only; the QR payload is unchanged and carries no personal data. |


## 8. Phase 4 Prompt 2 additions: offline preparation (ADR-0023)

### 8.1 Permissions and groups

| Change | Effect |
| --- | --- |
| New permission `entry.enable_offline_entry` | Enable or disable offline continuity for one event. Granted to Entry Device Administrators, and only event-scoped. |
| New permission `entry.emergency_wipe_device` | Order a destructive emergency wipe of a device's local store. It also requires an MFA step-up, an explicit confirmation and a reason code. Granted to Security Restriction Managers only; Entry Device Administrators do not hold it. |
| Security Restriction Managers gain `entry.view_entrydevice` | They can find the device to wipe. They still hold no device-management, verification, participant-browsing, document or export permission. |
| Preparation, self-test and "Block offline use" | Use the existing `entry.manage_entrydevice`. |
| Operator offline grant | Requires the operator's own live checkpoint session on an `OFFLINE_READY` device. It is issued online only and revoked when the operator session ends. |

### 8.2 Data classification

| Data | Class | Protection |
| --- | --- | --- |
| Offline Package plaintext | Personal (display name) plus internal access data | Never stored server-side. Encrypted in memory to the device's non-extractable ECDH key; the data key is discarded. The allow-list excludes every identity value, digest, fingerprint, lookup key, reference, hint, contact detail, document, note, restriction reason or category, role and photo. |
| Package ciphertext (private storage) | Derived; unreadable by the server | Kept for the configurable retention, then purged. Metadata rows are kept. |
| `EntryDeviceKey` | Public key material | SPKI only. Trigger-guarded; never deleted. |
| `DeviceWipeOrder.note_encrypted` | Restricted | AES-GCM field encryption. The audit records only that a note is present. |
| Device-reported state and `DeviceEvidenceReport` | Operational metadata | Counts, sequence numbers and a chain-head hash only; never the content of an operation. |
| Request nonces | Technical | A digest only, pruned after expiry. |
| `OfflinePackageBuild` | Operational metadata | Version, status, attempts, lease and a pending storage key. No package content. Never deleted. |
| `OfflineChangeJournal` | Derived technical data | Written only by database triggers. Holds a table name, an operation letter, and internal row and relation ids: never a name, identifier value, reason or other content. Pruned after `ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS`, except rows a live package still needs. |

### 8.3 Audit codes

These codes record versions, hashes, counts and reason codes only, never content:

- `ENT_OFFLINE_EVENT_ENABLED`, `ENT_OFFLINE_EVENT_DISABLED`
- `ENT_DEVICE_OFFLINE_PROVISIONED`
- `ENT_DEVICE_OFFLINE_SELF_TEST_PASSED`, `ENT_DEVICE_OFFLINE_SELF_TEST_FAILED`
- `ENT_DEVICE_OFFLINE_READY`, `ENT_DEVICE_OFFLINE_READINESS_WITHDRAWN`
- `ENT_DEVICE_OFFLINE_BLOCKED`
- `ENT_OFFLINE_PACKAGE_BUILD_REQUESTED`, `ENT_OFFLINE_PACKAGE_BUILD_ENDED` (cancelled or failed; reason code only)
- `ENT_OFFLINE_PACKAGE_BUILT`, `ENT_OFFLINE_PACKAGE_DOWNLOADED`, `ENT_OFFLINE_PACKAGE_DOWNLOAD_DENIED`
- `ENT_OFFLINE_PACKAGE_REVOKED`, `ENT_OFFLINE_PACKAGE_CIPHERTEXT_PURGED`
- `ENT_OFFLINE_DELTA_ISSUED`
- `ENT_OFFLINE_GRANT_ISSUED`, `ENT_OFFLINE_GRANT_REVOKED`
- `ENT_OFFLINE_REQUEST_REJECTED`
- `ENT_DEVICE_EMERGENCY_WIPE_ORDERED`, `ENT_DEVICE_EMERGENCY_WIPE_REFUSED`
- `ENT_DEVICE_EVIDENCE_REPORTED`

### 8.4 Known limits (not claimed)

- There is still no real MFA provider. The emergency wipe fails closed where `MFA_BACKEND` is unset.
- There is no CSP yet (Phase 4 Prompt 4).
- For browser-storage limitations, see ADR-0023 "Browser-security limitations".
- `events.Gate.offline_policy` is deprecated and non-authoritative (ADR-0023 §2, "Gate offline policy"). It authorizes nothing; only the global flag, the per-event setting and the current versioned device scope do. Removing it is a separately authorized future migration.
