# Phase 3 Prompt 2 — manual verification record

Scope: Digital Entry Pass lifecycle and the signed QR contract.
Related: [qr_contract.md](../security/qr_contract.md),
[pass_permissions_and_privacy.md](../security/pass_permissions_and_privacy.md)

**Status of this document:** the scenarios below are written for a human to
execute locally. **None is marked as manually executed.** Automated
evidence is cited per scenario; a scenario is marked executed only when a
person has actually run it and recorded the result here.

## Prerequisites

The signing key is supplied by the developer, generated **outside** the
repository, and referenced through `.env`. This project never generates,
prints, or archives a signing key.

```bash
uv run --env-file .env python manage.py migrate
```

Environment variables required by the credential layer (values are never
printed or committed):

- `QR_SIGNING_KEY_V1` — PEM-encoded P-256 private key (literal `\n`
  accepted for the newlines);
- `QR_SIGNING_ACTIVE_KEY_VERSIONS` — defaults to `1`;
- `QR_SIGNING_WRITE_KEY_VERSION` — defaults to `1`.

Start the local server:

```bash
uv run --env-file .env python manage.py runserver 8000
```

---

## 1. Verification key publication and promotion

**Steps.** Sign in as a member of `Credential Key Custodians`. Open
`/ops/badges/verification-keys/`. Paste the **public** PEM for `v1`,
publish it, then promote it to active.

**Expect.** The key appears as `PENDING`, then `ACTIVE`. Exactly one key is
`ACTIVE`. No private key is displayed anywhere on the page.

**Then.** Paste a PEM whose header is a private-key BEGIN marker rather
than a public-key one, and publish.

**Expect.** Refused with "public key material only"; nothing is stored.

**Automated evidence.** `apps/badges/tests/test_views.py`
(`test_the_key_surface_never_renders_private_material`,
`test_publishing_a_private_pem_is_refused_at_the_view`),
`apps/badges/tests/test_signing_and_verification.py`
(`test_only_one_key_can_be_active_at_a_time`).

**Executed:** not yet.

---

## 2. Generation is blocked without eligibility

**Steps.** Sign in as a member of `Pass Administrators`. Open the
credential page for an approved registration that is missing one of the
three current assignments. Press **Generate entry pass**.

**Expect.** A localized message naming the eligibility reason. No
credential row is created. An audit event `BDG_PASS_GENERATION_BLOCKED`
with result `DENIED` exists.

**Automated evidence.** `apps/badges/tests/test_lifecycle.py`
(`test_generation_is_blocked_without_the_required_assignments`,
`test_a_blocked_generation_is_audited_as_denied`),
`apps/badges/tests/test_views.py`
(`test_an_ineligible_registration_gets_a_localized_explanation`).

**Executed:** not yet.

---

## 3. Generate, activate, and view the credential

**Steps.** With all three assignments current and the registration
approved, press **Generate entry pass**, then **Activate entry pass**.

**Expect.** Status moves `INACTIVE` → `ACTIVE`. Credential version is `1`.
A fallback reference in the form `ASC-XXXX-XXXX-C` is shown. There is no
automatic activation at any point.

**Automated evidence.** `apps/badges/tests/test_lifecycle.py`
(`test_generation_creates_an_inactive_credential`,
`test_activation_is_explicit_and_never_automatic`).

**Executed:** not yet.

---

## 4. Suspend and resume

**Steps.** Press **Suspend entry pass**, then **Resume entry pass**.

**Expect.** Status `ACTIVE` → `SUSPENDED` → `ACTIVE`. While suspended the
participant sees "temporarily on hold" and **no** QR. Resume is only
available as an explicit control and only from `SUSPENDED`.

**Automated evidence.** `apps/badges/tests/test_lifecycle.py`
(`test_suspension_then_explicit_resumption`,
`test_only_a_suspended_credential_can_be_resumed`),
`apps/badges/tests/test_signing_and_verification.py`
(`test_suspended_is_not_reported_as_revoked`).

**Executed:** not yet.

---

## 5. Replacement rejects the old QR

**Steps.** Copy the participant's current entry pass code. Press **Replace
entry pass**. Compare the old and new codes.

**Expect.** The prior credential becomes `REPLACED` and its signed material
is unchanged. The new credential is `INACTIVE` with credential version `2`,
a different `jti`, and a different nonce. The **fallback reference is
unchanged**. Verifying the old code reports `REPLACED`.

**Automated evidence.** `apps/badges/tests/test_lifecycle.py`
(`test_replacement_supersedes_the_prior_credential`,
`test_the_stable_identifiers_survive_replacement`),
`apps/badges/tests/test_signing_and_verification.py`
(`test_each_credential_status_maps_to_its_own_result`).

**Executed:** not yet.

---

## 6. Concurrency and idempotency

**Steps.** Submit the same lifecycle form twice quickly (for example by
double-clicking, or by replaying the request with the same hidden
`operation_id`).

**Expect.** "This request was already processed." Exactly one credential
exists. The credential version sequence advanced by exactly one.

**Automated evidence.** `tests/concurrency/test_pass_credential_concurrency.py`
(all four tests, real multi-connection PostgreSQL),
`apps/badges/tests/test_lifecycle.py`
(`test_a_repeated_generate_operation_id_is_a_replay`,
`test_a_repeated_replace_operation_id_does_not_mint_a_second_credential`).

**Executed:** not yet.

---

## 7. Expiry fails closed without a worker

**Steps.** With no Celery worker or broker running, set an active
credential's `valid_until` to the past in the database, then verify its QR.

**Expect.** The result is `EXPIRED` even though the stored status still
reads `ACTIVE`. Running the optional sweep afterwards persists `EXPIRED`
and changes no decision.

**Automated evidence.** `apps/badges/tests/test_signing_and_verification.py`
(`test_expiry_fails_closed_without_any_sweep`),
`apps/badges/tests/test_lifecycle.py`
(`test_inactive_to_expired_is_persisted_by_the_sweep`,
`test_the_expiry_sweep_is_idempotent`).

**Executed:** not yet.

---

## 8. Emergency key revocation

**Steps.** With a valid active credential, revoke the signing key from the
verification-key surface, then verify the credential's QR.

**Expect.** `INVALID_KEY` immediately, regardless of the credential's own
expiry. Phase 3 makes no claim about offline devices receiving this update.

**Automated evidence.** `apps/badges/tests/test_signing_and_verification.py`
(`test_emergency_key_revocation_fails_verification_immediately`,
`test_a_retired_key_within_its_window_still_verifies`).

**Executed:** not yet.

---

## 9. Controlled fallback-reference lookup

**Steps.** Open `/ops/badges/fallback-reference/`. Enter the reference in
lower case without hyphens; then with one character mistyped; then a
partial reference.

**Expect.** The first resolves. The second is rejected by the check symbol.
The third finds nothing — there is no partial or wildcard search. Every
attempt is audited with a masked reference. A match opens the credential
page and grants nothing by itself.

**Automated evidence.** `apps/badges/tests/test_references.py` (all 27
tests, including normalization, check-symbol rejection, exact-match-only,
rate limiting, masked audit, and stability across replacement).

**Executed:** not yet.

---

## 10. Permission and scope boundaries

**Steps.** Repeat scenario 3 as: a `Pass Viewers` member; an administrator
scoped to a different event edition; an administrator scoped to a different
organization; a `Credential Key Custodians` member.

**Expect.** Every one is refused, and the controls are absent from the page
for the viewer. A key custodian can reach neither the credential page nor a
revoke command.

**Automated evidence.** `apps/badges/tests/test_views.py`
(`test_a_viewer_never_sees_a_lifecycle_control`,
`test_a_viewer_cannot_post_a_lifecycle_command`,
`test_wrong_event_scope_is_denied`,
`test_wrong_organization_scope_is_denied`,
`test_a_key_custodian_holds_no_credential_permission`).

**Executed:** not yet.

---

## 11. Participant surface, localization, and print

**Steps.** Sign in as the participant. Open **My entry passes** in English,
French, and Arabic. Open the print view and use the browser's print
preview.

**Expect.** Only the participant's own current credential is shown. A QR
value appears only while `ACTIVE`. Arabic renders right to left with the
status text translated, while the fallback reference and the credential
value stay left to right and unmirrored. The print sheet is monochrome-safe
and the credential value remains selectable text.

**Automated evidence.** `tests/browser/test_digital_entry_pass.py` (all four
tests, real Chromium), `apps/badges/tests/test_views.py` (localization,
ownership, no-store, and non-leakage assertions).

**Visual evidence captured during implementation:**
`var/screenshots/phase3_prompt2/` (English, Arabic RTL, print view,
non-active state).

**Executed:** not yet.

---

## 12. Not covered locally (deployment-only)

Do not attempt to verify these locally, and do not record them as passing:

- a real managed KMS/HSM signing integration;
- production key custody, escrow, or recovery;
- real Redis/Celery broker behaviour;
- real S3-compatible object storage;
- real email or SMS delivery;
- production database-role isolation;
- offline verification, Offline Packages, or offline revocation
  propagation (all Phase 4).
