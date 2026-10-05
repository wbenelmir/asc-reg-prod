# Identity verification: manual UAT guide for every role and account (UAT-01)

Date: 2026-10-01. Language: English. **Synthetic data only.** Record observed results in the
"Observed" column; never record a real identity, a screenshot showing identity data, a password,
a code or a token. These scenarios are human checks; they are not satisfied by the automated
tests, and none of them has been executed yet.

**Staging run order and real-integration checks:** this guide holds the scenario steps. The
sequence for running them in staging (prerequisites, deployment, accounts, per-integration
evidence, go/no-go) is `docs/testing/phase_04_staging_uat_handoff.md`.

## 0. Environment

| Where | Provider | How cases move |
| --- | --- | --- |
| Local development | Development simulation (synthetic bodies; "not official" everywhere) | After each submission run `uv run --env-file .env python manage.py run_identity_verification_jobs` |
| Staging without ministry access | Disabled backend: every Algerian case goes to manual review (`PROVIDER_NOT_CONFIGURED`) | Worker and beat |
| Staging with the official adapter | Only with the owner's authorization (integration guide §3) | Worker and beat |

Local simulation NINs (all invented): `990000000000000010` BENTEST/AMINA, 07/03/1990 (match);
`990000000000000028` OUZTEST/KARIM, presumed date (any birth date matches);
`990000000000000036` BENTESTI/SAMIR, 15/06/1992 (name mismatch for "Bentesty");
`009900000000000044` ZEROTEST/LINA, 20/11/1988 (leading zeros); `990000000000000052` (answers for
another NIN); `990000000000000079` (unreadable date); `990000000000000087` (no presumed flag);
`990000000000000001` (outage); `990000000000000002` (invalid response); any other NIN: not found.

Accounts to prepare (synthetic mailboxes under the team's control; scoped to the test event):

| Account | Group | Purpose |
| --- | --- | --- |
| Reviewer R | Registration Reviewers | Document review, correction, return |
| Manager M | Accreditation Managers | Final rejection, staff exception, participation decision |
| Intake I | Registration Intake | Read-only: must see no identity data |
| Outsider O | Registration Reviewers, scoped to ANOTHER event | Must see nothing |
| Participants P1 to P6 | — | Email OTP accounts |

## 1. Participant journeys

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| P1 | Sign in by email code; register as an Algerian with NIN `990000000000000010`, "Amina" / "Bentest", 07/03/1990; submit | Confirmation as before; workspace shows the registration "Submitted"; no identity outcome shown to the participant | |
| P2 | Register as an Algerian with `990000000000000028`, any date | After the worker runs: staff see "Verified by the ministry service" (simulation: "not official") with "Skipped (presumed date)" | |
| P3 | Register as an Algerian with an unknown NIN | Staff see "Needs manual review" / "did not find this NIN" | |
| P4 | Register as a French national: choose the foreign passport path; try to continue without the identity page | Refused: "Upload a photo or scan of the passport identity page." With the page: accepted; staff see "Foreign participant: passport review required"; no ministry check | |
| P5 | Register as an Algerian and choose the foreign passport path | Refused: "Algerian nationals use the national identity number path." | |
| P6 | After staff return P3 for correction: open the workspace | "Action required — Correct your identity information"; the page lists only the requested items, no reason | |
| P6b | Correct the NIN, upload the identity card, send | "Your corrected information was received"; same registration and reference; staff see a new revision; an email "Your response has been received" | |
| P6c | Press send again from the browser back button | "Your correction was already received."; no second revision | |

## 2. Reviewer R: document review

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| R1 | Open **Identity review** | Queue of cases in scope, oldest first; counts per status; NIN masked | |
| R2 | Filter by nationality group, nationality, reason, source; search a reference; search a full NIN with **Search** | Each filter narrows the list; the full-NIN search finds only cases whose evidence R may view; after any search the address bar shows only `ctx=...` (never the NIN or the text); "Clear search" removes it (IDV-C1) | |
| R3 | Open P3's case after the card is uploaded | Reason banner; submitted beside retained official data; card preview beside the facts (side by side on desktop, stacked on a phone) | |
| R4 | Zoom in/out, rotate, reset; switch documents; press `+`, `R`, `0`, `[`, `]` | The image changes; nothing is submitted | |
| R5 | Click in the note box and type `r` and `+` | Text is typed; the image does not change | |
| R6 | Press `V` | Focus moves into the Verify form; nothing is submitted | |
| R7 | Verify with the card and a preset reason, using "Verify and open the next case" | Success message; the next case of the same filters opens; P3's case shows "Verified manually" and source "Manual review of the national identity card"; participation status unchanged | |
| R8 | Return a case for correction with "Check your national identity number" and "Upload a clear photo of your national identity card" | The case shows "Returned for correction"; the participant's registration shows "Additional information required"; an email to the participant names no reason | |
| R9 | On a NOT_FOUND case with a card: **Correct NIN and recheck** with a changed NIN, without ticking the confirmation | Refused with a message; nothing changes | |
| R10 | Repeat R9 with the confirmation and the card | Confirmation dialog first; then "The NIN check was queued"; the case stays "Verification pending" until the worker answers | |
| R11 | Try to correct to a NIN another person holds | "Another person's registration holds this identifier"; nothing changes | |
| R12 | Look for "Reject finally" and "Staff-assisted exception" | Not offered to R | |

## 3. Manager M

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| M1 | Reject a case finally without an explanation | Refused | |
| M2 | Reject with an explanation and the confirmation box | Confirmation dialog; then "Identity rejected. Participation is decided separately."; no participation decision is recorded | |
| M3 | Staff-assisted exception on an Algerian NOT_FOUND case with the card, with an explanation | Confirmation dialog; source "Staff-assisted exception" (never "Ministry service") | |
| M4 | Open the participation review case of a verified registration | The identity status chip and a link to the identity review are shown; approving participation is still a separate decision | |

## 4. Boundaries and concurrency

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| B1 | Intake I opens `/ops/identity/` and a case URL | Refused (403); the generic document link of an identity card is "not found" for I | |
| B2 | Outsider O opens a case URL of the test event | Not found (404); queue empty | |
| B3 | R and M open the same case in two browsers; R verifies, then M verifies | M sees "This case changed after you opened it… Nothing was saved." | |
| B4 | Double-click "Verify identity" | One decision only | |
| B5 | Two participants submit the same NIN | The second case shows the duplicate warning; Verify is disabled; a forced POST is refused | |
| B6 | Stop the broker (staging), submit an Algerian registration | The participant sees the normal confirmation; the case stays pending; after the broker returns (or `run_identity_verification_jobs`), it is processed once | |

## 4b. IDV-C1 corrections (independent review R-IDV-01 to R-IDV-06)

Added 2026-10-02. Use a second synthetic test event E2 with the same scoping as the first, and
one participant P7 who registers in both events. None of these has been executed yet.

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| C1 | P7 registers in both events with the same unknown NIN; both cases reach manual review. R opens P7's case in event 1, and in a second tab corrects the NIN in P7's event-2 case (card, reason, confirmation). Back in the first tab, R presses **Verify identity** | "This case changed after you opened it… Nothing was saved." Reopening the case shows the reason "The identity data changed…" and the corrected NIN. The old NIN is never shown as verified anywhere | |
| C2 | As C1, but P7's event-1 case was already verified (NIN `990000000000000010`, names "Amina" / "Bentest"; event-2 names "Samira" / "Bentest"). R corrects the NIN in the event-2 case | The event-1 case returns to manual review ("The identity data changed…") and no longer shows a verification source | |
| C3 | P1 (`990000000000000010`, "Amina" / "Bentest") after the worker runs | The case shows the names as "AMINA BENTEST" with the note "The names were set to the official form (letter case or spacing only). As entered: Amina Bentest". The birth date is unchanged | |
| C4 | A participant with `990000000000000036` and the names "Samir" / "Bentest" | Manual review ("Names or birth date differ…"); the names are NOT changed | |
| C5 | R returns a case for correction; the participant opens the correction page, then withdraws the registration in another tab, then sends the correction from the first tab | The workspace says there is no identity correction to make; the registration stays "Withdrawn"; the correction link is gone; staff see "The registration was withdrawn, cancelled or not approved" and no actions | |
| C6 | M reopens the withdrawn registration of C5 (participation review) | The identity case waits for staff; R can return it for correction again; the participant's correction is then accepted | |
| C7 | M records NOT_APPROVED for a registration whose identity is in manual review | The identity review screen offers no action and says the registration is closed | |
| C8 | R searches a full NIN; copies the address into another account's browser | The other account sees "This search is no longer available"; the address never contains the NIN; browser history shows no NIN | |
| C9 | Operator, staging only: stop the worker mid-check five times (or wait for five lease expiries) | After the fifth attempt the case is in manual review with "Automatic checks stopped after the attempt limit"; the provider is not called again | |

## 4c. Owner decisions IDV-Q1 to IDV-Q3

Added 2026-10-02. Use the same synthetic events and roles: R (Registration Reviewers), M
(Accreditation Managers) and participants P8 to P12 with new synthetic mailboxes. Give each
registration its Participant Role, Badge Type and Access Profile before the approval steps. None
of these has been executed yet.

**IDV-Q1: approval requires a verified identity**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| Q1-1 | P8 submits with the unknown NIN; the case reaches manual review. M opens the participation review case | The Decision panel shows "The identity is not verified yet …". **Record Approved decision** returns to the case with the same message, and nothing is recorded | |
| Q1-2 | R verifies P8 from the card; M approves | Approved. The decision history shows one APPROVED decision | |
| Q1-3 | P8 also registers in E2 with the same NIN; R corrects that NIN in the E2 case | P8's E1 case returns to manual review. The E1 registration still reads "Approved", but its pass is no longer usable and entry refuses it ("not approved") | |
| Q1-4 | Local only: a simulation-verified case (`990000000000000010`). Start a local server with `IDENTITY_ALLOW_SIMULATED_PROVIDER=False` (never in staging or production) and try to approve | Refused with "verified only by the development simulation, which is not official evidence". With the default local setting, approval works, and the identity review shows "Development simulation (not official)" | |
| Q1-5 | A registration created before identity verification (if your data has one) | Approval is refused with "no identity verification case". Record the reference for IDV-Q4 | |

**IDV-Q2: final rejection**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| Q2-1 | P9 submits with the unknown NIN. M rejects the identity finally (reason, explanation, confirmation) | The identity screen says "Finally rejected. The registration is not approved; the participant was told …". The review case decision history shows "Not approved · identity rejected" | |
| Q2-2 | P9 opens the mailbox | One email "Your registration for … was rejected", with the reference and how to register again. It contains no reason, NIN, document or staff note. Repeating the steps never sends a second email for the same rejection | |
| Q2-3 | P9 signs in to the workspace | "This registration was rejected", the explanation and **Register again**. There is no Withdraw button on that card | |
| Q2-4 | P9 presses **Register again** twice quickly | One new draft with a new reference; the identity step opens. The rejected registration stays "Not approved" | |
| Q2-5 | M tries to reopen P9's rejected registration | Refused: "… identity was finally rejected, so it cannot be reopened" | |
| Q2-6 | R returns P10's case for correction instead | "Additional information required" on the same registration; no rejection email; the correction is accepted later | |
| Q2-7 | Close public registration for the event, then P9 opens the workspace | "Registration is closed at the moment …"; there is no Register again button | |
| Q2-8 | Repeat Q2-3 in French and Arabic | Same content. The new interface strings stay English (IDV-Q5); the email is localized; the Arabic layout is right to left | |

**IDV-Q3: Algerian participant without a usable NIN**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| Q3-1 | P11 (Algerian) starts a registration and stops at the NIN field | The NIN panel says to contact the registration team with the reference shown | |
| Q3-2 | R opens Registration intake, finds P11's draft by reference | The draft opens. R sees no NIN exemption panel, and a forged grant request is refused (403) | |
| Q3-3 | M finds the draft and grants the exemption without an explanation, then correctly (reason, explanation, confirmation) | The first attempt is refused. The second shows "Exemption: Active" and a Revoke form | |
| Q3-4 | P11 reloads the identity step | A third choice, "Another official Algerian document (no NIN)". Choose the national identity card, enter a synthetic card number, upload a synthetic photo, continue and submit | |
| Q3-5 | R opens the identity queue filtered by the route "Algerian document without NIN" | P11's case: "Needs manual review", reason "… without a usable NIN: documentary review", and the NIN exemption notice (reason, granter, explanation). There is no NIN correction and no staff-exception action | |
| Q3-6 | R verifies from the card photo | "Verified manually"; the source is "Manual documentary review (NIN exemption)". M can then approve (IDV-Q1) | |
| Q3-7 | P12: M grants, P12 declares a passport, then M revokes before submission; P12 submits from the page left open | The submission is refused and returns to the identity step, which no longer offers the exemption | |
| Q3-8 | A foreign participant registers as usual | Unchanged passport route ("Foreign participant: passport review required"); the exemption cannot be granted for that draft | |

## 4d. IDV-Q-C1 corrections

Added 2026-10-02. They replace Q2-4 and Q2-7 of §4c for invitation registrations, and add the
cases below. None has been executed yet.

Owner decisions of 2026-10-02 that these cases exercise (register §9): IDV-Q17 (origin-aware
register again) by C1-6 to C1-10; IDV-Q18 (a newer declaration returns older linked verified
cases to manual review) by C1-3 and C1-5; IDV-Q19 (one explicit workspace click to use a pending
invitation) by C1-8; COMM-IDV-01 (approved wording) by C1-11. The decisions accept the behavior;
they do not execute the cases.

**Lock order (finding 1)**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| C1-1 | Two staff windows on the same registration: R moves the review case to "In progress" (or sends an information request) at the same moment as M rejects the identity finally | Both actions finish, or one shows "changed since you loaded it". The registration ends "Not approved", with no open case and no active request. One rejection email is sent | |

**Validity of a shared identity document (finding 2)**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| C1-2 | P13 (foreign) is verified and approved in E1 with passport expiry D1. P13 starts an E2 registration with the same passport number and a later expiry D2 | The E1 identity review still shows expiry D1; E1 stays eligible until D1 | |
| C1-3 | R verifies P13's E2 passport (expiry D2) | E2 is verified. E1 returns to manual review ("The identity data changed…"), with a history entry; its pass is not usable until R verifies E1 again | |
| C1-4 | As C1-2 with an Algerian identity card on the NIN exemption route, the E2 declaration without an expiry | E1's card keeps its expiry date | |
| C1-5 | Both registrations filled before either was submitted (one shared identifier). E2 is returned for its passport details and P13 changes only the expiry | E1 is not silently extended: it returns to manual review with a history entry | |

**Register again by origin (finding 3)**

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| C1-6 | P14 registered from an invitation link and was finally rejected; the link is still valid | **Register again with your invitation**; the new draft shows the inviting organization | |
| C1-7 | As C1-6, but the campaign link was revoked (or expired, the campaign closed, or full) | No button: "Your invitation can no longer be used… ask the organization that invited you for a new invitation…" | |
| C1-8 | The organization issues a new link; P14 opens it while signed in | The workspace shows "You opened an invitation" and **Register with this invitation**; it opens the new draft | |
| C1-9 | Public registration closed, P14's invitation still valid | The invitation path still works; an open-origin participant sees "Registration is closed at the moment" | |
| C1-10 | P15 was registered on behalf by an organization and rejected | "This registration was made for you … Contact them to register again"; no button | |
| C1-11 | Read the rejection email in EN, FR and AR | It points to the workspace and mentions a new invitation; no reason, number or note. The wording is owner-approved for real delivery (COMM-IDV-01, 2026-10-02); this check proves delivery and rendering, not the wording | |

## 5. Participant email OTP (real delivery)

Follow the integration guide §6. Record separately from the automated tests:

| # | Steps | Expected | Observed |
| --- | --- | --- | --- |
| E1 | Request a code for a synthetic mailbox | One email within the expected time; correct sender, language and event name | |
| E2 | Use the code; reuse it | Signed in once; reuse refused | |
| E3 | Return-for-correction and resubmission emails | Received; no reason or provider result in the text | |

## 6. Controlled timing exercise (observed, not promised)

Prepare 10 synthetic NOT_FOUND cases with uploaded synthetic card images. One reviewer, on a normal
laptop and network:

1. Open the first case from the filtered queue; start a stopwatch.
2. For each case: look at the image, compare the fields, choose the preset reason, and use "Verify
   and open the next case".
3. Record the total time, the number of cases, any case that needed a second look, and the
   browser and machine used.

| Reviewer | Cases | Total time | Notes |
| --- | --- | --- | --- |
| | | | |

The automated exercise (`tests/browser/test_idv_review.py::test_controlled_timing_of_a_small_synthetic_queue`)
recorded about 0.6 to 0.8 s per case for navigation and image loading only on the development
host (`var/test_artifacts/phase4/idv/review-timing.json`); human reading time is excluded there.
