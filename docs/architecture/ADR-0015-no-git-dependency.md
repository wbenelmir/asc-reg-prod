# ADR-0015: No Git dependency at this stage; provider-neutral filesystem verification

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Developer execution-control decision (Prompt 2) |

## Context

Prompt 2 initially initialized local Git metadata (`git init`, no branch,
no commit) as the accepted plan's checkpoint 2a allowed ("only if absent").
The developer subsequently decided the project must remain
**local-filesystem-only** at this stage: no Git repository, no commit
history, no version-control-dependent verification of any kind.

## Decision

- The `.git` directory created earlier in Prompt 2 was removed. It
  contained no commit and no branch history, so nothing was lost.
- **No Git command is used from this point on**: no `git init`, `git add`,
  `git commit`, `git status`, `git diff`, `git check-ignore`, or remote
  configuration. None of this project's own tooling requires one.
- `.gitignore` is **retained as a future safety file** -- its rules stay
  correct and current -- but nothing in this project's verification
  depends on a version-control tool interpreting it.
- `scripts/check.py`'s local-safety verification was rewritten from a
  `git check-ignore`-based check to three purely filesystem/content-based
  checks, none of which require a `.git` directory:
  1. `check_gitignore_text_contract()` -- confirms `.gitignore`'s own text
     retains the required rules (`.env`, `.env.*`, `!.env.example`,
     `.venv/`, `var/`, `staticfiles/`).
  2. `check_env_file_is_excluded()` -- confirms `.env` exists locally and
     is structurally covered by the same exclusion set this project's own
     scan/manifest tooling applies (verified by asking that tooling
     directly, not by a separate claim).
  3. `check_env_example_placeholders_only()` -- confirms every value in
     `.env.example` is a documented placeholder or an approved non-secret
     literal default, never a real-looking value.
- Two new provider-neutral checks were added, replacing what `git
  status`/`git diff` would otherwise have been used for:
  - `scripts/check.py secret-scan` -- a shape-based scan (connection strings
    with embedded credentials, generic quoted `key="value"` secret
    assignments, Bearer tokens, AWS access key IDs, private key blocks)
    over every project-controlled file, excluding `.env`, `var/`, `.venv/`,
    generated artifacts, and vendored binary/minified assets by
    construction (`iter_project_files()`). An inline suppression marker
    (`SECRET_SCAN_ALLOW_MARKER`, on the flagged line or the line directly
    above it) is reserved for deliberately-synthetic test fixtures that
    exist to prove the scanner or the log-redaction logic works; every use
    of it is reviewed in the Prompt 2 report.
  - `scripts/check.py manifest` -- a SHA-256 manifest of every
    project-owned file, with the same exclusions as the secret scan except
    that vendored `PROVENANCE.md`/`LICENSE` files are included (their own
    provenance matters) while the compiled/minified assets they describe
    are not (already checksummed inside `PROVENANCE.md` itself).
- `scripts/check.py all` was updated to call the new checks and contains
  no Git invocation anywhere; verified empirically with `.git` absent.

## Consequences

- File-inventory and change tracking for review purposes now comes from
  `scripts/check.py manifest` (a SHA-256 manifest) and direct directory
  listings, not `git status`/`git diff`.
- If Git is adopted later, `.gitignore` is already correct and ready; no
  rule needs to be rewritten. Re-introducing `git init` at that point is a
  separate, explicit developer decision -- not implied by this ADR.
- Every check in this project remains runnable, and is verified to remain
  runnable, with no `.git` directory present.
