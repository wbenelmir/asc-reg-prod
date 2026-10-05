# Vendored asset provenance — htmx

| Field | Value |
| --- | --- |
| Package | htmx |
| Pinned version | 4.0.0 |
| Official upstream source | `https://github.com/bigskysoftware/htmx/releases/download/v4.0.0/htmx-4.0.0-dist.zip` |
| License | Zero-Clause BSD (`0BSD`) — confirmed against `https://raw.githubusercontent.com/bigskysoftware/htmx/v4.0.0/LICENSE` at vendoring time |
| SPDX identifier | `0BSD` |
| Vendored on | 2026-09-19 |
| Vendored in | Prompt 2 — Engineering Foundation |

**Correction to the accepted plan.** The Phase 1 plan's §5.3 anticipated htmx would be BSD-2-Clause. The actual upstream `LICENSE` file confirms **Zero-Clause BSD (0BSD)**, which is even more permissive. Recorded here as the plan instructed — confirmed against the upstream file, not assumed.

Only the single production build (`htmx.min.js`) was extracted from the official dist zip. The extension library (`dist/ext/*`), editor integrations, upgrade-check scripts, ESM build, and skills/documentation files were not vendored, per TRD `FE-002`.

## Vendored project files and SHA-256 checksums

| File | SHA-256 |
| --- | --- |
| `htmx.min.js` | `e484d9171a9db30a39c8f16e3d709d4137f3211c659f8e6125816635033d593f` |
| `LICENSE` | `d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38` |

Checksums are re-verified by `scripts/check.py assets`, which fails the run on any mismatch. No CDN reference exists anywhere in this project; templates load this file from local static paths only.

Re-verification: download the archive at the official upstream source URL above, extract `dist/htmx.min.js`, and compare `sha256sum` output against this table.
