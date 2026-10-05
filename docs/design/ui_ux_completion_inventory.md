# UI/UX Completion Gate — page and state inventory

Status: **Stage 3 complete: every custom user-facing page is on the design system (READY_FOR_LOCAL_REVIEW).** Stage 1 inventory, updated at Checkpoint 1 and at the end of Stage 3. This document
maps every custom user-facing page and state to its user role, route,
template, current visual status, required components, and its language/RTL,
responsive and accessibility risks, plus the planned treatment. It is a
working inventory, not a claim that any page is approved.

The design system these treatments follow is
[`ui_ux_design_system.md`](ui_ux_design_system.md). Finder adaptations are
recorded in [`finder_inventory.md`](finder_inventory.md).

## 1. Method

* Routes: the full resolver tree was enumerated with Django's URL resolver
  (119 non-admin patterns). POST-only command endpoints (assign, revoke,
  suspend, …) have no page of their own. Their success and failure states
  render on the owning detail page as a flash message or a 409 conflict
  page, and are inventoried there.
* Templates: every file under `templates/` (85 files) was read; there are no
  app-level template directories.
* States: derived from each view's render paths (success, validation error,
  conflict 409, not-open 503, unavailable 400, empty querysets) and from the
  permission flags passed to the template.
* Out of scope: Django's own administration (`/admin/`, mounted only by
  `config/settings/local.py`), the machine endpoints `healthz`, `readyz`,
  `entry/status/` (JSON), `entry/photo/…`, `my-passes/…/qr.png`,
  `documents/<id>/` (private file stream) and `ops/exports/<id>/download/`
  (file download).

### Status legend

| Status | Meaning |
| --- | --- |
| **DS** | Built on the Prompt 5 design system (`body.asc-ui` shell, shared components). Independently reviewed and approved in Prompt 5. |
| **DS-CP1** | Migrated to the design system in this gate (Checkpoint 1 slice). |
| **DS-S3** | Migrated to the design system in Stage 3 of this gate. |
| **Legacy** | Still on the legacy `base.html` chrome: plain Bootstrap markup, no shared shell, components, tokens or status language. |

### Roles

`Public` (no session) · `Participant` (OTP session) · `Org staff`
(operational user holding organization-scoped invitation/delegation
permissions) · `Reviewer` (reviews permissions) · `Accreditation` (accreditation
permissions) · `Intake` (`registrations.view_registration`) · `Badge ops`
(badge/credential/stock permissions) · `Device admin` · `Checkpoint operator`
(enrolled device + gate-scoped membership) · `Security lead` (observability)
· `Exports` (`exports.add_exportrequest`) · `Comms` (communications view).

## 2. Cross-cutting findings (Stage 1)

| # | Finding | Impact | Treatment |
| --- | --- | --- | --- |
| F1 | 40 page templates still used the legacy chrome: plain navbar with the text "ASC 2026" instead of the official logo, no page structure, Bootstrap default blue, raw status values, `dl.row` summaries and striped tables. | The product looks like two different applications. | Migrate every legacy page to one of the shells (Stage 2 slice, then Stage 3). |
| F2 | htmx 4 boosted navigation (`hx-boost` on `#main-content`) swaps the `<body>` contents but keeps the old `<body>` attributes and discards the new `<head>`. A boosted link from a legacy page into a design-system page (or the reverse) would therefore render without `asc-ui.css` or without the `asc-ui` body class. | Unstyled or half-styled pages after an in-page link. | **Fixed in CP1:** `asc-ui.css`, the Arabic font preloads and the `asc-ui` body class now come from `base.html` for every page. Shell body classes are semantic hooks only, and no CSS may depend on them (design system §2). |
| F3 | Authenticated operational user **without** `registrations.view_registration` (e.g. a Badge Stock Issuer or Checkpoint operator signing in without an `/entry/` `next`) enters an **infinite redirect loop**: sign-in → `/ops/registrations/` → decorator redirects to sign-in → sign-in redirects authenticated users to `/ops/registrations/` … Reproduced with Django's test client: `302 /ops/registrations/` ↔ `302 /accounts/ops/sign-in/?next=/ops/registrations/` indefinitely. The same loop occurs for any authenticated user visiting an operations page they lack permission for. | No access-denied page exists; the browser shows a "too many redirects" error. | **Fixed in Stage 3 (D1):** a signed-in user without permission gets the localized 403 page (`deny_operational_access`); anonymous users are redirected to sign-in with a validated `next`; sign-in lands on the first permitted area. Regression tests prove no loop (`apps/accounts/tests/test_access_denied.py`). |
| F4 | No project `403.html`, `404.html`, `500.html` or `400.html`/`csrf_failure` templates. Django's plain-text defaults are shown, untranslated and unstyled. | Error states outside the design system; RTL and localization missing. | **Fixed in Stage 3:** `400/403/404/500/csrf_failure.html` on `layouts/base_error.html`; 400 and 500 are static-safe (proven with database access blocked); `CSRF_FAILURE_VIEW` renders a localized page and never the reason (`apps/core/tests/test_error_pages.py`). |
| F5 | The language-switch browser test was flaky (Prompt 6 §I). **Root cause found and reproduced deterministically:** after `select_option`, `wait_for_load_state("networkidle")` resolved in 0.00 s while the *new* document was still parsing, blocked on a script-blocking stylesheet or parser-blocking script, so the inline restore script had not run yet. Injecting a 1.2 s delay on `bootstrap.rtl.min.css`, `app.css` or `htmx.min.js` fails the original synchronisation every time (`lang="ar"`, `given_names=""`). | Test flake; the product restore behaviour is correct, because the page is not rendered to a user before its stylesheets load. | **Fixed in CP1** (test synchronisation, not a retry): a shared `switch_language()` helper waits for the language navigation's own `load` event. A permanent regression test proves the fix under the same injected latency. |
| F6 | The participant withdraw action uses a native `confirm()` dialog (`registrations/workspace.html`). | Unstyled; cannot be themed or given RTL layout; acceptable fallback. | **Fixed in Stage 3:** shared `components/confirm_dialog.html` + `static/js/asc-enhance.js` replace `confirm()`; a `<noscript>` consent box is the no-JavaScript fallback. See §6.4. |
| F7 | `BootstrapFormMixin` gave `RadioSelect` inputs the `form-control` class (as if they were text inputs). | Radio buttons styled as text boxes by Bootstrap. | **Fixed in CP1:** radio and checkbox-group inputs receive `form-check-input`. Presentation-only; validation unchanged. |
| F8 | Several operational forms ask for raw UUIDs ("Operational user id", "Reference object id", "Candidate identifier id", export "Organization"). | Poor usability; error-prone. | **Fixed in Stage 3 (D2):** see §6.2 for every raw-ID field and its scoped replacement. |
| F9 | No CSP is configured (`SecurityMiddleware` only); inline scripts exist in `base.html` and templates. | Not a regression; noted because the gate forbids weakening CSP. | No change. New scripts are added only as static files or alongside the existing inline pattern. No `eval` is used. |

## 3. Page inventory

Columns: **Role** · **Route** (name) · **Template** · **Status** ·
**Required components** · **Language/RTL risk** · **Responsive risk** ·
**Accessibility risk** · **Planned treatment**.

### 3.1 Authentication, session and access states

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Start registration (OTP request), incl. validation error, rate-limited error, session-ended flash | Public | `accounts:otp-request` `/accounts/start/` | `accounts/otp_request.html` | **DS-CP1** | auth shell, field, error summary, flash | email is LTR inside RTL text | form column 100 % on mobile | focus order skip link → email within 6 Tabs (tested) | Public auth shell (Finder split sign-in) |
| Enter verification code, incl. wrong/expired code | Public | `accounts:otp-verify` | `accounts/otp_verify.html` | **DS-CP1** | auth shell, field, notice | email isolated with `<bdi dir="ltr">`; code input `dir="ltr"` | — | error focus; code is never echoed | Auth shell |
| Operational sign-in, incl. incorrect credentials, session-ended and next-destination flash | All staff | `accounts:operational-sign-in` | `accounts/operational_sign_in.html` | **DS-CP1** | auth shell (operations variant), field, error summary | — | — | password never restored on language switch (tested) | Auth shell |
| Participant session warning (inactivity / absolute) | Participant | every participant page | `partials/session_warning.html` | **DS-CP1** (restyled banner) | notice, button | long Arabic text | wraps | `aria-live=assertive` present | CP1: restyled within the shells as a tone-coded banner (same ids/classes used by tests) |
| Operational session warning | Staff | every ops page | `partials/session_warning.html` | **DS-CP1** (restyled banner) | notice, button | — | — | as above | As above |
| Session ended (redirect + flash on sign-in pages) | Participant / Staff | OTP request / sign-in | flash in auth shell | **DS-CP1** | flash notice | — | — | `role=status` | Covered by auth shell flash |
| Access denied | Staff | any ops route | `403.html` | **DS-S3** | error shell, actions | translated, RTL | card stacks | one h1, sign-out form | Implemented (F3, D1) |
| 404 / 500 / 400 / CSRF failure | All | — | `404.html`, `500.html`, `400.html`, `csrf_failure.html` | **DS-S3** | error shell | translated, RTL | card stacks | one h1, landmarks | Implemented (F4) |

### 3.2 Public and invitation entry points

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Home redirect | Public | `home` `/` | — (redirect to OTP request) | n/a | — | — | — | — | None |
| Invitation unavailable (invalid / expired / revoked), with or without open registration | Public | `invitations:invitation-start` | `invitations/invitation_unavailable.html` | **DS-S3** | public shell, empty/unavailable state, CTA | — | — | `role=alert` on a paragraph | Stage 3: public shell "unavailable" state |
| Claim link unavailable | Public | `invitations:on-behalf-claim` | `invitations/claim_unavailable.html` | **DS-S3** | as above | — | — | as above | Stage 3 |
| Claim conflict | Participant | `invitations:on-behalf-claim-resume` | `invitations/claim_conflict.html` | **DS-S3** | conflict state | — | — | as above | Stage 3 |
| Registration not open (503) | Participant | any wizard step | `registrations/no_open_event.html` | **DS-CP1** | workspace shell, empty state | — | — | — | Workspace shell empty state |

### 3.3 Registration wizard and participant workspace

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| My registrations (empty, draft, submitted, information required, approved, withdrawn) | Participant | `registrations:workspace` | `registrations/workspace.html` | **DS-CP1** | workspace shell, registration card, status chip, notice, empty state | reference code LTR; mixed Arabic/Latin event names | cards stack | withdraw confirm is native `confirm()` (F6) | Card list + status chips |
| Step 1 Identity (NIN / passport panels, optional passport-page upload, validation errors) | Participant | `registrations:step-identity` | `registrations/step_identity.html` | **DS-CP1** | wizard layout, stepper, field, radio cards, fieldset, file field, error summary, action bar | NIN/passport inputs `dir="ltr"`; Arabic labels with parentheses | two-column names collapse | error summary focus; hidden panel disabled | Wizard layout |
| Step 2 Contact (mobile required for Algeria / optional) | Participant | `registrations:step-contact` | `registrations/step_contact.html` | **DS-CP1** | as above | phone number LTR | — | required state announced | Wizard layout |
| Step 3 Professional (photo upload, existing photo) | Participant | `registrations:step-professional` | `registrations/step_professional.html` | **DS-CP1** | as above + file field | URLs LTR | 9 fields → 2-column grid | help text association | Wizard layout |
| Step 4 Interests | Participant | `registrations:step-interests` | `registrations/step_interests.html` | **DS-CP1** | checkbox cards, textareas | long French labels | checkbox grid wraps | fieldset/legend | Wizard layout |
| Step 5 Review (check answers, edit links) | Participant | `registrations:step-review` | `registrations/step_review.html` | **DS-CP1** | summary cards with edit links | dates/numbers; names `<bdi>` | dl stacks on mobile | edit links have context | Summary list |
| Step 6 Notices (privacy notice, terms, acknowledgements, notices unavailable) | Participant | `registrations:step-notices` | `registrations/step_notices.html` | **DS-CP1** | scrollable legal panel (focusable), checkbox fields | legal text direction from language | panel height on mobile | scroll region focusable with a name | Wizard layout |
| Confirmation (sent / queued / failed / unavailable email) | Participant | `registrations:confirmation` | `registrations/confirmation.html` | **DS-CP1** | success result, code display, notice | reference LTR (`strong.ltr-embed` kept) | — | — | Success pattern |
| Continue registration (POST → step) | Participant | `registrations:continue` | — | n/a | — | — | — | — | — |
| Additional information request (respond, draft saved, submitted, closed) | Participant | `reviews:my-information-request` | `reviews/participant_information_request.html` | **DS-S3** | workspace shell, fieldset per item, file field, two actions | localized request message | — | two submit buttons | Stage 3 |
| Withdraw registration (POST, 409 conflict) | Participant | `reviews:my-registration-withdraw` | flash on workspace / `reviews/conflict.html` | **DS-S3** | confirmation dialog | — | — | — | Stage 3 dialog (F6) |

### 3.4 Participant passes (Prompt 5)

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| My entry passes (active, suspended, revoked, expired, none) | Participant | `badges:participant-passes` | `badges/participant_passes.html` | DS | pass card, QR, chips | QR never mirrored (asserted) | QR stacks | — | Keep; verify under new foundation |
| Printable pass | Participant | `badges:participant-pass-print` | `badges/participant_pass_print.html` | DS | print sheet | — | print | — | Keep |
| Legacy status label partial | — | — | `badges/_status_label.html` | **Deleted in Stage 3** (unreferenced) | — | — | — | — | Removed |

### 3.5 Organization workspace, invitations and delegation

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Organization workspace dashboard (campaigns, batches, empty) | Org staff | `invitations:workspace-dashboard` | `invitations/workspace_dashboard.html` | **DS-S3** | operations shell, KPI tiles, tables, chips, empty | organization names mixed script | tables scroll | captions present | Stage 3 |
| Registration contexts (org scope) | Org staff | `invitations:workspace-registrations` | `invitations/workspace_registrations.html` | **DS-S3** | table, chips | reference LTR | table scroll | — | Stage 3 |
| Find an organization (search, results, none) | Org staff | `invitations:organization-search` | `invitations/organization_search.html` | **DS-S3** | search bar, result list, row actions | — | row actions wrap | search landmark present | Stage 3 |
| New invitation campaign (validation) | Org staff | `invitations:campaign-create` | `invitations/campaign_create.html` | **DS-S3** | form card, fields | date inputs | — | — | Stage 3 |
| Campaign detail (issued link shown once, rotate, status change) | Org staff | `invitations:campaign-detail` | `invitations/campaign_detail.html` | **DS-S3** | detail header, summary, one-time secret panel with copy, action panel | URL LTR | long URL wraps | secret shown once (announce) | Stage 3 |
| Upload delegation list (validation) | Org staff | `invitations:delegation-upload` | `invitations/delegation_upload.html` | **DS-S3** | form card, file field | — | — | — | Stage 3 |
| Delegation batch detail (rows, apply) | Org staff | `invitations:delegation-detail` | `invitations/delegation_detail.html` | **DS-S3** | summary, table, chips, action | masked emails LTR | table scroll | — | Stage 3 |
| Create on-behalf registration (claim link shown once) | Org staff | `invitations:on-behalf-create` | `invitations/on_behalf_create.html` | **DS-S3** | form, one-time secret panel | URL LTR | — | — | Stage 3 |

### 3.6 Staff intake and review workflows

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Registration intake list (rows, empty, 200-row cap) | Intake | `registrations:ops-intake-list` | `registrations/ops_intake_list.html` | **DS-CP1**, records in S3 | operations shell + section nav, page header, table, status chips, empty state | reference/code LTR | table from 768 px, stacked records below (UI checkpoint 1 correction) | caption, row link names | Operations list pattern |
| Registration intake detail | Intake | `registrations:ops-intake-detail` | `registrations/ops_intake_detail.html` | **DS-CP1** | detail header, summary cards, chips | names `<bdi>` | two-column → one | headings order | Detail pattern |
| Review queue (filters, results, pagination, empty) | Reviewer | `reviews:queue-list` | `reviews/queue_list.html` | **DS-CP1**, records in S3 | filter bar, table, chips, pagination, empty state | pagination arrows mirror | filters wrap; stacked records below 768 px | search landmark, labelled filters | Operations list pattern |
| Review case detail (summary, assignment, status, checklist, notes, duplicates, information requests, decisions, reopen/cancel; each section permission-gated) | Reviewer / Accreditation | `reviews:case-detail` | `reviews/case_detail.html` | **DS-CP1** | detail header, main/aside layout, section cards, tables, inline forms, decision panel | names/notes `<bdi>`; mixed-script notes | aside stacks under main | many forms: each labelled; decisions distinct by text + icon | Complex operational page pattern |
| Record Not Approved decision (validation) | Reviewer | `reviews:case-decision` | `reviews/decision_not_approved.html` | **DS-S3** | form card, danger action | — | — | — | Stage 3 |
| New information request (+ item formset) | Reviewer | `reviews:case-create-information-request` | `reviews/information_request_create.html` | **DS-S3** | form card, fieldsets, trilingual message fields | FR/AR text fields need `dir` per field | long form | nested error summaries | Stage 3: set `dir="rtl"`/`lang` on the Arabic fields |
| Information request detail (send / cancel / close) | Reviewer | `reviews:information-request-detail` | `reviews/information_request_detail.html` | **DS-S3** | summary, item list, actions | — | — | — | Stage 3 |
| Reopen registration (reason) | Reviewer | `reviews:registration-reopen` | `reviews/reopen_registration.html` | **DS-S3** | confirmation form | — | — | — | Stage 3 |
| Cancel registration operationally (reason) | Reviewer | `reviews:registration-cancel` | `reviews/cancel_registration.html` | **DS-S3** | destructive confirmation form | — | — | — | Stage 3 |
| Review conflict (409 stale version) | Reviewer | several POSTs | `reviews/conflict.html` | **DS-S3** | conflict state | — | — | `role=alert` | Stage 3 (shared conflict component) |

### 3.7 Accreditation

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Registration accreditation (roles, badge type, access profile, access rules; assign/change/revoke per permission) | Accreditation | `accreditation:registration-detail` | `accreditation/registration_detail.html` | **DS-S3** | detail header, section cards, tables with inline forms | localized role names | inline forms overflow on mobile | inline inputs lack visible labels (placeholders only) | Stage 3: labelled inline forms |
| Bulk assignment preview (targets, eligibility) | Accreditation | `accreditation:bulk-preview` | `accreditation/bulk_preview.html` | **DS-S3** | form card, results table, confirm execute | — | — | raw UUID input (F8) | Stage 3 |
| Bulk execution result | Accreditation | `accreditation:bulk-execute` | `accreditation/bulk_result.html` | **DS-S3** | results table, chips | — | — | yes/no text only | Stage 3 |
| Accreditation conflict (409) | Accreditation | assign/change/revoke | `accreditation/conflict.html` | **DS-S3** | conflict state | — | — | — | Stage 3 |

### 3.8 Passes, badges and stock (Prompt 5)

| Page / state | Role | Route | Template | Status | Treatment |
| --- | --- | --- | --- | --- | --- |
| Registration credential (generate, activate, suspend, resume, revoke, replace) | Badge ops | `badges:registration-credential` | `badges/registration_credential.html` | DS | Keep; now gets the operations section navigation |
| Badge issuance (issue, replace, return, lost, void; allocation select) | Badge ops | `badges:registration-badge-issuance` | `badges/registration_badge_issuance.html` | DS | Keep |
| Stock dashboard (locations, batches, ledger, allocations, reconciliation) | Badge ops | `badges:stock-dashboard` | `badges/stock_dashboard.html` | DS | Keep |
| Print batch detail (status, receive) | Badge ops | `badges:print-batch-detail` | `badges/print_batch_detail.html` | DS | Keep |
| Verification keys (promote, retire, emergency revoke) | Badge ops | `badges:verification-keys` | `badges/verification_keys.html` | DS | Keep |
| Fallback reference lookup | Badge ops | `badges:fallback-reference-lookup` | `badges/fallback_lookup.html` | DS | Keep |
| Badge conflict (409) | Badge ops | several POSTs | `badges/conflict.html` | DS | Keep |

### 3.9 Entry, checkpoint and devices (Prompt 5)

| Page / state | Role | Route | Template | Status | Treatment |
| --- | --- | --- | --- | --- | --- |
| Checkpoint setup / home | Checkpoint operator | `entry:home` | `entry/checkpoint_setup.html` | DS | Keep |
| Verify (idle, validation, lookups paused, degraded, offline, session ended) | Checkpoint operator | `entry:verify` (+ `verify-qr`, `lookup-*`) | `entry/verify.html` | DS | Keep |
| Result (7 result types, decision, override) | Checkpoint operator | `entry:verify-qr`, `entry:decision`, `entry:override` | `entry/result.html` | DS | Keep |
| Candidate selection | Checkpoint operator | `entry:lookup-*`, `entry:select-candidate` | `entry/candidates.html` | DS | Keep |
| Monitor | Checkpoint operator | `entry:monitor` | `entry/monitor.html` | DS | Keep |
| Device not enrolled | Device | `entry:*` | `entry/not_enrolled.html` | DS | Keep |
| Device activation | Device | `entry:device-activate` | `entry/device_activate.html` | DS | Keep |
| Entry conflict | Checkpoint operator | — | `entry/conflict.html` | DS | Keep |
| Device list / detail / activation code | Device admin | `entry:device-list`, `entry:device-detail`, `entry:device-activation-code` | `entry/device_list.html`, `entry/device_detail.html`, `entry/device_activation_code.html` | DS | Keep |
| Observability dashboard | Security lead | `entry:observability` | `entry/observability.html` | DS | Keep |

### 3.10 Exports and communications

| Page / state | Role | Route | Template | Status | Components | Lang/RTL risk | Responsive risk | A11y risk | Treatment |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Controlled exports (request form, recent exports, download, expiry, 409 scope failure) | Exports | `exports:workspace` | `exports/workspace.html` | **DS-S3** | operations shell, form card, table, chips | purpose codes shown raw (untranslated values) | inline form row | raw organization UUID (F8) | Stage 3; purpose labels need translation |
| Communication delivery | Comms | `communications:operations-messages` | `communications/operations_messages.html` | **DS-S3** | table, chips | language codes raw | table scroll | caption present | Stage 3 |

### 3.11 Documents and privacy

| Page / state | Role | Route | Template | Status | Treatment |
| --- | --- | --- | --- | --- | --- |
| Private document stream | Staff / Participant | `documents:document-stream` | — (file response) | n/a | None (no page) |
| Legal notices display | Participant | notices step | `registrations/step_notices.html` | **DS-CP1** | Scroll panel |
| Passport identity-page and photo upload notices | Participant | identity / professional steps | step templates | **DS-CP1** | Neutral privacy notices beside the field |

### 3.12 Shared chrome

| Element | Template | Status | Treatment |
| --- | --- | --- | --- |
| Legacy header / footer | `base.html` default blocks | **Removed in Stage 3** (every page uses a shell) | Removed |
| Language switcher | `partials/language_switcher.html` | DS (restyled by shell) | Keep; test synchronisation fixed (F5) |
| Flash messages | `components/flash_messages.html` | DS | Keep |
| Breadcrumbs | `components/breadcrumbs.html` | DS | Keep |
| Operations section navigation | `components/ops_nav.html` (new) | **DS-CP1** | New |
| Wizard stepper | `components/stepper.html` (new) | **DS-CP1** | New |
| Status chip | `components/status_chip.html` (new) | **DS-CP1** | New |
| Pagination | `components/pagination.html` (new) | **DS-CP1** | New |
| Legacy field / error summary partials | `partials/form_field.html`, `partials/error_summary.html` | **Deleted in Stage 3** | Replaced by `components/field.html`, `components/error_summary.html` |
| Emails | communications template versions (database content) | Not a page | Stage 3 review of RTL wrapper only if a template file exists |

## 4. Remaining inventory after Checkpoint 1

Migrated in Checkpoint 1 (**DS-CP1**): 16 of the 40 legacy page templates.
Authentication: 3 (`otp_request`, `otp_verify`, `operational_sign_in`).
Registration: 9 (six wizard steps, `confirmation`, `no_open_event`,
`workspace`; the old `_step_nav` partial is replaced by
`components/stepper.html`). Staff intake: 2 (`ops_intake_list`,
`ops_intake_detail`). Reviews: 2 (`queue_list`, `case_detail`). Also
restyled: the shared `session_warning` partial. New layouts: `base_public`,
`base_wizard`. Every Prompt 5 operations page also gains
the section bar.

Legacy page templates still to migrate in Stage 3: **24**; all are migrated in Stage 3 (§6).

* Invitations and organization workspace (11): `campaign_create`,
  `campaign_detail`, `claim_conflict`, `claim_unavailable`,
  `delegation_detail`, `delegation_upload`, `invitation_unavailable`,
  `on_behalf_create`, `organization_search`, `workspace_dashboard`,
  `workspace_registrations`.
* Reviews (7): `decision_not_approved`, `information_request_create`,
  `information_request_detail`, `participant_information_request`,
  `reopen_registration`, `cancel_registration`, `conflict`.
* Accreditation (4): `registration_detail`, `bulk_preview`, `bulk_result`,
  `conflict`.
* Exports (1): `workspace`. Communications (1): `operations_messages`.

Also for Stage 3: the legacy partials `partials/form_field.html` and
`partials/error_summary.html` (still included by 7 of the templates above)
and the unused `badges/_status_label.html`, to be removed once nothing
includes them; the legacy default header and footer in `base.html`; the
missing 403, 404, 500 and CSRF-failure pages (F4); the shared confirmation
dialog (F6).

Decisions for **F3** and **F8** were approved after Checkpoint 1 (§5) and
implemented in Stage 3 (§6).

## 5. Developer decisions recorded after Checkpoint 1 (2026-09-23)

These decisions were recorded at the Checkpoint 1 handover and are
**implemented in Stage 3** (authorized after the independent visual review of
Checkpoint 1, status approved for Stage 3 with required fixes).

**D1: access denied (F3), approved.**

* An authenticated user who lacks permission for the requested operations
  page receives a translated **403 access-denied page**, not a redirect.
* An unauthenticated user keeps being redirected to staff sign-in with a
  safe `next` value.
* Regression tests must prove that no redirect loop can occur (including
  sign-in → default destination without permission) and that permissions
  stay fail-closed.

**D2: no raw database IDs in staff forms (F8), direction approved.**

* User-facing forms must not ask staff to type opaque raw database IDs
  (for example "Operational user id").
* Use permission-scoped, human-readable choices. Where the result set can be
  large, use an accessible searchable selector, preferably an existing
  project component, and without adding a heavy dependency.
* Server-side permission and scope validation stays authoritative.
* Before implementing, document every raw-ID field found and its planned
  replacement. Fields known so far (to be completed by a full sweep):
  `reviews/case_detail.html` "Operational user id" (`assigned_user_id`) and
  "Candidate identifier id (optional)" (`candidate_identifier_id`);
  `accreditation/bulk_preview.html` "Reference object id"
  (`reference_object_id`); `exports/workspace.html` "Organization
  (optional)" (`organization_id`).

**D3: French terminology, approved.**

* Reviews area: **« Examen des dossiers »**.
* Review queue: **« File d’examen »**.
* « Révisions » must not be used in this workflow.
* The English, French and Arabic catalogues are to be updated consistently.
  **Implemented in Stage 3:** the French catalogue uses « Examen des
  dossiers », « File d’examen » and « Dossier(s) d’examen » throughout
  (13 entries). No « Révisions » or « révision » remains in the review
  workflow; English and Arabic are unchanged.

## 6. Stage 3 completion (2026-09-23)

### 6.1 Pages migrated in Stage 3 (24)

| Family | Templates | Shell |
| --- | --- | --- |
| Organization workspace | `invitations/workspace_dashboard`, `workspace_registrations`, `organization_search`, `campaign_create`, `campaign_detail`, `delegation_upload`, `delegation_detail`, `on_behalf_create` | operations |
| Public invitation states | `invitations/invitation_unavailable`, `claim_unavailable`, `claim_conflict` | public |
| Reviews | `reviews/decision_not_approved`, `information_request_create`, `information_request_detail`, `reopen_registration`, `cancel_registration`, `conflict` | operations (conflict: operations or participant) |
| Participant response | `reviews/participant_information_request` | participant workspace |
| Accreditation | `accreditation/registration_detail`, `bulk_preview`, `bulk_result`, `conflict` | operations |
| Exports, communications | `exports/workspace`, `communications/operations_messages` | operations |

New: `layouts/base_error.html` and the five error templates; components
`confirm_dialog`, `confirm_fallback`, `picker` and `conflict_state`.
Deleted: `partials/form_field.html`, `partials/error_summary.html` and
`badges/_status_label.html`, plus the legacy default header and footer
blocks of `base.html`. Django's administration (`/admin/`, local settings
only) is not part of the product interface and was not redesigned.

Every list page with a table (intake, review queue, campaigns, batches,
delegation rows, organization registrations, bulk preview and result,
exports, communications) renders a real table from 768 px and the same rows
as stacked records below it. Only one of the two is displayed, so nothing is
announced or focused twice.

### 6.2 Raw database-ID fields found and their replacements (F8, D2)

Full sweep of every template input and form field whose value is a
database id (`name="…_id"` in templates; `UUIDField` and `ModelChoiceField`
in forms).

| Page | Field (POST name kept) | Before | After | Server-side rule |
| --- | --- | --- | --- | --- |
| Review case detail, assignment | `assigned_user_id` | text box "Operational user id" | `components/picker.html`: select of `eligible_review_assignees(case)` (display name; email only to disambiguate), filter box above 8 choices | The same queryset validates the POST (`ScopedModelChoiceField`); out-of-scope, unknown, inactive and malformed ids give one generic message. The former global `get_object_or_404(OperationalUser)`, which let any user be assigned and revealed which ids exist, is gone. |
| Review case detail, duplicate resolution | `candidate_identifier_id` | text box "Candidate identifier id (optional)" | select of the case's own `duplicate_candidates_for(case)`, labelled type · country · masked value | The same queryset validates; any other identifier is refused. |
| Bulk accreditation preview | `reference_object_id` | text box "Reference object id" | grouped select (one group per kind) of `bulk_assignable_references(user, kind)`, narrowed to the chosen kind | `BulkPreviewForm` requires the object to be in the chosen kind's scoped list. The former `get_object_or_404` (404 versus "not authorized", and a server error on malformed ids) is gone. |
| Controlled exports | `organization_id` | text box "Organization (optional)" | picker of the organizations in `export_scope_choices(user)` | A value outside the scope, unknown or malformed is answered like a denied scope (409). |
| Controlled exports | `event_edition_id` | select (already scoped) | `ScopedModelChoiceField` over the same scope | Forged and malformed values answer 409 instead of causing a server error. |
| Review case detail, checklist | `item_code` | free-text "Item code" (a code, not an id) | select of the active checklist's own items, translated | The service still rejects any code outside the active checklist. |
| Registration accreditation (assign, change) | `reference_object_id` | already selects of active objects of the event | unchanged, now labelled | Malformed ids now answer 404 like unknown ids (previously a server error). |

Not changed, by design: hidden `expected_version`, `operation_id`,
`idempotency_key` and `form_token` inputs (concurrency and idempotency
tokens, never typed by a person); the Prompt 5 badge and entry selects
(`location_id`, `allocation_id`, `badge_type_id`, `destination_location_id`,
`override_reason_id`), which already are scoped, labelled selects. The Not
Approved decision's `internal_reason_code` and `participant_reason_code`
are free-text codes with no approved catalogue; introducing one is a
product decision and they are unchanged (reported as a remaining item).

### 6.3 Untranslated internal codes replaced

`apps/core/display_labels.py` and the `code_label` filter cover export
purposes and statuses, communication purposes and statuses, language codes,
delegation row problems, bulk preview and execution reasons, checklist
items, requested fields and document types. Access profiles and rules show
their localized names instead of their codes (`localized_name`, no schema
change). A test fails when a producible code has no label
(`apps/core/tests/test_display_labels.py`).

### 6.4 Destructive and consequential actions (shared dialog)

| Action | Page | Dialog tone |
| --- | --- | --- |
| Withdraw registration (was a native `confirm()`) | participant workspace | danger |
| Cancel operationally | cancel page | danger |
| Record a Not Approved decision | decision page | danger |
| Revoke a role, badge type, access profile or access rule | registration accreditation | danger |
| Cancel an information request | information request detail | danger |
| Execute a bulk assignment | bulk preview | danger |
| Apply delegation rows | delegation batch | primary |
| Rotate an invitation link | campaign detail | primary |
| Change a campaign status | campaign detail | primary |

Reopen stays a dedicated confirmation page with a required reason and no
extra dialog: it opens a new review case and is reversible by a later
decision. The Prompt 5 badge, key and device commands keep their approved
Prompt 5 markup (dedicated reason fields, danger styling) and were not
changed in this gate.
