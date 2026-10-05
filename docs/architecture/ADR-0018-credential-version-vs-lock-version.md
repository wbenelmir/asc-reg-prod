# ADR-0018: `credential_version` versus `VersionedModel.version`

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-22 |
| Related requirements | Schema §9.3, §16.3; TRD §10.3; Phase 3 Prompt 2 |

## Context

Schema §9.3 gives `DigitalEntryPass` a `version` column described as
"Positive integer, unique per Registration" -- that is the **credential
number** carried in the QR and checked during verification.

This project's `apps.core.models.VersionedModel` already defines a
`version` column with an entirely different meaning: the
**optimistic-concurrency lock counter** (Schema §16.3), incremented by an
atomic `UPDATE ... WHERE version = %(expected)s` in the service layer.

One column cannot honestly carry both meanings. Incrementing the lock
counter on an ordinary status change would silently invalidate every
already-issued QR that names the old number, and pinning the counter to the
credential number would defeat conflict detection.

## Decision

The two concerns get two columns with two names:

| Column | Meaning | Appears in the QR? |
| --- | --- | --- |
| `credential_version` | Monotonic credential number within one `PassCredentialSeries`, allocated under `SELECT ... FOR UPDATE`, carried as the `cv` claim, compared during verification | Yes |
| `version` (inherited from `VersionedModel`) | Optimistic-concurrency lock counter only | **Never** |

`credential_version` increments only when a new credential version is
issued. `version` increments on every controlled mutation of a credential
row and is never read by the QR contract, a selector projection, or a
participant-facing template.

Replacement additionally requires the caller to name the exact credential
being superseded (`expected_jti`). This is not redundant with the lock
version: a newly created credential starts at lock version 1, so a stale
`expected_lock_version` alone would match the replacement row a competing
request just created.

## Consequences

- The Schema's `version` column name is deliberately not reused. This
  divergence is recorded here rather than resolved silently.
- A test asserts that the lock counter never appears in a rendered QR
  payload, so the separation cannot erode.
- Three independent mechanisms prevent a double replacement: the series row
  lock, the `expected_jti` identity check plus the lock-version check, and
  the `bdg_pass_one_current_uq` partial unique constraint.
