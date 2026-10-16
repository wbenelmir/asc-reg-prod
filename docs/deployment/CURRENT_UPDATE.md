# Current update: IDV-FIRST (2026-10-16)

This is the entry point for the deployment team for the identity review
update of 16 October 2026. It adds to the general
[deployment guide](README.md) and follows the same conventions as the
[production release runbook](PRODUCTION_RELEASE_RUNBOOK.md) and the
[consolidated update 2026-10-12 procedure](CONSOLIDATED_UPDATE_2026_10_RELEASE.md).
Anything not repeated here (artifact build, migration credentials, health
checks, recovery) is as described in the deployment guide.

Registration is open in production and real applications exist. This update
must preserve every registration, account, identity record, document,
submission, decision, badge and audit event. It changes no record by itself.

## 1. Scope of the release

1. **Verification and correction from the first manual review, without a
   document.** On an active registration whose identity case is in manual
   review, a reviewer who may verify identities can use **Verify identity**
   and **Save corrections and verify identity** immediately, without first
   returning the case to the participant or asking for an upload. Given
   names, family name and date of birth can be corrected; so can the NIN,
   for reviewers who hold the NIN correction permission. When no reviewable
   document exists, or the reviewer does not rely on one, the choice **No
   identity document: manual reviewer decision** is used.
2. **Mandatory internal justification.** A verification without a document
   requires a no-document reason and an internal justification of at least
   10 characters. The justification is never shown to the participant and
   never written to the audit log. The identity is recorded as **Manual
   reviewer decision (no document)** (`MANUAL_REVIEWER_DECISION`), never as a
   document review or a Ministry verification. A changed NIN is still checked
   with the Ministry service first; foreign participants are never checked.
   The NIN exemption route and the staff-assisted exception still require
   their document.
3. **Full-NIN disclosure fix.** In "Save corrections and verify identity",
   the full NIN was previously included in a hidden form field for reviewers
   who could not edit it. Now only a reviewer holding both
   `correct_identity_nin` and `view_identity_evidence` for the case receives
   the NIN field and value. Every other reviewer receives no full NIN in the
   page, including error pages; the stored NIN is kept on the server, and an
   attempt to change it is refused.

Verifying an identity still never approves participation. The registration
enters participation review once, as before; the decision stays in the Review
queue. No correction request and no correction e-mail is created by a direct
verification. Operator notes: [identity review corrections](../operations/identity_review_corrections.md).

### What the release contains

| Item | Detail |
| --- | --- |
| Code and templates | `apps/people` (models, service, forms, views), `templates/identity/review.html`, the new `templates/identity/_without_document_note.html` |
| Compiled translations | `locale/fr/LC_MESSAGES/django.mo` and `locale/ar/LC_MESSAGES/django.mo` (ten new strings each); part of the artifact, so `compilemessages` is not run on the server |
| Migration | `people.0008_idv_first_manual_decision`: two new allowed values (a verification source and an attempt method). It runs no SQL and changes no table or row |
| Static assets | None changed |
| Dependencies, settings, environment variables, roles, scheduled tasks | None |

No data backfill is required.

## 2. Before changing anything: identify the deployed state

Run these checks first, from the deployed artifact root, with the runtime
settings (`DJANGO_SETTINGS_MODULE=config.settings.production`) and the normal
runtime environment. They read only; none of them needs the migration
password. Do not print or record environment values.

### 2.1 Deployed revision

Use the release record made when the artifact was built (source revision,
artifact SHA-256 and date; deployment guide §6). On each host, confirm that
the files match that record:

```bash
python scripts/check.py artifact-verify --root <artifact root> --manifest <manifest file>
uv sync --frozen --no-dev --check
```

The first command must exit 0 against the manifest of the revision that is
deployed now. To tell whether this update is already in the running tree,
check for the new migration file:

```bash
ls apps/people/migrations/ | grep 0008_idv_first_manual_decision
```

If it is listed, the code of this update is already deployed on that host.

### 2.2 Applied migrations

```bash
python manage.py showmigrations people
python manage.py migrate --plan
```

`showmigrations` shows which `people` migrations are applied (`[X]`).
`migrate --plan` shows what the migration step would apply; the runtime
settings allow reading the plan but never applying it.

### 2.3 What the result means

| `people.0007_upd_2026_10_identity_corrections` | `people.0008_idv_first_manual_decision` | Action |
| --- | --- | --- |
| applied | not applied; the plan lists only `0008` | Normal case: continue with §3 |
| not applied | not applied | **Stop.** The consolidated update 2026-10-12 has not been completed. Complete [its procedure](CONSOLIDATED_UPDATE_2026_10_RELEASE.md) first: it applies `people.0007` together with `registrations.0011`, `communications.0009` and `exports.0002`. Then repeat §2. Do not apply `0008` alone to get past this, and do not edit the migration table or fake a migration |
| applied | already applied; the plan is empty | The migration step is already done and must not be repeated, reversed or faked. If the code of this update is already deployed (§2.1) the update is in place: run the checks of §5 only. If the host still runs older code, skip the migration step of §4 and deploy the code (§4, steps 4 and 5) |
| any | the plan lists other migrations | **Stop.** They belong to another release. Identify which one, and ask the release owner before continuing |

Order matters: `0008` depends on `0007`, and the code of this update needs
the schema of the consolidated update.

## 3. Backup (mandatory)

Follow the established procedure; it is not repeated here:

* the full PostgreSQL backup, confirmed to restore, with its identifier
  recorded, as in the [production release runbook §1](PRODUCTION_RELEASE_RUNBOOK.md)
  and [deployment guide §7](README.md#7-migrations);
* the baseline counts (aggregates only), as in
  [review queue release §2](REVIEW_QUEUE_RELEASE.md), plus the number of
  identity decisions. Keep them for the comparison in §4.

The migration changes no data, but a restorable backup is required before
any production migration.

## 4. Deploy

Use the settings modules and credentials of the production runbooks:
`config.settings.production` for web, workers, beat and operator commands;
`config.settings.production_migration` with the migration password, in the
migration process only. (For a staging environment use `config.settings.staging`
and `config.settings.staging_migration`.)

1. Build the artifact from the reviewed revision as in the deployment guide §6
   (`uv sync --frozen --no-dev`, `scripts/check.py build-static`,
   `scripts/check.py artifact-manifest`), and record the source revision, the
   artifact SHA-256 and the date. No dependency changed. The compiled
   `.mo` files are in the artifact.
2. Stop web, workers and beat.
3. Apply the migration, with the migration settings module and password
   present in this process only:

   ```bash
   export DJANGO_SETTINGS_MODULE=config.settings.production_migration
   python manage.py migrate --plan
   python manage.py migrate
   ```

   The plan must list only `people.0008_idv_first_manual_decision`. Then,
   with the runtime settings, `python manage.py showmigrations people` must
   show it applied.
4. Deploy the artifact (code, templates and compiled translations) to every
   web, worker and beat host. No static asset changed, so the static files
   served by the reverse proxy need no change.
5. Start the web processes, then the Celery workers (they run the identity
   verification jobs; restarting them makes them load the new service
   code), then exactly one beat.
6. Health and configuration, as in the deployment guide §8:

   ```bash
   export DJANGO_SETTINGS_MODULE=config.settings.production
   python manage.py check --deploy
   python -m celery -A config inspect ping
   ```

   `/healthz` and `/readyz` must answer `200`.
7. Repeat the baseline counts of §3: registrations, submissions, identity
   verifications, documents, decisions and review cases must be identical
   (plus genuine new activity during the window). The migration changes none
   of them.

## 5. Checks after deployment

These are availability and behaviour checks. **Do not verify, correct or
return a real participant's case to test the release**, and do not change a
real registration. Where a check would need a write, run it in staging or on
a synthetic registration in a non-production environment.

1. **Both actions on a first manual review.** As a reviewer who may verify
   identities (for example a Registration Reviewer), open the page of any
   case already in manual review under `/ops/identity/`. Only open it. The
   page lists **Verify identity**, **Save corrections and verify identity**
   and **Return for correction**. For a case with no reviewable document, the
   page shows the notice "No reviewable document for this case...", and
   "No identity document: manual reviewer decision" is offered and preselected.
   The queue's source filter now lists "Manual reviewer decision (no
   document)".
2. **Required justification.** On the same page, the note field of both
   forms is labelled "Internal note or justification" and its help text says
   it is required without a document. The refusal itself (no justification,
   nothing saved) is covered by the update's tests; confirm it by submitting
   only in staging or on a synthetic registration, never on a real case.
3. **No full NIN for masked reviewers.** With an account that lacks
   `view_identity_evidence` for the case (the standard Registration
   Reviewers and Accreditation Managers roles hold it; use a custom role or
   a staging account), open the case and view the page source. There is no
   `name="nin"` input in the correction form, the page says the NIN stays as
   it is, and the NIN appears only masked. A quick test in the browser
   console is `document.documentElement.outerHTML.match(/\d{18}/g)`, which
   must return `null`. Do not copy or record any number that appears. A
   reviewer who holds `correct_identity_nin` and `view_identity_evidence`
   still sees the editable NIN field.
4. **Identity verification is still not attendance approval.** The identity
   page still says that verifying an identity does not approve participation.
   A registration verified in normal operation after the release appears in
   the Review queue as "Under review" and awaits its own decision; it is not
   approved and has no attendance days until a reviewer decides. Check this on
   the first genuine case that a reviewer verifies, without creating one.
5. **Translations.** Open the same case page in French and in Arabic; the
   notice and the new labels are translated.

## 6. Rollback limitations

* **Prefer a corrective release (roll forward).** This update prescribes no
  automatic downgrade and no database restore.
* **Old code does not recognize the new verification source.** The clearance
  that approval requires accepts only the sources known to the running
  code. A registration verified without a document stores
  `MANUAL_REVIEWER_DECISION`; the previous code does not list it, so it would
  refuse approval of that registration with a misleading clearance message
  ("simulation not accepted"), and would show the raw code instead of a
  label. Rolling the code back makes every such registration unable to be
  approved until the update is deployed again.
* **A rollback restores the NIN disclosure.** The previous code again
  includes the full NIN in the page of "Save corrections and verify
  identity" for reviewers who cannot edit it.
* **Check before any code rollback.** Count the registrations affected, with
  the runtime settings, aggregates only:

  ```python
  from apps.people.models import IdentityVerification

  print(IdentityVerification.objects.filter(verification_source="MANUAL_REVIEWER_DECISION").count())
  ```

  A non-zero result means a code rollback affects real registrations; decide
  with the release owner before reverting, and deploy the corrected release
  as soon as it is available.
* **The schema does not need to change back.** The migration only adds
  allowed values, and the previous code runs against it. Reversing
  `people.0008` is not required for a code rollback, and no downgrade or
  restore of the database is prescribed. Any such step needs the owner's
  decision and the backup of §3, and loses every record written after it.
* Decisions, attempts and audit events recorded by this update are valid
  history and are never deleted.

## 7. References

* [Deployment guide](README.md): artifact build, migrations, health checks.
* [Production release runbook](PRODUCTION_RELEASE_RUNBOOK.md): backup and
  settings conventions of the production release.
* [Consolidated update 2026-10-12](CONSOLIDATED_UPDATE_2026_10_RELEASE.md):
  the prerequisite release (`people.0007`).
* [Identity review corrections](../operations/identity_review_corrections.md):
  reviewer operations for this update.
* [Identity review permissions](../security/identity_review_permissions.md).
