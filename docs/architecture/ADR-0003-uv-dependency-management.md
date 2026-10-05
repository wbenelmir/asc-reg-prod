# ADR-0003: `uv` dependency management and lock policy

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | the project engineering rules ("Use `uv`, `pyproject.toml`, and a committed `uv.lock`") |

## Context

The project mandates `uv` for dependency management on native Windows,
targeting Python 3.14, with no container runtime.

## Decision

- `pyproject.toml` declares runtime dependencies under `[project.dependencies]`
  and development/test dependencies under `[dependency-groups.dev]` (PEP
  735), not `[project.optional-dependencies]` -- dev tools are never
  installed into a consumer's environment.
- The project uses `uv init --app --no-package` layout: a flat
  `pyproject.toml` with **no `[build-system]` table**. This is a Django
  application, not a distributable library; there is nothing to build a
  wheel from, and `manage.py` is the entrypoint, not a packaged console
  script.
- `uv.lock` is project-owned and is the single source of truth for exact
  resolved versions; `uv sync --all-groups` reproduces the environment
  exactly. (This project does not use Git at this stage -- ADR-0015 --
  so "project-owned" describes its current status; it becomes a committed
  file once version control is adopted, per the project engineering rules.)
- Every runtime dependency's Python 3.14 / Windows compatibility was
  verified empirically in Prompt 2 -- not merely by reading PyPI
  classifiers -- by installing into a scratch `uv venv --python 3.14` and
  running real imports plus targeted functional smoke tests (Celery eager
  dispatch, Pillow image round-trip, `cryptography` AES-GCM/HMAC
  round-trip) before the dependency was added to `pyproject.toml` and
  locked. See the Prompt 2 completion report for the exact evidence.
- `mypy`, `django-stubs`, and `pytest-cov` are deliberately **not**
  included (deferred per the accepted plan §5.2); `pip-audit` **is**
  included and approved.

## Consequences

- Reproducing the exact environment on another machine is `uv sync
  --all-groups` -- no separate requirements-freezing step.
- Adding a dependency later must repeat the empirical Python 3.14
  compatibility check before it is added to `pyproject.toml`, per this
  project's genuine-blocker policy (accepted plan §11.2, B1).
