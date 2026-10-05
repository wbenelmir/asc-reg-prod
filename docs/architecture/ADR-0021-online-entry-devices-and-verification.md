# ADR-0021: Online entry — venue structure, devices, sessions, and admission decisions

| Field | Value |
| --- | --- |
| Status | Proposed (Phase 3 Prompt 4 implementation; awaiting local review) |
| Date | 2026-09-22 |
| Related requirements | PRD UJ-08, FR-ENT-001…018, BR-ENT-001…005; TRD §12, §16, ENTRY-001…005, AUTHZ-001…004; Flow §10, §14.6, §15.3, OD-AF-12/13; UI/UX §9.4–9.7, §12.2, §19.3; Schema §5.2, §9.1, §11.1, §11.4–11.6, §12.3; ADR-0010 |

## Context

Phase 3 Prompt 4 authorizes the online (Plan A) entry and security
workflow only: enrolled devices, checkpoint and operator sessions, online
verification in the approved lookup order, explicit results, reason-coded
overrides bounded by a configurable catalogue, append-only idempotent Entry
Events, re-entry advisories, participant-detail clearing, a minimum-data
external-security view, and automatic temporary-account expiry. Offline
behaviour, Offline Packages, synchronization, PWA behaviour, and Event Edge
remain unauthorized.

ADR-0010 deferred `Venue`, `Zone`, `Gate`, and the `venue`/`gate` columns
of `ScopedGroupMembership` "until the venue/gate phase begins". This prompt
needs all of them, so this ADR records their introduction as the additive
follow-up ADR-0010 anticipated. ADR-0010 itself is not edited.

## Decisions

### 1. Application boundaries

* `events` gains `Venue`, `Zone` (optional parent), and `Gate` (Schema
  §5.2; TRD §13.1 assigns venues, zones, and gates to `events`).
* A new `entry` app (TRD §13.1 / Schema §2.2) owns devices, device scopes,
  device and operator sessions, security restrictions, the override
  catalogue, overrides, and Entry Events.
* `accreditation` keeps `AccessProfile`/`AccessRule` and gains only the
  configuration an entry decision reads: the Access Profile validity window
  and re-entry policy, and the Access Rule zone/gate/event-type/window/effect
  dimension (Schema §9.1). No assignment table changes.
* `accounts.ScopedGroupMembership` gains nullable `venue` and `gate` FKs.

### 2. Device credential: digest-only, two-factor enrollment

A device is registered by an authorized administrator (PENDING_ENROLLMENT,
scope version 1) and a one-time activation code is shown once. On the
physical device an administrator — **signed in there** — enters the code.
The server mints a 256-bit secret, stores only its SHA-256 digest, rotates
the public `device_key_id`, clears the code, and returns the secret to that
browser in an `HttpOnly`, `SameSite=Strict`, `Path=/entry/` cookie
(`Secure` follows `SESSION_COOKIE_SECURE`). A database read therefore never
yields a usable device credential, a leaked code alone never enrolls
anything, and re-enrollment invalidates the previous browser.

Rejected: WebCrypto non-extractable key pairs with request signing. They
add real value only for offline verification and sync (Plan B), which this
prompt does not authorize; they can be layered on later without changing
the device table's public identity.

Every lifecycle change (suspend, revoke, rescope, re-enroll, expire) ends
the device's open sessions in the same transaction. `authenticate_device`
treats an elapsed `expires_at` as expired immediately and persists it, so
correctness never depends on the expiry sweep having run.

### 3. Two short-lived session levels

* `EntryDeviceSession` — one checkpoint set-up (gate + zone) on a device,
  bounded by `ENTRY_DEVICE_SESSION_SECONDS` and the device expiry. The gate
  must equal the device scope's gate and the zone must be one of its
  permitted zones: this is the cross-checkpoint denial.
* `EntryOperatorSession` — one named operator on that set-up, with its own
  absolute (`ENTRY_OPERATOR_SESSION_SECONDS`) and inactivity
  (`ENTRY_OPERATOR_INACTIVITY_SECONDS`) lifetimes. A supervisor joins with
  their own operator session; shared accounts stay impossible.

`resolve_checkpoint` re-derives the whole chain on every checkpoint
request: device status and expiry, device session, the scope version it was
opened under, operator session lifetimes, and the user's checkpoint-scoped
`entry.verify_entry` permission.

### 4. Gate-scoped memberships fail closed everywhere else

`effective_scoped_memberships` treats venue/gate asymmetrically: a
membership narrowed to a venue or gate matches **only** a check that names
that venue/gate. Every pre-existing, non-checkpoint check names none, so a
gate-scoped membership can never grant event-wide capability through a
back-office screen — even if an administrator mistakenly put a back-office
permission in a gate-scoped group (regression-tested).

### 5. The evaluator is pure and collects every blocker

`apps.entry.services.access.assess_context` never writes and never decides.
It evaluates the exact selected Registration Context (never merging other
contexts, BR-ENT-001) in a fixed order: event edition, registration state
(both stop evaluation), DENY restrictions (person-level covers every
context, BR-ENT-003), pass status and window, current assignments and pass
snapshot staleness, Access Profile window, zone/gate rules (with zone
ancestry), prior admission and re-entry policy, review-level restrictions,
and unverified identity references.

All blockers are collected; the primary result is the most severe
(DENIED > STALE > MANUAL_REVIEW). An override must cover **every** blocker,
so an override approved for an expired pass can never quietly admit someone
who is also at the wrong zone.

Fail-closed rule for zones: with no matching ALLOW rule, the result is
`WRONG_ZONE`. There is no "entry by default".

### 6. Result vocabulary

`ALLOWED`, `ALLOWED_WITH_ADVISORY`, `MANUAL_REVIEW`, `DENIED`, `STALE`,
`UNSUPPORTED`, `TECHNICAL_ERROR`, each with a stable reason code.
Untrusted QR outcomes (malformed, unknown key, bad signature, mismatched
claims) collapse into one `INVALID_CREDENTIAL` so a presenter cannot
distinguish them. An exception anywhere in verification becomes
`TECHNICAL_ERROR`, never a false result. `UNSUPPORTED` and
`TECHNICAL_ERROR` never resolve a context and so never become an Entry
Event (database CHECK).

### 7. Decisions are server-held, re-evaluated, idempotent

A verification produces a `PendingVerification` kept in the server-side
session; the browser receives only its random `operation_id`. The decision
transaction takes an advisory lock on the operation id, replays or rejects
a reused id by command fingerprint (the Phase 3 `command_fingerprint`
helper, unchanged), locks the Registration row, re-assesses, and refuses
the decision as STALE if the assessment signature changed (pass revoked,
restriction added, another gate admitted the person a moment earlier). A
verification older than `ENTRY_DECISION_TICKET_SECONDS` is STALE. Real
multi-connection PostgreSQL tests prove the serialization.

Entry Events and overrides are append-only by a `BEFORE UPDATE OR DELETE`
trigger. A CHECK constraint makes `ADMIT` on a non-admittable result
impossible without a linked override, and pins `offline = FALSE`.

### 8. Override catalogue is a configuration boundary, seeded empty

`EntryOverrideReason` rows (per event) list which reason codes they may
bypass. Nothing is seeded: until the final catalogue (OD-AF-13) is
approved, no override is possible. Regardless of configuration, the
service refuses `INVALID_CREDENTIAL`, `NO_MATCH`, `UNSUPPORTED_CREDENTIAL`,
`WRONG_EVENT`, `REGISTRATION_NOT_APPROVED`, `PASS_REVOKED`,
`VERIFICATION_STALE`, `TECHNICAL_ERROR`, and any assessment touched by a
non-overrideable restriction (Flow §10.9).

### 9. Re-entry is advisory by default

Prior admission yields `ALLOWED_WITH_ADVISORY` (`PRIOR_ENTRY`, plus
`RECENT_REENTRY` inside `ENTRY_RECENT_REENTRY_SECONDS`). Denial happens
only when an Access Profile is explicitly configured `SINGLE_ENTRY`
(OD-AF-12). No anti-passback rule is invented.

### 10. Minimum-data projection

`apps.entry.selectors.result_projection` is the only path from participant
data to a checkpoint template: display name, registration reference,
optional photo (streamed through a session-bound nonce), Badge Type, and
the masked identity hint for identity lookups. External security accounts
never see the Badge Type or identity hint, and see no restriction wording
without `entry.view_restriction_reason` (FR-ENT-018). Wrong-event,
not-approved, and unresolved results project no participant field at all.

## Consequences

* Offline continuity remains a clean later addition: `OFFLINE_READY`,
  `OfflinePackage`, `SyncOperation`, and `ReconciliationCase` are absent,
  and two CHECK constraints (`entry_scope_online_only`,
  `entry_event_online_only`) must be relaxed by an explicit migration when
  Plan B is authorized.
* Until an override catalogue and access rules are configured, checkpoints
  deny by default. Operational set-up must configure zones, gates, rules,
  and (optionally) overrides before rehearsal.
* The four new migrations (`events.0004`, `accounts.0004`,
  `accreditation.0005`, `entry.0001`) are additive and reversible.
