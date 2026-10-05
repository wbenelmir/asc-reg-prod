# ADR-0017: `badges` app boundary while assignments stay in `accreditation`

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-22 |
| Related requirements | TRD §13.1; Schema §9.3, §9.4; ADR-0001; Phase 3 Prompt 2 |

## Context

TRD §13.1 assigns "badge types, assignments, credentials, printing and
issuance" to a single `badges` application. Phase 2 Prompt 4 instead placed
`BadgeType`, `ParticipantRole`, `AccessProfile`, `AccessRule`, and all four
assignment tables in `apps.accreditation`, and that state was independently approved
(recorded in the Phase 2 final review, kept outside the repository).

Phase 3 Prompt 2 introduces the Digital Entry Pass credential. Three options
existed:

1. move the accreditation reference data and assignment tables into a new
   `badges` app so the literal TRD table is satisfied;
2. add credentials to `apps.accreditation`, so one app owns assignments and
   credentials together;
3. create `apps.badges` for credentials only, leaving Phase 2's ownership
   untouched.

Option 1 would rewrite applied migration history, which the project engineering rules forbid
without explicit approval, and would reopen an approved phase for no
behavioural gain. Option 2 would mix two genuinely different lifecycles --
an assignment is an operational classification with its own supersede
chain; a credential is a signed artefact with key material, a validity
window, and a verification contract -- inside one module that is already
long.

## Decision

Option 3. `apps.badges` is created and owns **credential** concerns only:

- `PassCredentialSeries`, `DigitalEntryPass`, `PassLifecycleOperation`;
- `VerificationKey` (public key material only);
- `ParticipantEventPseudonym` (the QR `pid` claim);
- the canonical QR serialization, the signing adapter, and the verifier.

`apps.accreditation` keeps every reference-data and assignment model
exactly as Phase 2 built them. The one change made there is additive: a
`public_reference` column on `BadgeTypeAssignment`, because the QR must
carry an assignment reference that is not a database primary key.

Physical badge stock, print batches, stock locations, the stock ledger,
issuance, entry devices, device scope, entry events, overrides, security
restrictions, offline packages, and synchronization are **not** in this app
yet. They belong to later Phase 3 prompts and to Phase 4.

## Consequences

- The divergence from TRD §13.1's literal table is deliberate and recorded
  here, not an oversight discovered later in review.
- No approved Phase 2 model moves, and no historical migration is edited or
  squashed.
- When physical stock and entry work begin, stock models may join
  `apps.badges` and entry models will form `apps.entry`; neither requires
  moving anything that exists today.
- Cross-app access follows `BE-001`: `apps.badges` calls
  `apps.accreditation.services.evaluate_eligibility` and reads assignment
  rows through documented selectors, never by mutating another app's
  models.
