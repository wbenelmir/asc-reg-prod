# Vendored asset provenance — Bootstrap

| Field | Value |
| --- | --- |
| Package | Bootstrap |
| Pinned version | 5.3.8 |
| Official upstream source | `https://github.com/twbs/bootstrap/releases/download/v5.3.8/bootstrap-5.3.8-dist.zip` |
| License | MIT (confirmed against `https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/LICENSE` at vendoring time — not assumed) |
| SPDX identifier | `MIT` |
| Vendored on | 2026-09-19 |
| Vendored in | Prompt 2 — Engineering Foundation |

Only the LTR stylesheet, the RTL stylesheet (required by the approved Arabic RTL support), and the JS bundle (includes Popper) were extracted from the official dist zip. Demo pages, source maps for unused variants, grid-only and utilities-only builds, and the non-minified builds were not vendored, per TRD `FE-002` ("unused demo pages and plugins MUST be removed").

## Vendored project files and SHA-256 checksums

| File | SHA-256 |
| --- | --- |
| `bootstrap.min.css` | `d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede` |
| `bootstrap.min.css.map` | `48144faf6aa0fb3cd2ce748d9730238f888f4ab715f05dabd1c9af2c5671988a` |
| `bootstrap.rtl.min.css` | `b9048c83571ab73da54395b6b307219f9eb9c5447f4f8edb17a069eb0f19465a` |
| `bootstrap.rtl.min.css.map` | `7281121125b079c850e85bf8ebc0a731e36c467d9ab85f70c6a77588b883815b` |
| `bootstrap.bundle.min.js` | `e4fd49181388c48ec5040bd3fe66f57c29c8e67fcd8502b3354b96ec7ab47cc7` |
| `bootstrap.bundle.min.js.map` | `c61123e58cc0a4b65d737ba070c485911b3dbec6d7b802bdf6628395abd9c08b` |
| `LICENSE` | `4620c84ad5ce8602ff65640ed6b7c8b78ebb9e036584f0ebc1ccc88206a4bb51` |

Checksums are re-verified by `scripts/check.py assets`, which fails the run on any mismatch. No CDN reference exists anywhere in this project; templates load these files from local static paths only.

Re-verification: download the archive at the official upstream source URL above, extract, and compare `sha256sum` output against this table.
