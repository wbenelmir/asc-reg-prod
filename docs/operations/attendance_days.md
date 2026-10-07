# Attendance days: opening-day capacity, approval, badges and admission

An approval says which conference days a participant may attend:

| Category | Participant reads | Opening-day place |
| --- | --- | --- |
| `ALL_CONFERENCE_DAYS` | "Your participation is approved for the opening day and the following two conference days: [three dates]." | uses one |
| `FOLLOWING_TWO_DAYS` | "Your participation is approved for [two dates]. This approval does not include the opening day, [date]." | none |

The attendance category is separate from the participant role, the badge type,
the access profile and staff permissions. It only narrows on which days the
existing accreditation may be used; it never widens access.

Code: `apps/accreditation/attendance.py` (rules), `apps/accreditation/attendance_views.py`
(operations area `/ops/attendance/`).

## 1. Roles

| Role (group) | Can |
| --- | --- |
| Attendance Policy Managers | set the three conference days and the opening-day capacity; activate or switch off enforcement (edition-wide membership required) |
| Accreditation Managers | choose the days at approval; classify earlier approvals; change the days later |
| Accreditation Coordinators | read the days of the registrations in their scope |
| Badge Stock Issuers | record the attendance marking applied to a handed-over badge |

## 2. Configuration (per event edition)

`/ops/attendance/<event>/`: the opening day, the second and the third
conference day (calendar dates in the edition's timezone) and the opening-day
capacity, with a short reason. Every change is audited.

* The day boundary is the local midnight of the edition timezone, the same
  boundary the offline packages use. The hours of each day remain governed by
  the existing Access Profile and Access Rule windows.
* Nothing is preset. The page shows the edition's own start date only as a hint.
* The days can no longer change once any registration has attendance days or
  while enforcement is active. Every first grant (an approval or the
  classification of an earlier approval, whatever the category) locks the
  attendance settings, so a change of days and a grant never overlap: either
  the change comes first and the participant is told the new days, or the
  grant comes first and the change is refused.
* The capacity can never be set below the places already allocated.

## 3. Approval

The review case page requires an explicit choice (no option is preselected)
and shows the places left. The server:

* refuses an approval without a valid choice, or when the days and capacity
  are not configured;
* counts the opening-day places under a row lock on the edition's attendance
  settings, in the same transaction as the decision, so two approvals for the
  last place give exactly one opening-day approval;
* refuses an opening-day choice when the day is full -- the decision maker may
  then explicitly choose the two following days; nothing is downgraded
  automatically.

A place belongs to the approved, current registration, whatever its origin and
whether or not a pass exists. A withdrawal, an operational cancellation, a
reopening or a NOT_APPROVED outcome releases it; places are counted, never
stored as a counter, so nothing can be released twice.

The decision email (`APPROVAL_ATTENDANCE`) states the authorized days with the
dates. It never says that an entry pass is available: pass generation and
activation stay separate steps.

## 4. Registrations approved before attendance days existed

They stay **unclassified**: the participant reads that the organizers are
confirming the days; no dates are guessed. The worklist
`/ops/attendance/<event>/registrations/` lists them (default filter), with
counts. Classify each one with a reason; the participant receives one
`ATTENDANCE_CHANGE` email with the dates.

## 5. Later changes

The same worklist or the accreditation page of a registration changes the
days, with a reason. An upgrade needs a free opening-day place; a downgrade
releases one immediately. Every change is audited (actor, previous and new
category, reason) and the participant is notified once per change.

* Admission reads the current days at every verification: an existing,
  active entry pass needs no replacement and cannot admit on a day it no
  longer covers.
* The participant's pass page and print view show the current days.
* A physical badge already handed over keeps its old marking until it is
  re-marked (section 6); the worklist filter "Badge marking to record" lists
  them.

## 6. Physical badges

The badge stock is generic per badge type, so the days are never inferred from
the badge type. At handover the badge page shows the marking to apply
(sticker, overlay or print variant) in words and dates; the operator confirms
it. A badge whose marking does not match the current days is flagged until
its marking is recorded again. While enforcement is active, a badge is never
handed over to an unclassified registration.

## 7. Enforcement activation

Deployment does not change admission. On the edition's attendance page, an
Attendance Policy Manager activates enforcement once every required item of
the checklist holds:

1. the three days are configured;
2. the opening-day capacity is configured;
3. every approved, current registration is classified;
4. the opening-day places allocated are within the capacity;
5. every handed-over badge carries the marking of its current days.

Advisory items: the edition's own start and end dates contain the three days;
the number of current offline packages (activation revokes them so devices
download data prepared under the new rules).

When active, admission (QR, reference, identity and manual lookups, online and
offline) refuses, with a reason that no override can bypass:

| Reason code | When |
| --- | --- |
| `ATTENDANCE_DAY_NOT_AUTHORIZED` | a `FOLLOWING_TWO_DAYS` participant on the opening day |
| `ATTENDANCE_UNCLASSIFIED` | any day, for an approval without attendance days |
| `ATTENDANCE_NOT_CONFERENCE_DAY` | a day that is not one of the three |

Every other check (event, approval, identity, pass, zone, gate, time window,
restriction, re-entry) still applies. The decision maker resolves a refusal by
changing the days within capacity, never by an override.

Offline devices: a package is valid only for the local day of its preparation
and contains only the participants whose days cover that day; a later
downgrade reaches the device with the next critical delta
(`ATTENDANCE_NOT_AUTHORIZED`). Synchronized offline admissions are judged
with the days in force when they happened. Offline continuity stays disabled
unless it was enabled before.

**Switching off** (rollback): the same page, with a reason. Admission stops
checking the days; the days, the capacity and the history are kept, and the
current offline packages are revoked again.

**Enforcement history.** Each activation and each switch-off is kept as a
period (`AttendanceEnforcementInterval`: start included, end excluded). An
offline admission synchronized later is judged by the period that covered its
own time, so switching off and on again never changes how an earlier admission
is judged. The update that introduced these periods rebuilt them from what the
edition's settings still held; where earlier periods had already been
overwritten, the unknown span (from the creation of the settings to the
earliest known switch) is recorded as "earlier history not recorded" and is
treated as enforced, so an admission from that span is judged by the stricter
rule rather than silently accepted.

## 8. Audit codes

`ACC_ATTENDANCE_POLICY_UPDATED`, `ACC_ATTENDANCE_ENTITLEMENT_GRANTED`,
`ACC_ATTENDANCE_ENTITLEMENT_CLASSIFIED`, `ACC_ATTENDANCE_ENTITLEMENT_CHANGED`,
`ACC_ATTENDANCE_OPENING_CAPACITY_REFUSED`, `REV_APPROVAL_BLOCKED_ATTENDANCE`,
`ACC_ATTENDANCE_ENFORCEMENT_ACTIVATED`, `ACC_ATTENDANCE_ENFORCEMENT_ACTIVATION_REFUSED`,
`ACC_ATTENDANCE_ENFORCEMENT_DEACTIVATED`, `BDG_ATTENDANCE_MARKING_RECORDED`.
Summaries carry categories, dates, counts and capacity only.
