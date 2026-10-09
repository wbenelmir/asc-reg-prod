# Release procedure — participation review queue entry and Excel decisions

Registration is open in production and real applications exist. This release
must preserve every registration, account, identity record, document,
submission, consent record, decision, invitation, badge and audit event.
Follow the deployment guide (`docs/deployment/README.md`) for everything not
repeated here (production settings modules: `config.settings.production`,
`config.settings.production_migration`). Feature details:
`docs/operations/review_decision_workbook.md`.

What the release contains:

* code: verified registrations enter the Review queue automatically; the queue
  gains "Awaiting decision / Completed history / All cases" tabs and the Excel
  decision workbook;
* one additive migration, `reviews.0002_decision_workbook`: three new tables
  (export manifest, import preview, preview rows). It alters no existing table
  and moves no data;
* a new role, "Review decision workbook", assigned to nobody;
* two operator commands: `backfill_review_queue` (dry run by default) and
  `purge_review_decision_workbooks`.

Nothing in the migration, start-up or deployment hooks creates review cases
or changes a registration. The backfill is a separate, explicit step (§4).

## 1. Before the window

1. Build the artifact from the reviewed revision (`uv sync --frozen --no-dev`,
   `scripts/check.py build-static`, `artifact-manifest`). No dependency
   changed (the workbook uses the Python standard library only).
2. Agree with the owner which Accreditation Managers receive the workbook role
   and in which event/organization scope.

## 2. Backup (mandatory)

1. Stop web, workers and beat.
2. Take a full PostgreSQL backup and confirm it restores into a scratch
   database (the deployment guide's backup section). Record its identifier.
3. Preserve the uploaded media: snapshot or copy the private object storage
   (S3 bucket versioning or a copy of the private storage root). Record it.
4. Record the baseline counts (aggregates only), for example with
   `python manage.py shell` under the runtime settings module:

   ```python
   from apps.registrations.models import Registration, RegistrationSubmission
   from apps.people.models import IdentityVerification
   from apps.documents.models import Document
   from apps.reviews.models import ReviewCase, RegistrationDecision
   from django.db.models import Count
   print(Registration.objects.count(), RegistrationSubmission.objects.count(),
         IdentityVerification.objects.count(), Document.objects.count(),
         ReviewCase.objects.count(), RegistrationDecision.objects.count())
   print(list(Registration.objects.values("public_status").annotate(n=Count("pk")).order_by("public_status")))
   print(list(IdentityVerification.objects.values("status").annotate(n=Count("pk")).order_by("status")))
   ```

Never replace the production database with a local or test database.

## 3. Migrate and deploy

1. `python manage.py migrate` under `config.settings.production_migration`
   (applies `reviews.0002_decision_workbook` only; additive).
2. Deploy the code and restart web, workers and beat.
3. From now on every identity verification of an eligible registration opens
   its STANDARD / GENERAL review case and sets it UNDER_REVIEW in the same
   transaction. New submissions and identity checks continue normally during
   the next steps.

## 4. Backfill of registrations verified before the release

Run under the production runtime settings module, scoped to the edition:

```
python manage.py backfill_review_queue --event <EVENT_CODE>
```

Read the dry-run report (counts only): `eligible_missing_case` is the number
of cases the apply will create, `existing_case` those already queued,
`status_transitions` the SUBMITTED -> UNDER_REVIEW moves, `excluded` the
reasons (drafts, corrections, pending identity, decisions, withdrawals...). The
screenshot counts seen earlier are a moment in time; trust the dry run.

If the report is plausible:

```
python manage.py backfill_review_queue --event <EVENT_CODE> --apply
```

* `created` and `transitioned` should match the dry run, minus registrations
  that changed in between (reported under "changed before apply").
* Exit code 2 / `conflicts > 0`: some rows stayed locked by concurrent work;
  run the same command again (it is idempotent).
* A second `--apply` must report `created: 0`.

## 5. Reconciliation

Repeat the counts of §2.4:

* registrations, submissions, identity verifications, documents and decisions:
  **identical** to the baseline (plus genuinely new submissions made during
  the window);
* identity verification statuses: identical (the backfill never re-runs or
  changes identity verification);
* review cases: baseline + `created` (+ cases opened by new verifications);
* registration public statuses: only SUBMITTED -> UNDER_REVIEW moves, equal
  to `transitioned` (+ new verifications);
* `AuditEvent` rows with action `REV_CASE_ENQUEUED`: one per created case or
  transition.

Then open `/ops/reviews/queue/` as an Accreditation Manager: the "Awaiting
decision" tab lists the verified registrations.

## 6. Grant the workbook role (explicit)

From `/ops/staff-accounts/`, an account administrator grants the role
"Review decision workbook" to the agreed Accreditation Managers, with the same
event (and organization) scope as their Accreditation Manager role. Nobody
else gets it. Schedule `python manage.py purge_review_decision_workbooks`
daily (it expires old previews and erases their unapplied internal notes).

## 7. Rollback (compatible, no data loss)

* Code rollback: redeploy the previous revision. The three new tables stay in
  place, unused; do NOT reverse the migration and do NOT delete review cases or
  decisions created meanwhile -- they are valid review records that the
  previous code also understands (STANDARD / GENERAL cases, UNDER_REVIEW
  registrations, ordinary decisions).
* Registrations moved to UNDER_REVIEW stay UNDER_REVIEW; the previous code
  handles that status. No submitted data, document or identity result is
  touched by a rollback.
* Restoring the §2 backup is only for a destructive failure, and loses every
  submission made after it: decide it with the owner, never by default.
