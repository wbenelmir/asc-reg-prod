# ADR-0019: ES256 Compact JWS, `joserfc` verification, and the opaque signing-key boundary

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-22 |
| Related requirements | TRD §11.3, §11.4; PRD `FR-BDG-005`, `BR-BDG-003`; Schema §9.4; ADR-0004, ADR-0006; Phase 3 Prompt 2 |

## Context

The Digital Entry Pass QR must be a compact, signed, PII-free envelope.
TRD §11.3 names JWS with an asymmetric ES256 signing key as the preferred
baseline. TRD §11.4 requires that private signing keys live in a managed key
service or protected secret store and never enter source control,
committed environment files, or Offline Packages.

Two implementation questions followed.

**Which JOSE implementation?** Hand-writing Compact JWS parsing is exactly
where algorithm-confusion, non-canonical base64url, and signature-encoding
mistakes historically occur. `cryptography` was already an approved
dependency and supplies the P-256 primitive, but it has no JOSE layer.

**How does signing reach the key?** `joserfc.jws.serialize_compact()`
requires a private key object. Passing one to it would drag private key
material across the provider boundary -- and a future KMS/HSM-backed
provider physically cannot hand over a private key.

## Decision

**Algorithm.** ES256 (ECDSA P-256 with SHA-256) only, as a hard allowlist
of exactly one. The signature is the fixed-length raw `r || s` form
required by RFC 7518 §3.4; DER is rejected at the provider boundary.

**Verification** is delegated to `joserfc` (added as a reviewed dependency,
resolved at 1.7.5, depending only on the already-approved `cryptography`).
Every call passes `algorithms=["ES256"]` and a registry restricted to the
three permitted protected-header members, so `crit`, `jwk`, `b64`, and any
other member is rejected by the library as well as by this project's own
header allowlist. The verification algorithm is selected from the
**trusted stored `VerificationKey` record**, never from the presented
header.

**Signing** uses a minimal project-owned assembly adapter
(`apps.badges.credentials.issue`) that canonicalizes, base64url-encodes,
concatenates, asks `SigningKeyProvider.sign()` for a signature, and appends
it. The adapter implements **no cryptographic primitive**. Its RFC
compliance is proven rather than asserted: every token it produces is
round-tripped through `joserfc` with the corresponding public key in the
test suite, so the reviewed consumer validates the minimal producer.

**Key boundary.** `apps.core.crypto.signing.SigningKeyProvider` exposes
exactly five capabilities: current key id, active key ids, algorithm
confirmation, sign-these-bytes, and public-key retrieval. There is no
accessor that returns private key material and none may be added.
`EnvSigningKeyProvider` reads `QR_SIGNING_KEY_V<n>` from settings, which
`config/settings/_env.py` populates only from `os.environ` (ADR-0004). This
is a **fourth** independent versioned-key family, separate from identity
encryption, identity blind indexes, and rate-limit fingerprints, for the
same cross-contamination reason ADR-0006 and ADR-0007 already record.

`badges.VerificationKey` stores **public** key material only. The service
refuses any PEM containing a private-key marker, and a regression test
proves it.

**Canonical forms.** Exactly one byte form each:

- protected header: `{"alg":"ES256","kid":"<kid>","typ":"ASC-PASS"}`;
- payload: the eleven approved claims, sorted, no insignificant whitespace,
  integer timestamps, UTF-8.

**Rotation and revocation.** A RETIRED key keeps verifying credentials
issued before rotation while they remain within their own validity window;
a REVOKED key fails verification immediately for every credential bearing
its `kid`, regardless of that credential's expiry.

## Correction pass (Phase 3 Prompt 2, review verdict CHANGES REQUIRED)

Four decisions were tightened after independent review:

1. **The signature is persisted.** The first implementation re-signed the
   credential on every participant page render, which meant a "credential"
   was really a recipe whose inputs could move. A credential is now signed
   exactly once and the signature stored; display reconstructs the identical
   compact JWS without the private key.
2. **All eleven claims are immutable snapshots.** `bai` and `pid` were being
   read back from mutable rows at display time. They are now columns on the
   credential, populated inside the issuing transaction.
3. **Public keys are parsed canonically.** Keys are validated as EC/P-256
   public keys, canonicalized to DER SubjectPublicKeyInfo, and fingerprinted
   over DER rather than PEM text. Provider/key alignment is enforced at
   promotion and before every issuance.
4. **ACTIVE-only QR exposure became an invariant.** The
   `PASS_PRE_ACTIVATION_QR_ENABLED` setting was removed outright rather than
   left defaulting to off.

## Consequences

- Two runtime dependencies: `joserfc>=1.7.5,<2` for Compact JWS parsing and
  ES256 verification, and `segno>=1.6.6,<2` for server-side QR rendering.
  Both resolved and audited clean under Python 3.14. `pyzbar` is a
  test-only decoder used to prove an encode/decode round trip.
- Private key material never reaches an application table, a migration, a
  fixture, a log, a test vector, or a review archive -- including test
  keys, which are generated in memory and discarded.
- The committed public test vectors pin the canonical signing input, the
  payload hash, and the verification outcome of a **committed** signature.
  They deliberately do not pin a regenerated signature: ECDSA draws a
  random nonce, so re-signing the same input yields different bytes, and
  claiming reproducibility would be false.
- A real managed KMS/HSM integration remains **deployment-only**. Local
  tests prove the interface, not the integration, and this ADR does not
  claim otherwise.
- Offline verification, Offline Packages, and offline revocation
  propagation are Phase 4. Phase 3 publishes the public key set for online
  use and makes no claim about offline devices receiving emergency updates.
