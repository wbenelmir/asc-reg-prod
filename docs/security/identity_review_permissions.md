# Identity review permissions and role mapping (IDV-3, A13-05)

Date: 2026-10-01. Language: English. Code: `apps/people/models` (`IdentityVerification.Meta.permissions`),
`apps/people/policies/__init__.py`, `apps/reviews/apps.py` (group bootstrap).

## Permissions

Each is a separate Django permission, granted only through a `ScopedGroupMembership` whose own
group holds it, and checked against the exact event and organization scope of the case on every
endpoint **and** inside every service (a hidden button, a forged POST or a direct service call is
refused the same way; a case outside scope is a generic 404).

| Permission | Allows |
| --- | --- |
| `people.view_identityverification` | The identity queue and the review screen, with the NIN or passport number **masked** |
| `people.view_identity_evidence` | Evidence previews, the full NIN or passport number, full-NIN search, and streaming identity documents through the generic document link |
| `people.verify_identity_manually` | Verify from reviewed evidence (national identity card on the NIN route, passport identity page on the passport route) |
| `people.correct_identity_nin` | Correct a NIN from evidence and recheck it, or recheck an unchanged NIN after a technical failure |
| `people.return_identity_for_correction` | Return the identity to the participant with preset requests |
| `people.reject_identity` | The final identity rejection (explanation and deliberate confirmation required) |
| `people.apply_identity_exception` | The Algerian staff-assisted exception (explanation required) |
| `people.grant_nin_exemption` | Grant or revoke the NIN exemption route for one Algerian draft without a usable NIN (owner decision IDV-Q3; reason, explanation and confirmation required). Checked in the **registration's** exact scope, because no identity case exists yet |

## Mapping onto existing scoped roles (least privilege)

| Existing group | Identity permissions | Rationale |
| --- | --- | --- |
| Registration Reviewers | view, view evidence, verify manually, correct NIN, return for correction | Day-to-day document review casework |
| Accreditation Managers | all seven, including the final rejection and the staff exception, plus `grant_nin_exemption` | Managers already own final participation decisions |
| Registration Intake (read-only) | none | Read-only staff must not gain identity data or any mutation |
| Every other group | none | — |

The mapping is applied by the idempotent `post_migrate` bootstrap, which only adds permissions. A
user gains them only by holding a scoped membership in one of these groups; nothing grants them to
every staff account. An administrator can narrow a person's capability by creating a dedicated
group (for example view-only identity review) without code changes.

## Audit

Audited without any civil data (codes, statuses, counts and ids only): a case view
(`IDV_CASE_VIEWED`), an evidence preview (`IDV_EVIDENCE_VIEWED`), each decision
(`IDV_VERIFIED_MANUALLY`, `IDV_VERIFIED_BY_EXCEPTION`, `IDV_RETURNED_FOR_CORRECTION`,
`IDV_NIN_CORRECTED`, `IDV_RECHECK_REQUESTED`, `IDV_REJECTED`), the participant's resubmission, each
applied, discarded or retried provider result, and refused actions (`IDV_ACTION_REFUSED`, result
`DENIED`). Generic document streams keep their existing `DOCUMENT_STREAMED` event. Internal notes
are encrypted and shown only on the staff review screen.

IDV-C1 adds these audit events: `IDV_LINKED_CASE_UPDATED` (a case rebound after the same
person's identifier was replaced in another registration), `IDV_OFFICIAL_NAME_APPLIED`,
`IDV_PROVIDER_ATTEMPTS_EXHAUSTED`, `IDV_CLOSED_WITH_REGISTRATION`, and `IDV_QUEUE_SEARCHED`. The
last one records only the kind of search (`nin` or `text`) and the number of matches, never the
text.

## Queue search (IDV-C1, R-IDV-05)

* A search needs `people.view_identityverification`. A full-NIN search matches only cases
  where the reader also holds `people.view_identity_evidence`. It is an exact blind-index
  match, never a plaintext scan.
* The search is resolved once into case ids, kept in the reader's server-side session under a
  random reference. That reference is bound to the reader and the session and expires after 30
  minutes.
* Scope, and for a NIN search the evidence permission, is applied again on every page, tab,
  next-case step and redirect. A reader whose scope shrinks loses those results at once.
* Another reader's reference is ignored.
* No search text or NIN is ever stored in the session, a cookie or a URL.

## Closed registrations (IDV-C1, R-IDV-06)

Every identity command (verify, exception, return, correct NIN or recheck, reject, and the
participant resubmission) refuses a withdrawn, cancelled or not-approved registration, under
the registration lock. The review screen offers no action for such a case. An authorized
reopening restores the normal path.

## Owner decisions IDV-Q1 to IDV-Q3 (2026-10-02)

* **Approval and eligibility (IDV-Q1).** `reviews.add_registrationdecision` is unchanged, but the
  approval service refuses a registration whose identity is not cleared (ADR-0026 decision 19).
  It records `REV_APPROVAL_BLOCKED_IDENTITY` (result `DENIED`, reason = the clearance code such as
  `IDENTITY_NOT_VERIFIED`, `NO_IDENTITY_CASE` or `SIMULATION_NOT_ACCEPTED`). The approval audit
  adds the identity status and source, never identity data. The review case page shows the coded
  explanation to deciders only.
* **Final rejection (IDV-Q2).** `people.reject_identity` now also records the NOT_APPROVED
  participation decision. That decision is a consequence of the identity decision; the reviewer
  needs no participation permission for it, and its audit event (`REV_DECISION_RECORDED`, origin
  `identity_rejection`) names the identity decision. The participant sees only the coded outcome
  and the generic message; the reason and the note stay in the identity history. The participant's
  "Register again" (`IDV_REGISTRATION_RESTARTED`) acts only on the participant's own registration.
* **NIN exemption (IDV-Q3).** The grant needs `people.grant_nin_exemption` in the registration's
  exact scope, on the route and again in the service. Audit events, without the explanation or any
  identity value:
  * `IDV_NIN_EXEMPTION_GRANTED` (reason code);
  * `IDV_NIN_EXEMPTION_REVOKED` (by staff, or by the system for a grant left unused at submission);
  * `IDV_NIN_EXEMPTION_DOCUMENT_DECLARED` (participant; the document kind only);
  * `IDV_NIN_EXEMPTION_USED`.

  The documentary case is verified with the ordinary `people.verify_identity_manually`, from the
  declared document only. Staff find the draft by its exact registration reference on the intake
  page. That search uses no name and no identifier, so no identity value enters a URL.
