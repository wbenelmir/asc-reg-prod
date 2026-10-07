# Production release runbook — attendance days, staff accounts, sign-in image, review A01 corrections

Direct manual release after independent review and the owner's LOCAL
acceptance. No staging retest is part of this release. Follow the project's
deployment guide (`docs/deployment/README.md`) for everything not repeated
here; replace `staging` by `production` in its settings module names
(`config.settings.production`, `config.settings.production_migration`).
Production runtime checks after the release verify configuration, startup and
service availability; they are not feature acceptance (that was LOCAL).

Values the deployment team or the owner must supply (none is set by the
release): the real conference days, the opening-day capacity, the edition
timezone (already on the edition), the first account administrator's email,
the technical operator(s) for the Ministry NIN diagnostics, and an authorized
Ministry test identity number for the live check (§9).

## 0. Before the window

1. Confirm the reviewed source revision and build the artifact from it
   (`uv sync --frozen --no-dev`, `scripts/check.py build-static`,
   `artifact-manifest`). The lock includes `django-simple-captcha==0.7.0`
   and `django-ranged-response==0.2.0` (Pillow was already present). Static
   assets are all inside `staticfiles/`: `js/asc-captcha.js`, the updated
   `css/asc-ui.css` and `js/entry-offline.js`, and the conference artwork
   `img/brand/asc-conference-lockup.png` (already part of the project; now
   shown in the sign-in panels). Compiled French and Arabic catalogs
   (`locale/*/LC_MESSAGES/django.mo`) are part of the source.
2. Agree on the attendance values with the owner (days, capacity), on the
   first account administrator and on the diagnostics operator(s). Do not
   invent them.
3. Reverse proxy (and any load balancer, CDN or WAF in front of the
   application): apply the access-log masking for staff setup links (§3)
   BEFORE the first link is sent.
4. If offline entry continuity is enabled for the event (normally it is not),
   plan a device refresh after enforcement activation (§7).

## 1. Backup

Stop web, workers and beat. Take a full PostgreSQL backup and confirm it
restores (the deployment guide's §7). Record its identifier.

## 2. Migrations (compatible, additive)

With `DJANGO_SETTINGS_MODULE=config.settings.production_migration` and the
migration password (that process only):

```bash
python manage.py migrate --plan
python manage.py migrate
```

Expected new migrations:

| Migration | Effect |
| --- | --- |
| `accounts.0005_staff_credential_setup` | setup-link table, `manage_operational_accounts` permission |
| `accounts.0006_operationaluser_created_by` | nullable column (who created an account in the staff area) |
| `accreditation.0006_attendance_entitlements` | attendance policy and entitlements |
| `accreditation.0007_attendance_enforcement_intervals` | enforcement history table; rebuilt from the existing switch fields (no row when enforcement was never switched on) |
| `badges.0007_badgeissuance_attendance_marking` | marking columns (database default) |
| `entry.0006_attendance_reason_codes` | choices only |
| `communications.0008_attendance_templates` | `APPROVAL_ATTENDANCE`, `ATTENDANCE_CHANGE` |
| `people.0006_ministry_nin_diagnostics_permission` | one permission |
| `captcha.0001_initial`, `captcha.0002_alter_captchastore_id` | the image challenge table |

`post_migrate` creates the groups "Account Administrators", "Attendance Policy
Managers" and "Integration Diagnostics Operators"; Accreditation Managers and
Coordinators gain the attendance permissions. No existing approval is
assigned any attendance days.

Optional settings (defaults shown; set only to change them, identically in
web, worker and beat): `NIN_DIAGNOSTICS_MAX_PER_OPERATOR=5`,
`NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT=20`, `NIN_DIAGNOSTICS_WINDOW_SECONDS=600`.
No other new environment variable.

Then with the runtime settings:

```bash
python scripts/check.py audit-isolation
python manage.py check --deploy
python manage.py release_readiness
```

`check --deploy` must show no `accounts.E00x` error (they refuse
`ATOMIC_REQUESTS`, `CAPTCHA_TEST_MODE` and a mounted `captcha.urls`); the
runtime settings validation also refuses those at startup.
`release_readiness` stays BLOCKED (retention, identity provider and other
owner inputs). Its `legal_notices` item reports only that the owner's general
v3 wording carries no `[TO BE CONFIRMED]` marker: that is not a statement of
legal completeness (see the `privacy.0005` module docstring).

## 3. Reverse proxy: never log a setup link (deployment team)

A staff setup link carries a single-use secret in the path of its first
request, `/accounts/setup/<secret>/`. The application masks it in its own logs
and writes no access log (waitress); the proxy is the remaining place it could
be stored. Configure every proxy, load balancer, CDN or WAF in front of the
application to either log the path with that segment masked, or not log that
path at all, and never to log the `Referer` of such a request unmasked. An nginx
example (`map` + `log_format`, never `$request`) is in
`docs/operations/staff_accounts.md` §6; adapt it to the proxy actually used.

Verify after the change, with a deliberately invalid link (never a real one):
open `https://<host>/accounts/setup/proxy-log-check-0000000000/` (expect the
"link not valid" page) and confirm the proxy's access log shows
`***REDACTED***` (or no entry) instead of the last path segment.

Verified in LOCAL: the application redaction (tests) and the example's
regular expressions against synthetic paths (`logs/a01/r02_nginx_map_check.log`;
nginx itself was not run). The proxy configuration is the deployment team's
responsibility and must be verified on the real proxy.

## 4. Restart

Start web, then workers, then exactly one beat. Health checks as in the
deployment guide §8; the beat log now also shows
`accounts.purge_expired_staff_captchas` every 15 minutes.

Immediate behaviour after restart:

* Staff sign in with email, password and the security image. Existing staff
  sessions stay valid until they expire normally.
* Approvals now require an attendance choice; they are refused until §6
  configures the days and the capacity.
* Admission is unchanged until enforcement is activated (§8).

## 5. First account administrator (once)

With the runtime settings, on the server:

```bash
python manage.py bootstrap_account_administrator --email <agreed address> --display-name "<name>"
```

The setup link is emailed to that address (single use, 48 h by default). Use
`--deliver terminal` only if the email cannot be received; the link is then
printed once and must not be stored. The command is refused once an
administrator with full scope exists.

**If the command reports that the setup link could not be emailed** (SMTP
failure), the account exists but that link was revoked and the failure was
audited (`ACC_STAFF_CREDENTIAL_SETUP_DELIVERY_FAILED`, error class only).
Recover with the SAME address, by email once SMTP works, or on the terminal:

```bash
python manage.py bootstrap_account_administrator --email <same address> --resend
python manage.py bootstrap_account_administrator --email <same address> --resend --deliver terminal
```

`--resend` works only for that still-pending first administrator (no
password set, still active with full scope, no other administrator with a
password). It never creates or promotes an account; each new link replaces the
previous one. Audited as `ACC_STAFF_ACCOUNT_ADMINISTRATOR_SETUP_RESENT`.

The administrator then signs in and, at `/ops/staff-accounts/`, grants at
least one Attendance Policy Manager for the edition, checks the Accreditation
Managers who decide, and grants the **Integration diagnostics operator** role
("Every event edition", every organization) to the agreed technical
operator(s), preferably with an end of validity. A scoped account administrator
reads only the accounts and roles inside their scope.

## 6. Attendance configuration

An Attendance Policy Manager opens `/ops/attendance/` → the edition:

1. enter the opening day, the second and the third day (local dates) and the
   opening-day capacity, with a reason; save. Do this BEFORE the first
   approval: once any registration has days, the days cannot change (every
   first grant is serialized with a change of days).
2. check the advisory item "edition dates contain the three days". If the
   edition's own start/end dates are wrong, correct them through the agreed
   data-fix procedure before activating (they also drive device expiry and
   the minimum-age rule).

From now on approvals work and allocate opening-day places under the capacity.

## 7. Legacy classification and reconciliation

1. `/ops/attendance/<edition>/registrations/` lists every approved, current
   registration without days ("Not yet classified", with counts). An
   Accreditation Manager chooses the days for each one, with a reason. Each
   participant receives one notification with the dates. Nothing is
   classified automatically.
2. Opening-day places allocated must stay within the capacity (the page
   refuses an upgrade beyond it).
3. Physical badges already handed over: filter "Badge marking to record". For
   each, apply the sticker/overlay matching the shown days and record it on
   the badge page.
4. Digital passes need no replacement: admission reads the current days and
   the pass pages render them live. Participants who printed their pass
   before classification should reprint it (their notification says the pass
   shows the same days).
5. Offline packages: activation (§8) revokes the current ones; enabled
   devices must download new offline data before working offline.

## 8. Enforcement activation

On the edition's attendance page the checklist must show every required item
met (days, capacity, all classified, within capacity, badge markings). Then
"Activate enforcement". From then on admission refuses a two-following-days
participant on the opening day, any unclassified approval, and any day that
is not one of the three (never overrideable); all existing checks still apply.
Each activation and switch-off is kept as a period; synchronized offline
admissions are judged by the period that covered their own time.

Production checks after activation (configuration/availability, not feature
acceptance): the page shows "Admission enforcement active"; an audit event
`ACC_ATTENDANCE_ENFORCEMENT_ACTIVATED` exists; `/readyz` is healthy.

## 9. Ministry NIN service: live check (deployment team)

Only after the official adapter is configured (`NIN_PROVIDER_BACKEND` and the
`MINISTRY_NIN_API_*` settings, deployment guide), by a technical operator
holding the Integration diagnostics operator role, with an authorized test
number supplied by the Ministry. Page: `/ops/integrations/ministry-nin/`
(operator guide: `docs/operations/ministry_nin_diagnostics.md`).

1. Configuration shows "Settings complete" and the official adapter (no
   request is sent by opening the page).
2. "Test authentication" → expect "Authentication succeeded" (a fresh request,
   never the cached token). HTTP 400 (`auth_unexpected_status`) or 401/403:
   stop; check the authentication contract and credentials with the provider.
3. "Test lookup" with the authorized test number → expect "identity found" (or
   "no identity found" = HTTP 200 with `identite: null`, which is a working
   service). HTTP 301 (`redirect_refused`): correct `MINISTRY_NIN_API_BASE_URL`
   or the configured path (trailing slash) after confirming the canonical
   address with the provider; never enable redirect following or disable TLS
   checks.
4. Report outcome codes, HTTP statuses and durations only — never the number,
   a token, a credential or identity data.

The diagnostics never create or change a registration, an identity, a decision,
a pass, a badge or a notification. In LOCAL they were tested with a scripted
transport only; no Ministry request was made while preparing this release.

## 10. Rollback

* **Prefer switching enforcement off** (attendance page, with a reason):
  admission stops checking days; days, capacity, places and the enforcement
  history are kept; offline packages are revoked again. This is safe at any
  time and keeps seat and badge-marking consistency.
* **Code rollback while the schema stays**: the previous release does not know
  the new tables and keeps working on the old ones. It would approve without
  attendance days (those approvals then appear as unclassified after
  re-upgrading), would use the plain sign-in without the image, would show
  every staff account to every account administrator again (the R01 scope),
  and would judge synchronized offline admissions with the last switch only
  (the earlier periods stay stored and are used again after re-upgrading; a
  switch-on made by the previous release counts as enforced from its
  activation, and switching it off later records that period).
  Only roll code back with enforcement switched off first, otherwise
  rolled-back admission would silently ignore the opening-day restriction that
  participants and badges already reflect. The columns added to existing
  tables are nullable or have database defaults
  (`badges_badge_issuance.attendance_marking`,
  `accounts_operational_user.created_by_id`), so the previous release keeps
  working on them. Let the outbox drain of `APPROVAL_ATTENDANCE` /
  `ATTENDANCE_CHANGE` messages first (the previous release does not know those
  purposes). Reasoned only; not exercised in LOCAL.
* **Do not reverse the data migrations blindly.** Reversing
  `accreditation.0006` deletes every attendance decision and opening-day
  allocation (participants have already been told their days); reversing
  `accreditation.0007` deletes the enforcement history (earlier periods could
  then no longer be told apart); `communications.0008` refuses to reverse once
  any of its messages exists; reversing `accounts.0005` deletes setup links;
  reversing `captcha` drops only transient challenges. If a reversal is
  unavoidable, restore the §1 backup instead and tell affected participants
  through the communications team, because sent messages cannot be recalled.
* Credentials: setup links are single use and short-lived; after a rollback
  that removes them, administrators simply send new ones. A first
  administrator stranded by an email failure is recovered with `--resend`
  (§5) on the new release; the previous release has no such command, so do
  not roll back while the first administrator is still pending.

## 11. Known limitations to carry into operations

* The sign-in image has no non-visual alternative; staff who cannot read it
  need assistance from an administrator (password reset is not a substitute).
  No accessibility conformance is claimed for it.
* The attendance days of an edition cannot change once any registration has
  days; a correction needs a reviewed data fix.
* The notices published as `v3` are the owner's general wording: they omit the
  controller identity, contact, statute references and retention facts.
  `release_readiness` no longer flags them by marker; legal completeness
  remains an owner/legal decision, and retention remains BLOCKED.
* The setup-link masking in proxy logs depends on the proxy configuration (§3).
* Real SMTP delivery, the Redis counter and the Ministry service were not
  exercised in LOCAL; the Ministry live check (§9) is the deployment team's.
