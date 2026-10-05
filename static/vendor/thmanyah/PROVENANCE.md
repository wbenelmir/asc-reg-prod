# Vendored asset provenance — Thmanyah Sans (Arabic interface font)

| Field | Value |
| --- | --- |
| Package | thmanyah typeface — `thmanyahsans` family, WOFF2 build |
| Copyright | © 2026 thmanyah Publishing and Distribution, Reserved Font Name "thmanyah" |
| Local source | `docs/design/assets/Thmanyah-Font-Family.zip` (SHA-256 `5b16d16a091cda3b11f8c86de800793ecb75c78943b7e0091003cdff514ede99`), supplied by the developer as the official Arabic font |
| Archive paths used | `thmanyah typeface/thmanyahsans/woff2/thmanyahsans-{Regular,Medium,Bold}.woff2`, `LICENSE.pdf` |
| License | thmanyah Font License (proprietary; `LICENSE.pdf`, kept beside the files, unmodified) |
| License basis for web self-hosting | The bundled license restricts web embedding. On 2026-09-23 the developer stated in session that they hold an **extended license** permitting self-hosting on this platform and inclusion in review archives. The written extension is held by the developer and is not stored in this repository. |
| Vendored on | 2026-09-23 |
| Vendored in | Phase 3 Prompt 5 |

Only three weights of the sans family were extracted (400, 500, 700). The
serif display/text families, the OTF builds, the Light and Black weights,
the Arabic-language PDF guides, and the archive's macOS metadata
(`__MACOSX/`, `.DS_Store`) were not vendored (TRD `FE-002`: ship only what is
used). The font files are byte-for-byte copies: they are not subset,
renamed, converted, or otherwise modified (the license prohibits
modification). They are declared in `static/css/asc-ui.css` with
`@font-face` and applied to Arabic (`:lang(ar)`) only; English and French use
a system sans-serif stack.

## Vendored project files and SHA-256 checksums

| File | SHA-256 |
| --- | --- |
| `thmanyahsans-Regular.woff2` | `5bb4fa412273ca31d5c7a165191568b099069cb74bf1fc1dfcdc6a3c2a97552e` |
| `thmanyahsans-Medium.woff2` | `490bf85c58c82b5989a557ed18cd6c6e5a7518b8a440032acf946b5cb7de2850` |
| `thmanyahsans-Bold.woff2` | `90f7c5b4c796e102eee70e20a9346fe680c8bfe38289932b5e94290d9f150fb7` |
| `LICENSE.pdf` | `07bbb2321bd2b944333c25f4d55140ca22625666132394d7399a365bd8b79a10` |

Checksums are re-verified by `scripts/check.py assets`, which fails the run
on any mismatch or on any unlisted file in this directory. No CDN reference
exists; templates load these files from local static paths only.
