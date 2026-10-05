# Staging Beta release notes

Release: staging Beta 1 (2026-10-02). Scope: a deployable staging build for
Beta testing. It is not a production release; the production prerequisites in
the [UAT handoff](../testing/phase_04_staging_uat_handoff.md) §4.E remain open.

## What this release adds for deployment

* An HTTP entry point, `python -m config.serve` (waitress), and entry points
  that refuse to start without an explicit settings module.
* Separate runtime and migration settings modules; the migration-owner
  password is used only by the migration process, and `migrate` refuses the
  runtime settings.
* A ClamAV `clamd` scanner adapter (`INSTREAM`, Unix socket or private TCP,
  bounded timeouts and stream size) with a fail-closed result contract and an
  operator check, `manage.py check_malware_scanner`. An upload that cannot be
  scanned is refused with a "try again later" message (EN/FR/AR) and nothing is
  stored.
* SMTP configured from `EMAIL_*` and `DEFAULT_FROM_EMAIL`, with TLS rules and a
  bounded timeout.
* Certificate verification for a `rediss://` Celery broker.
* `manage.py provision_staging_uat` for the UAT accounts, scopes and synthetic
  test data, with preview, host confirmation, conflict stop, repeat safety and
  retirement.
* Build tooling: `scripts/check.py build-static`, `artifact-manifest`,
  `artifact-verify`; a placeholder-only configuration template
  `deploy/staging.env.example`, checked by `scripts/check.py local-safety`.
* A fix: the Registration Intake group now receives its permissions on the
  first `migrate` of a fresh database.

## Correction 1 (2026-10-02)

Two findings of the independent review are fixed. Nothing else changed.

* **No automatic re-send after an ambiguous SMTP outcome.** Before, every
  SMTP exception was a retryable failure, so a server that accepted the
  message and then lost or delayed its final answer could receive it again
  from the next task or sweep. Now only a confirmed failure is retried (an
  SMTP refusal reply, or a failure before the message was sent). Any other
  failure while sending is recorded as an unconfirmed attempt
  (`UNCONFIRMED:<error>`), the message stays in `SENDING`, and no task or
  sweep sends it again; it is escalated under COMM-01.
* **Provisioning proves ownership before reuse or retirement.** Before, an
  existing account at a planned address could be given UAT memberships, and
  `--retire` disabled any account matching the pattern, suspended all of its
  memberships and closed or archived records by fixed identifier. Now every
  record provisioning creates is recorded as owned by its provisioning set
  (new migration `core.0004_staging_provisioning_ownership`, two tables).
  Apply reuses only owned records and stops, writing nothing, on any other
  record with a planned address or identifier. Retire changes only owned
  records and stops on an unsafe state.

Decisions: [ADR-0027](../architecture/ADR-0027-staging-beta-deployment-contract.md).
Procedure: [deployment guide](README.md).

## Registration corrections (2026-10-04)

Six owner-reported registration defects are corrected, and the registration
lifecycle was reviewed for release blockers. Owner decisions: decision gate
§17.12. Live checks: [registration corrections checklist](../testing/registration_corrections_staging_checklist.md)
(RC-01 to RC-14, all NOT_EXECUTED).

* **Country catalog (C-01).** The owner-approved catalog of 248 countries and
  territories, with English, French and Arabic names, Palestine as `PS`, and
  `IL` and `XK` never selectable. Migration `core.0005_approved_country_catalog`
  installs it on a fresh database and reconciles an existing one (approved
  names, reactivation, other codes made inactive, nothing deleted, every
  existing reference kept); `manage.py reconcile_country_catalog` checks or
  repairs it later. Staging provisioning no longer creates countries. The phone
  calling-code list leaves out the seven territories without a calling code.
  [Operator guide](../operations/country_catalog.md),
  [ADR-0028](../architecture/ADR-0028-approved-country-catalog.md).
* **Profile photograph required.** It was shown as optional and only the final
  submission asked for it. The professional step now marks it required and
  refuses to continue without an eligible photo (or one already on file); the
  review step says when it is missing; the submission guard is unchanged.
* **Passport copy.** The passport route already required the identity page,
  whatever the residence, but its explanation and errors were English in French
  and Arabic. They are translated, the route texts say that Algerians living
  abroad stay on the NIN route and that foreign nationals need the page wherever
  they live, and only an eligible document counts.
* **Eligible documents.** A photo or passport page counts only when it is
  active, scanned clean, of the right type and purpose, and uploaded by the
  registration's own participant. Before, the check did not look at the
  uploader or the purpose (no screen could create such a file; the rule is now
  explicit and tested).
* **Official footer.** Exactly "Ministère de l'Économie de la Connaissance, des
  Start-up et des Micro-entreprise" and, smaller, "Direction des Systèmes
  d’Information (DSI)", in every language and layout (supersedes the empty
  footer of A-10; no link returns).
* **Official notices.** Version `v3` of the Privacy Notice and Registration
  Terms (`privacy.0005`) replaces `v2-draft`, which is retired, not edited; every
  earlier acceptance keeps its version. v3 removes the draft banner, uses the
  owner-supplied French ministry name and states the photo and passport-copy
  rules. The legal facts the owner has not supplied keep their "to be
  confirmed" markers (listed in the backlog); they still block production. The
  notices step now records acceptance only for the version it displayed: when a
  newer one was published meanwhile, it shows the page again and records nothing.
* **Register again after withdrawal.** A withdrawn registration offers the
  origin-aware next step of IDV-Q19: a new public draft while public
  registration is open, a new draft through the same still-valid invitation, or
  the organization or team to contact. The withdrawn registration stays
  withdrawn with its history; only its context is released. Nothing is carried
  over to the new draft. Staff can no longer reopen a withdrawn registration that
  a newer one replaced.
* **Found in the lifecycle review and fixed:** a wizard address opened with a
  pending invitation of a campaign the participant already had a registration
  in failed with a server error (duplicate context); it now resumes that
  registration. A direct withdrawal request on a registration closed by a final
  identity rejection overwrote its status; it is now refused. The participant
  identity-correction page and workspace strings are translated in French and
  Arabic.

Migrations: `core.0005` and `privacy.0005`, both data only (no schema change);
rollback limits in the deployment guide §7. No dependency change.

Local verification of this package (2026-10-04):

| Item | Result |
| --- | --- |
| New regression tests on the unchanged baseline tree | 77 failed, 25 passed, 1 module not importable (the catalog mechanism did not exist): the defects reproduced; the 25 that passed confirm the rules that were already enforced (the passport route itself) |
| The same test files on the corrected tree | 115 passed |
| Full local regression gate (`scripts/check.py all`) | PASSED: main suite 4527 passed, 6 skipped (the same opt-in skips as before); performance harness 2 passed; browser suite 285 passed; format, lint, safety, credential scan, asset checksums, database checks, migrations, deploy checks and dependency audit OK. A first attempt was interrupted by a host sleep (one browser test lost its network, `ERR_NETWORK_IO_SUSPENDED`; it passes alone) and was rerun unchanged |
| Upgrade of an existing database | A disposable database migrated and filled with the baseline code (submitted, withdrawn and draft registrations, v2 acceptances, active `IL`/`XK`/unlisted rows, a stale label) was migrated with this package: catalog in sync, history kept, v3 current, v2 acceptances unchanged, the photo rule enforced on the open draft, the withdrawn registration registered again; the database was then dropped |

Reported separately as before: `audit-isolation` fails locally by design (one
database role) and release readiness is BLOCKED.

## Local verification

The full local regression gate (`uv run --env-file .env python scripts/check.py all`)
passed in one attempt on the correction 1 tree (on the Beta 1 tree before it:
4377 passed, 6 skipped):

| Item | Result |
| --- | --- |
| Format, lint, local safety, credential scan, vendored asset checksums | OK |
| Database probe, Django check, missing-migration check | OK |
| Main suite | 4410 passed, 6 skipped |
| Performance harness (provisional) | 2 passed |
| Browser suite (Playwright, Chromium) | 279 passed |
| Static deploy checks (staging, production and both migration modules) | OK |
| Dependency audit (`pip-audit`) | no known vulnerabilities |

The six skips are the opt-in Redis integration tests (they need a disposable
test Redis, `ASC_TEST_REDIS_URL`) and the Unix-socket clamd protocol test (no
Unix socket server on the Windows development host; it runs on Linux).

Reported separately, as designed: `audit-isolation` fails locally because the
development database has a single owning role (it must pass in staging), and
release readiness is BLOCKED (see the deployment guide §7 for the expected
staging values).

## What local tests do not prove

The clamd adapter was tested against a simulated clamd server, SMTP against a
local fake SMTP server (including a server that accepts a message and then
loses or delays its final answer), and PostgreSQL role separation only as
configuration.
Real SMTP delivery, the real clamd daemon, S3 storage, the broker and counter
Redis, PostgreSQL role isolation, the reverse proxy and the ministry NIN
service are proven only by the live checks of the
[UAT handoff](../testing/phase_04_staging_uat_handoff.md).
