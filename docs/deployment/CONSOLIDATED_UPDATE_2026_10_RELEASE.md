# Release procedure — consolidated update 2026-10-12 (UPD-01 to UPD-09)

> A later update, IDV-FIRST (2026-10-16), builds on this release and needs
> `people.0007` from it. Its procedure is in
> [CURRENT_UPDATE.md](CURRENT_UPDATE.md). This document is kept as written for
> the 2026-10-12 release.

Registration is open in production and real applications exist. This release
must preserve every registration, account, identity record, document,
submission, decision, invitation, badge and audit event. Follow the deployment
guide (`docs/deployment/README.md`) for everything not repeated here
(production settings modules: `config.settings.production`,
`config.settings.production_migration`).

Feature notes:

* `docs/operations/identity_review_corrections.md` (UPD-01, UPD-02, UPD-03);
* `docs/operations/ministry_nin_diagnostics.md` (UPD-04);
* `docs/operations/registration_list_and_statistics.md` (UPD-05, UPD-08);
* `docs/operations/organizations_and_accreditation_settings.md` (UPD-06, UPD-07);
* `docs/operations/google_analytics.md` (UPD-09).

What the release contains:

* code, templates, static files (`static/css/asc-ui.css`, `static/js/asc-ui.js`,
  `static/js/asc-analytics.js`) and compiled translations
  (`locale/*/LC_MESSAGES/django.mo`);
* production settings (UPD-09): `config/settings/_analytics.py` (new) and
  `config/settings/production.py`. The production Content Security Policy
  gains `https://*.google.com` in `connect-src` (Google's documented source
  for GA4 collection); everything else in it is unchanged;
* four additive migrations:

  | Migration | Change |
  | --- | --- |
  | `people.0007_upd_2026_10_identity_corrections` | four columns on `IdentityDecision` (participant note, notification language and its basis, encrypted correction changes), all with a database default `''`; two new choice values (no data change) |
  | `registrations.0011_registration_submission_language` | `Registration.submission_language`, database default `''` |
  | `communications.0009_identity_correction_request_template` | data: the `IDENTITY_CORRECTION_REQUEST` message template in English, French and Arabic |
  | `exports.0002_export_campaign` | nullable `ExportRequest.invitation_campaign` |

* no new environment variable, dependency, role or scheduled task. The
  existing optional `ANALYTICS_GA4_MEASUREMENT_ID` is now documented
  (deployment guide §4): leave it unset in production to use `G-FDXGG6H52P`;
  an empty value switches analytics off.

Nothing in the migrations or start-up changes a registration, an identity
result or a decision. No data backfill is required.

## 1. Before the window

1. Build the artifact from the reviewed revision (`uv sync --frozen --no-dev`,
   `scripts/check.py build-static`, `artifact-manifest`). `build-static` runs
   `collectstatic`; the CSS and JS changed, and the stylesheet link carries a
   new version (`?v=1.2-upd-1012`), the analytics script `?v=2`.
2. The `.mo` files are part of the artifact; no `compilemessages` is needed on
   the server.

## 2. Backup (mandatory)

1. Stop web, workers and beat.
2. Take a full PostgreSQL backup and confirm it restores into a scratch
   database. Record its identifier.
3. Record the baseline counts (aggregates only), as in
   `docs/deployment/REVIEW_QUEUE_RELEASE.md` §2.4, plus
   `IdentityDecision.objects.count()` and
   `CommunicationMessage.objects.count()`.

## 3. Migrate and deploy

1. `python manage.py migrate` under `config.settings.production_migration`.
   Expect exactly the four migrations above. `python manage.py showmigrations
   people registrations communications exports` must then show them applied.
2. Deploy the code and static files; restart web, workers and beat.
3. `python manage.py check --deploy` under the runtime settings module.

## 4. Reconciliation (read-only)

* Repeat the §2.3 counts: registrations, submissions, identity verifications,
  documents, decisions and review cases are **identical** (plus genuinely new
  activity during the window). `CommunicationMessage` changes only by new
  activity. One new `MessageTemplate` (`IDENTITY_CORRECTION_REQUEST`).
* UPD-01 needs no data change: the identity queues are computed from the
  registration status, so withdrawn and cancelled registrations leave the
  actionable tabs as soon as the code is live.
* Before this release staff could reopen a withdrawn or cancelled
  registration. To list registrations that were reopened after a withdrawal
  or cancellation (read-only; references only), under the runtime settings
  module in `python manage.py shell`:

  ```python
  from apps.audit.models import AuditEvent
  from apps.registrations.models import Registration

  closed = AuditEvent.objects.filter(
      action_code__in=["REV_PARTICIPANT_WITHDRAWAL", "REV_OPERATIONAL_CANCELLATION"]
  )
  suspects = []
  for event in AuditEvent.objects.filter(action_code="REV_REGISTRATION_REOPENED"):
      if closed.filter(target_uuid=event.target_uuid, occurred_at__lt=event.occurred_at).exists():
          suspects.append(event.target_uuid)
  rows = Registration.objects.filter(pk__in=suspects).values_list("public_reference", "public_status")
  print(len(suspects), sorted(rows))
  ```

  If the list is not empty, give it to the owner: each case was a staff
  decision and is reviewed individually from the Review queue. Nothing is
  changed automatically.

## 5. Smoke tests (after restart)

As the superadmin, then as a reviewer and an intake staff member with a
limited scope:

1. `/ops/identity/`: the status counters add up; "Closed registrations" lists
   withdrawn or cancelled registrations and their case pages offer no action.
2. A case in manual review shows "Save corrections and verify identity" and
   the submission language; "Return for correction" shows "Note to
   participant".
3. `/ops/integrations/ministry-nin/` (diagnostics operator): configuration
   check only; run a lookup only with an authorized test number (see that
   page's guide §5).
4. `/ops/registrations/`: the counter "N matching of M in your scope" and
   page 2 are reachable; a name search works and its text is not in the URL.
5. `/ops/statistics/`: the page loads with a generation time; one XLSX export
   downloads; the PDF button opens the print view. Repeat in Arabic (RTL).
6. `/ops/organizations/` (superadmin): list, open an organization, copy a
   campaign link. Do not create test organizations in production.
7. `/ops/accreditation/settings/`: the event's settings and counts are shown.
8. Participant: sign in with e-mail code; the dashboard and an existing
   registration load.
9. Google Analytics: the smoke test of `docs/operations/google_analytics.md`
   §5 (rendered tag, CSP header, no request before consent, loader and one
   collection request after "Accept analytics", nothing after "Decline",
   nothing on staff pages, then GA4 Realtime). Use a normal browser profile
   without blockers; do not send test events from scripts. The owner applies
   the GA4 account settings of §4 of that note.

## 6. Rollback

* **Code rollback while the schema stays**: the added columns have database
  defaults or are nullable, so the previous release keeps inserting and
  reading those tables. Let the outbox drain of `IDENTITY_CORRECTION_REQUEST`
  messages first (the previous release does not know that purpose). Decisions
  of type "corrected and verified" and revisions from "staff identity
  correction" stay valid history; the previous release shows their raw codes.
  The previous release would again allow reopening withdrawn registrations
  and would show closed registrations in the identity tabs. Reasoned only;
  not exercised in a deployed environment.
* **Analytics only:** to stop Google Analytics without a code rollback, set
  `ANALYTICS_GA4_MEASUREMENT_ID` to an empty value and restart web; the
  banner, the script and the Google CSP sources disappear.
* **Do not reverse the migrations blindly.** Reversing `people.0007` drops
  participant notes and correction details; `communications.0009` refuses to
  reverse once any of its messages exists. If a reversal is unavoidable,
  restore the §2 backup instead, with the owner's decision, knowing it loses
  every submission made after it; sent messages cannot be recalled.
