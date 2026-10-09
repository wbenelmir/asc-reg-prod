# Participation review queue: automatic entry and Excel decisions

Two related behaviours of `/ops/reviews/queue/`:

1. A submitted registration whose identity is verified now enters the
   participation Review queue automatically (`apps/reviews/intake.py`).
2. Operators who hold the workbook role can export the cases awaiting a
   decision to Excel, fill in decisions, import the workbook, check the
   preview and then explicitly validate and apply the decisions
   (`apps/reviews/workbook.py`, `apps/core/xlsx.py`).

Identity review (`/ops/identity/`) stays a separate operation. Identity
verification never approves participation, never issues an invitation, a pass
or a badge and never sends a message: an authorized participation decision is
still required.

## 1. Automatic entry into participation review

Triggered, in the same database transaction as the identity result, by:

| Identity result | Source | Where |
| --- | --- | --- |
| `API_VERIFIED` | Ministry service (`MINISTRY_API`; the development simulation only where it is accepted) | `identity_verification._apply_lookup_result` |
| `MANUALLY_VERIFIED` | national identity card, passport page (foreign participants), NIN-exemption document | `identity_review.verify_identity_manually` |
| `MANUALLY_VERIFIED` | Algerian staff-assisted exception (`STAFF_EXCEPTION`, permission `people.apply_identity_exception`) | `verify_identity_manually(..., exception=True)` |

There is no other identity "special approval" status: the staff exception and
the NIN exemption are the existing authorized special cases and both end in
`MANUALLY_VERIFIED`.

### Eligibility (explicit allowlist)

A registration enters review only when ALL of these hold:

* public status `SUBMITTED` or `UNDER_REVIEW`, with its INITIAL submission
  evidence and `submitted_at`, and it is the participant's current context;
* internal status `PENDING_ASSIGNMENT`, `ASSIGNED`, `VERIFICATION_PENDING`,
  `REVIEW_IN_PROGRESS` or `QUALIFICATION_COMPLETE`;
* no current participation decision;
* no active information request and no correction pending
  (`ADDITIONAL_INFORMATION_REQUIRED` / `AWAITING_APPLICANT`);
* no duplicate review (`DUPLICATE_REVIEW`) and no open case other than the
  STANDARD / GENERAL one (duplicate, restricted, specialist, identity...);
* identity cleared (owner decision IDV-Q1): verified, identifier still
  current, document not expired, accepted source.

Excluded and left unchanged: drafts, withdrawn or cancelled registrations,
approved / not-approved registrations, identity pending / in manual review /
returned for correction / rejected, open information requests, duplicate or
restricted reviews. Exclusion is never a reason to delete anything.

### What happens to an eligible registration

* The one open STANDARD / GENERAL case is reused, or a new one is opened
  (status QUEUED, event edition and organization copied from the
  registration). Two open STANDARD cases are reported
  (`MULTIPLE_OPEN_STANDARD_CASES`) and left as they are.
* `SUBMITTED` becomes `UNDER_REVIEW`; a `VERIFICATION_PENDING` internal status
  follows its case (QUEUED -> `PENDING_ASSIGNMENT`, ASSIGNED -> `ASSIGNED`,
  started -> `REVIEW_IN_PROGRESS`). Nothing else of the registration changes.
* Audit `REV_CASE_ENQUEUED` (reason = trigger; statuses and a reuse flag only)
  and, for a new case, `REV_CASE_OPENED`.

Retries, duplicate worker deliveries and concurrent calls do not duplicate
cases: the Registration row is locked and every condition is checked again
under the lock (lock order: Registration, identity case, review cases).
Reopening (`reopen_registration`) still opens its own case for a closed
registration; the entry service then reuses that case.

## 2. Backfill of registrations verified earlier

Registrations verified before this release have no review case. The backfill
uses the same service and rules.

```
uv run --env-file .env python manage.py backfill_review_queue --event <EVENT_CODE>
uv run --env-file .env python manage.py backfill_review_queue --event <EVENT_CODE> --apply
```

* Default: DRY RUN, nothing written. Prints `examined`, `eligible_missing_case`,
  `existing_case`, `status_transitions`, `excluded_total` and the exclusion
  reasons. Counts only: no reference, name or identifier. `--json` prints the
  same report as JSON.
* `--apply`: creates only the missing cases and the transitions above, one
  short transaction per registration (lock wait 5 s, three attempts). It
  prints `created`, `transitioned`, `conflicts` and the registrations that
  changed between scan and apply. Exit code 2 when some rows stayed locked:
  run it again.
* `--event` takes `EventEdition.code`; `--all-events` is required to examine
  every edition. `--batch-size` (default 200) bounds each read.
* Idempotent: a second `--apply` creates nothing. It never approves, never
  sends a communication, invitation or badge, and never calls the Ministry.
* It never runs automatically (not in a migration, start-up or deploy hook).

## 3. Roles and permissions

| Need | Permission | Granted through |
| --- | --- | --- |
| See the queue | `reviews.view_reviewcase` | Registration Reviewers, Accreditation Managers |
| Decide one case | `reviews.add_registrationdecision` | Accreditation Managers |
| Use the Excel workbook | `reviews.bulk_registrationdecision` **and**, for every row, `reviews.add_registrationdecision` in that row's scope | role "Review decision workbook" (group *Review Decision Workbook Operators*) + Accreditation Managers |

Nobody receives the workbook role automatically, and no existing group holds
its permission. An account administrator grants it explicitly from the staff
account area (`/ops/staff-accounts/`), normally to an Accreditation Manager, with the
same event edition (and organization, if any) as that manager's decision role.
Each row is still checked against the individual decision rule
(`review_cases_visible_to(user, "add_registrationdecision")`) and against the
workbook permission's own scope, at preview and again at final validation.
Hiding the buttons is not the control: every endpoint and the service refuse.

## 4. Operator guide (Excel decisions)

1. Open `/ops/reviews/queue/`. The tabs separate **Awaiting decision**
   (open cases, the default), **Completed history** and **All cases**.
2. Apply the filters you need. The "Decisions in Excel" panel shows how many
   rows will be exported: every case awaiting a decision that matches the
   filters, on every page, in your decision scope only.
3. **Export Excel** downloads `asc-review-decisions-<date>.xlsx`
   (sheets *Decisions* and *Instructions*; at most 1000 rows; a larger
   selection is refused, narrow the filters).
4. Fill in only the yellow columns. Leave every other column unchanged.
5. **Import Excel and preview** with the saved file (`.xlsx`, at most 2 MB).
   The preview shows the statistics and every proposed or invalid row. It has
   **not** applied anything.
6. If there is no invalid row, choose **Validate and apply decisions**. Every
   decision is checked again and all are recorded, or none.
7. If anything changed since the export (a decision taken individually, an
   assignment, an identity change) or any row is invalid, nothing is applied:
   correct the workbook, or export again, and import again. The error report
   (CSV) lists row numbers, references and problems.

An export can be imported for 3 days by the operator who exported it; a
preview can be applied for 2 hours by the operator who uploaded it.

### Field mapping (template `ASC-RQX-1`)

| Column | Editable | Meaning |
| --- | --- | --- |
| Registration reference | no | `Registration.public_reference` (checked against the record) |
| Participant | no | submitted full name (display only, literal text) |
| Registration category | no | source kind (open, invitation, delegation, on behalf, on site) |
| Identity verification | no | identity status |
| Review status | no | review case status |
| Registration status | no | public status |
| Current attendance profile | no | current entitlement, if any |
| **Decision** | yes | `KEEP` (default, no change), `ACCEPT`, `REJECT` |
| **Attendance profile** | yes | `ACCEPT` only, required: `ALL_DAYS` -> `ALL_CONFERENCE_DAYS` (all three days, opening day included, uses an opening-day place); `FOLLOWING_TWO_DAYS` -> `FOLLOWING_TWO_DAYS` |
| **Rejection reason** | yes | `REJECT` only, required: the internal reason code of a Not Approved decision (plain text, at most 64 characters) |
| **Internal note** | yes | `REJECT` only, optional, at most 2000 characters; internal, never shown to the participant (the approval service has no note) |
| registration_id, review_case_id | no | stable identifiers |
| registration_version, review_case_version, identity_version | no | versions at export (staleness check) |
| export_id, template_version | no | binding to the server-side export manifest |

No identity number, document link, photo, password, code or token is
exported. Display cells are literal text (no formula is ever written; text
cells carry the "quote prefix"). Sheet protection is set without any secret and is a
convenience only. The server never trusts the file's names, statuses, scope,
identifiers or versions: it resolves the real records and compares them with
the export manifest.

### Validation (preview and final)

File refusals: not an `.xlsx`, larger than 2 MB, macros, external links, a
DOCTYPE/ENTITY declaration, too many rows, a missing sheet, changed headers,
an unknown template version, an unknown or expired export, an export of
another account.

Row errors: formula in a cell, unknown row, duplicate registration or case,
changed reference or technical column, wrong export, unknown decision, KEEP
with a profile/reason/note, ACCEPT without or with an unknown profile, ACCEPT
with a reason or note, REJECT without or with an invalid reason, REJECT with a
profile, out of scope, stale (registration, case or identity changed),
no longer eligible (with the reason), approval prerequisites missing
(participant role, badge type, access profile), attendance days not
configured, not enough opening-day places for all ALL_DAYS approvals of an
event.

Final validation locks the preview, then every registration of the batch (in
id order), checks every row again and applies the decisions through
`record_approved_decision` / `record_not_approved_decision` in one
transaction. Their notifications are queued in that transaction and sent only
after commit. Applying the same preview again returns it unchanged.

### Audit and retention

`REV_DECISION_WORKBOOK_EXPORTED`, `_PREVIEWED`, `_REFUSED`, `_APPLIED`,
`_APPLY_REFUSED` (counts and ids only), and per decision the ordinary
`REV_DECISION_RECORDED` with correlation id `decision-workbook:<preview id>`;
each preview row keeps a link to the decision it recorded. The uploaded file is
never stored. `manage.py purge_review_decision_workbooks` expires old previews
and erases the internal notes of every preview that was not applied.
