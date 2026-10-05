# ADR-0027: Staging Beta deployment contract

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-10-02 |
| Related | TRD §5, §15; ADR-0008 (audit isolation), ADR-0009 (Redis/Celery boundary), ADR-0013 (private storage), ADR-0025 (challenge counter), ADR-0026 (identity verification); [deployment guide](../deployment/README.md) |

## Context

The application was complete for a staging Beta but not deployable by an
independent operations team: no application server was declared, the
migration-owner credentials had no supported use, document uploads had only
test stubs for malware scanning, email could reach only a local relay, there
was no way to create the UAT accounts in staging, and several entry points
silently defaulted to the local development settings.

## Decisions

1. **Application server.** waitress (pure Python, any OS) through the
   project's own entry point `python -m config.serve`. It requires an explicit
   `DJANGO_SETTINGS_MODULE`, sends no `Server` header and leaves
   `X-Forwarded-*` headers to the application's own trusted-proxy guard
   (`clear_untrusted_proxy_headers=False`), so there is one trust decision.
   `config.wsgi`, `config.asgi` and the Celery app no longer default to the
   local settings.
2. **Database identities.** The shared deployed configuration lives in
   `config/settings/_deployment.py`. Runtime modules (`staging`, `production`)
   connect as the restricted runtime role and refuse to start when
   `DATABASE_MIGRATION_PASSWORD` is present. Migration modules
   (`staging_migration`, `production_migration`) connect as the migration
   owner and do not need the runtime password. `manage.py migrate` refuses to
   apply migrations under a runtime module. Audit isolation is still proven
   after the migration by `scripts/check.py audit-isolation` (ADR-0008).
3. **Malware scanning.** `apps.documents.scanning.ClamdScanner` implements the
   clamd `INSTREAM` protocol over a Unix socket or private TCP, with bounded
   connect and exchange timeouts, a bounded stream size and strict answer
   parsing. Only `stream: OK` is clean; `FOUND` is a positive result; anything
   else raises `ScannerUnavailable`, the upload is refused with a retry
   message and nothing is stored. Staging and production accept only this
   adapter; the stubs are refused at runtime outside local and test settings.
   `manage.py check_malware_scanner` is the live operator check.
4. **Email.** Django's SMTP backend configured from `EMAIL_*` and
   `DEFAULT_FROM_EMAIL`. Validation requires TLS (STARTTLS or implicit) for any
   non-loopback server and for any authenticated exchange, a bounded timeout
   and a real sender. Only a failure the server confirmed is retried, within
   the bounded policy: an explicit SMTP refusal, or a failure before the
   message was sent (no connection, greeting, TLS or sign-in). Any other
   failure while sending, such as a lost connection or a timeout while waiting
   for the final answer, may follow the server's acceptance: the attempt is
   recorded as unconfirmed, the message stays in `SENDING`, and nothing sends
   it again automatically (COMM-01 open). Django's mail API does not report
   the SMTP stage, so this is deliberately conservative.
5. **Broker TLS.** A `rediss://` broker verifies certificates by default
   (`CELERY_BROKER_USE_SSL`); options in the URL that weaken verification are
   refused, as for the counter Redis.
6. **Staging provisioning.** `manage.py provision_staging_uat`: staging only,
   preview by default, host confirmation, conflict-stop, repeatable, no
   passwords (accounts are created with unusable passwords; operators use
   `changepassword`), only documented scoped groups, synthetic reference data
   only (since 2026-10-04 no country at all: the countries come from the
   approved catalog of ADR-0028, which provisioning only checks), and a
   `--retire` path that deletes nothing. Every account,
   membership, organization, event edition, campaign and link it creates is
   recorded as owned by its provisioning set (`core.StagingProvisioningSet`,
   one per `--email-pattern`). A repeat run reuses only owned records;
   `--retire` changes only owned records; any other record with a planned
   address or identifier is a conflict that stops the run before it writes.
7. **Build.** The runtime artifact is built from a pinned revision with locked
   dependencies and collected static files before it is frozen and hashed
   (`scripts/check.py build-static`, `artifact-manifest`, `artifact-verify`).
   Verification and startup do not modify it.

## Consequences

* Operators choose the OS, service manager, reverse proxy and infrastructure;
  the repository supplies a portable contract and verification commands.
* Real integrations (SMTP, clamd, S3, Redis, PostgreSQL role isolation, the
  ministry service) remain unproven until the live checks of the UAT handoff
  are run; passing configuration checks are not proof.
* The registration-intake group bootstrap now runs on every app's
  `post_migrate`, so the group has its permissions after the first `migrate`
  of a fresh database (previously it stayed empty until a second run).
