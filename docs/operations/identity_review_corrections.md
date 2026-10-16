# Identity review: cancellations, corrections and notes to participants

Operations notes for the identity review pages (`/ops/identity/`). Code:
`apps/people/selectors/identity.py` (queue), `apps/people/services/identity_review.py`
(actions), `apps/people/views.py`, `templates/identity/`.

Identity verification is never a participation decision. A verified identity
moves an eligible, active registration to participation review once; the final
decision stays in the Review queue.

## 1. Withdrawn and cancelled registrations

A registration withdrawn by the participant or cancelled by the team stops its
identity processing: open Ministry checks are discarded and an open case is
kept as history with the reason "registration closed".

* The status tabs (Manual review, Pending, Returned to participant...) and
  their counters show active registrations only. A closed registration
  (withdrawn, cancelled by the team, or not approved) appears only under
  **Closed registrations** (and in "All"), with its registration status
  ("Withdrawn", "Cancelled by the team", "Not approved").
* Its case page shows a notice and offers no action. Any action sent from a
  page opened before the withdrawal or cancellation is refused with a clear
  conflict message ("This registration was withdrawn or cancelled...");
  nothing is changed.
* A withdrawn or cancelled registration is never reactivated: the Review
  queue no longer offers "Reopen" or "Cancel" for it, the services refuse it,
  and Ministry answers, background jobs and imports that arrive later change
  nothing. The participant may register again; the new registration is a new
  record and the old one stays as history.

No data correction is needed after deployment: the queues are computed from
the registration status, so registrations closed before the release disappear
from the actionable tabs immediately.

## 2. Save corrections and verify identity

On a case in manual review, a reviewer who holds "verify identity manually"
can correct the first name(s), last name, date of birth and (on the national
identity number route, with "correct identity number") the NIN, then press a
single **Save corrections and verify identity** button. Required: the
supporting document that justifies the correction and a reason (data
corrected from the document, typing error corrected, name variant
confirmed); a note is optional. Without a document, see "Verifying without
a document" below.

The NIN field is shown, with the full NIN, only to a reviewer who holds both
"correct identity number" and "view identity evidence" for the case. Any
other reviewer gets no NIN in the page at all (not even hidden): the stored
NIN is kept on the server and cannot be changed, and a NIN posted anyway that
differs from it is refused (403, audited as a refused action).

What happens:

1. Everything is validated before anything is written.
2. If the NIN changed and the Ministry service is configured for automatic
   checks, the corrected NIN is looked up once, outside any database lock.
3. In one transaction, re-checking that the case and registration did not
   change meanwhile (otherwise a visible conflict, nothing saved): the
   corrections are saved, a new identity revision is recorded, earlier queued
   checks are discarded, and the identity is marked
   * **verified by the Ministry service** only when that lookup confirmed the
     corrected values, or
   * **manually verified** otherwise (no lookup, service unavailable, or a
     mismatch the reviewer accepts on the document).
4. The decision records the reviewer, the reason, the document and the field
   names changed (old and new values are kept encrypted in the decision; the
   audit event lists field names only).
5. The registration enters participation review exactly once.

A presumed Ministry birth date is never used to overwrite a date entered by
the participant or the reviewer, and is never completed.

### Verifying without a document (IDV-FIRST)

From the first manual review, **Verify identity** and **Save corrections and
verify identity** are available even when no reviewable identity document
exists, without returning the case or asking for an upload. An existing
document stays selectable (a single one is preselected, as before).

* Choose **No identity document: manual reviewer decision**, the reason
  "No identity document: the reviewer confirms (or corrected the data and
  confirms) ..." and write the internal justification (at least 10
  characters, never shown to the participant). A document reason without a
  document, or the no-document reason with a document, is refused.
* The identity is recorded as **Manual reviewer decision (no document)**
  (`MANUAL_REVIEWER_DECISION`), never as a document review or a Ministry
  verification; no document is created or marked as reviewed. The decision
  history keeps the reviewer, time, reason and justification (encrypted);
  the audit event records the basis, never the justification.
* A changed NIN is still looked up first: a confirming Ministry answer is
  still recorded as a Ministry (or simulation) verification. Foreign
  participants are never looked up.
* The NIN exemption route still needs its declared document (owner decision
  IDV-Q3). The staff-assisted exception still needs its document.
* No correction request and no correction e-mail is created; **Return for
  correction** stays available when the participant must provide something.

## 3. Note to the participant

"Return for correction" has an optional multiline **Note to participant**
(up to 2,000 characters, no control characters). The page shows the language
of the participant's submission, independently of the reviewer's interface
language:

* recorded at submission (registrations submitted after this release);
* derived from the accepted privacy notice for older registrations;
* "Unknown" otherwise: the reviewer must then choose the message language
  explicitly; nothing is sent until a language is chosen.

The note is stored with its own correction request (each cycle keeps its own
note), shown to the participant on the correction page as plain text with
its line breaks (never as HTML), and sent once by e-mail through the outbox
in the participant's language. A double submission or a delivery retry never
sends it twice.
