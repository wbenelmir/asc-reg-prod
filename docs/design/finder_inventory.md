# Finder Component and Asset Inventory

Finder (Createx Studio, "Finder | Directory & Listings Bootstrap HTML
Template", © 2024 Createx Studio) is a licensed, read-only reference at
`reference/finder/`, excluded from version control and from every review
archive. **No Finder file, asset, icon, font, image or code was copied** into
the project. Every entry below is a *pattern adaptation*: Finder values or
structures were read and re-expressed as project-owned ASC tokens, CSS and
Django templates on top of the project's own vendored Bootstrap 5.3.8.
Because nothing was copied, no Finder licence notice needs to be carried in
project files; the attribution below is recorded for traceability.

Finder files inspected (only these): `README.md`,
`assets/css/theme.css` (`:root` tokens and the `.btn`, `.card`,
`.form-control`, `.form-label`, `.alert`, `.badge`, `.nav-link`, `.navbar`,
`.table`, `.list-group`, `.list-group-borderless`, `.nav-pills` rules), and
`account-listings.html` (header and account sidebar/content structure).

| Date | Project component | Finder source path | Use type | Adaptation summary | License checked | Reviewer |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-23 | Neutral colour ramp, border, canvas tokens (`static/css/asc-ui.css` `--asc-heading`, `--asc-text*`, `--asc-canvas`, `--asc-surface-sunken`, `--asc-border`) | `assets/css/theme.css` `:root` (`--fn-heading-color`, `--fn-body-color`, `--fn-tertiary-bg`, `--fn-secondary-bg`, `--fn-border-color`) | Pattern (values adapted) | Grey ramp re-tuned for WCAG AA on ASC surfaces (e.g. body text darkened from `#4e5562` to `#3f4652`, muted from `#6c727f` to `#5f6673`); Finder's red primary replaced by ASC deep blue. | Finder purchase licence (developer-held); no copy | Pending developer review |
| 2026-09-23 | Radius and elevation tokens (`--asc-radius*`, `--asc-shadow*`) | `theme.css` `--fn-border-radius*`, `--fn-box-shadow*` | Pattern | 0.5 rem base radius and soft grey shadows re-expressed as ASC tokens. | As above | Pending |
| 2026-09-23 | Card (`.asc-card`) | `theme.css` `.card` | Pattern | Hairline border, 1.5 rem padding, subtle shadow; ASC radius-lg. | As above | Pending |
| 2026-09-23 | Buttons (`.asc-ui .btn*`, `.asc-btn-xl`) | `theme.css` `.btn`, `.btn-lg` | Pattern | 500–600 weight, 0.5 rem radius, larger checkpoint size (56 px); colours are ASC semantic solids. | As above | Pending |
| 2026-09-23 | Form labels and controls (`.asc-ui .form-label`, `.form-control`) | `theme.css` `.form-label`, `.form-control` | Pattern | Heading-colour 600-weight labels, stronger borders, 44 px minimum height. | As above | Pending |
| 2026-09-23 | Pill tabs for lookup methods (`.asc-tabs`) | `theme.css` `.nav-pills`; `account-listings.html` nav pills | Pattern | Pill track with raised active pill; wraps on mobile. | As above | Pending |
| 2026-09-23 | Entry/participant navigation pills (`.asc-subnav`, `.asc-topnav`) | `theme.css` `.list-group-borderless`, `.nav-link`; `account-listings.html` sidebar | Pattern | Borderless rounded navigation items with a tinted active state, rendered horizontally for the tablet-first entry shell. | As above | Pending |
| 2026-09-23 | Data tables (`.asc-table`) | `theme.css` `.table` | Pattern | Hairline rows, muted small header, hover tint, tabular numerals. | As above | Pending |
| 2026-09-23 | Status chips (`.asc-chip*`) | `theme.css` `.badge` | Pattern | Pill chip with semantic tone and an icon; never colour alone. | As above | Pending |
| 2026-09-23 | Notices (`.asc-notice*`) | `theme.css` `.alert` | Pattern | Subtle tone background with a logical inline-start accent bar and icon. | As above | Pending |
| 2026-09-23 | Application bar layout (`templates/layouts/*`) | `account-listings.html` `<header class="navbar …">` | Pattern (structure) | Brand-left / actions-right bar re-expressed in Django templates; navy per UI/UX §13.4 (light for the participant shell). | As above | Pending |
| 2026-09-23 | Icon language (`static/img/icons.svg`) | `assets/icons/finder-icons.*` (visual style observed on pages only) | Style reference only | Project-owned SVG geometry drawn on a 24 px grid with 2 px round strokes to match the line-icon style. The Finder icon font was not copied or used. | Not applicable (no copy) | Pending |

### UI/UX Completion Gate, Checkpoint 1 (2026-09-23)

Additional Finder files inspected (only these, body markup only):
`account-signin.html`, `add-property-details.html`, and again
`account-listings.html` (sidebar list-group and its offcanvas behaviour).
As before, **nothing was copied**: no Finder file, image, icon, font, script
or markup fragment is in the project.

| Date | Project component | Finder source path | Use type | Adaptation summary | License checked | Reviewer |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-23 | Public / authentication shell (`templates/layouts/base_public.html`, `.asc-auth*`) | `account-signin.html` (form column ~416 px beside a cover column shown from `lg`) | Pattern (structure) | Narrow form column beside a panel from `lg`; Finder's cover image and social sign-in are replaced by the ASC navy panel with three factual statements and the ASC diagonal motif; the form sits in a card. | Finder purchase licence (developer-held); no copy | Pending developer review |
| 2026-09-23 | Wizard step list (`components/stepper.html`, `.asc-stepper`, `.asc-step*`) | `add-property-details.html` sidebar (`fi-check-circle` done, `fi-arrow-right-circle` current, `fi-circle` pending) | Pattern | Vertical step list on desktop with check, current and number markers; a compact numbered strip with a progress bar on small screens (Finder uses a horizontal scrolling row, replaced here so nothing scrolls or clips). Project icons only. | As above | Pending |
| 2026-09-23 | Wizard action bar and progress (`.asc-form-actions`, `.asc-stepper-bar`) | `add-property-details.html` sticky footer (4 px progress, Back / Next) | Pattern | Back (outline, arrow-prev) and Continue (primary, arrow-next) at the foot of the form card, not sticky; the progress bar moved into the step list. | As above | Pending |
| 2026-09-23 | Choice cards (`.asc-choice`, identity document) | `add-property-details.html` `.btn-check` + `.nav-link` option cards | Pattern | Two option cards with title and description; the native radio stays visible and focusable (Finder hides it). | As above | Pending |
| 2026-09-23 | Operations section bar (`components/ops_nav.html`, `.asc-sectionnav`) | `account-listings.html` `list-group-borderless` account sidebar + offcanvas below `lg` | Pattern | Borderless navigation items with a tinted current state, laid out horizontally under the navy bar so Prompt 5 tables keep their width; below `md` a native `<details>` disclosure replaces Finder's JavaScript offcanvas. | As above | Pending |

Not used from Finder: its Inter font, icon font, images, JavaScript, build
system, routing, page templates, and marketplace colour palette.

### UI/UX Completion Gate, Stage 3 (2026-09-23)

Additional Finder file inspected (only this one, `<main>` markup only):
`404-icon.html`. Nothing was copied: no Finder file, image (its traffic-cone
illustration included), icon, font, script or markup fragment is in the
project.

| Date | Project component | Finder source path | Use type | Adaptation summary | License checked | Reviewer |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-23 | Error and access pages (`templates/layouts/base_error.html`, `400/403/404/500/csrf_failure.html`, `.asc-error*`) | `404-icon.html` (centred narrow column, a 52 px bordered icon tile, a short heading, supporting text) | Pattern (structure) | One centred ASC card with a project icon tile, an "Error NNN" kicker, one h1, two short paragraphs and one or two actions. Finder's illustration, site search and marketing links are not used; the 500 and 400 pages are static-safe. | Finder purchase licence (developer-held); no copy | Pending developer review |

Project-owned, with no Finder source: the responsive stacked records
(`.asc-records`, `.asc-record*`), the shared confirmation dialog
(`components/confirm_dialog.html`, `.asc-dialog*`, native `<dialog>`), the
filterable picker (`components/picker.html`), the one-time secret panel
(`.asc-secret*`), the accreditation command cards (`.asc-assignment*`,
`.asc-command*`) and the shared conflict state
(`components/conflict_state.html`).


### UX-1: shared controls, brand mark and footer (2026-09-29)

**No Finder file was opened for this package, and nothing was copied.** The
new controls reuse the ASC tokens already recorded above (0.5 rem radius,
hairline border, focus ring, soft shadow), which are themselves pattern
adaptations of Finder's `.form-control`, `.form-label` and `.card` rules. The
Finder vendor libraries that ship with the template (Choices.js 11.0.2 MIT,
Cleave.js 1.6.0 Apache-2.0, flatpickr 4.6.13 MIT) were **not** used, copied or
loaded: the search-and-select control, the digit counter and the date entry
are project code, and no dependency was added.

| Date | Project component | Finder source path | Use type | Adaptation summary | License checked | Reviewer |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-09-29 | One-time code cells (`.asc-otp-input`, `input[data-asc-otp]`, `static/js/asc-enhance.js`) | Visual language only: the `.form-control` radius, hairline border and focus treatment already adapted in `asc-ui.css` | Pattern (values reused, no Finder source read in this package) | One accessible text input drawn as six cells with layered CSS gradients and monospace letter-spacing; the script only sanitizes digits. Finder's split-input sign-in pages were not consulted for markup. | Finder purchase licence (developer-held); no copy | Pending developer review |
| 2026-09-29 | Search-and-select (`.asc-combobox*`, `setupCombobox`) | Visual language only: `.form-select` and dropdown-menu look (radius, shadow, active tint) already adapted in `asc-ui.css` | Pattern | Project-owned WAI-ARIA 1.2 combobox over a native select. Finder's Choices.js integration is not used. | As above | Pending |
| 2026-09-29 | Day / Month / Year entry (`.asc-date-parts*`, `apps/core/widgets.py`) | None | Project-owned | Three labelled numeric inputs in one fieldset. flatpickr is not used. | Not applicable | Pending |
| 2026-09-29 | Digit counter (`.asc-digit-counter`) | None | Project-owned | A running "n / max" beside a digits-only identifier; it counts and never edits. Cleave.js is not used. | Not applicable | Pending |
| 2026-09-29 | Footer official links (`templates/partials/footer_official.html`, `.asc-footer-links`) | None | Project-owned | Two authorized external links with a decorative inline SVG glyph drawn for this project. | Not applicable | Pending |
| 2026-09-29 | Compact brand mark (`static/img/brand/asc-mark*.{svg,png}`, `scripts/build_brand_icons.py`) | None (the source is the project's authorized logo, not Finder) | Derivative of the project logo | The twelve stripe paths of `asc-logo.svg`, copied byte for byte into a square viewBox; PNGs rendered from that SVG. No Finder favicon or icon was used. | Project logo; developer-held | Pending developer visual approval (D-04) |
