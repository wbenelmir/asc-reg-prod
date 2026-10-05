# ADR-0006: Field encryption and versioned HMAC blind indexes

| Field | Value |
| --- | --- |
| Status | Accepted; implemented in Prompt 3 |
| Date | 2026-09-19 |
| Related requirements | Schema §4.5/§4.6/§17.2/§17.3; TRD `SEC-009`; accepted plan §5.1, §6.1 |

## Context

`ContactPoint.value_encrypted`/`value_hash` and `IdentityIdentifier
.value_encrypted`/`search_hash` (Schema §4.5/§4.6) require application-level
encryption plus a **versioned** HMAC blind index for exact matching, so
NIN/passport/email/phone values are never stored, logged, or indexed in
clear text, while still supporting exact-match lookup and key rotation.

## Decision

Implemented in Prompt 3 (no project model exists yet in Prompt 2), built
directly on the `cryptography` package (verified Python 3.14/Windows
compatible in Prompt 2 -- see the completion report):

- `apps/core/crypto.KeyProvider` -- an interface with an environment-backed
  implementation for local/test. Production key management is a later
  decision (accepted plan risk R6); no key ever enters application tables
  or source control (Schema §17.3).
- `apps/core/fields.EncryptedTextField` -- application-level authenticated
  encryption (AES-GCM) of the raw value, storing ciphertext plus a
  key-version tag so rotation is possible without a single flag day.
- `apps/core/fields.BlindIndexField` -- a **versioned** HMAC-SHA-256 over a
  normalized form of the plaintext, keyed by a per-purpose HMAC key with
  its own version. Two independent versioned-key families exist by design:
  identity blind indexes (`ContactPoint`, `IdentityIdentifier`) and the
  **separate** rate-limit HMAC key family (ADR-0007) -- rotating one never
  disturbs the other.
- Indexes on encrypted plaintext fields are prohibited (Schema §16.2); only
  the blind-index column is ever indexed for exact matching.

## Consequences

- Verified-active NIN/passport uniqueness and active login-enabled email
  matching are enforced via partial unique indexes on the blind-index
  column, never on the encrypted value.
- Key rotation is a first-class operation: old and new key versions can be
  active simultaneously, exactly as required for the rate-limit HMAC key
  family in ADR-0007.
- No third-party encrypted-model-field package is used -- key versioning
  and blind-index derivation stay project-owned and auditable.
