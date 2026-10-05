# Digital Entry Pass QR contract

Phase: 03 — Prompt 2
Status: implemented and locally verified
Related: [ADR-0017](../architecture/ADR-0017-badges-app-boundary.md),
[ADR-0018](../architecture/ADR-0018-credential-version-vs-lock-version.md),
[ADR-0019](../architecture/ADR-0019-es256-compact-jws-and-signing-key-boundary.md)

This document is the normative wire contract for the ASC 2026 Digital Entry
Pass. An independent verifier — including the Phase 4 offline verifier —
can be written from this document plus the committed public test vectors,
without reading this codebase.

---

## 1. Envelope

Compact JWS (RFC 7515 §7.1), three base64url segments joined by `.`:

```
BASE64URL(UTF8(protected_header)) "." BASE64URL(payload) "." BASE64URL(signature)
```

Base64url is **unpadded** (RFC 7515 §2). There is no JSON serialization
form and no unprotected header: compact serialization has none by
construction, and a verifier must not accept one.

---

## 2. Canonical protected header

Exactly three members, sorted, no whitespace, UTF-8:

```json
{"alg":"ES256","kid":"<key-id>","typ":"ASC-PASS"}
```

| Member | Rule |
| --- | --- |
| `alg` | Exactly `ES256`. No other value is accepted, ever. |
| `kid` | Matches `^v[1-9][0-9]{0,3}$`, at most 8 characters. |
| `typ` | Exactly `ASC-PASS`. Deliberately not `JWT`: this is not a JWT and must not be consumed as one. |

**Rejected without exception:** any additional member (including `crit`,
`jwk`, `jku`, `x5u`, `x5c`, `x5t`, `cty`, `b64`), any missing member,
`alg: none`, any HMAC algorithm, any algorithm not equal to `ES256`,
algorithm guessing or negotiation, an algorithm taken from the presented
header rather than from the stored key record, an unknown or malformed
`kid`, and duplicate JSON members.

---

## 3. Canonical payload

Exactly eleven claims, sorted, no insignificant whitespace, integer
timestamps, UTF-8:

| Claim | Type | Rules |
| --- | --- | --- |
| `apc` | string | Access Profile code. `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, ≤ 64 characters. |
| `bai` | string | Badge-assignment public reference. Base64url, exactly 22 characters. |
| `btc` | string | Badge Type code. `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, ≤ 64 characters. |
| `cv` | integer | Credential version. ≥ 1. |
| `eid` | string | Event-edition public code. `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, ≤ 32 characters. |
| `exp` | integer | Expiry, epoch seconds. ≥ 1, and strictly greater than `nbf`. |
| `jti` | string | Per-version credential identifier. Base64url, exactly 22 characters. |
| `n` | string | Nonce. Base64url, exactly 16 characters (96 bits). |
| `nbf` | integer | Not-before, epoch seconds. ≥ 1. |
| `pid` | string | Event-scoped participant pseudonym. Base64url, exactly 22 characters. |
| `v` | integer | Payload schema version. ≥ 1. Currently `1`. |

Integer claims are bounded above by 2^62. A JSON boolean is **not** an
acceptable integer, even though Python's `bool` subclasses `int`.

There is **no extension mechanism**. A verifier rejects any payload with an
additional claim, a missing claim, a wrongly typed value, a nested value,
an out-of-range length, or a character outside the declared alphabet.

### 3.1 Excluded values (normative)

The payload MUST NOT contain, in any form: given name, family name, full
name, email address, telephone number, date of birth, nationality, national
identity number, passport number or issuing country, identity-document
reference or link, registration reference, organization name or identifier,
participant role, internal note, authorization role or permission, or **any
database primary key or UUIDv7 value**.

`bai`, `jti`, and `pid` are random opaque identifiers generated with a
CSPRNG. None is derived from a primary key, a UUIDv7, a registration
reference, or any identity value.

---

## 4. Signing input and signature

```
signing_input = BASE64URL(protected_header_bytes) "." BASE64URL(payload_bytes)
```

- `payload_hash` stored alongside the credential is `SHA-256(signing_input)`,
  hex-encoded, as regeneration evidence.
- The signature itself is **persisted** at issuance (`signature_hex`, 64 raw
  bytes hex-encoded). A credential is signed exactly once, ever.
- The signature is ES256 over `signing_input`, encoded as the fixed-length
  raw concatenation `r || s`, **64 bytes** (RFC 7518 §3.4).
- A DER-encoded signature is not the JWS wire format and is rejected at the
  signing-provider boundary before a token is ever emitted.

---

## 5. Verification order and result precedence

Lower tier wins. A verifier evaluates in this order and returns the first
matching result.

| Tier | Result codes |
| --- | --- |
| 1 | `MALFORMED`, `UNSUPPORTED_VERSION`, `UNKNOWN_KEY`, `INVALID_KEY`, `INVALID_SIGNATURE` |
| 2 | `CREDENTIAL_NOT_FOUND`, `CREDENTIAL_MISMATCH`, `WRONG_EVENT` |
| 3 | `REVOKED` |
| 4 | `REPLACED` |
| 5 | `SUSPENDED` |
| 6 | `INACTIVE`, `NOT_YET_VALID` |
| 7 | `EXPIRED` |
| 8 | `VALID` |

Concrete steps:

1. Reject input larger than the configured byte bound **before** decoding.
2. Require exactly three `.`-separated segments.
3. Decode each segment as strict unpadded base64url: reject padding,
   characters outside the alphabet, impossible lengths, and non-canonical
   encodings whose unused trailing bits are non-zero.
4. Require a 64-byte signature.
5. Parse the protected header with duplicate-member rejection and validate
   it against the three-member allowlist.
6. Resolve `kid` to a stored `VerificationKey` **before** verifying
   anything. Unknown → `UNKNOWN_KEY`. Not usable (PENDING, REVOKED, or
   outside its own `not_before`/`not_after` window) → `INVALID_KEY`.
7. Verify the signature over the received signing input, using the
   algorithm recorded on the stored key.
8. Parse and validate the payload against the eleven-claim allowlist.
9. Reject a payload version outside the supported set.
10. Resolve `jti` to exactly one credential.
11. Compare `eid` with the credential's event code; compare `cv` with
    `credential_version`; compare `btc`, `apc`, `n`, and the key id with the
    stored snapshot; compare `nbf`/`exp` with the stored validity window.
12. Apply credential status, then the validity window.

**Terminal beats expired.** `REVOKED` and `REPLACED` are evaluated before
the validity window, so a revoked credential presented after its expiry
still reports `REVOKED` rather than collapsing into `EXPIRED`.

**Expiry never depends on a worker.** Expiry is derived from `valid_until`
on every call. If a persisted status still reads `ACTIVE` because no sweep
has run, verification still returns `EXPIRED`. The optional sweep exists
for reporting and queue hygiene only; removing it changes no decision.

### 5.1 Internal result codes versus presenter-facing messages

The codes in the table above are **internal diagnostic values**. They are
returned to operational code and recorded in audit evidence; they are
deliberately *not* what a person presenting a credential is shown.

| Internal code | Safe message shown to the presenter |
| --- | --- |
| `MALFORMED`, `UNSUPPORTED_VERSION`, `UNKNOWN_KEY`, `INVALID_KEY`, `INVALID_SIGNATURE`, `CREDENTIAL_NOT_FOUND`, `CREDENTIAL_MISMATCH` | "This pass could not be verified." |
| `WRONG_EVENT` | "This pass is not for this event." |
| `REVOKED`, `REPLACED` | "This pass is no longer valid. Refer to the accreditation desk." |
| `SUSPENDED` | "This pass is on hold. Refer to the accreditation desk." |
| `INACTIVE`, `NOT_YET_VALID` | "This pass is not active yet." |
| `EXPIRED` | "This pass has expired." |
| `VALID` | "Verified." |

The collapsing is deliberate. Seven distinct internal failures map to one
presenter message so that probing cannot distinguish "unknown key" from "bad
signature" from "no such credential". `REVOKED` and `REPLACED` also share a
presenter message: a presenter learns their pass is dead, not *why*, while
the audit trail keeps the two apart for the operator.

**Failure disclosure.** No parser detail, no exception text, and no internal
code reaches the presenter. The raw QR value is never logged, never echoed,
never placed in a URL or query string, and never written to an audit
summary.

---

## 6. Key states and rotation

| State | May sign | May verify |
| --- | --- | --- |
| `PENDING` | No | No |
| `ACTIVE` | Yes (exactly one at a time) | Yes |
| `RETIRED` | No | Yes, while within its own window |
| `REVOKED` | No | **No — immediately, for every credential bearing this `kid`** |

- **Exactly one current signing key** is enforced by a partial unique
  constraint over `ACTIVE` rows.
- **Rotation overlap:** promoting a new key retires the previous one.
  Credentials issued before the rotation keep verifying against the retired
  key for as long as they remain within their own validity window.
  Existing credentials are **not** re-signed on rotation.
- **Emergency revocation** takes effect immediately for online
  verification. Phase 3 makes **no claim** that offline devices receive
  emergency key updates: offline packages, offline verification, and
  offline revocation propagation are Phase 4.
- `badges.VerificationKey` stores **public** key material only. Private
  signing keys live exclusively behind
  `apps.core.crypto.signing.SigningKeyProvider`, which returns signatures
  and public keys but never private bytes.

### 6.1 Canonical key parsing and fingerprinting

Every published key is parsed with `cryptography` before it is stored, and
only an **EC public key on NIST P-256 (secp256r1)** is accepted. A private
key, an RSA key, an EC key on another curve, and malformed PEM are all
refused with one safe message — an operator learns the key was refused, not
which parser branch refused it.

Keys are canonicalized to **DER SubjectPublicKeyInfo** and the fingerprint is
`SHA-256` over those DER bytes, never over the PEM text. Two PEM strings that
differ only in line wrapping, CRLF endings, or trailing whitespace describe
the same key; comparing or fingerprinting the text would make a formatting
difference read as a key mismatch, or let two encodings of one key be
published as if they were distinct.

### 6.2 Provider alignment

Two gates, both fail-closed:

* **At promotion** — the signing provider's current `kid` must equal the
  pending key's `kid`, and its public half must canonicalize to the same DER.
  Promoting a key the provider cannot sign with would mint credentials that
  verify nowhere.
* **Before every issuance** — the provider's current `kid` and public key
  must match the ACTIVE `VerificationKey`, and that key must be inside its
  `not_before`/`not_after` window.

A provider error, a missing public key, a mismatch, or an invalid window all
refuse issuance rather than producing an unverifiable credential.

### 6.3 Retirement versus emergency revocation

| | Retirement | Emergency revocation |
| --- | --- | --- |
| Effect on existing credentials | Keep verifying while within their own validity window | Fail immediately, regardless of their own `exp` |
| Typical reason | `PLANNED_ROTATION`, `ROTATION_SUPERSEDED` | `SUSPECTED_COMPROMISE`, `CONFIRMED_COMPROMISE` |
| Reversible | No | No |

Both are POST-only, CSRF-protected, permission-checked
(`badges.manage_verificationkey`), idempotent on their `operation_id`, and
audited with a controlled reason code and no key material. Promotion retires
the outgoing key automatically, which is the normal rotation path; explicit
retirement exists for taking a key out of service without a successor.

---

## 7. Credential lifecycle

Persisted statuses: `INACTIVE`, `ACTIVE`, `SUSPENDED`, `REVOKED`,
`EXPIRED`, `REPLACED`. **"Not generated" is the absence of a row**, never a
stored status.

| From | Permitted targets |
| --- | --- |
| *(no row)* | `INACTIVE` (generation) |
| `INACTIVE` | `ACTIVE`, `REVOKED`, `REPLACED`, `EXPIRED` |
| `ACTIVE` | `SUSPENDED`, `REVOKED`, `REPLACED`, `EXPIRED` |
| `SUSPENDED` | `ACTIVE` (explicit resume), `REVOKED`, `REPLACED`, `EXPIRED` |
| `REVOKED`, `REPLACED`, `EXPIRED` | *(terminal)* |

- Activation is always an explicit authorized operation. There is no
  automatic activation.
- A suspended credential fails verification as `SUSPENDED` — distinct from
  `REVOKED`, and reversible only by an explicit authorized resume.
- **Only `ACTIVE` exposes a usable QR to the participant. This is a hard
  invariant, not a policy with an escape hatch.** There is no
  pre-activation setting: the former `PASS_PRE_ACTIVATION_QR_ENABLED` flag
  was removed, and `issue_pass_token` refuses every non-ACTIVE status
  unconditionally. `INACTIVE`, `SUSPENDED`, `REVOKED`, `REPLACED`, and
  `EXPIRED` never yield a token or a QR image, whatever the settings say.
- An ACTIVE credential also yields no token or QR image once its
  Registration Context is no longer an active approved context (withdrawn,
  operationally cancelled, superseded or not approved), and activation and
  resumption are refused for such a context under the Registration row lock
  (Phase 3 Prompt 8, P8-06). The credential itself is not revoked: its
  history is kept and online verification keeps denying it.
- At most one credential per registration may be in `INACTIVE`, `ACTIVE`,
  or `SUSPENDED` at any time, enforced by `bdg_pass_one_current_uq`. The
  service layer and the constraint are built from the same
  `NON_TERMINAL_STATUSES` tuple, so they cannot disagree.

---

## 8. Replacement freshness

| Identifier | Behaviour across replacement |
| --- | --- |
| `PassCredentialSeries.public_id` | **Stable** |
| `PassCredentialSeries.fallback_reference` | **Stable** |
| `credential_version` (`cv`) | **Increments** |
| `jti` | **Changes** |
| `nonce` (`n`) | **Changes** |
| `DigitalEntryPass.id` | New row |
| `version` (lock counter) | Per-row, never in the QR |

Because the fallback reference is stable, a participant who printed it
keeps a working locator after their credential is replaced.

Replacement runs in one transaction that: locks the Registration row (first,
the same order generation, activation and resumption use — Phase 3 Prompt 8);
locks the series; loads and locks
the current non-terminal credential; confirms `expected_jti` and
`expected_lock_version`; re-runs eligibility; transitions the outgoing
credential to `REPLACED`; allocates the next credential version; issues the
new `INACTIVE` credential; links the chain; and writes the idempotency
record, the audit record, and the outbox row **inside the same
transaction**. Only delivery of already-committed outbox work happens after
commit.

The outgoing credential's signed material — `jti`, `nonce`, `payload_hash`,
`signing_key_id`, snapshot codes, validity window — is never mutated.

An old QR is rejected by **two independent checks**, either of which alone
is sufficient:

1. its `jti` resolves to exactly one credential row, and that row's status is
   `REPLACED` — a terminal state evaluated at precedence tier 4, ahead of the
   validity window;
2. every claim is compared against that row's own immutable snapshot, so a
   token whose `cv`, `bai`, `pid`, `btc`, `apc`, `n`, or validity window does
   not match the stored credential is rejected as `CREDENTIAL_MISMATCH`.

Note what this does *not* rely on: there is no "current version" lookup that
a replayed old token could race. The old token names its own credential row,
and that row says `REPLACED`.

---

## 8bis. The issued artefact is immutable

A credential is signed **exactly once**, at issuance, inside the locked
transaction that creates it. What is persisted is enough to reconstruct the
identical compact JWS without the private key:

| Stored | Role |
| --- | --- |
| The eleven claim values, as immutable columns | The canonical payload |
| `signing_key_id` | The canonical protected header |
| `signature_hex` | The ES256 signature, 64 raw bytes hex-encoded |
| `payload_hash` | Regeneration evidence, verified at issuance and on every verify |

Consequences that matter operationally:

* **Display never signs.** Rendering a participant's pass, or its QR image,
  reconstructs the stored artefact. The signing provider is not called.
* **A pass survives its key.** After a valid rotation overlap the old private
  key can be removed entirely; already-issued credentials still display and
  still verify, because verification needs only the published public key.
* **Mutable rows cannot change an issued credential.** `bai` and `pid` are
  snapshot columns. Editing the underlying `BadgeTypeAssignment` or
  `ParticipantEventPseudonym` afterwards changes nothing about what was
  signed.
* **Verification is immediate and fail-closed.** A freshly issued credential
  is verified against the published public key *before* its transaction
  commits. One that does not verify is never stored.
* **Replacement issues a new artefact** and leaves the replaced one
  byte-identical: its `jti`, `nonce`, `signature_hex`, and `payload_hash` are
  never mutated.

## 8ter. QR rendering

The participant surface renders a **real, scannable QR symbol** — a PNG
image produced server-side. Text containing a compact JWS is not a QR code,
and this document does not describe one as such.

| Property | Value |
| --- | --- |
| Encoder | `segno` (pure Python, no native build, no external service) |
| Error correction | M (~15 % recovery) |
| Quiet zone | 4 modules, the standard minimum |
| Typical symbol | Version 16 (81×81 modules) for the ~380-character payload |
| Rendered size | 320 CSS pixels, from a ~712-pixel PNG |

Delivery is an **authenticated image endpoint**. The URL carries only the
credential series' random `public_id`; the token is reconstructed inside the
request and encoded straight to bytes in memory. The token therefore never
enters a URL, a query string, a log line, or a cache. The response is
`Cache-Control: no-store` with `X-Content-Type-Options: nosniff`, and a
non-ACTIVE credential yields 404 rather than an image.

The QR container is explicitly `dir="ltr"` so the symbol is never mirrored
inside Arabic RTL layout, and the `<img>` carries descriptive alt text.

## 9. Participant fallback reference

A **locator**, never an authenticator and never an admission decision.

| Property | Value |
| --- | --- |
| Encoding | Crockford Base32 (payload alphabet excludes `I`, `L`, `O`, `U`) |
| Payload characters | 8 (40 bits) |
| Check characters | 1 (Crockford mod-37 check symbol) |
| Stored / normalized form | 9 characters, uppercase |
| Displayed form | 15 characters, `ASC-XXXX-XXXX-C` |
| Accepted input | Any casing, optional `ASC` prefix, any hyphens or spaces; Crockford input aliasing (`I`/`L` → `1`, `O` → `0`) applied to the payload only |

The check symbol is validated **before** any database access, so a single
mistyped character costs no query.

Uniqueness rests on the database constraint plus a bounded retry in the
allocating service. With 40 bits a collision is unlikely but **finite**, and
the code treats it as an ordinary handled event rather than assuming it
away.

Security properties: exact-match-only lookup, no listing or partial search,
rate-limited per actor, audited on every attempt with a **masked**
reference, and it grants nothing on its own — the full server-side
verification decision still applies.

**The 40-bit width is a transcription and casual-enumeration control, not a
secret.** Its security rests entirely on the controls above.

---

## 10. Public test vectors

`tests/assets/qr_vectors/vectors.json` contains, for a synthetic key:

- the public key PEM;
- the canonical protected-header bytes, payload bytes, signing input, and
  payload hash;
- 13 cases, each with a `compact_jws` value, whether its signature is
  valid, and the expected structural result code.

**No private key is committed, including a test key.** Signing keys used in
tests are generated in memory and discarded when the process exits.

The vectors pin the canonical signing input, the payload hash, and the
verification outcome of a **committed** signature. They deliberately do not
pin a regenerated signature: ECDSA draws a random nonce `k`, so re-signing
the same input produces different bytes. Any claim of byte-reproducible
ECDSA signatures would be false.

---

## 11. Explicit Phase 4 deferrals

Not implemented, not claimed, and not proven by anything in Phase 3:

- Offline Packages and their generation, signing, encryption, or expiry;
- offline QR validation on a device;
- the protected local NIN/passport lookup index;
- the durable offline operation queue, reconnection upload, deduplication,
  and delta refresh;
- `SyncOperation`, `ReconciliationCase`, and the conflict workspace;
- offline revocation propagation and device wipe;
- PWA service worker and installable-application behaviour;
- Event Edge deployment in any form.

Also deferred from this prompt and explicitly out of scope here: gate
admission decisions, `EntryEvent`, entry devices and device sessions,
physical badge stock, print batches, and badge issuance.

---

## 12. Deployment-only boundaries

Locally verified tests in this repository prove the **interface and the
contract**. They do not prove, and this document does not claim:

- a real managed KMS/HSM signing integration;
- production key custody, escrow, or recovery;
- real Redis/Celery broker behaviour;
- real S3-compatible object storage;
- real email or SMS delivery;
- production database-role isolation;
- offline verification or offline revocation propagation.
