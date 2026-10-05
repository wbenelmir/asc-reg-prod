# Vendored asset provenance — ALTCHA widget

| Field | Value |
| --- | --- |
| Package | `altcha` (npm), the ALTCHA web component |
| Pinned version | 3.2.3 (npm `latest` dist-tag, published 2026-09-20T09:11:01Z) |
| Official upstream source | `https://registry.npmjs.org/altcha/-/altcha-3.2.3.tgz` |
| Tarball integrity (npm registry record) | `sha512-yBHJIOoGZyU4/ap3AnlL2kChEVw40QP142BtxYZN5dCjAeBlaScuAEWxmrLDVQjlRBplayapUIvARs0eG48OVw==` |
| Tarball SHA-1 (npm `shasum`) | `2de3558e534617309e129441c66562c8eab0aa54` |
| Tarball SHA-256 (computed at vendoring) | `36a41435083c5c6a8abd9e15129d12b1d424aedf4e88a2eca51165c57350ccbb` |
| License | MIT — `package.json` `"license": "MIT"` and the tarball's `LICENSE.txt` (Copyright (c) 2023-2026 Daniel Regeci, BAU Software s.r.o.) |
| Bundled third-party code | the Svelte 5 runtime, compiled into `altcha.min.js` (MIT, Copyright (c) 2016-2025 Svelte Contributors; `LICENSE-svelte.md` fetched from `https://raw.githubusercontent.com/sveltejs/svelte/main/LICENSE.md`). The `hash-wasm` dependency is used only by the Argon2id and scrypt workers, which are NOT vendored. |
| Security advisories | none published for `altcha-org/altcha` or `altcha-org/altcha-lib-py` (GitHub security pages, checked 2026-09-30); `pypi.org/pypi/altcha/json` lists no vulnerabilities for 2.1.0 |
| Server library | `altcha` 2.1.0 on PyPI (MIT), pinned in `pyproject.toml` / `uv.lock`; v2 challenge protocol (`parameters` + `signature`), matching this widget |
| Vendored on | 2026-09-30 |
| Vendored in | UX-C2 (owner decision UX-D01 option M) |

The tarball was downloaded from the official npm registry, its SHA-512 was
compared with the registry's own `dist.integrity` value before use (equal), and
only these files were extracted. Each keeps its upstream bytes; only the file
name is flattened:

| Upstream path in the tarball | Vendored file |
| --- | --- |
| `package/dist/external/altcha.min.js` | `altcha.min.js` (the strict-CSP build: no bundled worker, no inline style) |
| `package/dist/external/altcha.css` | `altcha.css` |
| `package/dist/workers/pbkdf2.js` | `pbkdf2.js` (the only algorithm this project issues: `PBKDF2/SHA-256`) |
| `package/dist/i18n/en.js` | `i18n-en.js` |
| `package/dist/i18n/fr-fr.js` | `i18n-fr-fr.js` |
| `package/dist/i18n/ar.js` | `i18n-ar.js` |
| `package/LICENSE.txt` | `LICENSE.txt` |

No CDN, ALTCHA Cloud, Sentinel, remote verification or separately deployed
service is used. Challenges are issued and verified by this application
(`apps/core/human_check.py`). The files contain no `eval` and no
`new Function`. The widget's footer link and logo are hidden by configuration,
and the optional human-interaction signature collector is disabled.

## Vendored project files and SHA-256 checksums

| File | SHA-256 |
| --- | --- |
| `altcha.min.js` | `082729509fa6bd56aa4f30e9dcf132d8a89a0675695dad13d20c4f5fb636d9ec` |
| `altcha.css` | `03622256effb4e962d0c1da9a59684cf237f541f290703934c07a97dabcbad47` |
| `pbkdf2.js` | `7862add9c9d3d847ded3da928edcf406b46478754b9883351111414825b9c86f` |
| `i18n-en.js` | `3fd99ea6812a2290c1592fe4dc0098685ba8ffd12e61788f3d30032eef63c114` |
| `i18n-fr-fr.js` | `62ce6e8d06a6c0fb4ac8b1f6f872f603a3b005ce8d000de989cd926ab7ba52c1` |
| `i18n-ar.js` | `1ccb34ddeb0f0e4a51931d24c43abbfa819aa1e2890cb49a659aa8aab18a056d` |
| `LICENSE.txt` | `bee1fb9d9c97d42c12c22332c5250f2fb1ef5c2321f6f82beb23e3b77be50a29` |
| `LICENSE-svelte.md` | `06257f4847b13a49a039efb17b944c141e71b12fd8d231b362d86919067d77a3` |

Checksums are re-verified by `scripts/check.py assets`, which fails the run on
any mismatch or on any unlisted file in this directory.

Re-verification: download the tarball at the official upstream source URL
above, check its SHA-512 against the integrity value, extract the listed paths
and compare `sha256sum` output against this table.
