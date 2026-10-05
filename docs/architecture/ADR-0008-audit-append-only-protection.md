# ADR-0008: Audit append-only protection -- honest two-level contract

| Field | Value |
| --- | --- |
| Status | Accepted; local layer implemented in Prompt 3, deployment-team layer is a prerequisite handed off, not implemented by this project |
| Date | 2026-09-19 |
| Related requirements | Schema §15.2, §17.4; accepted plan §5.6; conflict C7 |

## Context

Schema §15.2 expects "insert-only access to audit partitions" and §17.4
expects separate database roles for migrations vs. application read/write.
Locally, the single configured PostgreSQL role (`asc2026_app`) **owns**
`asc2026_dev`. A table owner cannot be denied `UPDATE`/`DELETE` on its own
table by ordinary `GRANT`/`REVOKE` -- PostgreSQL simply does not enforce
that boundary against an owner. Claiming otherwise would be inaccurate.

## Decision

Two distinct controls, verified at two distinct points in the lifecycle,
and never conflated:

**A. Static configuration validation (Prompt 2, `config/settings/
validation.py`)** checks only that staging/production *declare* a
migration-owner identity distinct from the runtime identity
(`DATABASE_MIGRATION_USER != DATABASE_USER`). This is a configuration-shape
check: no catalog query, no `audit_event` table required, and it never
blocks `manage.py migrate` run with the migration-owner configuration.

**B. Local protection (Prompt 3)**: application services expose append
operations only -- no service, selector, or admin path can update or
delete an `AuditEvent`; corrections create new, linked events (`DATA-005`).
`audit.0001` additionally installs a `BEFORE UPDATE OR DELETE ON
audit_event` trigger that raises an exception, as defence in depth against
application mistakes. **Stated honestly: the owning role can disable or
drop this trigger.** Real database-role privilege isolation is **not
verified locally** and is listed as not covered in every Phase 1 test
report.

**C. Post-migration deployment-readiness check (`scripts/check.py
audit-isolation`, Prompt 3+)**, run by the deployment team after migrations
with the **restricted runtime credentials** against a live, migrated
database: proves the runtime role does not own `audit_event`, holds no
`UPDATE`/`DELETE`/`TRUNCATE`/DDL authority over it (directly or via granted
roles or `PUBLIC`), holds exactly the required `INSERT`+`SELECT`, and that
the trigger exists and is enabled. Its failure fails deployment readiness.

No claim anywhere in this project states that (A) or a local run of (C)
proves real database ownership or grants -- only a genuine post-migration
run against the deployment team's separated roles does that.

## Consequences

- Prompt 2 and Prompt 3 never create, rename, or alter a PostgreSQL role.
- The deployment team's prerequisite is explicit: a migration-owner role
  that applies DDL, and a distinct restricted runtime role with only
  `INSERT`+`SELECT` on `audit_event`.
- Every Phase 1 review pack names "production database-role isolation" in
  its not-covered section, separately from "trigger present and enabled",
  which *is* verified locally.
