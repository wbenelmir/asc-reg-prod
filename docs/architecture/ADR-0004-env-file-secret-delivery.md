# ADR-0004: Environment-file and secret-delivery contract

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Accepted plan §5.8; blocker B3 (resolved) |

## Context

The project engineering rules forbid committing secrets and require environment-based
configuration. The project also forbids adding a dotenv-parsing dependency
and forbids Django reading `.env` automatically.

`uv` 0.12.17 was verified in Prompt 2 to support `uv run --env-file <path>
<command>`, which populates the **process environment** of the child
process before it starts -- confirmed with a harmless non-secret probe
variable, visible with the flag and absent without it.

## Decision

- Django settings read `os.environ` only (via `config/settings/_env.py`'s
  typed helpers: `env`, `require`, `env_bool`, `env_int`, `env_list`).
  Django itself never opens `.env`.
- The real, populated `.env` file stays local and gitignored (`.gitignore`:
  `.env`, `.env.*`, with `!.env.example` re-including the template).
- `.env.example` is project-owned and contains **placeholders only** --
  every value that must be real is written as `__SET_IN_LOCAL_ENV__` or
  `__SET_BY_DEPLOYMENT_TEAM__`. This project does not use Git at this
  stage (ADR-0015); `.env.example` becomes a committed file once version
  control is adopted.
- Every local command that needs a secret is invoked as
  `uv run --env-file .env <command>`.
- Static deploy checks (`scripts/check.py deploy`) deliberately do **not**
  use `--env-file .env` -- they inject documented synthetic, non-secret
  values into the subprocess environment instead, so the real `.env` is
  never involved in a deploy-check run.
- `require(name)` raises `django.core.exceptions.ImproperlyConfigured` with
  a message containing only the variable *name*, never its value.
- `apps/core/redaction.py` additionally redacts the literal current value
  of every sensitive environment variable from any log line it appears in,
  as defence in depth.

## Consequences

- No `django-environ`, `python-dotenv`, or similar dependency exists in
  this project.
- A missing required variable fails fast and loud at settings-import time,
  before any command does anything meaningful -- this is by design
  (fail-closed), not a bug to work around with defaults.
- Prompt 2 could not be verified end-to-end against a real PostgreSQL
  connection until the developer populated their own `.env` with the real
  `DATABASE_PASSWORD` and a generated `DJANGO_SECRET_KEY` -- this project
  never invents or asks for that value in chat (see the Prompt 2 completion
  report).
