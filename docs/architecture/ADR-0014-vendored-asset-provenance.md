# ADR-0014: Vendored third-party asset provenance policy

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | TRD `FE-001`, `FE-002`; accepted plan §5.3 |

## Context

TRD `FE-002` requires an asset inventory identifying retained CSS,
JavaScript, fonts, icons, and licenses, with unused demo pages and plugins
removed. No CDN dependency is permitted (offline-capable, no third-party
runtime trust).

## Decision

Every vendored third-party browser asset (Bootstrap, htmx; axe-core from
Prompt 4 onward) is vendored under `static/vendor/<name>/` (or
`tests/assets/vendor/<name>/` for test-only assets) with a `PROVENANCE.md`
recording:

- the exact pinned version;
- the official upstream source URL;
- the license (an included `LICENSE` file plus its SPDX identifier,
  **confirmed against the actual upstream file at vendoring time, not
  assumed** -- htmx 4.0.0's `LICENSE` was confirmed to be Zero-Clause BSD
  (`0BSD`), correcting an earlier plan assumption of BSD-2-Clause);
- the SHA-256 checksum of every vendored file;
- the vendoring date and actor.

`scripts/check.py assets` recomputes every checksum against `PROVENANCE.md`
and fails the run on any mismatch; it is part of `scripts/check.py all`.

Only the files actually needed are vendored -- for Bootstrap 5.3.8: the LTR
stylesheet, the RTL stylesheet (required by the approved Arabic RTL
support), and the JS bundle (includes Popper); for htmx 4.0.0: the single
production build. Demo pages, unused build variants, extension libraries,
editor integrations, and non-minified sources are not vendored.

**No CDN reference exists anywhere in this project.** Templates load these
files from local static paths only.

## Consequences

- Re-verifying provenance requires no network access beyond the recorded
  upstream URL and a `sha256sum` comparison against the table in
  `PROVENANCE.md`.
- Adding a new vendored asset (e.g. axe-core in Prompt 4, or a font once
  the visual direction is approved) must follow this exact same pattern:
  pinned version, upstream URL, confirmed license, checksum, provenance
  date/actor, checksum wired into `scripts/check.py assets`.
