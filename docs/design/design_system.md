# ASC 2026 design system (Phase 3 Prompt 5)

> Extended platform-wide by the UI/UX Completion Gate:
> [`ui_ux_design_system.md`](ui_ux_design_system.md). The Prompt 5 record
> below is kept unchanged.

Status: implemented for the Entry & Security, participant pass, badge
operations, device administration and entry observability screens. The
design checkpoint was independently reviewed on 2026-09-23 ("approved with minor
corrections"); the three corrections are applied (see §7). This document is
not a claim of final brand approval: UI/UX §13 remains `[PROPOSED]`.

## 1. Sources

| Source | Use |
| --- | --- |
| `reference/finder/` (licensed, read-only) | Primary visual reference: neutral grey ramp, border/radius tokens, soft card elevation, borderless list navigation, pill tabs, form-label weight, table styling. Adapted, never copied; see `finder_inventory.md`. |
| `docs/design/assets/logo.svg` | Official logo, served unmodified as `static/img/brand/asc-logo.svg` at a fixed height with automatic width (537:240 preserved, asserted by the browser suite). |
| `docs/design/assets/Thmanyah-Font-Family.zip` | Arabic interface font: Thmanyah Sans 400/500/700 WOFF2, self-hosted from `static/vendor/thmanyah/` (see `PROVENANCE.md` there). |
| UI/UX Specification §9–§14 | Result pattern, connection states, colour roles, token groups, component inventory. |

**Font decision (developer, 2026-09-23):** Thmanyah Sans is used for
**Arabic only**; English and French use a system sans-serif stack
(`system-ui, "Segoe UI", Roboto, …`). The bundled Thmanyah license restricts
web embedding; the developer stated they hold an **extended license**
permitting self-hosting and inclusion in review archives. The written
extension is held by the developer and is not in the repository.

UI/UX §13.3 proposed IBM Plex Sans / IBM Plex Sans Arabic. That section is
`[PROPOSED]`, and the developer's explicit instruction to use Thmanyah as the
official Arabic font supersedes it; no IBM Plex asset is available locally,
and nothing is loaded from a CDN.

## 2. Architecture

* `static/css/asc-ui.css`: the whole design system. Every rule is scoped
  under `body.asc-ui`, so pages still on the legacy `base.html` chrome are
  unaffected.
* `templates/layouts/`: `base_entry.html` (tablet-first navy shell, connection
  indicator, checkpoint identity strip and entry navigation),
  `base_operations.html` (navy shell with breadcrumbs), `base_workspace.html`
  (light participant shell). All three extend `base.html` through new blocks
  (`extra_head`, `body_attrs`, `header`, `main`, `footer`, `extra_scripts`);
  the blocks' default content is the previous markup, so existing pages are
  unchanged.
* `templates/components/`: `icon`, `field`, `form_fields`, `error_summary`,
  `flash_messages`, `breadcrumbs`.
* `static/img/icons.svg`: project-owned line-icon sprite (24 px grid, 2 px
  round strokes, Finder's icon language). Icons are always decorative
  (`aria-hidden="true"`) next to visible text.
* `static/js/entry.js` (checkpoint behaviour) and `static/js/asc-ui.js`
  (double-submit guard for the operations/workspace shells).

## 3. Tokens

Brand roles follow UI/UX §13.2: navy `#102B3A` for the application bar,
deep blue `#326EB5` for interaction, cyan/green/light cyan for decoration only
(the diagonal motif under the application bar and on the pass card).

Spacing is an 8 px rhythm (`--asc-space-1` … `-5`), radii 6/8/12 px, soft
grey elevation, a 44 px minimum target (`--asc-target`) and 56 px checkpoint
actions (`--asc-target-lg`).

### Measured contrast (WCAG 2.2 relative luminance)

| Pair | Colours | Ratio |
| --- | --- | --- |
| text on surface | `#3f4652` / `#ffffff` | 9.51:1 |
| text-muted on surface | `#5f6673` / `#ffffff` | 5.78:1 |
| text-muted on canvas | `#5f6673` / `#f5f7fa` | 5.38:1 |
| heading on canvas | `#111827` / `#f5f7fa` | 16.53:1 |
| blue (link/primary) on white | `#326eb5` / `#ffffff` | 5.21:1 |
| white on primary | `#ffffff` / `#326eb5` | 5.21:1 |
| white on blue-strong (hover) | `#ffffff` / `#255896` | 7.20:1 |
| success-fg on success-bg | `#11663a` / `#eaf6ef` | 6.34:1 |
| white on success-solid | `#ffffff` / `#177245` | 5.95:1 |
| info-fg on info-bg | `#1d4f8c` / `#eaf1fa` | 7.24:1 |
| warning-fg on warning-bg | `#7a3d00` / `#fff4e0` | 7.72:1 |
| white on warning-solid | `#ffffff` / `#a3470b` | 6.06:1 |
| danger-fg on danger-bg | `#9b1c14` / `#fdecea` | 7.15:1 |
| white on danger-solid | `#ffffff` / `#b42318` | 6.57:1 |
| neutral-fg on neutral-bg | `#3f4652` / `#eef1f6` | 8.40:1 |
| white on navy (app bar) | `#ffffff` / `#102b3a` | 14.70:1 |
| focus ring on white | `#1b5faa` / `#ffffff` | 6.43:1 |
| focus ring on navy | `#63c5e3` / `#102b3a` | 7.44:1 |
| cyan (decoration only, never text) | `#029cbc` / `#ffffff` | 3.24:1 |

The browser suite additionally measures the rendered contrast of result
headings and primary actions (`CONTRAST_JS` in
`tests/browser/test_entry_ui_prompt5.py`, threshold 4.5:1).

## 4. Verification result pattern (UI/UX §9.5)

| Result | Tone | Icon | Announcement |
| --- | --- | --- | --- |
| Verified | success | check-circle | polite |
| Verified — advisory | info | info | polite |
| Manual verification required | warning | user-check | assertive |
| Outdated credential or verification | warning | refresh | assertive |
| Do not admit | danger | x-circle | assertive |
| Unsupported credential | danger | ban | assertive |
| Technical error — verify again | neutral | alert-octagon | assertive |

Each result has a distinct icon, a text heading and a tone: never colour
alone. The heading stays until acknowledged; participant details and
decision controls clear after inactivity (Flow §15.3).

## 5. States designed

Idle/empty scanner, loading ("Verifying…" plus double-submit guard), success,
advisory, manual review, denial, unsupported, technical error, field
validation error with a linked and focused summary (values never echoed),
lookups paused (rate limit), degraded online (slow gate or technical-error
burst), connection lost (lookups blocked), session ended (passive check),
details cleared, no permitted method, device not enrolled, unauthorized or
conflicting request, empty pass list, empty metrics window, and empty tables.

## 6. Direction and bidi rules (UI/UX §11.3)

* One stylesheet serves LTR and RTL through logical properties; Bootstrap's
  RTL build is loaded for Arabic.
* Only directional icons (arrows, sign-out, breadcrumb chevrons) mirror
  (`flip=True`); logos, QR codes and other symbols never do.
* **Dynamic values are isolated semantically, never by forcing a component
  to LTR:** names and free text use `<bdi>` (direction from their own
  content); inherently left-to-right codes use `<bdi dir="ltr"
  class="asc-code">`; inputs for codes carry `dir="ltr"`. Only the QR
  symbol's own wrapper is `dir="ltr"`.
* Error summaries isolate the field label and the message separately, so a
  label with parentheses or a Latin fragment cannot reorder in RTL.
* Arabic headings are never letter-spaced; Arabic body line height is 1.75.

## 7. UI checkpoint corrections (2026-09-23)

1. **Mobile device-scope chips** wrap onto whole rows; the browser suite
   asserts every chip lies fully inside the viewport and the list never
   scrolls.
2. **Dynamic LTR values in RTL** (gate, zone, device, reference, code,
   operator) are isolated with `<bdi>` / `<bdi dir="ltr">`; the former
   forced-LTR `.asc-ltr` class was removed. Asserted in the browser suite.
3. **Arabic validation-summary parentheses**: label and message are
   separately isolated; the browser suite measures that "(" renders to the
   right of ")" in the Arabic summary.

A fourth instance of the same class was found and fixed during propagation:
the participant pass caption inherited `dir="ltr"` from the QR container and
rendered scrambled in Arabic. It is now outside the QR wrapper, and asserted.

## 8. Responsive behaviour

Entry is tablet-first; operations desktop-first; participant adaptive.
Mobile: the application bar wraps, the checkpoint strip becomes a compact
grid, lookup tabs wrap, result layouts stack, KPI tiles pair up, and data
tables stay tables inside a horizontal scroller with edge shadows (the
scroller is the containing block for visually hidden labels, which otherwise
widened the page). No page scrolls horizontally at 390 px (asserted for
every mobile capture).
