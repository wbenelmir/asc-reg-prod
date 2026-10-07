# Deploying the ASC 2026 Registration Platform (staging Beta)

The single entry point for the operations team. It assumes experience with
Python web applications, PostgreSQL, Redis, a reverse proxy and a service
manager; it does not choose those for you. Everything below uses only what
this repository contains. Detailed acceptance scenarios are linked, not
repeated: [UAT handoff](../testing/phase_04_staging_uat_handoff.md),
[identity provider guide](../operations/identity_provider_integration_guide.md),
[counter and sign-in guide](../testing/phase_04_p4_4_c3_staging_guide.md).

## 1. What is proven, what you supply, what you must verify live

| | Status |
| --- | --- |
| **Application, completed and tested locally** | Registration, review, identity verification, accreditation, communications and entry features; the `config.serve` HTTP entry point (waitress); runtime/migration database-identity separation and the `migrate` guard; ClamAV clamd adapter (simulated-protocol tests); SMTP delivery (local fake SMTP server); broker TLS verification; staging provisioning command; build and artifact tooling. Local verification results: [Beta release notes](beta_release_notes.md). |
| **Supplied by operators** | Server OS and service manager, reverse proxy and TLS certificate, PostgreSQL 17 with two roles, Redis with TLS (two databases), an S3-compatible private bucket, a clamd daemon, an SMTP service and sender domain, secret values, test mailboxes, backups. |
| **Requires live verification in staging** | SMTP delivery to real mailboxes, clamd verdicts on the real daemon, S3 read/write, broker and counter Redis (including an outage), PostgreSQL role isolation (`audit-isolation`), the reverse proxy, and, only if the owner authorizes it, the ministry NIN service. None of these is proven by a passing configuration check. |

## 2. Supported versions

| Component | Version |
| --- | --- |
| Python | 3.14 (`.python-version`; `requires-python = ">=3.14,<3.15"`) |
| Dependencies | exactly `uv.lock`, installed with `uv` (Django 5.2, Celery 5.6, waitress 3.0) |
| PostgreSQL | 17 |
| Redis | 6 or later with TLS (ACL support is used by the optional counter user) |
| ClamAV | a current `clamd` that supports `INSTREAM` (Unix socket or private TCP) |
| SMTP | any server offering STARTTLS or implicit TLS, or a relay on the same host |

## 3. Processes

| Process | Command (from the artifact root) | Settings module | Count |
| --- | --- | --- | --- |
| Web | `python -m config.serve` | `config.settings.staging` | one or more |
| Worker | `python -m celery -A config worker --loglevel=INFO` | `config.settings.staging` | one or more |
| Scheduler | `python -m celery -A config beat --loglevel=INFO --schedule <writable path>/celerybeat-schedule` | `config.settings.staging` | **exactly one** |
| Migration (one-off) | `python manage.py migrate` | `config.settings.staging_migration` | one, while the others are stopped |
| Operator commands (one-off) | `python manage.py <command>` | `config.settings.staging` | as needed |

* `DJANGO_SETTINGS_MODULE` must be set explicitly for every process. The web
  entry point, `config.wsgi`, `config.asgi` and the Celery app refuse to start
  without it; only `manage.py` keeps the local default for developers.
* `python` is the interpreter of the artifact's virtual environment.
* Set `PYTHONDONTWRITEBYTECODE=1` (or mount the artifact read-only) so no
  process writes into the frozen artifact.
* Logs are JSON lines on standard error; collect them with the service
  manager. They carry correlation ids and codes, never secrets or document
  contents.
* `config.serve` listens on `ASC_HTTP_LISTEN` (default `127.0.0.1:8000`) with
  `ASC_HTTP_THREADS` threads. Run several web processes on different ports
  behind the proxy for capacity; the ALTCHA counter is shared through Redis.

## 4. Configuration

All variables, with required/optional notes and placeholders only:
[`deploy/staging.env.example`](../../deploy/staging.env.example). Inject them
with your secret mechanism; nothing reads a `.env` file in staging. Startup
validation refuses an incomplete or unsafe configuration and names the
variable, never its value.

Generate key material on a trusted host (never reuse local values):

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"   # DJANGO_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"   # each *_KEY_V1 HMAC/encryption key
openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt  # QR_SIGNING_KEY_V1 (store as one line with literal \n)
```

Key points the validation enforces:

* **Database:** runtime processes use `DATABASE_USER`/`DATABASE_PASSWORD` and
  refuse to start if `DATABASE_MIGRATION_PASSWORD` is in their environment.
* **Redis:** `CELERY_BROKER_URL` and `REDIS_URL` are both `rediss://`, name
  different databases, and may not weaken certificate verification. For a
  private CA append `?ssl_ca_certs=/path/ca.pem` to each URL. The broker
  verifies certificates even when the URL has no options. Redis has exactly
  these two roles here: the Celery broker (no result backend is used) and the
  ALTCHA challenge counter (use eviction policy `noeviction`). The Django cache
  is in-process and holds no shared state.
* **Scanner:** `MALWARE_SCANNER_BACKEND` is the clamd adapter (the default);
  set exactly one of `CLAMD_SOCKET_PATH` or `CLAMD_HOST`. Keep clamd on a
  private interface or socket; uploads never leave your infrastructure.
* **SMTP:** `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`,
  optional `EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` (only with TLS),
  `EMAIL_TIMEOUT_SECONDS`, `DEFAULT_FROM_EMAIL`. Plain SMTP is accepted only to
  a relay on `127.0.0.1`.
* **Ministry NIN service:** disabled by default (`DisabledNinProvider`; every
  Algerian case goes to manual review). Enable only with the owner's
  authorization, following the
  [identity provider guide](../operations/identity_provider_integration_guide.md);
  the authentication response contract (API-01) is still open, and no real
  call has been made.

## 5. PostgreSQL roles

Create two roles and a database (run as a database administrator; role and
database names are yours). This SQL is the intended shape; it was not executed
in development (no role-creation privilege there), and `audit-isolation` (§7)
is the check that proves the result.

```sql
CREATE ROLE asc_owner LOGIN PASSWORD '<migration owner password>';
CREATE ROLE asc_runtime LOGIN PASSWORD '<runtime password>';
CREATE DATABASE asc_staging OWNER asc_owner ENCODING 'UTF8';
\connect asc_staging
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO asc_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE asc_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO asc_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE asc_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO asc_runtime;
```

After the first migration (§7), restrict the audit table:

```sql
REVOKE UPDATE, DELETE, TRUNCATE ON audit_event FROM asc_runtime;
```

`DATABASE_USER` = `asc_runtime`, `DATABASE_MIGRATION_USER` = `asc_owner`.

## 6. Build the runtime artifact

Build from a pinned revision on a build host, then freeze. The review archive
of a delivery proves which source was reviewed; it is not the runtime
artifact.

```bash
git clone <private repository URL> asc-src
git -C asc-src rev-parse <reviewed tag or commit>        # record the source revision
mkdir asc-artifact && git -C asc-src archive <reviewed tag or commit> | tar -x -C asc-artifact
cd asc-artifact                                         # a clean tree: no .git, var/ or .env
uv sync --frozen --no-dev                               # locked dependencies into .venv
uv run --no-sync python scripts/check.py build-static   # collectstatic, synthetic values, no secrets
uv run --no-sync python scripts/check.py artifact-manifest --root . --output ../asc-artifact.sha256
```

Then make the tree read-only and deploy that exact tree (with its `.venv`
and `staticfiles/`) to every web, worker and beat host. Record the source
revision, the artifact SHA-256 printed above and the date. `--no-sync` keeps
`uv run` from adding the development tools to the artifact's environment. The
commands are POSIX shell; adapt them to your build host.

* A virtual environment embeds its path: create it at the path it will run
  from, or rebuild it there from the same `uv.lock`.
* `build-static` runs `collectstatic` with the staging settings and the
  synthetic, non-secret values of `scripts/check.py`; the output depends only
  on the static sources.
* Verification never modifies the artifact:

  ```bash
  uv sync --frozen --no-dev --check        # exit 0 = environment matches uv.lock; changes nothing
  python scripts/check.py artifact-verify --root <artifact> --manifest <file>
  ```

* Writable runtime paths stay outside the artifact: the beat schedule file,
  logs, temporary files. Private documents live in the S3 bucket.
* The reverse proxy serves `/static/` from the artifact's `staticfiles/`
  directory (the application does not serve static files).

## 7. Migrations

Stop web, workers and beat; take a backup and confirm it restores. Then, with
the full environment plus `DJANGO_SETTINGS_MODULE=config.settings.staging_migration`
and `DATABASE_MIGRATION_PASSWORD` (this process only):

```bash
python manage.py migrate --plan          # read the plan first
python manage.py migrate
```

Then, back in the runtime environment (`config.settings.staging`):

```bash
python scripts/check.py audit-isolation  # must exit 0: runtime role does not own or alter audit_event
python manage.py release_readiness       # records the state; BLOCKED is expected (see below)
```

* `migrate` refuses to apply migrations under the runtime settings (`--plan`
  and `--check` still work there).
* The first `migrate` also creates the operational groups and their
  permissions (`post_migrate`).
* The registration corrections of 2026-10-04 add two data migrations:
  `core.0005_approved_country_catalog` installs or reconciles the approved
  country catalog ([operator guide](../operations/country_catalog.md); confirm
  afterwards with `python manage.py reconcile_country_catalog`, which must
  print "In sync"), and `privacy.0005_official_registration_notices_v3`
  publishes the official `v3` Privacy Notice and Registration Terms and retires
  `v2-draft` without editing it.
* The attendance, staff-account and sign-in image update adds
  `accounts.0005_staff_credential_setup`, `accreditation.0006_attendance_entitlements`,
  `badges.0007_badgeissuance_attendance_marking`, `entry.0006_attendance_reason_codes`
  (choices only), `communications.0008_attendance_templates` (the
  `APPROVAL_ATTENDANCE` and `ATTENDANCE_CHANGE` messages) and the
  `django-simple-captcha` package's own `captcha.0001`/`0002` (its challenge
  table). All are additive; existing approvals stay unclassified. Install the
  locked dependencies before the new code starts. Operator steps after the
  migration: [attendance days](../operations/attendance_days.md),
  [staff accounts](../operations/staff_accounts.md). Reversing
  `communications.0008` refuses once any of its messages exists; reversing
  `accreditation.0006` deletes every attendance decision and must not be done
  while enforcement is active -- switch enforcement off instead.
* The review corrections of 2026-10-07 add `accounts.0006_operationaluser_created_by`
  (a nullable column), `accreditation.0007_attendance_enforcement_intervals`
  (the enforcement history table, rebuilt from the existing settings without
  inventing lost periods) and `people.0006_ministry_nin_diagnostics_permission`
  (one permission; `post_migrate` creates the "Integration Diagnostics
  Operators" group). All are additive. See the
  [Ministry NIN diagnostics page](../operations/ministry_nin_diagnostics.md)
  and, for the reverse proxy, the access-log requirement for staff setup links
  ([staff accounts §6](../operations/staff_accounts.md)).
* **Rollback limits.** Never reverse migrations on a database that holds test
  data without the owner's approval: reversing `people` to `0002` destroys the
  identity history, and `communications.0007` refuses to reverse once any
  `IDENTITY_REJECTION` message exists. Reversing `core` to `0003` deletes the
  staging provisioning ownership records, after which `--retire` refuses to
  touch the UAT set. Reversing `core.0005` keeps every country row (it cannot
  restore earlier names or flags), and reversing `privacy.0005` refuses once any
  acceptance or consent references a `v3` version. A message that was sent
  cannot be recalled by any rollback. Prefer a new artifact (roll forward) or a restore of the
  pre-migration backup.
* `release_readiness` reports production-release prerequisites. Expected in
  staging: `retention` BLOCKED (owner/legal inputs; `legal_notices` reports
  only whether a `[TO BE CONFIRMED]` marker remains -- the owner's general v3
  wording has none, which is not a statement of legal completeness),
  `sensitive_operation_step_up` BLOCKED without an `MFA_BACKEND`,
  `identity_verification_provider` BLOCKED while the ministry adapter is
  disabled, and `challenge_issuance_counter` READY only after a real counter
  write. These do not prevent a staging Beta; they block production.

## 8. Start and health checks

Start web, then workers, then exactly one beat. Behind the proxy set
`TRUSTED_PROXY_FORWARDING_ENABLED=True` and `TRUSTED_PROXY_CIDRS` to the
proxy's addresses; the proxy must set `X-Forwarded-Proto` and
`X-Forwarded-For`, allow request bodies of at least 16 MiB, and restrict
`/readyz` to the health checker.

| Check | Expected |
| --- | --- |
| `GET /healthz` | `200` (process alive) |
| `GET /readyz` | `200` with `"database": "reachable"`, `"storage": "reachable"`, `"challenge_counter": "shared"`; `503` when the database or storage fails |
| `python manage.py check --deploy` | no error; `entry.W001` without `MFA_BACKEND`, `people.W001` while the ministry adapter is disabled |
| `python -m celery -A config inspect ping` | every worker answers |
| `python -m celery -A config inspect registered` | lists `people.dispatch_due_identity_verification_jobs` |
| beat log | the identity sweep fires every 60 s, the outbox sweep every 5 min, the expired sign-in image sweep (`accounts.purge_expired_staff_captchas`) every 15 min |

## 9. Staging accounts and test data

```bash
python manage.py provision_staging_uat --confirm-host <staging host> \
  --email-pattern 'uat-{key}@<team-controlled domain>'            # preview
python manage.py provision_staging_uat --confirm-host <staging host> \
  --email-pattern 'uat-{key}@<team-controlled domain>' --apply    # write
python manage.py changepassword uat-reviewer@<domain>             # once per account, interactive
```

* Staging only (refused in production), preview by default, refuses to run
  unless `--confirm-host` matches `DJANGO_PUBLIC_BASE_URL`, and stops on any
  conflict without writing (exit code 1).
* Ownership: everything it creates is recorded as owned by the provisioning
  set of that `--email-pattern`. A repeat run with the same pattern reuses
  only those records. Any other existing record with a planned address or
  identifier is a conflict: an account at a planned address (ordinary or
  privileged), an edition `ASC2026-UAT2`, an organization of the same name,
  or a campaign `UAT-E1-INVITATION`/`UAT-E2-INVITATION` that this set did not
  create, or a record of another set. A provisioned membership that someone
  suspended is not granted again. Resolve a conflict by hand or choose
  another pattern; the tool never takes over such a record.
* It creates the UAT accounts of the [account matrix](../testing/phase_04_staging_uat_handoff.md)
  with unusable passwords and only their documented scoped groups; a second,
  invitation-only edition `ASC2026-UAT2`; one synthetic organization; one
  active invitation campaign per edition (the links are printed once). It
  creates no country: it checks that Algeria and France are selectable in the
  approved catalog (migration `core.0005`) and stops with a conflict otherwise.
* The external-security test account queues an account-access email to its
  test mailbox when it is created.
* Participants register themselves through the normal email-code flow.
* Clean up: `... --retire` (preview) then `... --retire --apply` with the
  same pattern. It changes only owned records: it disables the provisioned
  accounts, suspends the memberships provisioning granted (a membership
  someone else granted to a UAT account is left as it is and reported),
  revokes the provisioned links (or a link rotated from one), closes the
  provisioned campaigns and archives `ASC2026-UAT2`. It stops without writing
  when no set exists for the pattern, when a provisioned account was given
  staff or superuser rights, or when a provisioned campaign has a link that
  provisioning did not issue. Nothing is deleted, and a retired set is not
  reopened: a later UAT round needs a fresh staging database.

## 10. Short acceptance checks

1. `/healthz`, `/readyz`, `check --deploy`, `inspect ping` as in §8.
2. Scanner: `python manage.py check_malware_scanner` (PING, VERSION, a
   harmless clean buffer must be CLEAN), then
   `python manage.py check_malware_scanner --file <path> --expect infected`
   with the standard antivirus test file obtained from its official publisher.
   Output is the verdict only.
3. SMTP: request a participant code at `/accounts/start/` for a test mailbox;
   one email arrives from `DEFAULT_FROM_EMAIL`; the code works once.
4. Staff sign-in at `/accounts/ops/sign-in/` with a provisioned account:
   email, password and the characters of the security image; no MFA prompt
   ([staff sign-in image](../security/staff_sign_in_captcha.md)).
5. Upload a synthetic identity image during a test registration; staff see it
   in the identity review; with clamd stopped the upload is refused with a
   "try again later" message and nothing is stored.
6. Then run the scenarios of the [UAT handoff](../testing/phase_04_staging_uat_handoff.md)
   and the [registration corrections checklist](../testing/registration_corrections_staging_checklist.md).

## 11. Recovery

| Situation | Action |
| --- | --- |
| Web or worker crash | Restart it; work is durable in PostgreSQL (outbox, identity jobs with leases). |
| Broker down | Submissions continue; jobs and messages wait. After recovery, beat re-dispatches; `python manage.py run_identity_verification_jobs --limit 50` drains identity jobs by hand. |
| Counter Redis down | `/readyz` shows `degraded`; a stricter per-process limit applies; alerts `HUMAN_CHECK_COUNTER_DEGRADED`/`RECOVERED` on logger `asc2026.ops.alerts`. |
| clamd down | Uploads are refused with a retry message; nothing is stored; restore clamd. |
| SMTP failing | A failure the server confirmed (an SMTP refusal reply, or no connection, greeting, TLS or sign-in) is retried up to 3 times, then `FAILED`. Any other failure while sending (a lost connection, a timeout waiting for the server's final answer) may follow acceptance: the attempt is recorded with response code `UNCONFIRMED:<error>` and the message stays in `SENDING`. A worker stopped during the call also leaves `SENDING`, with no attempt. A message in `SENDING` is **never re-sent automatically**: check the mailbox and the SMTP server's own log, record the outcome, and escalate to the owner (open decision COMM-01). Do not edit rows or re-send by hand. Communication operators see message status at `/ops/communications/`. |
| Ministry adapter misbehaving | Set `NIN_PROVIDER_BACKEND=apps.people.nin_provider.DisabledNinProvider` and restart; cases go to manual review. |
| Bad release | Roll forward with a new artifact, or restore the pre-migration backup (see the rollback limits in §7). |

## 12. Related documents

* [Attendance days, opening-day capacity and enforcement](../operations/attendance_days.md)
* [Staff accounts and scoped access](../operations/staff_accounts.md)
* [Staff sign-in security image](../security/staff_sign_in_captcha.md)
* [Beta release notes and verification summary](beta_release_notes.md)
* [Beta backlog and open decisions](beta_backlog.md)
* [Manual GitHub handoff for the owner](github_handoff.md)
* [UAT handoff: account matrix, live checks, go/no-go](../testing/phase_04_staging_uat_handoff.md)
* [Registration corrections: coverage matrix and checks RC-01 to RC-14](../testing/registration_corrections_staging_checklist.md)
* [Country catalog: install and reconcile](../operations/country_catalog.md)
* [Identity UAT scenarios](../testing/phase_04_idv_manual_uat_guide.md)
* [Architecture decisions](../architecture/)
