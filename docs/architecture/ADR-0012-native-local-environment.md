# ADR-0012: Native local environment, no containers

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Developer decision (native Windows local environment) |

## Context

An earlier version of the Phase 1 plan assumed a Docker/Podman Compose
stack for local PostgreSQL, Redis, and MinIO. The developer's actual local
environment is native Windows 10 with a natively installed PostgreSQL
17.11 (host `localhost`, port `5433`, database `asc2026_dev`, role/owner
`asc2026_app`), no container runtime, and no local Redis.

## Decision

- No `compose.yaml`, container file, container instruction, or
  container-specific verification exists anywhere in this project.
- PostgreSQL 17.11 is a natively installed prerequisite, verified
  read-only by `scripts/check.py db-probe` (raw `psycopg` connection --
  no Django ORM, no test-database machinery, so it can never trigger a
  migration).
- Redis is not installed locally and is not required (ADR-0009).
- Private object storage uses a local filesystem adapter, not MinIO
  (ADR-0013).
- An existing PostgreSQL 16 installation on the same machine is left
  entirely untouched and never satisfies the PostgreSQL 17 baseline --
  `scripts/check.py db-probe` explicitly rejects any server whose reported
  version does not start with `PostgreSQL 17`.
- Deployment infrastructure (hosting, orchestration, secret management) is
  explicitly out of scope for this project and belongs to a later,
  specialized deployment team.

## Consequences

- Every command in this project's README and `scripts/check.py` runs
  natively on Windows via `uv run`.
- Staging and production settings modules still exist and are still
  validated (`config/settings/staging.py`, `production.py`) so the
  fail-closed configuration boundary is real and testable, even though no
  infrastructure for those environments is created by this project.
