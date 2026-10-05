# ADR-0002: UUID primary keys for business entities

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | TRD §13.3, Backend Schema §2.1/§3.1, `OD-DATA-01`, conflict C2 |

## Context

TRD §13.3 says internal database keys **MAY** use integers for storage
efficiency, with a separate public identifier for externally exposed
resources. Backend Schema §2.1/§3.1 says business entities **MUST** use a
PostgreSQL `uuid` primary key, with `bigint` permitted only for high-volume
append-only tables alongside a global UUID event identifier.

The TRD *permits* integers; the Schema *requires* UUIDs. The Schema is the
more specific authority for persistence, so no conflict-resolution
escalation is needed here -- the Schema's stricter rule controls.

## Decision

- Every Phase 1 business-entity model uses a UUID primary key.
- `AuditEvent` uses the Schema-sanctioned exception: a `bigint` physical
  primary key (for partition-friendly index locality) plus a separate
  `event_uuid` global identifier column.
- Public references (e.g. `ASC26-R-004812`) remain entirely separate from
  primary keys, per Schema §3.1.
- UUIDs are generated with **UUIDv7**, using `uuid.uuid7()` from the
  Python 3.14 standard library -- confirmed working in this project's
  Prompt 2 dependency-compatibility check. This satisfies `OD-DATA-01`
  ("UUIDv7 where supported; UUIDv4 fallback") with **zero third-party
  dependency**.

## Consequences

- No `django-uuid7` or similar package is added.
- Every Prompt 3 model definition explicitly declares
  `id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)`
  rather than relying on Django's `DEFAULT_AUTO_FIELD` (which remains set to
  `BigAutoField` only to silence Django's system check for the rare model
  with no explicit primary key -- no Phase 1 model relies on it).
