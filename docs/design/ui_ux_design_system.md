# ASC 2026 product design system (UI/UX Completion Gate)

Status: **Stage 3 complete: every custom user-facing page uses this design
system (READY_FOR_LOCAL_REVIEW).** Checkpoint 1 (shared foundation and
representative slice) passed the independent visual review with required fixes,
which Stage 3 applies. This document
supersedes nothing in the Prompt 5 record. It extends
[`design_system.md`](design_system.md), which still holds the Prompt 5
tokens, measured contrast table, verification-result pattern and the three
UI checkpoint rules, to the whole platform. Where the two overlap, this
document is the platform-wide rule and `design_system.md` is its origin.

Page-by-page status and treatment: [`ui_ux_completion_inventory.md`](ui_ux_completion_inventory.md).
Finder adaptations: [`finder_inventory.md`](finder_inventory.md).

## 1. Principles

1. **One platform.** Every page renders inside one of four shells and uses
   the same tokens, components and status language. No page styles itself
   in isolation.
2. **Content first, quiet chrome.** Neutral grey surfaces, one interaction
   blue, navy only for the application bar and the public brand panel.
   Gradients are limited to that panel, and the ASC diagonal motif appears
   as a 3 px rule under application bars and once on the public panel.
3. **Meaning is never colour alone.** Every status carries a text label and
   an icon. Every error carries text and an icon, and is linked from a
   focused summary.
4. **Direction by logic, not by mirroring.** Logical CSS properties only.
   Dynamic values are isolated with `<bdi>`. Only directional icons flip.
5. **Presentation never authorizes.** Navigation lists only the areas a
   user's permissions would open, but views keep enforcing permission and
   scope (`apps/core/navigation.py`).

## 2. Architecture

| Layer | Location | Notes |
| --- | --- | --- |
| Stylesheet | `static/css/asc-ui.css` | Single file. Loaded by `base.html` for **every** page; every `<body>` carries `asc-ui` (gate finding F2). |
| Shells | `templates/layouts/` | `base_public.html` (authentication and public), `base_workspace.html` (participant), `base_wizard.html` (registration wizard, extends workspace), `base_operations.html` (back office), `base_entry.html` (checkpoint), `base_error.html` (Stage 3: 400, 403, 404, 500, CSRF failure; static-safe). `base.html` has no legacy chrome left. |
| Components | `templates/components/` | `field`, `form_fields`, `error_summary`, `flash_messages`, `breadcrumbs`, `breadcrumb_trail` (new), `icon`, `status_chip` (new), `stepper` (new), `pagination` (new), `ops_nav` (new); Stage 3: `confirm_dialog`, `confirm_fallback`, `picker`, `conflict_state`. The legacy `partials/form_field.html` and `partials/error_summary.html` are deleted. |
| Template tags | `apps/core/templatetags/asc_ui.py` | `status_chip`, `operations_nav`, `operations_home_url`, `asc_control` filter, `ltr_isolate` filter; Stage 3: `code_label` (translated labels for stored codes, `apps/core/display_labels.py`) and `get_item`. |
| | `apps/registrations/templatetags/registration_ui.py` | `wizard_stepper`, `wizard_previous_url`. |
| Behaviour | `static/js/asc-ui.js`, `static/js/entry.js`, `static/js/asc-enhance.js` (Stage 3, every page: confirmation dialog, filterable picker), inline scripts in `base.html` | Progressive enhancement only; every form works without JavaScript. |
| Icons | `static/img/icons.svg` | 41 project-owned symbols (10 added in CP1: `check`, `arrow-prev`, `edit`, `menu`, `building`, `download`, `mail`, `log-in`, `filter`, `inbox`). |

**Boosted navigation rule.** htmx 4 boosting (`hx-boost` on `#main-content`,
kept on the public, wizard and participant-registration pages exactly as on
the legacy chrome) swaps the body contents but **keeps the previous
`<body>` attributes and discards the new `<head>`**. Therefore:

* no CSS rule may depend on a shell body class (`asc-public`,
  `asc-workspace`, `asc-operations`, `asc-entry`); they are semantic hooks;
* page-specific stylesheets are linked inside the content block (as the
  printable pass already does), never in `<head>`;
* the operations and entry shells are not boosted.

## 3. Tokens

Unchanged from Prompt 5 (`design_system.md` §3), including the measured
contrast table. Additions in CP1:

| Token / value | Use | Contrast |
| --- | --- | --- |
| `--asc-blue-strong` `#255896` | Kicker text (`.asc-kicker`), current stepper marker | 7.20:1 on white, 6.71:1 on canvas |
| `--asc-danger-solid` `#b42318` as outline-button text | `.btn-outline-danger` | 6.57:1 on white |
| `#fbfcfe` | Conditional sub-panel surface (`.asc-subpanel`) | body text 9.26:1 |
| `.asc-optional` tag (`#5f6673` on `#eef1f6`) | "Optional" field tag | 5.10:1 |
| Navy panel radial highlight `rgba(50,110,181,.45)` | Public brand panel only | white text 14.70:1 (navy) to 9.33:1 (brightest point); cyan-light kicker ≥ 4.72:1 |

Chip contrast is asserted in the browser suite for every rendered chip
(≥ 4.5:1, `test_operations_pages_have_no_overflow_and_readable_chips`).

## 4. Typography

| Role | Size | Weight | Notes |
| --- | --- | --- | --- |
| Page title (h1) | `clamp(1.5rem, 1.2rem + 1vw, 2rem)` | 700 | One h1 per page (asserted). Auth pages up to 2rem. |
| Brand panel title (h2) | `clamp(1.75rem, 1.2rem + 1.6vw, 2.5rem)` | 700 | Public shell only. |
| Section / card title | 1.0625–1.25rem | 700 | `.asc-card-title`, `.asc-form-section-title`, `.asc-card-toolbar-title`. |
| Kicker | 0.875rem | 600 | Context above an h1 ("Registration · ASC 2026"). |
| Body | 1rem | 400 | Line height 1.55 (Latin), 1.75 (Arabic). |
| Labels | 1rem | 600 | Heading colour. Optional fields get an "Optional" tag in the wizard. |
| Meta / table header | 0.8125–0.875rem | 600 | Muted. |
| Codes | 0.95em mono | — | `.asc-code` in `<bdi dir="ltr">`; never wraps inside tables. |

Fonts: Thmanyah Sans 400/500/700 (Arabic only, self-hosted, developer-held
extended licence; `static/vendor/thmanyah/`), system sans-serif for English
and French (`design_system.md` §1). Only Regular and Bold are preloaded, and
only on Arabic pages. The original ZIP is never served. Arabic headings are
never letter-spaced; upper-case transforms are disabled for Arabic.

**Load proof (Stage 3).** A family name in CSS proves nothing, so the browser
suite checks the real thing (`tests/browser/test_ui_ux_stage3.py`): the three
`.woff2` files answer HTTP 200 with `font/woff2`; after `document.fonts.ready`
and `document.fonts.load()` with Arabic text every Thmanyah `FontFace` reports
`loaded` for 400, 500 and 700; Chromium's own platform-font report (DevTools
protocol `CSS.getPlatformFontsForNode`) shows the custom `thmanyahsans-*` face
for body text, headings, labels, buttons, validation messages, field errors,
status chips, table headers and navigation; English and French pages request
no Thmanyah file and render with a system face. A control test aborts the font
requests and shows the same checks fail while the CSS family name is still
present. `font-synthesis: none` on Arabic prevents faked bold or italic; the
600 label weight resolves to the supplied Bold file.

## 5. Spacing, layout and breakpoints

8 px rhythm (`--asc-space-1…5`). Containers: `container-xl` (operations,
entry, public), `container-lg` (participant). Supported widths, all
asserted free of page-level horizontal overflow in CP1: 360, 390 (phone),
820 (tablet), 1440 (desktop), 1920 (wide).

| Breakpoint | Behaviour |
| --- | --- |
| < 576 | Stepper markers only; the current step name joins the count line. Filter grid one column below 400. |
| < 768 | Cards 1rem padding; form grids and choice cards one column; wizard actions stack full-width (primary first); operations section bar collapses into a disclosure; light application bars keep their controls beside the logo. |
| < 992 | Public brand panel hidden; wizard stepper moves above the form as a compact strip; application bar not sticky. |
| < 1200 | Detail pages stack the command column under the evidence column (DOM and focus order preserved), two command cards per row where they fit. Filters in four columns (tablet), search full width. |

## 6. Components

### 6.1 Buttons and action hierarchy

* One primary (`btn-primary`) per form. Destructive actions use
  `btn-outline-danger` beside the primary, or `btn-danger` only on a
  dedicated confirmation page. Positive decisions use `btn-success`.
* Secondary actions: `btn-outline-primary` (commands) or
  `btn-outline-secondary` (navigation such as Back or Clear filters).
* Minimum height 44 px for every button, including `btn-sm` (asserted);
  `asc-btn-lg` (48 px) for wizard and authentication primaries; `asc-btn-xl`
  (56 px) at the checkpoint.
* Directional icons (`arrow-next`, `arrow-prev`, `log-in`, `log-out`,
  `chevron-next`) are always passed `flip=True`.
* Action bars (`.asc-form-actions`): Back link at the start and primary at
  the end; on phones they stack with the primary on top. Back is always a
  link (a GET page), so each wizard form keeps exactly one submit button.

### 6.2 Forms and validation

* `components/field.html` renders label → help → control → error. The
  control goes through `asc_control`, which merges the Bootstrap class,
  `aria-describedby` (help and error ids), `aria-invalid`, `aria-required`
  and `dir="ltr"` for email, URL and any field passed `control="ltr"` (codes,
  identity numbers, phone numbers, dates). Idempotent with forms that
  already set these attributes.
* Single checkboxes render input-then-label inside a bordered 44 px row.
  Checkbox groups render as `.asc-check` tiles. Radio choices that switch
  content render as `.asc-choice` cards with the native radio visible.
* Conditional groups (identity panels) are `fieldset.asc-subpanel` with a
  visible legend. Hidden panels are disabled, as before.
* Errors: `components/error_summary.html` at the top (focused on load,
  label and message separately isolated), plus a per-field message with an
  icon. Values that must not be echoed are never re-shown.
* Model choice selects show a translated "Select…" prompt instead of
  Django's `---------`.

### 6.3 Cards, summaries and metrics

`.asc-card` (hairline border, 12 px radius, soft shadow). Variants:
`.asc-card-flush` for tables with a `.asc-card-toolbar` (title and count)
and `.asc-card-foot` (pagination, notes). `.asc-card-form` for an inline
command inside an evidence card. Review summaries use `.asc-summary`
(heading plus an "Edit <section>" link with section context for assistive
technology). KPI tiles: unchanged (`.asc-kpi`).

### 6.4 Tables, filters, pagination

* **Stage 3 (UI checkpoint 1 correction):** list pages render their rows
  twice: a real table from 768 px (`.asc-wide-only`) and the same rows as
  stacked records below it (`.asc-narrow-only`: `ul.asc-records >
  li.asc-record`, a record title with the reference, a `dl` of label/value
  pairs, one action per record). Only one is displayed; `display: none`
  removes the other from the accessibility tree and the focus order. Chips
  wrap inside records; every chip and action is asserted inside its record
  and the viewport at 360 and 390 px in English, French and Arabic.
  Prompt 5 tables keep their approved scrolling behaviour.
* Wide-screen tables stay tables, inside `.asc-table-wrap` (horizontal
  scroll with edge shadows). The row header is `th scope="row"` (the
  reference). Numbers use `.asc-num` (tabular, end-aligned). Row actions
  carry visually hidden context ("View ASC26-UI-0001").
* Filter bar (`.asc-filters`): a card with a labelled `role="search"` form,
  search field with a leading icon, selects, Apply, and Clear filters when
  any filter is present.
* Pagination (`components/pagination.html`): Previous / "Page X of Y" /
  Next, with disabled ends rendered as `aria-disabled` spans. Hidden when
  there is one page; the result count is always shown in the toolbar,
  using proper plural forms (6 in Arabic).

### 6.5 Status language

`{% status_chip family value label %}`. The tone and icon come from
`apps/core/templatetags/asc_ui.py::_STATUS_TONES`; a test fails if a stored
status value has no mapping.

| Family | success | info | warning | danger | neutral |
| --- | --- | --- | --- | --- | --- |
| registration (public) | Approved | Submitted, Under review | Additional information required | Not approved | Draft, Withdrawn |
| processing (internal) | Qualification complete | Assigned, Review in progress | Verification pending, Duplicate review, Awaiting applicant | — | Pending assignment, Closed |
| review case | Completed | Assigned, In progress | Waiting | — | Queued, Cancelled |

Pass, badge and entry statuses keep their Prompt 5 components.

### 6.6 Alerts and feedback

`.asc-notice` (info, success, warning, danger, neutral) with an icon and a
logical inline-start accent. Flash messages use it with `role="status"`.
Session-expiry warnings are a full-width warning banner under the
application bar (`.asc-session-banner`), revealed by the existing timer.
Success of a completed journey uses the result pattern (`.asc-result`,
confirmation page).

### 6.7 Navigation

* **Public shell:** logo (links to the start page, or to operational
  sign-in on that page) and the language switcher in a `nav` labelled
  "Language".
* **Participant:** logo, "My registrations" / "My entry passes"
  (`aria-current="page"` on the list, `"true"` while inside that area),
  language, sign out.
* **Wizard stepper** (`components/stepper.html`): an ordered list of six
  steps inside a `nav` named by "Step N of 6". Done steps are links with a
  check and "(completed)"; the furthest reached step stays linked when the
  participant edits an earlier one; upcoming steps are plain text; the
  current step has `aria-current="step"`.
* **Operations section bar** (`components/ops_nav.html`): the areas the
  user's permissions open, from `apps.core.navigation.OPERATIONS_SECTIONS`.
  Each section's permission is proven against its real view in
  `apps/core/tests/test_ui_foundation.py`. The brand link goes to the first
  permitted area. Below 768 px it is a native `<details>` disclosure (open
  without JavaScript).
* **Breadcrumbs:** back-office detail pages; the chevron mirrors in RTL.

### 6.8 Dialogs and confirmations

**Shared confirmation dialog (Stage 3).** One native `<dialog>`
(`components/confirm_dialog.html`) per page; a form opts in with
`data-confirm` (question), `data-confirm-title`, `data-confirm-action`
(button label) and `data-confirm-tone="primary"` for a non-destructive
consequential action. `asc-enhance.js` intercepts the submission, opens the
modal (background inert), focuses "Go back" first; Escape or "Go back"
closes it and returns focus to the button that opened it. Opening the dialog
executes nothing: only the confirm button re-submits the form through the
browser's own submission (`requestSubmit`), so CSRF, POST-only, htmx
boosting, the double-submit guard and every server check apply unchanged. A
visibly incomplete `novalidate` form goes straight to server validation
instead of asking to confirm. Without JavaScript, `components/confirm_fallback.html`
renders a required consent checkbox inside `<noscript>`. The native
`confirm()` is gone. The list of covered actions is in the inventory §6.4;
reopen remains a dedicated reason page.

### 6.9 Empty, loading, error and conflict states

* Empty: `.asc-empty` (icon tile, heading, one sentence, optional action),
  inside the card whose content is missing.
* Loading: the double-submit guard (`asc-ui.js`) marks the form
  `aria-busy`, shows the localized "Working…" text and disables the
  submitter; the entry shell keeps its Prompt 5 "Verifying…" state.
* Error: error summary plus field errors (§6.2); server conflicts (409)
  render `components/conflict_state.html` ("Nothing was changed", the
  view's message as a flash notice, one way back), in the operations shell
  or, for participant commands, the participant shell.
* Error pages: `layouts/base_error.html` on the public shell (light bar,
  official logo, language switcher when a request exists), one centred card
  with an icon tile, an "Error NNN" kicker, one h1 and actions. 403 offers
  the operations home and sign-out; 400 and 500 render without a request
  context and touch no database, session or user; the CSRF page never shows
  the technical reason.
* Choices instead of ids (F8): `components/picker.html` is a labelled native
  select of the server's permission-scoped choices; above 8 choices a filter
  box hides non-matching options locally (no request, nothing outside the
  list can be found). `apps.core.forms.ScopedModelChoiceField` validates the
  POST against the same scope and answers forged, unknown and malformed ids
  with one generic error.
* Unavailable / not open: an empty-state card with the next useful action.

## 7. Direction, language and mixed content

All Prompt 5 rules (`design_system.md` §6–§7) apply platform-wide:

* `<bdi>` for names and free text; `<bdi dir="ltr" class="asc-code">` for
  references, codes and event codes; `dir="ltr"` on inputs for codes,
  numbers, email, URL and dates.
* `ltr_isolate` isolates a value inside `{% blocktranslate %}` without
  changing the msgid (for example the email in "We sent a 6-digit code to
  …", or the reference in "Review case for …").
* Error summaries isolate the label and the message separately.
* Arabic plural forms are complete (6 forms) for every count string.
* The public panel's decorative motif mirrors in RTL; logos, QR codes and
  non-directional icons never mirror.
* Language switching preserves only the opted-in safe wizard fields; the
  test synchronisation defect behind the Prompt 6 flake is fixed (inventory
  F5).

## 8. Accessibility checklist (asserted in the browser suite)

| Requirement | Evidence |
| --- | --- |
| Skip link first, then header, then content | `test_start_page_focus_order_…`, `test_skip_link_…` |
| Visible focus | 3 px outline via `:focus-visible` on every control; `.asc-choice` shows it on the card; asserted on auth and wizard pages |
| Landmarks and headings | banner, `nav` (language / participant / sections / stepper / breadcrumb), main, contentinfo; exactly one h1 per page |
| Labels and error association | `aria-describedby` / `aria-invalid` asserted on the identity step; the summary receives focus |
| Contrast ≥ 4.5:1 | design_system.md table; chips measured in the browser |
| Not colour alone | chips always carry text and an icon (asserted) |
| Touch targets ≥ 44 px | buttons, toggles and wizard links (asserted at 390 px) |
| Reflow | no page-level horizontal overflow at 360–1920 px in EN and AR |
| Reduced motion | global rule in `app.css` and `asc-ui.css` |

Screen-reader, camera and hardware-scanner sessions have **not** been
performed; nothing here claims them. Nor has any physical phone or tablet
been used: widths are emulated in headless Chromium.
