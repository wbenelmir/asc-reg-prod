# ADR-0026: Post-submission identity verification, durable jobs and staff review

| Field | Value |
| --- | --- |
| Status | Accepted (implementation, IDV-1 to IDV-4) |
| Date | 2026-10-01 |
| Related requirements | Amendments A-12 and A-13 (`docs/execution/requirements_overlay_2026-10-01_identity_and_auth.md`, `..._A13_identity_implementation.md`); TRD §8.4, §13.4, §15.3; Schema §4.6, §7.1, §7.3, §16; ADR-0001, ADR-0006, ADR-0007, ADR-0009, ADR-0013 |

## Context

Before this change the NIN "verification" ran synchronously in the draft identity step, inside its
database transaction, through a local stub that marked any 18-digit NIN ending in an even digit as
verified (finding F-PS-01). The observed ministry contract is different: a lookup by NIN that
returns the official identity, or `identite: null`. The owner decided (A-13) that verification
runs after final submission, asynchronously, from durable work; that identity status stays
separate from participation; that a result applies only to the exact identity revision it checked;
that duplicates are checked against pending and verified claims under concurrency; and that staff
review the remaining cases from evidence.

## Decisions

1. **Ownership.** All identity models live in `apps.people` (ADR-0001: `people` owns identity
   references and matching). The staff screens are `apps.people.views` under `ops/identity/`;
   evidence files stay in `apps.documents`.
2. **State model.** `people.IdentityVerification`, one per submitted Registration, with its own
   status: `PENDING`, `API_VERIFIED`, `MANUAL_REVIEW`, `MANUALLY_VERIFIED`,
   `RETURNED_FOR_CORRECTION`, `REJECTED`. A separate `reason_code` (current manual-review reason)
   and `verification_source` (only while verified: `MINISTRY_API`, `SIMULATED_API`,
   `MANUAL_NATIONAL_ID_CARD`, `MANUAL_PASSPORT`, `STAFF_EXCEPTION`; a check constraint enforces
   "source if and only if verified"). It never changes participation status, except the
   documented mapping: a return for correction sets the existing Additional Information Required
   public status (internal `AWAITING_APPLICANT`); the participant's resubmission sets `SUBMITTED`
   (internal `VERIFICATION_PENDING`); a rejection of a returned case sets `UNDER_REVIEW`. Identity
   verification never approves participation.
3. **Revisions.** `people.IdentityRevision` is an append-only snapshot per submission,
   resubmission, NIN correction or recheck. It stores no clear identity value: a keyed HMAC
   `fingerprint` of route, identifier, names and birth date (with its key version) and the names of
   changed fields. Every attempt is bound to a revision.
4. **Durable work (outbox).** `people.IdentityVerificationJob` (one per revision) is inserted in
   the submission or correction transaction. After commit, a `robust=True` `on_commit` callback
   tries `.delay()`; any broker error is swallowed and logged by class name (ASYNC-01). A beat task
   (`people.dispatch_due_identity_verification_jobs`, every 60 s) re-enqueues due jobs, and
   `manage.py run_identity_verification_jobs` processes them inline when the broker is down. In
   local and test eager mode after-commit dispatch is off, so the provider is never called inside a
   participant's request even there.
5. **Worker.** `process_identity_job` claims a job with a lease token (row lock,
   `skip_locked`, `of=("self",)`), checks staleness (revision superseded, status changed,
   registration closed, fingerprint changed), calls the provider OUTSIDE any transaction
   (enforced: Django's `atomic_blocks` must hold only test-case blocks) inside one of
   `IDENTITY_PROVIDER_MAX_CONCURRENCY` session-level advisory-lock slots (class 8), then applies the
   result in a new durable transaction: verification row lock, job row lock, lease check, stale
   check, comparison, final duplicate recheck, state change, attempt, audit. Transient failures
   retry on `IDENTITY_VERIFICATION_RETRY_DELAYS_SECONDS` (60, 300, 900, 3600 s; five attempts) and
   then go to manual review; definitive outcomes (not found, mismatch, invalid response) are never
   retried as outages. A lost lease or a stale result is recorded with `applied=False` and changes
   nothing. A pending case whose identity changed elsewhere goes to manual review
   (`IDENTITY_DATA_CHANGED`) instead of waiting forever.
6. **Provider seam.** `NinLookupProvider.lookup(nin)` returns a typed `LookupOutcome`. Backends:
   the official `MinistryNinProvider` (standard-library HTTPS, see the contract document);
   `DisabledNinProvider` (default outside local development); development simulations
   (`LocalSimulationNinProvider`, which answers synthetic bodies through the same contract code;
   `UnavailableSimulationNinProvider`). Staging and production refuse every backend other than the
   official and the disabled one at startup (A13-06), and the worker refuses a simulation at run
   time unless `IDENTITY_ALLOW_SIMULATED_PROVIDER` is on (local and test only). A simulated match is
   `API_VERIFIED` with source `SIMULATED_API` and is labelled "not official" everywhere.
7. **Contract and matching** are pure functions in `apps.people.identity_contract` (see
   `docs/security/identity_provider_contract.md`).
8. **Duplicates and locking.** NIN uniqueness is person-level and event-independent: a VERIFIED
   identifier of another person always conflicts; a DECLARED one conflicts while it is the current
   identifier of an active registration whose identity is pending, in manual review or returned.
   Drafts never conflict, and the same Person never conflicts with itself. Every decision that
   depends on that answer holds the same transaction-level advisory locks as identifier creation
   (class 6, every active blind-index version). The existing partial unique index on VERIFIED
   identifiers is the database backstop. Lock order everywhere: Registration row, then
   IdentityVerification row, then identifier advisory locks, then identifier rows; no path takes an
   advisory lock and then waits for another case's row.
9. **Staff commands** (`apps.people.services.identity_review`) check their own dedicated
   permission in the case's exact scope, lock with `expected_version` (a stale decision is a 409
   conflict), and write an append-only `IdentityDecision`. Permissions and the role mapping are in
   `docs/security/identity_review_permissions.md`.
10. **Evidence.** A new `NATIONAL_ID_CARD` document type, requested only for Algerian manual
    review, uses the existing image formats, 8 MB limit, private storage and malware scan. The
    passport identity page is now required (and always offered) on every foreign passport path;
    the per-event switch is no longer consulted (the column stays). Only ACTIVE and CLEAN
    documents are reviewable evidence. Staff preview identity documents through
    `ops/identity/cases/<id>/evidence/<doc>/` (exact scope, `view_identity_evidence`, no-store,
    nosniff, same-origin resource policy, audited); the generic document stream now also requires
    `view_identity_evidence` for identity documents.
11. **Route rules.** Algerian nationals must use the NIN path (form and service refuse the
    passport path for them); foreign nationals use the passport path with no ministry job. The
    staff-assisted exception verifies an Algerian NIN-route case in manual review from an official
    Algerian document (national identity card or a staff-requested passport identity page), with
    `apply_identity_exception`, a preset reason and a written explanation; it is recorded as
    `STAFF_EXCEPTION` and never overrides a duplicate.

## Amendment IDV-C1 (2026-10-02, independent review CHANGES_REQUIRED)

These points supersede the lock order in decision 8 and complete decisions 5, 6, 9 and 11. The
findings are R-IDV-01 to R-IDV-06 of the independent review; the evidence is in
the historical review record `phase_04_idv_c1_report.md` (kept outside the repository).

12. **Lock order (supersedes decision 8).**
    1. The Person row (`FOR NO KEY UPDATE`), taken first by the submission start, every staff
       command and the participant resubmission. It serializes one person's identity lifecycle
       without blocking rows that only refer to the person.
    2. Registration rows, then IdentityVerification rows: the command's own case first, then the
       person's other cases that share its identifier, by registration id. The worker (applying a
       result, stopping an exhausted job) and the closure step take the Registration before the
       case as well, because a result inserts rows that refer to the Registration.
    3. The identifier advisory locks (class 6).
    4. The identifier rows.

    No path takes an advisory lock and then waits for a row, and only holders of the Person lock
    wait for a second registration.
13. **Shared identifiers (R-IDV-01).** One person's registrations in different events share one
    identifier. When it is replaced in one case (a staff NIN correction or a participant
    resubmission), every other current case of the person that uses it is rebound to the
    replacement in the same transaction: a new revision (`LINKED_IDENTITY_CHANGE`), a new version,
    and an `IdentityDecision` with the originating actor.
    * An open or verified sibling goes to manual review (`IDENTITY_DATA_CHANGED`) and its
      verification source is cleared.
    * A returned sibling stays returned.
    * A rejected one keeps its decision.

    The final write of any verification (worker or staff) re-reads and locks the identifier row
    after the advisory lock, checks the revision fingerprint against it, and updates it only from
    DECLARED or VERIFIED. A replaced, revoked or expired identifier is never promoted, and a manual
    confirmation of a changed identity is refused (`IdentityStateError`).
14. **Closed registrations (R-IDV-06).** A withdrawal, an operational cancellation or a NOT_APPROVED
    decision ends the identity work in the same transaction
    (`close_identity_work_for_registration`).
    * Pending jobs are discarded.
    * A pending case or an open correction request goes to manual review (`REGISTRATION_CLOSED`).
    * Decided cases keep their decision.

    The participant correction is offered and accepted only while the registration awaits the
    participant (Additional Information Required). Every staff identity command refuses a closed
    registration. No identity service changes a closed Registration. An authorized reopening
    finds the case waiting for staff.
15. **Official name form (R-IDV-02, A13-04, IDV-03).** When a verified result differs from a
    submitted name only by case or spacing, the official Latin form becomes the current profile
    name.
    * It is set in the same transaction, recorded as a revision (`OFFICIAL_NAME_NORMALIZATION`)
      whose fingerprint covers the new names, so the case stays revision-bound.
    * An `OFFICIAL_NAME_APPLIED` decision names the fields and the source (ministry or
      simulation).
    * The submitted originals stay in the immutable submission snapshot.
    * Substantive differences are never corrected, and the birth date is never touched.
16. **Attempt budget (R-IDV-04, completes decision 5).** Every claim of a job is an attempt,
    including one whose worker died. An expired lease is reclaimed only while attempts remain.
    After that, the job becomes EXHAUSTED and the case goes to manual review
    (`PROVIDER_ATTEMPTS_EXHAUSTED`) in one transaction, without another provider call. A busy
    provider slot is not an attempt; a late result from a lost lease changes nothing.
17. **Absolute request deadline (R-IDV-03, completes decision 6).**
    `MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS` bounds the whole HTTPS request: name resolution, TCP
    connect, TLS handshake, sending, headers and body. Every socket call is given only the time
    left, and a response that completes after the deadline is refused.
    * Name resolution cannot be interrupted, so it runs on a daemon thread that is abandoned at
      the deadline. The request still ends on time.
18. **Private queue search (R-IDV-05).** The search is a CSRF-protected POST. It is resolved once
    into case ids kept in the reviewer's server-side session under a random reference (`ctx`), so
    no search text or NIN enters a URL, a cookie or the session. The reference is user- and
    session-bound and expires after 30 minutes. Scope (and the evidence permission for a NIN
    search) is applied again on every use. A legacy `?q=` address is redirected without being
    used.

The amendment adds the choices-only migration `people.0004_idv_c1_lifecycle_choices` (no SQL).

## Amendment IDV-Q (2026-10-02, owner decisions IDV-Q1 to IDV-Q3)

The owner approved IDV-Q1, IDV-Q2 and IDV-Q3 on 2026-10-02. These points complete decisions 2,
9 and 11 and supersede the sentence "identity verification never approves participation" only
in the narrow sense below: identity still never approves anything, but approval and eligibility
now require it. The evidence is in the historical review record `phase_04_idv_q_owner_decisions_report.md` (kept outside the repository).

19. **Approval needs a cleared identity (IDV-Q1).** `record_approved_decision` refuses, before
    any write and with a DENIED audit event (`REV_APPROVAL_BLOCKED_IDENTITY`, reason = the
    clearance code), unless the registration's identity is cleared
    (`apps.people.selectors.clearance`). Cleared means all of the following:
    * an identity case exists. A registration submitted before identity verification has none,
      and is refused as `NO_IDENTITY_CASE`; it is never assumed verified, and IDV-Q4 decides
      any backfill;
    * the case is `API_VERIFIED` or `MANUALLY_VERIFIED`;
    * the identifier of its current revision is still `VERIFIED`;
    * the document has not expired;
    * the source is not `SIMULATED_API`, unless `IDENTITY_ALLOW_SIMULATED_PROVIDER` is on (local
      and test only; staging and production refuse it at startup).

    The check runs under the Registration lock and locks the case `FOR NO KEY UPDATE` (lock
    order: Registration, then case). An identity command therefore cannot change the case
    before the approval commits. The approval audit records only the identity status and
    source. Identity and participation keep separate histories.
20. **Downstream eligibility follows the identity (IDV-Q1).** The one shared definition of an
    active approved context (`active_approved_context_q` and `is_active_approved_context`) adds
    the same clearance. It is used by passes, physical badges, online entry, offline packages
    and deltas. The accreditation boundary `evaluate_eligibility` adds it too
    (`IDENTITY_NOT_CLEARED`), and so does the offline re-evaluation `approved_at` (from current
    rows, like the rest of the registration state). An approval recorded before the identity was
    rejected, rebound to review, replaced, expired or found simulated therefore stops being
    eligible everywhere at once. The participation decision is not rewritten.
21. **A final identity rejection is a participation outcome (IDV-Q2).** `reject_identity`
    records, in its own transaction, a NOT_APPROVED `RegistrationDecision`
    (`record_identity_rejection_outcome`, internal reason `IDENTITY_REJECTED`, participant
    reason `IDENTITY_NOT_VERIFIED`).
    * The decision supersedes the current decision, an earlier approval included.
    * Open review work ends, and an open correction request is withdrawn.
    * The context is marked non-current. This releases its deduplication key, so the
      participant can start a new registration from the same account
      (`start_registration_after_identity_rejection`, "Register again", while the public
      channel is open). The rejected registration keeps its reference and history, and cannot
      be reopened.
    * One notification is queued with the dedicated `IDENTITY_REJECTION` template, with
      idempotency key `identity-rejection:<identity decision id>`. It names no reason,
      provider result, identifier, document or note. The note and reason stay in the
      `IdentityDecision`.
    * A return for correction is unchanged: it stays the remediable path on the same
      registration.
22. **NIN exemption route (IDV-Q3).** `people.NinExemption` is a staff grant for ONE Algerian
    draft without a usable NIN.
    * The grant needs `people.grant_nin_exemption` in the registration's exact scope (Accreditation
      Managers only), a preset reason, a written explanation (encrypted) and a deliberate
      confirmation. It is audited and revocable while the registration is a draft.
    * With it, and only with it, the identity step offers `NIN_EXEMPTION`: an Algerian national
      identity card (new identifier type `NATIONAL_ID_CARD`, the card's own number) or an
      Algerian passport (issuing country DZ, expiry required), with a photo that goes through
      the existing private, scanned document path.
    * The final submission marks the grant USED and creates the case on route `NIN_EXEMPTION`,
      in `MANUAL_REVIEW` with reason `NIN_EXEMPTION_REVIEW`. No job is scheduled and no NIN
      exists for it. An unused grant is closed (REVOKED by the system) when the registration is
      submitted on another route.
    * Verification is documentary only: `verify_identity_manually` from the declared document's
      image, source `MANUAL_NIN_EXEMPTION`. The NIN correction and recheck, and the NIN-route
      staff exception, refuse the route.
    * The cross-person duplicate rule applies to the document number. The foreign-national
      passport route is unchanged.
    * Lock order: the Registration, then the exemption. The submission takes the Person between
      them, as before.

The amendment adds `people.0005_idv_owner_decisions` (choices, plus the `people_nin_exemption`
table) and the data migration `communications.0007_idv_q2_identity_rejection_template`.

## Amendment IDV-Q-C1 (2026-10-02, consolidated correction of IDV-Q)

The owner decisions stay as approved. These points correct how decisions 19 to 22 are applied;
the evidence is in the historical review record `phase_04_idv_q_c1_report.md` (kept outside the repository).

23. **Review-command lock order (corrects decision 21).** Every review command that may change a
    Registration locks the Registration row first, and then its own review case or information
    request (`_lock_registration_of`).
    * The commands covered are the case assignment and status change, the duplicate resolution,
      sending, cancelling and closing an information request, and the participant's information
      response.
    * Every command that ends review work already uses this order: decisions, withdrawal,
      operational cancellation and the final identity rejection lock the Registration, then the
      open cases and requests. The former reverse order could deadlock with a rejection.
    * Version checks and terminal-state guards are unchanged. Two guards are added: a completed
      or cancelled case is never assigned, and a closed (NOT_APPROVED or WITHDRAWN) registration's
      review work is never reopened by these commands.
24. **A bound identifier is never changed in place (corrects decisions 19 and 22).** An
    identifier that is VERIFIED, or that any submitted identity revision uses, keeps its expiry.
    * A new draft that declares another expiry (later, earlier, or none) gets a new identifier
      row.
    * A participant correction that changes only the expiry replaces the identifier, like a
      number change, and rebinds the person's other cases (decision 13).
    * Before a staff verification, an older verified declaration of the same document by the
      same person is retired (REPLACED). Its cases are rebound to manual review with a recorded
      decision (`supersede_same_person_declarations`).
    * Another registration's eligibility therefore changes only through an authorized
      verification, never silently. A genuine expiry still blocks eligibility.
25. **Register again by origin (corrects decision 21).** The next step follows the rejected
    registration's own origin (`restart_option_for`):
    * **Open registration:** a new public registration while public registration is open.
    * **Invitation:** a new registration through the SAME invitation, while its link could start
      one now (active, not expired, campaign valid and not full, event not closed). It is created
      by `create_invited_draft_registration`, which re-validates under lock. Capacity is still
      enforced at submission.
    * **Unusable invitation:** no button. The participant is told that a new invitation from the
      inviting organization is needed. A new invitation opened while signed in can be used from
      the workspace (`start_pending_invitation`).
    * **On behalf or delegation:** contact the organization or the registration team.

    The public channel is never offered as a way around an invitation. The rejection email
    points to the workspace for the next step. Its copy (`communications.0007`, corrected in place
    while unapplied on every persistent database) was `v1-draft` placeholder copy when this
    amendment was written; the owner approved the wording on 2026-10-02 (see the owner decisions
    below).

The amendment adds no migration.

### Owner decisions of 2026-10-02 on IDV-Q-C1

Recorded by the staging-preparation assignment (documentation and metadata only; no behavior,
migration or setting changed):

* **COMM-IDV-01:** the exact EN/FR/AR `IDENTITY_REJECTION` copy in `communications.0007` is
  approved for real delivery, subject to normal staging configuration, delivery testing and
  release gates. The approval covers the wording only; real delivery stays NOT_PROVEN until the
  staging UAT records it. The version label `v1-draft` is unchanged (it is a shared label, not a
  status), and the copy and its content hash are unchanged.
* **IDV-Q17 (decision 25):** the origin-aware register-again behavior is accepted as implemented.
* **IDV-Q18 (decision 24):** the conservative rule is accepted: verifying a newer declaration of
  the same document returns the person's older linked verified cases to manual review.
* **IDV-Q19 (decision 25):** one explicit workspace click to use a pending invitation is accepted.

## Consequences

* The draft identity step no longer calls any provider and no longer creates verification
  attempts; the former draft-step stub tests were rewritten or replaced (see the IDV report).
* `release_readiness` has a new item `identity_verification_provider` (BLOCKED unless the
  official adapter is selected and fully configured) and `check --deploy` warns `people.W001`.
* Identifiers marked VERIFIED by the old stub in a development database are not rewritten by the
  migration (no production data exists); a development database should be recreated or reviewed.
* Registrations submitted before this change have no identity case; they are not forced.
* Migrations `people.0003_identity_verification` and `documents.0002_national_id_card_document_type`
  are additive; rollback is `migrate people 0002` and `migrate documents 0001` (it drops the new
  tables and columns and therefore the identity history recorded since).
