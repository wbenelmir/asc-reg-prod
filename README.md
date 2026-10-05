# ASC 2026 Registration Platform

Web registration, identity verification, accreditation, badge, entry and
event-operations platform for the African Startup Conference 2026
(5-7 December 2026, Algiers). English, French and Arabic (right to left)
interfaces.

* **Deploying it:** [docs/deployment/README.md](docs/deployment/README.md) is the
  single entry point for the operations team.
* **Publishing it to GitHub:** [docs/deployment/github_handoff.md](docs/deployment/github_handoff.md).
* **Open items:** [docs/deployment/beta_backlog.md](docs/deployment/beta_backlog.md).
* **Product and technical baseline:** [docs/specifications/](docs/specifications/)
  (read-only source material) and the architecture decisions in
  [docs/architecture/](docs/architecture/).

## Technical baseline

- Python 3.14, Django 5.2 LTS, PostgreSQL 17
- `uv` with a committed `uv.lock`
- Django Templates, HTMX and Bootstrap 5 (vendored locally with recorded
  provenance -- see `static/vendor/*/PROVENANCE.md`); no single-page application
- Celery with a Redis broker, and a shared Redis challenge counter (staging and
  production); eager Celery locally
- waitress as the WSGI server (`python -m config.serve`)
- ClamAV `clamd` for malware scanning, SMTP for email, S3-compatible private
  storage for documents (staging and production)
- A modular monolith: one Django project, one app per domain under `apps/`

## Local development

Prerequisites: Python 3.14, `uv`, a native PostgreSQL 17 instance, and GNU
gettext (`msgfmt`) for compiling translations. No container runtime is needed.

1. **Install dependencies:**

   ```bash
   uv sync --all-groups
   ```

2. **Create your local environment file.** Copy `.env.example` to `.env` and
   fill in the values marked `__SET_IN_LOCAL_ENV__`. `.env` is ignored by Git
   and never committed; `.env.example` holds placeholders only. Generate the
   secret and key values with:

   ```bash
   uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

3. **Check database connectivity** (read-only, credentials redacted, refuses
   anything other than PostgreSQL 17):

   ```bash
   uv run --env-file .env python scripts/check.py db-probe
   ```

4. **Apply migrations and seed the minimum synthetic local data.** The
   migrations install the approved country catalog and the current legal
   notices; the seed adds a synthetic sector, the current event and interest
   topics (local only):

   ```bash
   uv run --env-file .env python manage.py migrate --settings=config.settings.local
   uv run --env-file .env python manage.py seed_phase1_local_data --settings=config.settings.local
   ```

5. **Run the development server:**

   ```bash
   uv run --env-file .env python manage.py runserver
   ```

Locally, mail goes to `var/mail/`, documents to `var/private/`, the malware
scanner is a deterministic stub and the NIN provider is a labelled development
simulation. None of these substitutes can be selected in staging or
production; the deployed settings refuse to start with them.

## Everyday commands

Every command that needs a secret uses `uv run --env-file .env ...`; Django
never reads `.env` itself.

```bash
uv run --env-file .env python manage.py check
uv run --env-file .env python manage.py makemigrations --check --dry-run
uv run ruff format --check .
uv run ruff check .
uv run python scripts/check.py local-safety     # .gitignore, .env exclusion, placeholder-only examples
uv run python scripts/check.py secret-scan      # credential-shaped strings in project files
uv run python scripts/check.py assets           # vendored asset checksums
uv run python scripts/check.py deploy           # staging/production configuration shape (synthetic values)
uv run --env-file .env pytest -q --ignore=tests/browser --ignore=tests/performance
uv run pip-audit
```

The full local regression gate (format, lint, safety, credential scan,
database checks, the main, performance and browser suites, deploy checks,
dependency audit, release-readiness report):

```bash
uv run --env-file .env python scripts/check.py all
```

The browser suite needs Playwright's Chromium (`uv run playwright install chromium`).
Test screenshots and timing reports are written under `var/test_artifacts/`
(ignored by Git).

## Repository layout

```text
config/            Django project: settings (local, test, staging, production and their
                   migration variants), URLs, Celery app, WSGI/ASGI and the waitress entry point
apps/              One application per domain (registrations, people, reviews, accreditation,
                   badges, entry, communications, documents, invitations, ...)
templates/         Django templates
static/            Project-owned and vendored front-end assets
locale/            Translation catalogs (en, fr, ar)
deploy/            Deployment configuration template (placeholders only)
scripts/check.py   Quality, safety, build and verification commands
tests/             Cross-cutting, browser, performance and concurrency tests
docs/              Specifications, architecture decisions, deployment, operations,
                   security, testing and decision records
```

## Licensed material

The purchased Finder template is a licensed visual reference kept outside
version control (`reference/finder/`); adapted elements are recorded with their
attribution in [docs/design/finder_inventory.md](docs/design/finder_inventory.md).
Third-party asset licences and checksums are in `static/vendor/*/PROVENANCE.md`.
