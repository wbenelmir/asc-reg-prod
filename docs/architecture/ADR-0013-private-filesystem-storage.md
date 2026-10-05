# ADR-0013: Private filesystem storage behind the S3-compatible interface

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | Schema §7.2, §17.2; TRD `SEC-006`; accepted plan §5.11, §5.12 |

## Context

Schema §17.2 and TRD `SEC-006` require protected document bytes (the
official profile photograph; the conditional, policy-driven passport
identity page) in private, encrypted object storage -- never in
PostgreSQL. No container runtime is used (ADR-0012), so MinIO is not
available as a local S3-compatible service either.

## Decision

`apps/documents/storage.py` defines a `PrivateStorage` abstract interface
with **no public-URL method of any kind** -- a structural guarantee, not a
convention: `save`, `open`, `exists`, `delete`, `size`, `content_type` only.
Bytes can only ever be retrieved through application code that
deliberately opens a stored object after an authorization decision (the
Prompt 4 authenticated, policy-checked, audited streaming view, with the
exact denial behavior specified in accepted plan §5.12).

- **Local and test**: `PrivateFileSystemStorage`, rooted at `var/private/`
  (local) or a `tmp_path`-derived directory (test) -- outside `static/`,
  `staticfiles/`, any `MEDIA_ROOT`, and every served path. `var/` is
  gitignored.
- **Staging and production**: an S3-compatible backend
  (`django-storages[s3]` + `boto3`, already a locked dependency) selected
  by `PRIVATE_STORAGE_BACKEND = "s3"`. This is declared and configured for
  the deployment team but **never exercised or verified locally** --
  `get_private_storage()` raises `NotImplementedError` with an explicit
  message if `"s3"` is selected in this codebase's own test runs, so the
  gap cannot be silently mistaken for a working implementation.
- No document bytes are stored in PostgreSQL under either backend.
- The official profile photograph is the only Phase 1 upload enabled by
  default; the passport identity-page request remains disabled by default
  and policy-/request-driven, never a universal field.

## Consequences

- S3-compatible integration is explicitly listed as **not covered
  locally** in every Phase 1 test report -- never claimed as verified.
- Adding the real S3 implementation later is additive: it satisfies the
  same `PrivateStorage` interface, so no calling code changes.
