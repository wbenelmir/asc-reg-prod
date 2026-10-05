# Digital Entry Pass permissions, audit, and privacy classification

Phase: 03 — Prompt 2
Related: [qr_contract.md](qr_contract.md), [ADR-0017](../architecture/ADR-0017-badges-app-boundary.md), [ADR-0019](../architecture/ADR-0019-es256-compact-jws-and-signing-key-boundary.md)

---

## 1. Permissions

Seven permissions are added. All are enforced through the single shared
primitive `apps.accounts.policies.has_scoped_permission` /
`operational_permission_required`. No parallel permission checker exists.

| Permission | Grants | Notes |
| --- | --- | --- |
| `badges.add_digitalentrypass` | Generate the first credential for a registration | Gated by `evaluate_eligibility` |
| `badges.view_digitalentrypass` | Read credential state and history | Also gates the published key set and the fallback lookup |
| `badges.activate_digitalentrypass` | `INACTIVE → ACTIVE` | Always explicit; no automatic activation exists |
| `badges.suspend_digitalentrypass` | `ACTIVE → SUSPENDED` | Separate from resume by design |
| `badges.resume_digitalentrypass` | `SUSPENDED → ACTIVE` | A "suspend only" role can exist without the power to restore |
| `badges.revoke_digitalentrypass` | Revoke, and replace | Replacement is a revocation of the prior version plus a fresh issue |
| `badges.manage_verificationkey` | Publish, promote, retire, revoke verification keys | Held in isolation — see §2. Retirement and emergency revocation are POST-only, CSRF-protected, idempotent, and audited with a controlled reason code. |

### 1.1 Groups

| Group | Permissions |
| --- | --- |
| Pass Administrators | `add`, `view`, `activate`, `suspend`, `resume`, `revoke` + `registrations.view_registration` |
| Pass Viewers | `view` + `registrations.view_registration` |
| Credential Key Custodians | `manage_verificationkey` **only** |

`Credential Key Custodians` deliberately hold **nothing else**: the ability
to rotate a signing key must never imply the ability to read or alter a
participant's credential. A regression test asserts a custodian is refused
on both the credential page and a revoke command.

No group defined here receives any document, export, review, or entry
permission. Scope (event edition, organization, time window, membership
status) applies exactly as elsewhere — a `ScopedGroupMembership` contributes
scope only when its own `Group` grants that exact permission.

### 1.2 Enforcement boundaries

- Every mutation is POST-only and CSRF-protected; a GET returns 405 and a
  POST without a CSRF token returns 403 (both asserted by tests).
- Hiding a control is presentation, never authorization: the endpoint
  re-checks independently, and tests assert both the absent control and the
  refused POST.
- Participant surfaces are scoped by person ownership, never by a supplied
  identifier. The print view and the QR image endpoint are addressed by the
  series' random `public_id` **and** additionally filtered to the
  authenticated participant, so guessing an identifier still reaches nothing.
- Controlled fallback-reference lookup routes through the same
  `scope_filtered_queryset` primitive as the credential detail page. A match
  outside the caller's event or organization scope returns exactly what an
  unknown reference returns, so the response cannot prove a registration
  exists somewhere the caller may not see. The lookup budget is charged
  **before** the check symbol is evaluated, so malformed attempts are
  throttled too rather than being a free oracle.
- Every lifecycle `operation_id` is bound to one exact command by a
  fingerprint over `(operation type, actor, target, parameters)`. A reused
  identifier for a different command is a conflict, never a replay — a
  replayed suspend can never report a revoke's outcome.

---

## 2. Audit

New stable action codes, all written **inside** the transaction they
describe:

| Code | Recorded when |
| --- | --- |
| `BDG_PASS_GENERATED` | A credential is issued |
| `BDG_PASS_ACTIVATED` | `INACTIVE → ACTIVE` |
| `BDG_PASS_SUSPENDED` | `ACTIVE → SUSPENDED` |
| `BDG_PASS_RESUMED` | `SUSPENDED → ACTIVE` |
| `BDG_PASS_REVOKED` | A credential is revoked |
| `BDG_PASS_REPLACED` | A credential is superseded |
| `BDG_PASS_EXPIRY_PERSISTED` | The optional sweep persists `EXPIRED` |
| `BDG_PASS_GENERATION_BLOCKED` | Generation refused (result `DENIED`) |
| `BDG_PASS_LIFECYCLE_REPLAYED` | Reserved for replay evidence (constant `CREDENTIAL_LIFECYCLE_REPLAYED`) |
| `BDG_PASS_FALLBACK_REFERENCE_LOOKUP` | Every lookup attempt, success or failure, including malformed input and out-of-scope matches |
| `BDG_VERIFICATION_KEY_PUBLISHED` / `_PROMOTED` / `_RETIRED` / `_REVOKED` | Key lifecycle |

The refusal audit for a blocked generation is deliberately written in its
**own** transaction. Recording it inside the failed one would roll it back
with everything else, and a refusal that leaves no evidence is worse than
no refusal at all.

### 2.1 What audit and logs must never contain

Raw QR values, signing inputs, signatures, private key material, clear
identity identifiers, and full fallback references. Audit summaries carry
only `jti`, `credential_version`, `status`, `signing_key_id`, and
`payload_version`; fallback lookups carry a **masked** reference
(`ASC-XXXX-****-*`). A regression test scans every audit summary produced by
a full lifecycle and asserts the token, its payload segment, its signature
segment, and its signing input are all absent.

`QR_SIGNING_KEY_V<n>` is registered in
`apps.core.credential_shapes.VERSIONED_KEY_NAME_PATTERNS`, the one family
definition shared by the standalone credential scanner and the
log-redaction layer (`apps.core.redaction` reuses that tuple rather than
keeping its own copy). Redaction covers the family in two ways:

* **Exact value** (Phase 3 Prompt 8, P8-03): for every configured
  `QR_SIGNING_KEY_V<n>`, whatever versions are active, both the raw
  environment value (single line, literal `\n` sequences) and the
  normalized multi-line PEM the signing provider actually loads
  (`apps.core.credential_shapes.normalize_pem_env_value`, the same
  function the provider uses) are masked wherever they appear: in the log
  message and its arguments, in structured `extra=` fields, and in rendered
  exception and stack text. Before this correction the exact-value layer
  covered only the three older key families, so the key text on its own
  was not masked.
* **Assignment shape**: a `QR_SIGNING_KEY_V<n>=…` or `…: …` occurrence is
  masked by name, even when the value is not one currently configured.

Redaction is the last line of defence. The signing provider never logs,
returns, or places key material in an exception message in the first place.

---

## 3. Privacy and retention classification

No new privacy subsystem and no new retention duration is invented here.
The records below are classified against the **existing** Phase 2
foundations (`apps.privacy`: `RetentionCategory`, `LegalHold`,
`PrivacyRequest`).

| Record | Classification | Contains personal data? | Notes |
| --- | --- | --- | --- |
| `DigitalEntryPass` | Operational credential evidence | No direct identifiers | Holds opaque identifiers, codes, a validity window, and lifecycle actors. Links to a `Registration`, which is itself classified. |
| `PassCredentialSeries` | Operational credential evidence | No | Two random opaque identifiers plus a version counter. |
| `PassLifecycleOperation` | Idempotency and operational evidence | No | Operation identifier, type, actor, and links. |
| `ParticipantEventPseudonym` | Pseudonymous linkage | Indirect | A random value linking a `Person` to an event edition. It is a pseudonym, not an identifier: it reveals nothing by itself, but it is linkable within one event and is therefore treated as personal data for access-control purposes. |
| `VerificationKey` | Public cryptographic material | No | Public keys only; never personal data. |

Retention behaviour follows the existing foundations rather than a new
policy:

- credential rows are lifecycle evidence tied to their `Registration` and
  inherit that record's retention category and any `LegalHold` placed on
  it;
- a `LegalHold` on a registration therefore protects its credential history
  from routine deletion, through the existing `apps.privacy` selectors;
- terminal credential versions are never deleted or mutated by the
  application — corrections create new rows, exactly as assignments and
  audit events already do;
- **no retention duration is set here.** `OD-007` (final retention
  durations for restricted records) remains open and must be closed by the
  developer and legal owners before production launch.

### 3.1 Participant-facing disclosure

| Credential status | Participant sees | Usable QR |
| --- | --- | --- |
| *(no row)* | "not yet available" | No |
| `INACTIVE` | Localized status + fallback reference | No |
| `ACTIVE` | Localized status + fallback reference + a real scannable QR image | **Yes** |
| `SUSPENDED` | "temporarily on hold, contact the accreditation desk" | No |
| `REVOKED` | "no longer valid, contact the accreditation desk" | No |
| `EXPIRED` | "validity period has ended" | No |
| `REPLACED` | Not shown — the newer current credential is listed instead | — |

A terminal credential is shown **as terminal**, not as absent: a revoked or
expired pass reads "no longer valid" or "validity period has ended", never
"you do not have an entry pass yet". Suspended and revoked deliberately carry
the same informational content so the participant surface does not disclose
which one applies.

ACTIVE-only QR exposure is a hard invariant with no configuration escape; the
former `PASS_PRE_ACTIVATION_QR_ENABLED` setting was removed. No internal
reason, reviewer identity, assignment history, signing-key detail, or
previous credential version is ever exposed. Participant credential
responses are sent `Cache-Control: no-store`.
