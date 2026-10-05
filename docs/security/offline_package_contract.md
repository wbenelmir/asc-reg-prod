# Offline Package contract (normative)

Phase: 04 — Prompt 2.
Status: implemented, READY_FOR_LOCAL_REVIEW.
Related: [ADR-0023](../architecture/ADR-0023-pwa-offline-package-preparation.md),
[ADR-0019](../architecture/ADR-0019-es256-compact-jws-and-signing-key-boundary.md),
[QR contract](qr_contract.md),
[device API](../api/entry_device_api_v1.openapi.json).

This document is the wire contract between the server and an enrolled device. The
single source of truth for field sets is `apps/entry/offline_contract.py`; the
tables below restate it.

## 1. Encodings

- **base64url.** Unpadded (RFC 4648 §5). Decoders reject padding, characters
  outside the alphabet, and non-canonical encodings.
- **Canonical JSON.** Sorted keys, no insignificant whitespace, UTF-8. Numbers
  are integers only; a floating-point value is refused.
- **Timestamps.** Integer epoch seconds (UTC).

## 2. Signatures

- **Format.** ES256 compact JWS. The header is exactly
  `{"alg":"ES256","kid":"<kid>","typ":"<typ>"}` in canonical form. The signature
  is raw `r || s` (64 bytes).
- **Signing key.** The Offline Package key family: `kid` matches `^p[1-9][0-9]{0,3}$`
  (never a QR `v<n>` key). The device selects the verification key from its
  **pinned** trust anchors by `kid`. A key found inside a payload is never a
  trust root.
- **Types.** Each signed object type has its own `typ`, so one can never pass as
  another:

  | `typ` | Object |
  | --- | --- |
  | `ASC-OPKG` | package manifest |
  | `ASC-ODELTA` | critical delta manifest |
  | `ASC-OGRANT` | operator offline grant |
  | `ASC-OWIPE` | emergency wipe order |

## 3. Encryption envelope

`POST /entry/api/v1/offline/package/` answers with one of three responses. The request
itself never builds a package:

| Status | Meaning |
| --- | --- |
| `200` | The stored bytes of the newest READY version. A retry returns byte-identical bytes. |
| `204` | `have_version` already equals that version. |
| `202` | `{"status": "BUILDING", "package_version": <reserved or null>, "retry_after_seconds": n}` with a `Retry-After` header. The device's one open build is pending or running, and the device asks again later, at most `build_poll_limit` times in a row. |

A `200` response (and every `…/delta/` response) is:

```json
{"manifest": "<compact JWS>", "ciphertext": "<base64url>"}
```

For a package, `kind = OPKG` and `object_id = package_id`. For a delta,
`kind = ODELTA` and `object_id = package_id + ".d" + delta_version`. With those:

- **Body encryption.** AES-256-GCM under a random 256-bit data key, with a random
  96-bit IV. The AAD is `ASC-<kind>-BODY-v1|<device public id>|<object_id>`.
- **Key wrap** (ECIES-style; WebCrypto `deriveBits` + `HKDF` + `AES-GCM`):
  1. The server generates an ephemeral P-256 key pair and computes ECDH with the
     device's registered UNWRAP public key. The shared secret is the 32-byte
     x-coordinate.
  2. HKDF-SHA256 derives the wrapping key from that shared secret. The salt is a
     random 32 bytes and the info is `ASC-<kind>-WRAP-v1|<device>|<object_id>`.
  3. AES-256-GCM encrypts the data key under the wrapping key. The IV is a random
     96 bits and the AAD is `ASC-<kind>-DEK-v1|<device>|<object_id>`.
- **Discarded material.** The server keeps neither the data key nor the ephemeral
  private key.

## 4. Package manifest (`ASC-OPKG`)

Exact keys:

| Key(s) | Content |
| --- | --- |
| `typ`, `schema_version` (1), `package_id`, `package_version` | Identification; the version increases monotonically per device |
| `device`, `event`, `scope_version`, `sensitivity` | Binding |
| `issued_at`, `data_cutoff_at`, `aging_at`, `stale_at`, `expires_at` | Validity bands |
| `entry_count`, `ciphertext_sha256` (hex), `ciphertext_length` | Integrity |
| `enc` | `{alg: "A256GCM", iv, aad}` |
| `wrap` | `{alg: "ECDH-ES-P256+HKDF-SHA256+A256GCM", epk (SPKI base64url), salt, iv, wrapped_key, recipient}`, where `recipient` is the SHA-256 of the device's unwrap SPKI DER |

The device accepts a package only if **all** of the following hold:

- the signature verifies under a pinned key;
- the key set is exact and `typ` is correct;
- `schema_version` is supported;
- `device` is the device itself;
- `wrap.recipient` is its own unwrap key;
- `scope_version` equals the server-announced current one;
- `expires_at` is in the future;
- the ciphertext length and hash match;
- decryption succeeds;
- the body matches the allow-list and repeats the manifest's identity and band fields;
- `package_version` is strictly newer than the active one (equal means "up to date").

## 5. Package body

Top-level keys: `schema_version`, `package_id`, `package_version`, `device`,
`event`, `gate`, `zones`, `scope_version`, `sensitivity`, `issued_at`,
`data_cutoff_at`, `aging_at`, `stale_at`, `expires_at`, `entries`,
`revoked_passes`, `qr_keys`, `override_reasons`.

| Structure | Exact keys |
| --- | --- |
| entry | `jti`, `pid`, `bai`, `cv`, `apc`, `btc`, `valid_from`, `valid_until`, `display_name`, `badge_label{en,fr,ar}`, `assignment_until`, `profile_window{from,until}`, `reentry`, `zone_rules[{zone, rules[{effect, from, until}]}]`, `restrictions[{severity, overrideable, from, until}]`, `last_admitted_at` |
| revoked pass | `jti`, `status` |
| QR key | `kid`, `spki`, `status`, `not_before`, `not_after` (used to verify QR passes, never packages) |
| override reason | `code`, `names{en,fr,ar}`, `overridable_reason_codes`, `requires_note` |

**`last_admitted_at`** (Phase 4 Prompt 3 correction 5, decision D3 option S1).
It is the latest admission of the context known at the data cutoff. The
source is whichever is later of:

* the context's latest admission Entry Event;
* a synchronized offline admission that the server kept only in its
  `SyncOperation`, with no Entry Event (`apps.entry.selectors.admissions`).

The key set and the offline evaluator are unchanged. Critical deltas never
restate this value.

**Which contexts are packaged.** Only contexts that are approved, current, not
withdrawn or cancelled, and have an ACTIVE, unexpired pass whose snapshot still
matches the current Badge Type and Access Profile assignments. The Access Profile
must be active, and at least one ALLOW rule must be reachable at the device gate
and zones. Any other QR is a **local miss**, which routes to Manual Review.

**Forbidden in every form:** NIN or passport values, their digests, fingerprints
or lookup keys; registration reference; masked identity hints; email; phone;
documents; notes; restriction reason or category; Participant Role; photos.
**QR (`jti`) is the only offline lookup key.**

## 6. Critical delta (`ASC-ODELTA`)

**Manifest keys:** `typ`, `schema_version`, `package_id`, `package_version`,
`delta_version`, `device`, `scope_version`, `issued_at`,
`critical_delta_cutoff_at`, `ciphertext_sha256`, `ciphertext_length`, `enc`,
`wrap`.

**Body keys:** `schema_version`, `package_id`, `package_version`,
`delta_version`, `device`, `scope_version`, `issued_at`,
`critical_delta_cutoff_at`, `passes[{jti, status ∈ REVOKED|REPLACED|SUSPENDED|EXPIRED|INACTIVE}]`,
`restrictions[{jti, restrictions[…]}]`,
`access_withdrawn[{jti, reason ∈ REGISTRATION_NOT_APPROVED|ASSIGNMENT_CHANGED|ACCESS_RULE_CHANGED|PASS_CHANGED}]`,
`withdrawn_access_profiles[apc]`, `revoked_kids[kid]`, `rebuild_required`,
`rebuild_reasons ⊆ {SCOPE_CHANGED, CHECKPOINT_CHANGED, OVERRIDE_CATALOGUE_CHANGED, KEY_SET_CHANGED, UNTRACKED_CHANGE, BULK_CHANGE, SNAPSHOT_MISSING}`.

**Blocking rebuild reasons** (`REBUILD_BLOCKING_REASONS`, sent to the device as
`rebuild_blocking_reasons`): `SCOPE_CHANGED`, `UNTRACKED_CHANGE`, `BULK_CHANGE` and
`SNAPSHOT_MISSING`. After any of them the device is **Blocked** offline, not merely
Stale, until it activates a new package. The server also withholds that package from
download (`202 BUILDING`).

**Source of truth.** The delta is derived from the database change journal written by
triggers (`OfflineChangeJournal`, ADR-0023 §5), never from `updated_at`. It contains every
journaled change that the package's recorded PostgreSQL snapshot does not cover.
Anything the journal cannot translate precisely fails closed with one of the blocking
reasons.

**Issuance.** The server re-reads the device, then the package, under row locks. It never
issues a delta for a package that is not READY, has expired, belongs to a blocked device,
or was re-scoped or re-provisioned. Such a request is refused with
`PACKAGE_NOT_CURRENT` or the availability code.

**Acceptance.** The device verifies the signature under a pinned key and checks
schema, device, package id and version, and scope. It also checks
`delta_version > last accepted` and `critical_delta_cutoff_at ≥ previous cutoff`.
A delta **only** advances `critical_delta_cutoff_at`. It never changes
`package_data_cutoff_at` or the band.

## 7. Operator grant (`ASC-OGRANT`) and wipe order (`ASC-OWIPE`)

- **Grant keys:** `typ`, `schema_version`, `grant_id`, `device`, `display_name`,
  `permissions` (⊆ `VERIFY_OFFLINE_QR`, `OVERRIDE_OFFLINE`), `sensitivity`,
  `issued_at`, `expires_at`.
- **Grant expiry:** the earlier of the operator session's absolute expiry and
  the contact time plus 2 h (Standard) or 1 h (Sensitive).
- **Wipe-order keys:** `typ`, `schema_version`, `order_id`, `device`,
  `reason_code`, `issued_at`.

## 8. Request proofs

Signed calls carry two headers:

- `X-ASC-Device-Nonce`: a nonce from the last `X-ASC-Next-Nonce` header or
  heartbeat. It is single-use, device-bound, and valid for
  `ENTRY_OFFLINE_NONCE_SECONDS`.
- `X-ASC-Device-Signature`: base64url raw ES256, made with the device's
  registered SIGN key, over

```
ASC-DEVICE-PROOF-v1\n<purpose>\n<nonce>\n<hex SHA-256(raw request body)>
```

The purposes are: `provision` (signed with the NEW key), `package`, `delta`,
`self-test`, `grant`, `heartbeat` (optional; needed only for a stored state
report), and `wipe-report`.

## 9. Bands and states

Bands follow `settings.ENTRY_OFFLINE_VALIDITY` (approved values; see ADR-0023
§5). A package is EXPIRED at or after `expires_at`, STALE at or after
`stale_at`, AGING at or after `aging_at`, and FRESH otherwise.

The seven operational states and their permitted actions are fixed by
`PERMITTED_ACTIONS`. The device receives them as data in the shell configuration.

## 10. Heartbeat directives

| Directive | Device obligation |
| --- | --- |
| `BLOCK{reason}` | State Blocked |
| `PURGE_PACKAGE{reason}` | Delete package slots and in-memory package data only |
| `LOCK_OPERATIONS{reason}` | `PENDING` → `LOCKED`; keep every record; refuse new records |
| `REVOKE_GRANTS{grants}` | Drop those grants |
| `EMERGENCY_WIPE{order}` | Verify the order under the pinned keys and report counts, sequence range and chain head. Then ask the other tabs of the application to close, and delete the local database. The wipe is complete **only** when `deleteDatabase()` fires `onsuccess`: `onblocked` is a Blocked "waiting" state and `onerror` is retried, and neither is ever reported as wiped. Only after that success, clear this application's caches and unregister **only** its own `/entry/` service-worker registration. |

An unauthenticated answer (401 `DEVICE_NOT_AUTHENTICATED`) is handled like
`BLOCK` + `PURGE_PACKAGE` + `LOCK_OPERATIONS`.

## 11. Not in this contract (Prompt 3 or later)

- offline admission decisions;
- operation schema and upload;
- acknowledgement (the `ACKNOWLEDGED` record state);
- quarantine ingestion;
- conflict classification and reconciliation;
- Event Edge.
