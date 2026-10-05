# Registration correction package: coverage matrix and staging checklist

Language: English. Audience: the owner, the UAT coordinator, reviewers and operators.
Package: registration corrections of 2026-10-04 (decision gate §17.12). It runs **after** the
deployment guide and next to the [UAT handoff](phase_04_staging_uat_handoff.md), whose accounts
(§2), ground rules, evidence rules and audit queries (end of INT-13) apply unchanged.

**States.** Every live check below is **NOT_EXECUTED** until a named person runs it in staging.
Only a human moves a check to PASSED, FAILED or ESCALATED. Local tests never prove SMTP delivery,
the clamd daemon, S3 storage, either Redis, PostgreSQL role isolation or the ministry service.

## 1. What changed for registration

| # | Correction | Rule now in force |
| --- | --- | --- |
| 1 | Country catalog | 248 approved countries and territories with EN/FR/AR names; `IL` and `XK` never selectable; history kept ([operator guide](../operations/country_catalog.md)) |
| 2 | Profile photograph | Required at the professional step and at the final submission; never shown as optional |
| 3 | Passport copy | Required on the passport route before submission, whatever the residence; the route follows the nationality; requirement and errors in EN/FR/AR |
| 4 | Official footer | Exactly two French institutional lines, the second smaller, in every language |
| 5 | Legal notices | Official version `v3` of the Privacy Notice and Registration Terms; older versions and acceptances kept; acceptance recorded only for the version the page showed |
| 6 | Register again after withdrawal | A new, separate registration by the origin rules; the withdrawn one stays withdrawn |

## 2. Coverage matrix (registration lifecycle)

Local status: the result of the automated tests on the delivered tree (see the release notes for
the full gate). Live status: the manual staging check.

| Scenario | Origin | Automated evidence | Manual staging check | Local | Live |
| --- | --- | --- | --- | --- | --- |
| Email code sign-in, one account across registrations | All | `apps/accounts/tests/`, `tests/browser/test_registration_happy_path.py` | INT-03, RC-02 | PASS | NOT_EXECUTED |
| Public registration from start to confirmation | Public | `apps/registrations/tests/test_views.py`, browser happy path | RC-05 to RC-08 | PASS | NOT_EXECUTED |
| Invitation registration, revalidation, capacity at submission | Invitation | `apps/invitations/tests/`, `tests/concurrency/test_invitation_campaign_capacity.py`, `test_required_documents.py` (INVITATION) | RC-11 | PASS | NOT_EXECUTED |
| On-behalf draft and participant claim | On behalf | `apps/invitations/tests/`, `test_required_documents.py` (ON_BEHALF) | RC-12 | PASS | NOT_EXECUTED |
| Country choices, labels in EN/FR/AR, IL/XK absent, history kept | All | `apps/core/tests/test_country_catalog.py`, `tests/foundation/test_migration_reversibility.py` | RC-01, RC-04 | PASS | NOT_EXECUTED |
| Draft save, resume, validation, final submission, double submission | Public | `test_wizard_services.py`, `test_completeness.py`, `test_uxc2_corrections.py`, `tests/concurrency/test_registration_submission_idempotency.py` | RC-05 to RC-08 | PASS | NOT_EXECUTED |
| Profile photograph required (missing, invalid, unsafe, resume, bypass) | All three | `apps/registrations/tests/test_required_documents.py`, `tests/browser/test_registration_corrections.py` | RC-05 | PASS | NOT_EXECUTED |
| Passport copy on the passport route; Algerian living abroad on the NIN route | Public | `test_required_documents.py` | RC-06 | PASS | NOT_EXECUTED |
| NIN behaviour with the local simulation | Public | `apps/people/tests/test_idv_worker.py`, `test_nin_provider.py` | INT-07 track A | PASS | NOT_EXECUTED; real ministry BLOCKED (UAT-02, API-01, STG-NET-01) |
| Manual identity review, correction request, participant response | All | `apps/people/tests/test_idv_review_services.py`, `test_idv_views.py` | INT-08 | PASS | NOT_EXECUTED |
| Approval and rejection, identity clearance | All | `apps/reviews/tests/`, `apps/people/tests/test_idv_q1_approval_gate.py`, `test_idv_q2_rejection_outcome.py` | INT-08, INT-09 | PASS | NOT_EXECUTED |
| Notifications, uncertain SMTP outcome never re-sent | All | `apps/communications/tests/test_smtp_delivery.py` | INT-09, RC-13 | PASS | NOT_EXECUTED (COMM-01 open) |
| Official notices v3, acceptance of the version shown | All | `apps/privacy/tests/test_official_notices_v3.py`, migration test | RC-03, RC-07 | PASS | NOT_EXECUTED |
| Withdrawal, then register again; closed channel; unusable invitation; concurrency | Public, invitation, on behalf | `apps/registrations/tests/test_register_again_after_withdrawal.py`, `tests/concurrency/test_register_again_after_withdrawal_race.py` | INT-11, RC-09 to RC-12 | PASS | NOT_EXECUTED |
| Register again after an identity rejection (no regression) | Public, invitation, on behalf | `apps/people/tests/test_idv_q_c1_register_again_origin.py`, browser `test_idv_q_c1_register_again.py` | INT-10 | PASS | NOT_EXECUTED |
| Official footer, mobile, RTL | All | `apps/core/tests/test_official_footer.py`, browser `test_registration_corrections.py` | RC-04 | PASS | NOT_EXECUTED |
| Permissions, scope, private document access | Staff | `apps/documents/tests/`, `apps/people/tests/test_idv_views.py` | INT-12, RC-14 | PASS | NOT_EXECUTED |
| Malware scanning of uploads | All | simulated clamd protocol tests, stub scanner tests | INT-04 | PASS (simulated) | NOT_EXECUTED |

## 3. Ordered staging checklist

Accounts are those of the UAT handoff §2. New synthetic participants for this package: **P-RC1**
(open registration, Algerian living abroad), **P-RC2** (foreign national living abroad), **P-RC3**
(invitation), **P-RC4** (on behalf, created by OB), each a team-controlled mailbox; P-FR and P-AR
for the language checks. Evidence never holds a secret, a code, a NIN, a name or an image: use
command exit codes, HTTP statuses, screenshot file names, test references and audit codes.

### RC-01 Migrations and the country catalog

* **Role:** deployment operator (migration settings), then runtime settings.
* **Action:** `python manage.py migrate --plan` lists `core.0005_approved_country_catalog` and
  `privacy.0005_official_registration_notices_v3`; take the pre-migration backup; `migrate`;
  then, with the runtime settings, `python manage.py reconcile_country_catalog` and
  `python manage.py release_readiness`.
* **Expected:** the preview prints "In sync" and exits 0; Q-CAT gives `selectable` 248 and
  `il_xk_selectable` 0; `release_readiness` still reports `legal_notices` BLOCKED (the unconfirmed
  legal facts) and `retention` BLOCKED.
* **Evidence:** plan lines, exit codes, the Q-CAT row, the readiness item states.
* **State:** NOT_EXECUTED.

### RC-02 Provisioning creates no country

* **Role:** deployment operator.
* **Action:** `python manage.py provision_staging_uat --confirm-host <host> --email-pattern '<pattern>'` (preview).
* **Expected:** two lines `country DZ` and `country FR` with action `exists` and detail
  `approved catalog (C-01)`; no country is created; no conflict.
* **Evidence:** the two lines, exit code.
* **State:** NOT_EXECUTED.

### RC-03 Official notices on the legal page

* **Role:** anyone, signed out. **Route:** `/legal/` in English, French and Arabic.
* **Expected:** both documents show version `v3`; no "DRAFT", "BROUILLON" or "مسودة" banner; the
  French controller name is the owner-supplied ministry name; the unconfirmed facts still appear as
  "TO BE CONFIRMED" / "À CONFIRMER" / "يُستكمل" markers (expected until the owner supplies them).
* **Evidence:** screenshot file names per language; version label.
* **State:** NOT_EXECUTED.

### RC-04 Footer, country lists and labels

* **Role:** P-AR and P-FR on a phone-sized browser (about 375 px) and a desktop.
* **Routes:** `/accounts/start/`, `/workspace/`, `/register/identity/`, `/register/contact/`;
  staff sign-in page `/accounts/ops/sign-in/`.
* **Expected:** the footer shows exactly "Ministère de l'Économie de la Connaissance, des Start-up
  et des Micro-entreprise" and, smaller, "Direction des Systèmes d’Information (DSI)", no prefix,
  no link, no horizontal scrolling, on the right in Arabic with its words in order. The nationality
  list finds "فلسطين" / "Palestine", "La Réunion", "Sahara occidental"; it has no Israel and no
  Kosovo. The calling-code list has no Antarctica.
* **Evidence:** screenshot file names; pass or fail per item.
* **State:** NOT_EXECUTED.

### RC-05 Profile photograph required

* **Role:** P-FR. **Route:** `/register/professional/`.
* **Expected:** the photograph field is marked required (no "Facultatif"); continuing without a
  photo shows "Téléversez une photo de profil. Elle est obligatoire." and stays on the step; a
  JPEG or PNG continues; `/register/review/` shows the photo on file. With clamd stopped (INT-04),
  an upload is refused with the "try again later" message and the step does not advance.
* **Evidence:** screenshot file names; the HTTP flow (stays / continues).
* **State:** NOT_EXECUTED.

### RC-06 Passport copy and identity route

* **Role:** P-RC2 (foreign national living abroad) and P-RC1 (Algerian national living abroad).
* **Route:** `/register/identity/` in English, then French or Arabic.
* **Expected:** P-RC2: the passport choice says the identity page is required; continuing without
  it shows the upload error (translated); with a page it continues. P-RC1: the NIN choice says
  "including those living abroad"; the passport route is refused for an Algerian national; no
  passport page is asked; after submission the case route is NIN (with the ministry adapter
  disabled it goes to manual review).
* **Evidence:** screenshot file names; per test reference the identity case route and status
  (identity queue, M).
* **State:** NOT_EXECUTED.

### RC-07 Submission records the v3 notices

* **Role:** P-RC1, then DBA-RO. **Route:** `/register/notices/`, then confirmation.
* **Expected:** submission succeeds; Q-ACCEPT shows for the test reference `PRIVACY_NOTICE` and
  `TERMS` with label `v3` in the language the page was shown in; a registration submitted before
  the migration still shows its `v2-draft` rows.
* **Evidence:** Q-ACCEPT rows (references, codes, labels, languages only).
* **State:** NOT_EXECUTED.

### RC-08 Confirmation email

* **Preconditions:** INT-03 (SMTP).
* **Expected:** one confirmation email per submission in the registration language; a message
  left in `SENDING` with an `UNCONFIRMED:` attempt is never re-sent automatically (escalate, COMM-01).
* **Evidence:** mailbox count per test reference; `/ops/communications/` status (CO).
* **State:** NOT_EXECUTED.

### RC-09 Withdraw and register again (public)

* **Role:** P-RC1. **Routes:** `/workspace/` (Withdraw button), then **Register again**.
* **Expected:** after withdrawal the card shows "You withdrew this registration" and **Register
  again**; one click opens a new draft at `/register/identity/` with empty steps; the old
  registration stays "Withdrawn" and its confirmation page still opens; a second click does not
  create a second draft. Q-AUD-CASE for the old reference shows `REV_PARTICIPANT_WITHDRAWAL` and
  `REG_RESTARTED_AFTER_WITHDRAWAL` once each. The new registration needs its own notices
  confirmation and creates its own acceptance rows (Q-ACCEPT).
* **Evidence:** per test reference the Q-AUD-CASE and Q-ACCEPT rows; screenshot file names.
* **State:** NOT_EXECUTED.

### RC-10 Register again while public registration is closed

* **Role:** CM closes public registration at `/ops/events/registration-channels/`; P-RC1 (with a
  second withdrawn open registration, or before RC-09).
* **Expected:** no button; "Registration is closed at the moment…"; after CM reopens, the button
  returns. Nothing is created while closed.
* **Evidence:** pass or fail; times of the channel changes.
* **State:** NOT_EXECUTED.

### RC-11 Register again with an invitation

* **Role:** P-RC3 registers through the E1 campaign link (`/invite/<token>/`), submits, withdraws;
  IM for the link.
* **Expected:** **Register again with your invitation** opens a new invitation draft of the same
  campaign; after IM revokes or rotates the link, a withdrawn registration shows that a new
  invitation is needed and offers no public route; opening a new invitation while signed in and
  pressing **Register with this invitation** opens one new draft. Capacity is enforced at
  submission (a withdrawn submission still counts toward the campaign).
* **Evidence:** pass or fail per row; Q-AUD-CASE rows per test reference.
* **State:** NOT_EXECUTED.

### RC-12 On-behalf registration withdrawn

* **Role:** OB creates a registration for P-RC4 at
  `/organizations/workspace/on-behalf/new/<organization id>/`; P-RC4 claims it, completes and
  submits it (photo required), then withdraws it.
* **Expected:** the card says to contact the organization or the registration team; no button;
  OB can create a new on-behalf registration for the same address through the normal process.
* **Evidence:** pass or fail; Q-AUD-CASE rows.
* **State:** NOT_EXECUTED.

### RC-13 Staff reopening of a replaced registration

* **Role:** M at `/ops/reviews/registrations/<id>/reopen/` for the withdrawn registration of RC-09.
* **Expected:** refused with a visible conflict ("replaced by a newer registration"); the
  registration stays "Withdrawn". A withdrawn registration that was not replaced can still be
  reopened as before.
* **Evidence:** HTTP status; registration status.
* **State:** NOT_EXECUTED.

### RC-14 Private documents after the corrections

* **Role:** I, O and R (UAT handoff §2), with the submitted registration of P-RC2.
* **Expected:** R opens the passport page from the identity review of that case (audited
  `IDV_EVIDENCE_VIEWED`); I gets "not found" for the generic document address
  `/documents/<document id>/` of that passport page; O gets 404 for the case. No participant
  page links to a stored document.
* **Evidence:** HTTP statuses per account; Q-AUD-CASE rows.
* **State:** NOT_EXECUTED.

## 4. Read-only queries (DBA-RO)

They were executed against the real schema on a disposable test database. Replace `<ref-n>`.

```sql
-- Q-CAT: the country catalog in the database
SELECT count(*) FILTER (WHERE is_active AND code NOT IN ('IL', 'XK')) AS selectable,
       count(*) FILTER (WHERE is_active AND code IN ('IL', 'XK')) AS il_xk_selectable,
       count(*) FILTER (WHERE NOT is_active) AS inactive_kept
FROM core_country;

-- Q-ACCEPT: the notice versions each test registration accepted
SELECT r.public_reference, d.code, v.version_label, a.language, a.action
FROM privacy_acceptance_record a
JOIN registrations_registration r ON r.id = a.registration_id
JOIN privacy_legal_document_version v ON v.id = a.legal_document_version_id
JOIN privacy_legal_document d ON d.id = v.legal_document_id
WHERE r.public_reference IN ('<ref-1>', '<ref-2>')
ORDER BY 1, 2;
```

## 5. Evidence log

| Check | Operator | Date and UTC time | Result | Evidence reference (file name only) |
| --- | --- | --- | --- | --- |
| RC-01 | | | | |
