# P4-4 hardening notes: deployment-facing behaviour

Date: 2026-10-01. Companion to `p4_4_threat_permission_data_flow_matrix.md`. This file states what
P4-4 changed that a deployment must know, and what stays unproven locally. It executes nothing and
approves nothing. Sections marked **P4-4-C3** record owner decisions MFA-01 (revised, amendment
A-11) and CACHE-01 (ADR-0025); the staging steps are in `docs/testing/phase_04_p4_4_c3_staging_guide.md`.

## 1. Response headers

* **CSP** (`apps/core/middleware/security_headers.py`). Enforced on every response:
  * `default-src 'self'`, same-origin scripts, styles, fonts, workers and manifest;
  * `img-src 'self' data:`, `form-action 'self'`;
  * `object-src`, `frame-src`, `media-src`, `base-uri` and `frame-ancestors` set to `'none'`;
  * no `'unsafe-inline'`, no `'unsafe-eval'`, no nonce and no wildcard;
  * `upgrade-insecure-requests` in staging and production;
  * a view that sets its own CSP keeps it.

  Templates must never contain an inline `<script>`, an `on*=` handler or a `style=` attribute; a
  unit test guards the templates, and the browser suite fails on any CSP console violation.
* **htmx and inline styles.** htmx 4 settles a swap by copying the old element's attributes onto
  the new one with `setAttribute`. A `style` attribute that script (or a browser tool) left on an
  element was therefore re-applied as an inline style, which the CSP refuses; the first P4-4 gate
  found this. `<meta name="htmx-config">` in `templates/base.html` now adds `style` to
  `morphIgnore`, keeping htmx's own default entry. The setting is read once, from the first page
  (boosted swaps drop the new `<head>`).
* **Permissions-Policy** denies camera, microphone, geolocation, payment, usb, serial, hid and
  display-capture.
  * No page uses any of these today. A checkpoint scanner types into a text field like a
    keyboard, and camera decoding is not implemented (ADR-0023; the PRD FR-ENT-001 gap).
  * If camera decoding is added later, the policy for those pages must change in a reviewed step
    (`camera=(self)`), never globally.
* **Static files.** Django's middleware sets these headers only on responses that Django serves.
  The web server or CDN that serves `/static/` must send at least:
  * `X-Content-Type-Options: nosniff`;
  * long-lived caching for the hashed files (DEP-004).

  The service worker (`/entry/sw.js`) and the manifest are Django views, so they get the policy.

## 2. HSTS rollout (decision HSTS-01)

Staging and production accept `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`
and `DJANGO_SECURE_HSTS_PRELOAD`. The defaults are the previously approved one year with
subdomains and preload. Validation refuses:

* a zero max-age;
* preload without includeSubDomains;
* preload below one year.

The recommended rollout needs no code change:

1. 300 s, then 1 day, then 1 week, then 1 year.
2. Preload only after the owner confirms that the domain and every subdomain serve HTTPS
   permanently. Preload is hard to undo.

## 3. Proxies and HTTPS

* `X-Forwarded-Proto` is removed unless the direct peer is inside `TRUSTED_PROXY_CIDRS` with
  forwarding enabled.
* `SECURE_PROXY_SSL_HEADER` is set only in that case.
* An invalid CIDR entry is refused by its position, without echoing its value.
* The deployment must list its load balancer addresses exactly (P4-5).

## 4. Throttles and where their state lives

| Limit | Counter | Behaviour under outage | Setting |
| --- | --- | --- | --- |
| OTP issuance per recipient and per network | PostgreSQL (advisory locks, exact) | Fails with the database | `OTP_MAX_ISSUANCES_*` |
| Operational sign-in, per typed email (since the last success) and per network | Audit trail in PostgreSQL. Exact per email: an advisory lock covers the password check. Best effort per network. | Fails with the database | `OPERATIONAL_SIGN_IN_WINDOW_SECONDS` (900), `OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL` (10), `OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK` (50) |
| ALTCHA challenge endpoint, per IPv4 address or IPv6 /64 | Django cache, HMAC key. **P4-4-C3:** a shared Redis counter in staging and production (`REDIS_URL` database); per process locally | P4-4: **fails open**, logged without the address. **P4-4-C3:** a stricter per-process fallback with alerts; refused if no counter can count | `HUMAN_CHECK_CHALLENGE_*`, `HUMAN_CHECK_COUNTER_*` |
| Checkpoint lookups, fallback reference | Audit trail | Fails with the database | `ENTRY_LOOKUP_*`, `FALLBACK_REFERENCE_LOOKUP_*` |

* **Operational sign-in.** A refused attempt answers 429 with one generic message. It is
  identical for a known and an unknown email, and the password is not checked.
  * The limits are reviewable defaults, not an owner-approved policy.
  * A zero or negative value is treated as 1, so a misconfiguration cannot lock every account.
  * An auditor finds the attempts for an address with
    `apps.accounts.operational_sign_in.email_target_uuids(email)`.
* **ALTCHA counter (CACHE-01).** With the default LocMemCache, each worker process counts on its
  own. The recommendation is to back Django's cache with the already approved Redis in staging and
  production. It stays defence in depth: the proof of work and the database OTP limits still apply.
* **P4-4-C1 (R-05).** Staging and production do not override `CACHES`, so today the issuance limit
  is per process, and a cache error allows the request. Production protection of challenge
  issuance and anonymous session growth is therefore **PARTIAL**. The OTP action's own controls
  (ALTCHA verification, binding, expiry, atomic one-time use, database OTP limits) are exact and
  unaffected. The counter-store and degraded-mode options, the recommendation and the required
  tests are in the historical review record `phase_04_p4_4_c1_owner_decision_brief.md` (kept outside the repository) §C. Nothing is selected.
* **P4-4-C3 (CACHE-01 decided; ADR-0025).** The two bullets above describe the state before this
  package.
  * Staging and production now count issuance in one shared Redis counter: an atomic MULTI/EXEC of
    `INCR` and `EXPIRE` per request, keys `asc:abuse:v1:hc-issue:<HMAC digest>:<window>`.
  * The connection has 500 ms connect and socket timeouts and no retries.
  * On any Redis failure, each process applies the stricter fallback (20 per network per
    10 minutes by default) and retries Redis every 15 s.
  * `asc2026.ops.alerts` reports `HUMAN_CHECK_COUNTER_DEGRADED`, `HUMAN_CHECK_COUNTER_RECOVERED` and
    `HUMAN_CHECK_COUNTER_UNAVAILABLE`, rate-limited and with fixed fields only.
  * Bounds: 60 per network and window when healthy; *N* × 20 for *N* processes when degraded;
    60 + 20 *N* in a window that overlaps an outage.
  * Deployment requirements: `REDIS_URL` must be `rediss://` with certificate checks, in a database
    separate from the broker (validation enforces both), on a Redis with
    `maxmemory-policy noeviction` (not enforceable by the application; see the staging guide).
  * Locally the counter stays per process, and no Redis is needed.
  * Real-Redis behaviour is **NOT PROVEN** until the staging verification runs.

## 5. Sessions

* `SESSION_COOKIE_AGE` is 10 h, the longest approved lifetime. The participant and operational
  inactivity and absolute clocks still apply on top.
* A session created only for an ALTCHA challenge expires after 35 min of inactivity. A sign-in
  restores the ordinary lifetime.
* Sessions stay database-backed.

## 6. Periodic maintenance (`CELERY_BEAT_SCHEDULE`)

The schedule is declarative. It takes effect only where the deployment runs `celery beat`, which
is a P4-5 runbook item. Every entry is idempotent, and none deletes participant data:

* `entry.expire_entry_access`;
* `entry.cleanup_offline_packages`;
* `entry.prune_verification_samples`;
* `dispatch_pending_communication_events`;
* **`communications.redeliver_overdue_deferred_messages`** (new, P44-F05);
* `core.clear_expired_sessions`;
* `core.purge_human_challenge_uses`.

The DEFERRED sweep re-attempts a message whose retry is more than 120 s overdue:

* at most 100 messages per run;
* at most one attempt per attempt number;
* within the three-attempt bound.

A message stuck in SENDING (a worker died during the provider call, or, since the staging Beta
correction, the SMTP exchange ended without the server's final answer and the attempt was recorded
as `UNCONFIRMED:<error>`) is **never** re-sent automatically, because the provider may already have
delivered it. Its policy is decision COMM-01.
Until then, operators see such messages in `ops/communications/`.

No retention or purge job for participant data is scheduled. The retention periods (OD-007) and
the legal facts (C-09) are not approved. `privacy.W002` reports this at
`manage.py check --database default`. Since P4-4-C1, `privacy.W003` and `privacy.W004` report an
assessment that could not run, and `manage.py release_readiness` gives the explicit result:

* `READY`, `BLOCKED` or `NOT_ASSESSED` per item (database schema, legal notices, retention,
  operational MFA) and overall; exit code 0, 1 or 2;
* **P4-4-C3:** the operational MFA item is replaced by `operational_sign_in`. It is READY under
  the revised MFA-01 requirement (email and password), and the report's `facts` state
  `operational_sign_in_mfa_enforced: false`, so MFA is never reported as implemented. Two items
  are added and assessed independently:
  * `sensitive_operation_step_up`: BLOCKED while no genuine step-up provider loads, because the
    emergency wipe is then unavailable;
  * `challenge_issuance_counter`: BLOCKED unless the shared Redis counter is configured and
    completed a counter write (**P4-4-C4**: never a `PING` alone; see ADR-0025, last section).

  Removing the sign-in blocker never makes the overall result READY by itself.
* legal and retention facts are `NOT_ASSESSED` when the schema is unavailable or has unapplied
  migrations, because stale rows can look resolved (observed on the development database, where
  the UX-3 data migration is unapplied);
* read-only, sanitized (counts and fixed sentences only), and never a legal or go-live approval.

`scripts/check.py all` prints it beside the local regression result, never inside it.

## 7. Readiness (`/readyz`)

* It reports `database`, `cache` and `storage` only as `reachable` or `unreachable`, with 503 when
  any fails.
* **P4-4-C3 (CACHE-01).** The `cache` field is removed: it was a round trip through the
  process-local `LocMemCache` and proved nothing about Redis. `challenge_counter` takes its place:
  * `shared`: a shared counter write completed within the retry interval (**P4-4-C4**; P4-4-C3
    used a `PING`, which Redis answers even while it refuses writes). Before any write, the
    first probe makes one capability write on `asc:abuse:v1:hc-issue:readiness-probe`
    (TTL 60 s), and while degraded it writes only when the retry schedule allows;
  * `local`: no shared counter is configured (local development and tests only);
  * `degraded`: the shared counter fails and the bounded fallback limits issuance;
  * `unavailable`: no counter can count, so issuance is refused. **P4-4-C4:** a working shared
    counter is `shared` even if the fallback failed earlier; the fallback's health counts again
    only when the shared counter fails.

  HTTP status:
  * 200 `ok` when the database and storage are reachable and the counter is `shared` or `local`;
  * 200 `degraded` when the counter is degraded and every other probe passes. A degraded node
    still serves bounded traffic, and failing every node on a shared Redis outage would take the
    whole service out of the load balancer;
  * 503 `unavailable` when the counter is unavailable, or when the database or storage fails,
    whatever the counter state.

  The Celery broker is a different Redis database; the counter's capability write never stands
  for it, and the broker is still not probed (below).
* It carries `Cache-Control: no-store`, and never includes an exception text, a host, a bucket or
  a key.
* The storage probe asks whether a key that never exists is present. For S3 this is one HEAD
  request per probe. Restrict `/readyz` to the load balancer's health checker at the proxy.
* **P4-4-C1 (R-02).** That P4-4 probe ignored the answer, so a missing local root was "reachable".
  The probe is now backend-aware (`apps.documents.storage.check_private_storage_ready`):
  * filesystem: the root exists and is a directory, the process has read, write and search
    permission as the operating system reports (on Windows this reflects only the read-only
    attribute, not ACLs), the root can be listed, and a lookup completes;
  * S3: `HeadBucket` (the endpoint answers, the credentials are accepted, the bucket exists), then
    a HEAD on the absent key; that is two requests per probe, and the runtime credentials need
    `s3:ListBucket` on the bucket;
  * a healthy absent object is never a failure.

  **Not proven:** that an upload, a read of an existing object or a delete succeeds (object
  permissions, free space, quotas, encryption). Nothing is written by the probe. Real S3 behaviour
  is proven only by deployment evidence; the regressions use botocore's offline `Stubber`.
* **Not probed (P4-5):**
  * the Celery broker and worker liveness;
  * the mail provider;
  * the alignment of the QR signing key with the published verification key.

## 8. Not proven locally

These are required before production, and none of them is claimed by any P4-4 test:

* a real MFA provider and enforcement at operational sign-in (P44-F06, MFA-01; see
  `p4_4_c1_mfa_options_comparison.md`). **P4-4-C3:** no longer required (MFA-01 revised,
  A-11); a genuine step-up provider for the emergency wipe is still absent (STEPUP-01);
* a shared, production-wide ALTCHA issuance counter and its degraded mode (CACHE-01).
  **P4-4-C3:** implemented; its behaviour against a real Redis (cross-process atomicity, expiry,
  outage, recovery) remains unproven until the staging verification;
* a Redis broker and cache, a Celery worker and beat;
* S3-compatible private storage, a mail provider, a malware scanner and the NIN provider;
* HSM or KMS custody of the signing keys;
* database-role isolation for the audit table (audit-isolation Fact B);
* the proxy CIDRs;
* HSTS on the real domain;
* managed devices, real browsers other than Chromium, and screen readers.
