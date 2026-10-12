# Registration list and statistics

Code: `apps/registrations/ops_list.py`, `apps/registrations/reporting.py`
(shared segment definitions), `apps/registrations/statistics.py`,
`apps/registrations/statistics_exports.py`, `apps/registrations/statistics_views.py`.

Both pages need `registrations.view_registration` and show only the reader's
authorized scope (event, organization). The superadmin sees everything.

## 1. Registrations (`/ops/registrations/`)

* Every registration in scope is reachable: server-side pagination, 50 rows
  per page, newest first (creation time, then id, so the order is stable
  between pages).
* Filters (reference, status, situation, event, source, organization,
  campaign, link, delegation list, nationality, residence, role, attendance
  day, submission date range) are applied before pagination; the counters
  show "N matching of M in your scope".
* Name and e-mail search: typed in a separate form sent by POST, never in the
  URL. An e-mail is matched exactly; a name or reference by its beginning
  (2 characters minimum). The search is resolved once on the server and kept
  in the reader's session (up to 500 matches; beyond that the page asks for a
  narrower search). Each search writes an audit event with the number of
  matches only, never the text.
* The page distinguishes "no registration in your scope", "no registration
  matches these filters" and an unauthorized request (403).

The situations (segments) used by the filters, the statistics and the
drill-down links are defined once in `apps/registrations/reporting.py`, so a
statistics figure and the list it links to always agree.

## 2. Statistics (`/ops/statistics/`)

Navigation: **Statistics**. All figures are computed on the server for the
current filters (event, organization, source, campaign, nationality,
residence, role, attendance day, date range) and show their generation time.
Sections: overview, workload and waiting times, exceptions, final-approved
population, sources and origin, composition, progress over time. Each figure
links to the matching registration list.

Definitions (also shown at the bottom of the page and included in every
export):

* **Registration cohort**: registrations first submitted within the date
  range (all dates when empty), counted in their current state.
* **Events**: "Progress over time" counts submissions, decisions and
  withdrawals dated within the range. Earlier history is not reconstructed;
  current-state counts are never shown as trends. With weekly bars, each bar
  is labelled with its Monday but counts only the dates inside the range (the
  range is never widened to whole weeks); the cumulative series starts from
  the count before the first requested date.
* **Date range**: a range entered in reverse order is put in order, the same
  way for the list, the statistics, the charts and the exports.
* **Final-approved**: current decision "Approved" on a current, approved
  registration that is neither withdrawn nor cancelled. A person who
  registered again is counted once. Identity verification alone is never
  counted as approval.
* Percentages state their denominator. Dates are calendar days in the
  event's time zone.

### Exports

Three reports, each one click from the page, for the entire filtered dataset
(not only the rows shown):

| Report | Content | Extra permission |
| --- | --- | --- |
| Registration situation | Overview, segments, sources, composition, definitions | none |
| Final-approved population | Counts and, for export holders, the attendee list | attendee list: `exports.add_exportrequest`, limited to the registrations of the reader's export scope |
| Workload | Queues, waiting times, exceptions | none |

* **XLSX**: one workbook per report, a sheet per section, a "Filters and
  definitions" sheet, right-to-left sheets in Arabic. Text is always written
  as literal text with a quote prefix (a value such as `=HYPERLINK(...)` is
  shown, never evaluated); numbers are numeric cells.
* **Attendee list**: viewing registrations never confers bulk export. The
  list contains only the final-approved registrations that are both in the
  report's filters and in the reader's export scope (event AND organization
  of `exports.add_exportrequest`, `registrations_visible_for_export`). When
  some final-approved registrations fall outside it, the page, the workbook
  and the print report state "N of M final-approved registrations are not
  listed". Without the export permission the report has no list (aggregates
  only). The superadmin receives the complete list.
* **Breakdowns**: the page shows each long source or composition list as its
  25 largest groups plus one "Other (N more)" row that adds up the rest, so
  every list still sums to its total, and says so. The XLSX and PDF reports
  list every group.
* **PDF**: a print-ready page opens and starts the browser's print dialog;
  choose "Save as PDF". No server-side PDF engine is installed, so the PDF is
  produced by the reader's browser with the platform fonts.

Every export writes an audit event (report, format, the names of the filters
used, and for the final-approved report the number of attendee rows listed
and withheld and the scope status; never filter values or personal data) and is sent with `Cache-Control: no-store`.
