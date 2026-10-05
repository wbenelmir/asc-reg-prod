# Staging UAT handoff: accounts, live integration checks and go/no-go

Language: English. Audience: the owner, the UAT coordinator and the operators.

**Deploy first.** Build, configure, migrate, start and provision the staging
environment with the [deployment guide](../deployment/README.md). This
document starts where that guide ends: it lists the test accounts, the live
integration checks that only a running staging environment can prove, and the
go/no-go questions. It does not repeat the deployment steps.

**Source versus runtime artifact.** The reviewed source is identified by its
Git revision (and the delivery's review archive hash). The runtime artifact is
built by the operations team from that revision and recorded with its own
hash and manifest (deployment guide §6). Only the recorded artifact runs.

| Guide | Used for |
| --- | --- |
| [Deployment guide](../deployment/README.md) | Build, configuration, migrations, start, health checks, provisioning, recovery |
| [Counter and sign-in guide](phase_04_p4_4_c3_staging_guide.md) | Redis counter setup, `/readyz`, password-only sign-in, counter outage and recovery |
| [Identity UAT guide](phase_04_idv_manual_uat_guide.md) | Every identity scenario step (P, R, M, B, C, Q, C1 series) |
| [Identity provider guide](../operations/identity_provider_integration_guide.md) | Ministry adapter configuration, worker recovery, participant email |
| [Reviewer quick guide](phase_04_idv_staff_review_quick_guide.md) | What a reviewer sees on the identity screens |
| [Identity review permissions](../security/identity_review_permissions.md) | Permission-to-group mapping and audit codes |
| [Identity open questions](../execution/identity_open_questions_2026-10-01.md) §9 | Owner decisions of 2026-10-02 |
| [Registration corrections checklist](registration_corrections_staging_checklist.md) | The 2026-10-04 registration corrections: coverage matrix and checks RC-01 to RC-14 |

**Check states.** Every check in §3 starts as **NOT_EXECUTED** (it can run once
staging is deployed; nobody has run it) or **BLOCKED** (it cannot run until a
named input exists). Only a human moves a check to **PASSED**, **FAILED** or
**ESCALATED**, and records who, when and where. No automated test, local run
or document edit changes a state.

**Ground rules.**

* Synthetic data and authorized test identities only. Mailboxes are under the team's control.
* Never write a secret, a code, a token, a full NIN, a name or a document image into a ticket, a
  chat, a screenshot or the evidence log. Record codes, statuses, counts and timestamps.
* Never touch a shared or production service. Staging only, and only the approved test instances.
* Stop at the first failed stop condition and decide; do not push on.

## 1. Inputs and their state

| ID | Input | State | Needed for |
| --- | --- | --- | --- |
| STG-ART-01 | The recorded runtime artifact (deployment guide §6) | Repository side done (application server, build and artifact tooling); **operators build and record it** | Everything |
| STG-ACC-01 | UAT accounts, scopes and test data | Repository side done (`provision_staging_uat`); **operators run it** with team-controlled mailboxes and set the passwords | §2, every signed-in check |
| STG-DB-01 | Migration with the owner identity | Done: `config.settings.staging_migration` | Deployment |
| STG-SCAN-01 | Malware scanning | Repository side done (ClamAV clamd adapter); **operators supply clamd** | INT-04 and every upload |
| STG-MAIL-01 | Email | Repository side done (SMTP from `EMAIL_*`); **operators supply SMTP and the sender domain's SPF, DKIM and DMARC** | INT-03, INT-09, INT-10 |
| STG-REDIS-01 | Broker and counter Redis with TLS, alert routing for `asc2026.ops.alerts` | **Operators** | INT-05, INT-06 |
| UAT-02, API-01, API-02 | Written authorization for a real ministry check; the authentication response shape; other date formats | **Open (owner, external)** | INT-07 track B |
| STG-NET-01 | Ministry network path, certificate chain, availability windows | **Open (external)** | INT-07 track B |
| COMM-01 | Policy for a message left in `SENDING` with an unknown outcome | **Open (owner)**; the system never re-sends such a message | INT-09 and INT-14 stop and escalate in that case |
| IDV-Q4 to IDV-Q16 | Open owner questions of the identity register | Open; no check depends on them | — |

## 2. Test accounts and role matrix

`python manage.py provision_staging_uat` creates these accounts (deployment
guide §9); its `{key}` is shown in brackets. Staff accounts use email and
password (owner decision MFA-01 revised, amendment A-11); the readiness fact
`operational_sign_in_mfa_enforced` stays `False`. Participants use the email
code and register themselves. Every address is a mailbox under the team's
control. Every membership is scoped to the editions below, never broader.

Test data created with them: edition **E1** (`ASC2026`, the migration-seeded
edition, unchanged, open for public registration), a second edition **E2**
(`ASC2026-UAT2`, invitation only, never open), one synthetic organization, and
one active invitation campaign with a usable link in each edition. Account
**O** is scoped to E2 only and is used against E1 case addresses.

| Account | Type and exact group | Scope | Used for |
| --- | --- | --- | --- |
| R [`reviewer`] | Operational, **Registration Reviewers** | E1 and E2 | Identity UAT guide §2 (R1 to R12), §4b, §4c, §4d review steps |
| M [`manager`] | Operational, **Accreditation Managers** | E1 and E2 | Identity UAT guide §3, final rejection, staff exception, NIN exemption, participation decisions |
| I [`intake`] | Operational, **Registration Intake** (read-only) | E1 | B1: must see no identity data and no identity route |
| O [`outsider`] | Operational, **Registration Reviewers** | E2 only | B2: must see nothing of E1 (404) |
| A [`coordinator`] | Operational, **Accreditation Coordinators** | E1 | Assign a Participant Role, Badge Type and Access Profile before approval steps (identity UAT guide §4c) |
| CO [`comms`] | Operational, **Communication Operators** | E1 | Read `/ops/communications/` to count messages and read their status (no body shown) |
| CM [`channels`] | Operational, **Registration Channel Manager** | E1 | Close and reopen public registration (Q2-7, C1-9), at `/ops/events/registration-channels/` |
| IM [`invitations`] | Operational, **Invitation Manager** | E1 and the organization | Rotate or reissue a campaign link (C1-7, C1-8) |
| OB [`onbehalf`] | Operational, **On-Behalf Registrar** | E1 and the organization | Create the on-behalf registration of P15 (C1-10) |
| OU [`orguser`] | Operational, **Organization User** | E1 and the organization | Optional: confirm a view-only organization user cannot issue links |
| T-ADMIN [`admin`] | Operational, no group | — | Password-only sign-in checks (INT-02) |
| T-TEMP [`temp`] | Operational, no group, `active_until` set | — | Expiry check (INT-02) |
| T-EXT [`external`] | External security, **External Security (Temporary)** | E1 | Temporary external account check (INT-02); an account-access email is queued when it is created |
| T-SRM [`srm`] | Operational, **Security Restriction Managers** | E1 | Emergency wipe refused without `MFA_BACKEND` (INT-02) |
| P1 to P15, P-FR, P-AR | Participant, email code, synthetic mailboxes | — | P1 to P7 (identity UAT §1, §4b), P8 to P12 (§4c), P13 to P15 (§4d); P-FR and P-AR register in French and Arabic for the language checks |
| DBA-RO | Read-only database role, not an application account | the staging database | The audit and count queries of §3 |

What each role must be refused is part of the check, not a footnote: Intake sees no identity
route, reviewers see neither **Reject finally** nor **Staff-assisted exception**, an outsider sees
404 for a case outside scope, and a forged POST fails the same way as a hidden button (identity UAT
guide R12, B1, B2, Q3-2).

## 3. Live integration checks

Each check lists its preconditions, the operator action, the expected result, the evidence to
retain, the cleanup and its state. Scenario steps live in the guides; the **Action** names the
exact section to run. **Evidence** never contains a secret, a code, a NIN, a name, an address
or an image; it holds timestamps, status codes, counts, audit action codes and the operator's name.

Run the checks in this order. A check that depends on an earlier one stays BLOCKED if that one is.
Every check needs the deployed staging environment (STG-ART-01).

**Per-case versus aggregate evidence.** Where the expected result belongs to one case (one synthetic registration), the evidence is **one row per case**, identified by the restricted test reference (the registration's `public_reference`, held by the UAT coordinator), never by an address, name, message body or identifier: use Q-MSG, Q-STUCK and Q-AUD-CASE (end of INT-13). Totals by status, language or action code (Q-AUD) are used only for events that no case targets, such as sign-in events, and as a cross-check; a total never stands in for a per-case row.

### INT-01 HTTPS, readiness and schedule

* **Preconditions:** the deployment guide §6 to §8 done.
* **Action:** the health checks of deployment guide §8; read the beat log across two schedule intervals.
* **Expected:** `/readyz` 200 with `shared`; workers answer; the identity sweep fires every 60 s.
* **Evidence:** the date, `/readyz` status and body keys, the `inspect ping` result, the beat log lines (task names and times only).
* **Cleanup:** none.
* **State:** NOT_EXECUTED.

### INT-02 Password-only staff sign-in

* **Preconditions:** the §2 staff accounts exist with passwords set; email is not needed.
* **Action:** counter and sign-in guide §4 table (success, wrong password, throttle after 10 failures, suspended account, expired `active_until`, scope refusal, sign-out, session limits, wipe unavailable without `MFA_BACKEND`).
* **Expected:** as that table states; no MFA prompt; audit `ACC_OPERATIONAL_SIGN_IN_SUCCEEDED`, `ACC_OPERATIONAL_SIGN_IN_FAILED`, `ACC_OPERATIONAL_SIGN_IN_THROTTLED`.
* **Evidence:** the pass or fail per row; the aggregate counts of the three sign-in audit codes from query Q-AUD (end of INT-13) for the test window, compared with the number of attempts the operator made (sign-in events target no case, so an aggregate is the right form here).
* **Cleanup:** throttle windows expire by themselves; `provision_staging_uat --retire` at the end of the Beta.
* **State:** NOT_EXECUTED.

### INT-03 Participant email code (real delivery)

* **Preconditions:** SMTP configured (STG-MAIL-01); a synthetic mailbox; the ALTCHA check working (INT-06 not required).
* **Action:** identity UAT guide §5 (E1 to E3); counter and sign-in guide §5.
* **Expected:** one email within the expected time from `DEFAULT_FROM_EMAIL`, in the right language and with the event name; the code works once; reuse is refused; a wrong code is refused with bounded attempts.
* **Evidence:** sent-at and received-at times, sender domain, language, pass or fail. Never the code.
* **Cleanup:** delete the test emails from the mailbox.
* **State:** NOT_EXECUTED.

### INT-04 Private document storage and malware scanning

* **Preconditions:** clamd and the private bucket configured; `check_malware_scanner` passed for the clean buffer and for the standard antivirus test file (deployment guide §10); a synthetic image file.
* **Action:** as participant P4 (foreign passport page) and P6b (identity card), upload a synthetic image; open it as staff R through the identity evidence view; read the object listing as the platform operator. Then stop clamd, try one more upload, and restart clamd.
* **Expected:** the upload is accepted only after a clean scan; a document pending or rejected by the scan is not shown and cannot be used; the object is private (no public URL, no unsigned access); R sees it only through the application; with clamd stopped the upload is refused with "The file could not be checked for safety at the moment…" and nothing is stored.
* **Evidence:** scan verdicts from `check_malware_scanner`, upload results, the object privacy setting as recorded by the platform operator, and per test reference the `IDV_EVIDENCE_VIEWED` rows of Q-AUD-CASE. No image.
* **Cleanup:** remove the synthetic objects by the platform operator's approved procedure.
* **State:** NOT_EXECUTED.

### INT-05 Broker, worker and scheduler

* **Preconditions:** workers and beat running; a participant (P3) who submits an Algerian registration.
* **Action:** identity UAT guide B6 (stop the broker, submit, restore the broker), then run `python manage.py run_identity_verification_jobs --limit 50` until it reports 0 if the sweep has not already processed the job.
* **Expected:** the participant sees the normal confirmation; the job stays `PENDING` while the broker is down and is processed exactly once afterwards; no duplicate case or decision.
* **Evidence:** job counts by status from Q-JOBS before, during and after; and, for the test reference, the Q-AUD-CASE rows `IDV_VERIFICATION_STARTED` and `IDV_PROVIDER_RESULT_APPLIED`, each expected once for that case.
* **Cleanup:** restore the broker; confirm `/readyz` and `inspect ping`.
* **State:** NOT_EXECUTED.

### INT-06 Shared Redis counter: count, outage, recovery

* **Preconditions:** the counter Redis (STG-REDIS-01); at least two web processes; one dedicated synthetic client address.
* **Action:** counter and sign-in guide §7 steps 1, 3, 4 and 5 (step 2 is optional, step 6 is not to be forced).
* **Expected:** 60 × `200` and 1 × `429` across processes; during an outage `/readyz` is 200 `degraded` and each process alerts `HUMAN_CHECK_COUNTER_DEGRADED`; after recovery `shared` and one `HUMAN_CHECK_COUNTER_RECOVERED` per process; writes refused while reachable never show `shared`; the `release_readiness` counter item is BLOCKED, then READY only after a real write.
* **Evidence:** the status-code counts, `/readyz` states and times, alert names with times (no address or URL), the `release_readiness` item states. No Redis URL.
* **Cleanup:** counter and sign-in guide §8: remove the temporary rule, restore any ACL, confirm `shared`; never `FLUSHDB` or `FLUSHALL`.
* **State:** NOT_EXECUTED.

### INT-07 Identity routes and the ministry adapter

Two tracks. Run track A always; run track B only with the owner's written authorization (UAT-02).

* **Track A, disabled provider (no ministry access).**
  * **Preconditions:** accounts R, M and participants; `NIN_PROVIDER_BACKEND` is the disabled provider; clamd working (uploads).
  * **Action:** identity UAT guide P3 to P5 and R1 to R12 for the routes: Algerian NIN route, foreign passport route, the NIN-exemption route (§4c Q3-1 to Q3-8).
  * **Expected:** every Algerian case goes to "Needs manual review" with reason `PROVIDER_NOT_CONFIGURED`; passport and exemption routes unchanged; Intake sees nothing.
  * **Evidence:** the case status and reason codes per route, per test reference, the `IDV_*` rows of Q-AUD-CASE (end of INT-13).
  * **Cleanup:** cancel or withdraw the synthetic registrations by the approved procedure.
  * **State:** NOT_EXECUTED.
* **Track B, official adapter.**
  * **Preconditions:** UAT-02 authorization naming the method and the identities; API-01 and STG-NET-01 resolved; identity provider guide §2 configured, `release_readiness` item `identity_verification_provider` READY; the owner supplies each identity (informed consent; never copied from elsewhere). Invented numbers are not recognized by the real service, so "found" needs an owner-authorized identity.
  * **Action:** identity provider guide §3, for three outcomes: **found** (an authorized identity: expected `Verified by the ministry service`, source "Ministry service"), **not found** (a deliberately mistyped NIN: `NOT_FOUND`, manual review), **provider outage** (the operator blocks the **staging** worker's egress path to the ministry origin on the staging network only, then submits one authorized registration).
  * **Expected (outage):** the case stays pending and retries after about 60 s, 5 min, 15 min and 1 h, then goes to manual review with `PROVIDER_UNAVAILABLE`; after the path is restored, a reviewer uses **Correct NIN and recheck** with the same NIN (identity provider guide §5). The whole outage run takes about 80 minutes.
  * **Evidence:** outcome codes only, the date, the environment, the `check --deploy` and `release_readiness` output, the reviewer's name. No screenshot of identity data.
  * **Cleanup:** restore the egress path, remove the authorized registrations by the approved procedure, confirm the provider item state.
  * **State:** BLOCKED by UAT-02, API-01 and STG-NET-01.

### INT-08 Manual review, correction, staff exception and participation

* **Preconditions:** INT-07 track A; accounts R, M, A.
* **Action:** identity UAT guide §2 (R1 to R12), §3 (M1 to M4), §4 (B1 to B5), §4b (C1 to C9), §4c Q1-1 to Q1-5 and Q2-6.
* **Expected:** as each row states, including: return for correction keeps the same registration and reference, the participant sees only the requested items, a correction is received once, a stale form shows "This case changed after you opened it… Nothing was saved.", approval is refused without a verified identity, and a staff verification of a newer declaration returns the person's older verified cases to manual review (owner decision IDV-Q18).
* **Evidence:** the pass or fail per row; audit codes `IDV_VERIFIED_MANUALLY`, `IDV_RETURNED_FOR_CORRECTION`, `IDV_NIN_CORRECTED`, `IDV_LINKED_CASE_UPDATED`, `IDV_ACTION_REFUSED`, `REV_APPROVAL_BLOCKED_IDENTITY`, as Q-AUD-CASE rows per test reference (never as a total).
* **Cleanup:** as INT-07 track A.
* **State:** NOT_EXECUTED.

### INT-09 Final rejection: one queued message per rejection, and observed delivery

* **Preconditions:** INT-03 passed; the template is published; accounts M, CO and DBA-RO; the UAT coordinator holds the **restricted test references** (the `public_reference` of each synthetic registration) for P9, P-FR and P-AR; the three controlled test mailboxes; the beat and a worker running.
* **What the platform guarantees, and what it does not.**
  * Guaranteed by design: at most one `IDENTITY_REJECTION` message **record** per identity decision. The record is queued in the rejection's transaction and its idempotency key is bound to the decision and is unique.
  * Not guaranteed: exactly-once **provider** delivery. Only a failure the SMTP server confirmed is retried (up to 3 attempts): an explicit refusal reply, or a failure before the message was sent. A server that sends a refusal reply after it actually accepted the message would break that rule and could deliver it twice. Any other failure while sending (a lost connection, a timeout waiting for the final answer) leaves the message in `SENDING` with one attempt whose response code starts with `UNCONFIRMED:`; a worker stopped during the call leaves `SENDING` with no attempt. A message in `SENDING` is never re-sent automatically (no task or sweep picks it up) and may or may not have been delivered. The policy for that case is open: COMM-01.
  * This check therefore verifies the **queue** and records the **observed** delivery to each controlled mailbox. It never claims exactly-once delivery.
* **Action:**
  1. Identity UAT guide Q2-1 to Q2-3 and Q2-8, once for each of the three languages.
  2. After each rejection wait at least two outbox sweep intervals (10 minutes), repeat the rejection attempt as M, and run `python manage.py run_identity_verification_jobs --limit 50`.
  3. The mail operator records, per controlled mailbox, how many messages for that rejection arrived and when (a count and a time; never the body).
  4. DBA-RO runs Q-MSG, Q-STUCK and Q-AUD-CASE (end of INT-13) with the test references.
* **Expected:**
  * Q-MSG, one row per test reference: `rejection_messages` is `1`; the status is `SENT` (or `DELIVERED` if the provider reports receipts); `delivery_attempts` is at least `1`, normally `1`;
  * the repeated rejection attempt is refused and Q-MSG still shows `1` for that reference;
  * each controlled mailbox shows the observed count (expected `1`), in the participant's language, with the reference, the workspace pointer and the invitation note, and no reason, NIN, document or staff note; the wording is the approved COMM-IDV-01 copy; the Arabic text and layout read right to left;
  * Q-AUD-CASE, per test reference: `IDV_REJECTED` once and `REV_DECISION_RECORDED` once for the rejection (more `REV_DECISION_RECORDED` rows only if the test recorded earlier decisions on that registration).
* **Evidence:** one block per test reference, with no name, address, identifier or body: the Q-MSG row, the Q-STUCK result (empty), the mailbox observation (count and received-at), the Q-AUD-CASE rows, and the reviewer's yes or no on the rendered wording. Totals by status and language are not evidence for this check.
* **Stop and escalate (COMM-01):** stop the check and report to the owner when any of these holds:
  * Q-STUCK lists a message in `SENDING`;
  * `rejection_messages` is not `1` for a test reference;
  * `delivery_attempts` is greater than `1`;
  * a mailbox count is `0` or greater than `1` while the message is `SENT`.

  Do **not** re-send, edit rows, delete the message, or restart workers to unstick it. Record the test reference, the status, the attempt count, the Q-STUCK `last_attempt` code and the mailbox count; the mail operator reads the SMTP service's own log for that message and records what it shows; the owner decides. The check stays **ESCALATED (COMM-01)**, never PASSED, until the owner decides. A `QUEUED` or `DEFERRED` message that reaches `SENT` on the next sweep is not an escalation; record that it did.
* **Cleanup:** delete the test emails from the mailboxes; the rejected synthetic registrations stay as history or are removed by the approved procedure.
* **State:** NOT_EXECUTED.

### INT-10 Register again by origin

* **Preconditions:** INT-09 done for at least one rejected participant; the invitation campaign, IM, OB and CM available.
* **Action:** identity UAT guide §4d C1-6 to C1-10 and Q2-3, Q2-4, Q2-7; C1-8 for the pending invitation.
* **Expected (owner decisions IDV-Q17 and IDV-Q19):**
  * open registration: **Register again**, only while public registration is open;
  * usable invitation: **Register again with your invitation**, revalidated, with capacity checked at submission;
  * unusable invitation: no button, the participant is told to ask the inviting organization for a new invitation; after IM issues one and the participant opens it signed in, the workspace shows **Register with this invitation** and it takes exactly one click;
  * on-behalf registration: the participant is told to contact the organization or team, no button.
* **Evidence:** pass or fail per row; the `IDV_REGISTRATION_RESTARTED` row of Q-AUD-CASE per test reference of the registration it was pressed on.
* **Cleanup:** reopen public registration if CM closed it; remove synthetic drafts by the approved procedure.
* **State:** NOT_EXECUTED.

### INT-11 Withdrawal

* **Preconditions:** a registration with an open identity case and a pending job; the participant signed in.
* **Action:** identity UAT guide §4b C5 and C6 (withdraw from the workspace at `/register/workspace/registrations/<id>/withdraw/` through its button, not by address).
* **Expected:** the registration is "Withdrawn"; the correction link is gone; staff see "The registration was withdrawn, cancelled or not approved" and no actions; pending jobs are discarded; no provider call after the withdrawal; an authorized reopening restores the normal path.
* **Evidence:** the case status, job count by status from Q-JOBS, and the `REV_PARTICIPANT_WITHDRAWAL` and `IDV_CLOSED_WITH_REGISTRATION` rows of Q-AUD-CASE for the test reference.
* **Cleanup:** as INT-07 track A.
* **State:** NOT_EXECUTED.

### INT-12 Permissions and scope boundaries

* **Preconditions:** accounts R, M, I, O, A, CO.
* **Action:** identity UAT guide R12, B1, B2, Q3-2, plus the refusals in §2 of this document.
* **Expected:** I gets 403 on `/ops/identity/` and a case address, and "not found" on the generic document link of an identity card; O gets 404 and an empty queue; reviewers are not offered final rejection or exception; a forged request is refused the same way; one permission denied is audited as `IDV_ACTION_REFUSED` with result `DENIED`.
* **Evidence:** the HTTP status per row; the `IDV_ACTION_REFUSED` rows of Q-AUD-CASE for the test reference of each case a refused request targeted.
* **Cleanup:** none.
* **State:** NOT_EXECUTED.

### INT-13 Audit

* **Preconditions:** the database operator's read-only role DBA-RO; the restricted test references; at least INT-08 and INT-09 done.
* **Action:** run the queries below for the test window and compare with the expected codes of each check.
* **Expected:** every sensitive step above produced its audit event. Case-targeted steps are verified **per case** with Q-AUD-CASE; Q-AUD is used only for events no case targets, and as a cross-check. The events carry codes and ids only, and no event holds a NIN, a name, a note or a document. The table is append-only for the runtime role (`audit-isolation`, deployment guide §7).
* **Evidence:** the query output (test references, action codes, results and counts only). State for each expected result whether the evidence is per case or aggregate.
* **Cleanup:** none; audit events stay.
* **State:** NOT_EXECUTED (after INT-08 and INT-09).

Read-only queries for DBA-RO. Table and column names are read from the models, and the queries were executed against the real schema on a disposable test database. Replace `<start>` with the UTC start of the test window and the `<ref-n>` values with the restricted test references. None of them returns an address, name, message body or identifier.

```sql
-- Q-AUD: aggregate audit codes in the window (use only for events no case targets)
SELECT action_code, result, reason_code, count(*) FROM audit_event
WHERE occurred_at >= '<start>' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- Q-AUD-CASE: audit rows per test registration. Registration-targeted events and
-- identity-case-targeted events are both mapped back to the registration's reference.
WITH target AS (
  SELECT r.public_reference, r.id AS target_uuid, 'Registration' AS target_type
  FROM registrations_registration r WHERE r.public_reference IN ('<ref-1>', '<ref-2>')
  UNION ALL
  SELECT r.public_reference, v.id, 'IdentityVerification'
  FROM registrations_registration r
  JOIN people_identity_verification v ON v.registration_id = r.id
  WHERE r.public_reference IN ('<ref-1>', '<ref-2>')
)
SELECT t.public_reference, e.action_code, e.result, count(*) AS events
FROM target t
JOIN audit_event e ON e.target_type = t.target_type AND e.target_uuid = t.target_uuid
WHERE e.occurred_at >= '<start>'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- Q-JOBS: identity jobs by status, and stale pending jobs
SELECT status, count(*) FROM people_identity_verification_job GROUP BY 1;
SELECT count(*) FROM people_identity_verification_job
WHERE status = 'PENDING' AND next_attempt_at < now() - interval '10 minutes';

-- Q-MSG: one row per test registration, including a registration with no message (0)
SELECT r.public_reference,
       count(DISTINCT m.id) AS rejection_messages,
       count(a.id) AS delivery_attempts,
       string_agg(DISTINCT m.status || '/' || m.language, ', ') AS status_language
FROM registrations_registration r
LEFT JOIN (
  SELECT m2.id, m2.registration_id, m2.status, m2.language
  FROM communications_message m2
  JOIN communications_message_template_version v ON v.id = m2.template_version_id
  JOIN communications_message_template t ON t.id = v.template_id
  WHERE t.code = 'IDENTITY_REJECTION'
) m ON m.registration_id = r.id
LEFT JOIN communications_delivery_attempt a ON a.message_id = m.id
WHERE r.public_reference IN ('<ref-1>', '<ref-2>')
GROUP BY r.public_reference ORDER BY r.public_reference;

-- Q-STUCK: a rejection message that has not reached a settled state after 10 minutes
-- (a message in SENDING is an ambiguous outcome: see INT-09, COMM-01. Its last attempt
-- shows UNCONFIRMED:<error> after a lost SMTP answer, or nothing after a stopped worker)
SELECT r.public_reference, m.status, m.created_at,
       (SELECT a.response_code FROM communications_delivery_attempt a
        WHERE a.message_id = m.id ORDER BY a.attempt_number DESC LIMIT 1) AS last_attempt
FROM communications_message m
JOIN communications_message_template_version v ON v.id = m.template_version_id
JOIN communications_message_template t ON t.id = v.template_id
JOIN registrations_registration r ON r.id = m.registration_id
WHERE t.code = 'IDENTITY_REJECTION'
  AND m.status IN ('QUEUED', 'SENDING', 'DEFERRED')
  AND m.created_at < now() - interval '10 minutes'
  AND r.public_reference IN ('<ref-1>', '<ref-2>')
ORDER BY m.created_at;
```

An event whose target is neither the registration nor its identity case (for example a NIN exemption record) does not appear in Q-AUD-CASE; check it in Q-AUD and say so in the evidence.

### INT-14 Broker and counter Redis interruption during a rejection

An optional extension of INT-09. It checks that one message is queued per rejection when a service is interrupted, and records the **observed** delivery. It makes no exactly-once delivery claim. Run it only if INT-05, INT-06 and INT-09 passed and INT-09 had no COMM-01 escalation.

* **Preconditions:** as INT-09, one more participant with a test reference and a controlled mailbox.
* **Action:** stop the **broker** (not the counter Redis), record the rejection as M, restore the broker, wait two outbox sweep intervals. Separately, repeat with the **counter Redis** cut and restored (counter and sign-in guide §7 steps 3 and 4) and record a rejection during the outage. Then run Q-MSG, Q-STUCK and Q-AUD-CASE for each test reference, and have the mail operator record the mailbox observation.
* **Expected:** per test reference, the rejection is recorded once (`IDV_REJECTED` once in Q-AUD-CASE); Q-MSG shows `rejection_messages` `1`; the message is `QUEUED` while the broker is down and reaches `SENT` after the broker returns and the next outbox sweep; the mailbox count is recorded (expected `1`); a counter outage does not stop the rejection or the queuing of its message.
* **Evidence:** per test reference: the Q-MSG row, the Q-STUCK result, the mailbox observation, the Q-AUD-CASE rows, and the `/readyz` states and times.
* **Stop and escalate (COMM-01):** exactly as in INT-09. A broker interruption is the situation in which a message can be left in `SENDING` with an unknown outcome; do not re-send or unstick it, and do not mark the check PASSED.
* **Cleanup:** restore both services; confirm `/readyz` `shared`.
* **State:** NOT_EXECUTED (after INT-05, INT-06 and INT-09).

## 4. Go/no-go checklist

Five separate questions. A yes to one never implies a yes to the next. Tick a box only when a named
human has the evidence.

### A. Local test success

| Item | State |
| --- | --- |
| Full local regression gate on the delivered source revision | PASSED (see the [Beta release notes](../deployment/beta_release_notes.md)) |

### B. Configuration readiness

| Item | State today |
| --- | --- |
| Runtime artifact built, frozen and recorded with its own hash and manifest | Operators |
| `check --deploy` clean with the staging settings | NOT_EXECUTED |
| `audit-isolation` exit 0 with the runtime identity | NOT_EXECUTED (locally it fails Fact B by design: one role) |
| `release_readiness` overall | **BLOCKED** expected: `legal_notices` (C-09) and `retention` (OD-007) until approved facts exist; `sensitive_operation_step_up` without a step-up provider (STEPUP-01); `identity_verification_provider` while the ministry adapter is disabled; `challenge_issuance_counter` until a real counter write. These block production, not the staging Beta |
| `operational_sign_in` | READY (password-only, revised requirement) |

### C. Real integration proof

| Item | State |
| --- | --- |
| INT-01 to INT-06 | NOT_EXECUTED |
| INT-07 track A / track B | NOT_EXECUTED / BLOCKED (UAT-02, API-01, STG-NET-01) |
| INT-09 one queued rejection message per rejection and observed delivery to each controlled mailbox (no exactly-once provider claim; COMM-01 open) | NOT_EXECUTED |
| Real ministry service, real email, real Redis, real storage, real broker, real clamd | **NOT_PROVEN** |

### D. Human UAT

| Item | State |
| --- | --- |
| INT-08, INT-10 to INT-14 | NOT_EXECUTED |
| Registration corrections RC-01 to RC-14 ([checklist](registration_corrections_staging_checklist.md)) | NOT_EXECUTED |
| Identity UAT guide §1 to §6 (P, R, M, B, C, Q, C1 series) and the controlled timing exercise | NOT_EXECUTED |
| Wording approval of the rejection email (COMM-IDV-01) | DECIDED 2026-10-02: approved. This is not delivery proof (INT-09) |
| Owner decisions IDV-Q17, IDV-Q18, IDV-Q19 | DECIDED 2026-10-02: accepted. The UAT cases that exercise them are NOT_EXECUTED |

### E. Production readiness

Not authorized. Production needs, beyond everything above: approved legal facts (C-09; the official
v3 notices still mark them as unconfirmed), retention periods with a purge job (OD-007), the
assistance path for a stateless or unlisted participant (the rest of C-01; the country catalog
itself was approved and installed on 2026-10-04), hosting, data location, key custody
and backups (OD-006), capacity figures (OD-001, INFRA-001, OD-008), database-role isolation for the
audit table, proxies, HSTS (HSTS-01), a restore drill and rehearsals, COMM-01, the step-up decision
(STEPUP-01), the open identity questions (IDV-Q4 to IDV-Q16), and the deployment organization's
own go/no-go decision. **No-go until each is evidenced.**

## 5. Evidence log and sign-off

Keep the log in a restricted evidence folder, outside the repository and outside any review
archive. One row per check; never a secret or personal data.

| Check | Operator | Date and UTC time | Environment | Result | Evidence reference (file name only) |
| --- | --- | --- | --- | --- | --- |
| INT-01 | | | | | |

| Role | Name | Decision | Date |
| --- | --- | --- | --- |
| UAT coordinator | | | |
| Deployment operator | | | |
| Database operator | | | |
| Owner | | | |
