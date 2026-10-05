# ADR-0001: Django application boundaries

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | TRD §13.1, Backend Schema §2.2, accepted Phase 1 plan §6.1, conflict C1 |

## Context

TRD §13.1 and Backend Schema §2.2 propose two different Django application
breakdowns. Notably: `Person`/`ParticipantAccount`/`ContactPoint`/
`IdentityIdentifier` live in `people` per the TRD but in `core` per the
Schema; the TRD gives invitations their own app while the Schema folds them
into `organizations`; and the Schema proposes `verification`, `reviews`,
`accreditation`, and `privacy` apps that the TRD does not mention at all.

Per the project's authority order, the TRD governs architecture and the
Backend Schema governs persistence entities -- so where they disagree on
*where a model lives* (an architectural boundary question), the TRD is
authoritative.

## Decision

The TRD §13.1 application list is the authoritative spine: `core`,
`accounts`, `events`, `people`, `organizations`, `registrations`,
`documents`, `privacy`, `communications`, `audit` (plus `invitations`,
`badges`, `entry` for later phases). Every Schema-only app is retained only
where it protects a genuinely distinct domain boundary that the TRD does not
already cover, and consolidated otherwise so that **no model has two
possible owners**:

| Schema §2.2 app | Disposition | Reason |
| --- | --- | --- |
| `verification` | Consolidated: `IdentityVerificationAttempt` → `people`; `StoredObject`/`Document` → `documents` | TRD `people` already owns "identity references, matching"; TRD `documents` already owns "purpose-bound requests, private files and lifecycle". |
| `reviews` | Consolidated into `registrations` | TRD `registrations` explicitly owns "workflow, requests and qualification". |
| `accreditation` | Consolidated into `badges` | TRD `badges` explicitly owns "badge types, assignments, credentials". |
| `privacy` | Retained as its own app | Legal documents, acceptance, consent and retention have no home in TRD §13.1, form a genuinely distinct domain with distinct permissions, and are required by Prompt 3/4. |

`core.OutboxEvent` is transport infrastructure consumed by `communications`,
not audit evidence, so it stays in `core` rather than `audit`.

Full Phase 1 model-to-app ownership table: see the accepted plan §6.1.

## Consequences

- One canonical owner per model; cross-app imports go through documented
  service functions and selectors (`BE-001`), never arbitrary direct writes.
- The TRD/Schema mismatch itself is not resolved in the specifications --
  only in this project's implementation. It should be raised for a
  synchronized specification update at the developer's discretion (see
  the accepted plan §11.5).
- `AuthenticationChallenge` (Schema §12.4 places it under operational users,
  but it serves participant email OTP too) is assigned to `accounts` as the
  authentication-mechanism owner shared by both actor types -- a
  clarification, not a conflict (conflict C5).
