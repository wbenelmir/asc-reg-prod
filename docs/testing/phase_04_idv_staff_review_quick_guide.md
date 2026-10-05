# Identity review: staff quick guide

For reviewers and managers. Identity verification confirms **who** the person is; it never
approves participation, which stays a separate decision.

## The queue (Operations → Identity review)

* Tabs by status, with counts: **Needs manual review** (your work), Verification pending, Returned
  for correction, Verified by the ministry service, Verified manually, Rejected.
* Filters: reason, nationality group (Algerian NIN / foreign passport), nationality, verification
  source, event. The NIN is masked in the queue.
* **Search** (its own box and button) by reference or name. A full 18-digit NIN works only if you
  may view evidence. The text is sent privately: the address bar only ever shows `ctx=...`, never
  what you typed. "Clear search" ends it. A search expires after 30 minutes and works only for
  you.
* Oldest first. Open a case; your filters and search travel with you.

## The review screen

* **Why this case needs a person**: the reason. Examples: not found, names differ, unreadable
  date, duplicate identifier, foreign passport, participant resubmitted, service problem.
  * "The identity data changed…": the same person's NIN was corrected in another registration.
    This case now shows the corrected NIN, and any earlier verification of the old NIN no longer
    counts.
  * "The registration was withdrawn, cancelled or not approved": nothing can be done until the
    registration is reopened.
  * "Automatic checks stopped after the attempt limit": the service could not be reached, or the
    checking worker stopped, five times.
* **Submitted and official data** — only the facts the platform keeps. "Different" rows are
  highlighted. "Match (case or spacing only)" is accepted automatically, and the official form
  becomes the registration's name. A note then shows the names as the participant entered them.
  Any other spelling, accent or order difference needs you and is never changed automatically. A presumed birth date is shown as "Not compared": the
  participant's date is kept and is **not** ministry-confirmed.
* **Identity evidence** — the document preview. A document pending or rejected by the safety scan
  is not shown and cannot be used. A red **duplicate** banner means another person's registration
  holds this identifier: verification is blocked until that is resolved.

## Actions

| Action | When | Notes |
| --- | --- | --- |
| **Verify identity** | The document matches the submitted data | Choose the document and a preset reason. "Verify and open the next case" continues the queue. |
| **Return for correction** | The participant can fix it (typo, missing or unreadable document) | Tick the requests; the participant sees only those, never your reason or note. |
| **Correct NIN and recheck** | The card shows a different NIN (typing error), or a technical failure needs a new check | A changed NIN needs the document and the confirmation box; a confirmation dialog follows. The case stays pending until the ministry answers. The person's other registrations that used the old NIN switch to the corrected one: they return to manual review, or stay returned for correction. |
| **Staff-assisted exception** (managers) | An existing Algerian NIN-route case in manual review whose identity cannot go through the ministry, confirmed from an official Algerian document | Written explanation required; recorded as an exception. It does not help an Algerian who cannot register for lack of a usable NIN (open question IDV-Q3). |
| **Reject finally** (managers) | The identity cannot be accepted (wrong person, suspected forgery, no valid evidence) | Explanation, confirmation box and dialog. The participant cannot correct it afterwards. |

If someone else (or the system) changed the case while you were looking, you get "This case changed
after you opened it… Nothing was saved." — open the case again. This also happens when the same
person's NIN was corrected in another registration meanwhile.

A closed registration (withdrawn, cancelled, not approved) shows no actions.

## Keyboard (ignored while typing; none submits a decision)

`+` / `-` zoom · `R` / `Shift+R` rotate · `0` reset · `[` / `]` previous / next document ·
`V` go to the Verify form · `N` next case without deciding · `Q` back to the queue.

## Never

* Copy identity data, document images or screenshots of them outside the platform.
* Verify a case that shows the duplicate banner by any other route.
* Treat "Development simulation (not official)" as a real verification (local and test only).
