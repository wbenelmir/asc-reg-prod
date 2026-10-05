# ADR-0010: Venue/Zone/Gate deferral

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Schema §5.2, §12.3; accepted plan §6.1 |

## Context

Schema §5.2 defines `Venue`, `Zone`, and `Gate`, and §12.3
`ScopedGroupMembership` includes `venue_id`/`gate_id` scope columns. Phase 1
has no entry, checkpoint, or gate-scoped behavior at all -- those belong to
a later Implementation Plan phase (badge/entry work).

## Decision

`Venue`, `Zone`, and `Gate` are **not created** in Phase 1. Prompt 3's
`events.EventEdition` migration covers only the minimum event configuration
Prompt 3 actually needs (code, name, timezone, registration window,
status, supported languages). `ScopedGroupMembership` (Prompt 3) therefore
carries **event-edition, organization, time, and status scope only** -- the
`venue_id`/`gate_id` columns are added when the venue structure itself is
built.

## Consequences

- This is a deliberate, documented deferral, not an omission discovered
  during review.
- When the venue/gate phase begins, `ScopedGroupMembership` gains two
  nullable FK columns via an additive migration; no Phase 1 data or
  constraint needs to change.
