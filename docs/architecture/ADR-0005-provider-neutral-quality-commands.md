# ADR-0005: Provider-neutral quality commands

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | the project engineering rules (no provider-specific CI/CD without explicit approval); Implementation Plan §8.3 |

## Context

Implementation Plan §8.3 requires a CI baseline (formatting, linting,
static checks, tests, missing-migration checks, security/dependency scans,
template/localization checks, build/deploy validation) from Phase 1
onward. The project engineering rules separately forbid introducing provider-specific CI/CD
without an explicit developer decision. No CI provider or hosting decision
has been made yet.

## Decision

`scripts/check.py` is a single, dependency-free (stdlib + already-approved
tools) Python script exposing one subcommand per check, plus an `all`
aggregator:

- `local-safety` -- `.gitignore` text contract, `.env` existence/exclusion,
  `.env.example` placeholders-only contract (no Git required -- see
  ADR-0015)
- `secret-scan` -- accidental-credential scan over project-controlled files
  (excludes `.env`, `var/`, `.venv/`, vendored minified assets, generated
  artifacts)
- `manifest` -- SHA-256 manifest of every project-owned file
- `assets` -- vendored third-party asset checksum verification
- `db-probe` -- read-only PostgreSQL connectivity/version probe
- `deploy` -- static, configuration-only `check --deploy` for staging and
  production
- `all` -- runs every check available at the current stage of the project,
  in order, and reports a clear pass/fail summary plus an explicit
  "not yet applicable" list for checks that later prompts will add

Every function is importable from `scripts.check`, so exactly the same
logic backs both the CLI entrypoint and the automated `pytest` suite under
`tests/foundation/` -- there is one source of truth, not a script and a
separate reimplementation in tests.

**No GitHub Actions workflow, GitLab CI file, or any other provider-specific
configuration exists in this project.** Any future CI provider (once
chosen) calls `uv run --env-file .env python scripts/check.py all` as its
one entrypoint.

## Consequences

- Choosing a CI provider later requires no change to how checks are
  defined -- only a thin provider-specific wrapper invoking one command.
- `scripts/check.py all`'s subcommand set grows across Prompts 3-8
  (`db-state`, `audit-isolation`, the real-concurrency OTP suite, browser
  E2E, etc.) without changing this architecture.
