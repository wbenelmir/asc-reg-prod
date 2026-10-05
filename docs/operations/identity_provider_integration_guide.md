# Identity verification: owner-run integration and configuration guide

Date: 2026-10-01. Language: English. For the owner and the deployment team. **Placeholders only**:
never paste a real credential, token, NIN, name or response into this file, a ticket or a review
package.

The code is complete and tested with synthetic stand-ins. This guide covers what only the owner can
do: supply the missing provider facts, configure the real adapter, prove it with an authorized
method, set up real participant email, and run and recover the worker. Four states must be kept
separate:

| State | Today |
| --- | --- |
| Code completion | Done (IDV-1 to IDV-4, synthetic tests) |
| Configuration readiness | **Not done**: no environment has the adapter configured; `release_readiness` reports `identity_verification_provider` BLOCKED |
| Real-integration proof | **NOT_PROVEN**: no call to the ministry service has ever been made |
| Staging acceptance / production release | Not started / not authorized |

## 1. The one input still needed from the provider (API-01)

The adapter cannot read a token until its location in the authentication response is known. Supply,
**with every secret value replaced by `__REDACTED__`**:

1. one sanitized authentication **success** response body (`POST /api/auth/`), showing where the
   token is (for example `{"token": "__REDACTED__"}` or `{"data": {"access": "__REDACTED__"}}`);
2. whether that body states a lifetime, and in which form (seconds from now, or an absolute Unix
   time), and under which key;
3. how a token is renewed (a new `POST /api/auth/`, or a refresh call), and how an expired token is
   reported on a lookup (status code and body);
4. the status codes and bodies of a rejected authentication and of a forbidden lookup.

Also still unknown (not needed to start, needed to operate): the network path (allowlisted egress
address, VPN), the certificate chain (whether a private CA must be configured), availability
windows, and any other date format the service may return (API-02).

## 2. Configuration (staging first)

Set these as secret environment values on the web **and** worker processes:

```text
NIN_PROVIDER_BACKEND=apps.people.nin_provider.MinistryNinProvider
MINISTRY_NIN_API_BASE_URL=https://<ministry-origin>
MINISTRY_NIN_API_USERNAME=<secret>
MINISTRY_NIN_API_PASSWORD=<secret>
MINISTRY_NIN_API_AUTH_TOKEN_PATH=<dotted.path.from.step.1>
# Only if step 1 shows a lifetime:
MINISTRY_NIN_API_AUTH_EXPIRY_PATH=<dotted.path>
MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT=relative_seconds   # or unix_epoch_seconds
# Optional, with their defaults:
MINISTRY_NIN_API_TOKEN_CACHE_SECONDS=300
MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS=5
MINISTRY_NIN_API_READ_TIMEOUT_SECONDS=10
MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS=20
MINISTRY_NIN_API_MAX_RESPONSE_BYTES=65536
MINISTRY_NIN_API_CA_BUNDLE=<path to a PEM file, only for a private CA>
IDENTITY_PROVIDER_MAX_CONCURRENCY=4
```

The observed paths (`/api/auth/`, `/api/get/{nin}`) are the defaults
(`MINISTRY_NIN_API_AUTH_PATH`, `MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE`). Leave
`IDENTITY_ALLOW_SIMULATED_PROVIDER` unset: staging and production refuse to start with it on, and
refuse every stub or simulation backend.

Checks after a restart:

```text
python manage.py check --deploy          # no people.W001 warning any more
python manage.py release_readiness       # identity_verification_provider: READY
```

A malformed value (an `http://` origin, a path in the origin, a bad dotted path, an out-of-range
timeout, a missing CA file) stops startup with the variable name, never its value. A missing value
only keeps the adapter unavailable: every Algerian case then goes to manual review
(`PROVIDER_NOT_CONFIGURED`), which is safe.

## 3. Authorized real-provider test (UAT-02, owner-run)

Only with the owner's written authorization of the method and of the identities used, in staging:

1. Register through the public wizard with an authorized identity (the person's informed consent;
   never a record copied from elsewhere). Submit.
2. Within a minute the case leaves "Verification pending". Open it in **Identity review**.
3. Expected outcomes to record (status and reason only, never the data): an exact match
   (`Verified by the ministry service`, source "Ministry service"); a deliberately mistyped NIN
   (`NOT_FOUND`); a presumed-date identity if one is available (date comparison "Skipped
   (presumed date)").
4. Record the evidence as: date, environment, outcome codes, the `check --deploy` and
   `release_readiness` output, and the reviewer's name. Do not capture screenshots that show
   identity data.

Until this is done, real-provider status stays NOT_PROVEN in every report.

## 4. Logs, proxies, tracing and APM

The application never logs a lookup URL, a header, a body or an exception message from the
adapter, and its log filter masks `/api/get/<value>`, any 18-digit run, bearer values and the
configured ministry credentials. Infrastructure must not undo this:

* an egress proxy must not log request lines for the ministry origin, or must mask the path after
  `/api/get/`;
* APM or tracing agents must not capture outbound URLs or headers for `http.client` (disable URL
  capture, or exclude the ministry origin);
* never enable `http.client` debug output (`HTTPConnection.debuglevel`) in any environment;
* error trackers must use the project's log formatter, or scrub the same patterns.

**Operational routes (`/ops/identity/`, IDV-C1, R-IDV-05).** The staff queue search is a
CSRF-protected POST to `/ops/identity/search/`.

* The application never puts a search text or a NIN in a URL. Links, tabs, pagination, the review
  screen and the decision redirects carry only an opaque `ctx` reference. A `?q=` address is
  redirected without being used.
* The search text exists only in that POST body. Never log request bodies for `/ops/identity/`
  (reverse proxy, WAF, APM request capture, error-tracker "request data"); most tools capture
  bodies only when told to, so keep that off.
* Query strings on `/ops/identity/` hold only filter codes, ids and `ctx`. Logging them is
  allowed, but omitting query strings for these routes is the safer default.
* Case pages show the full NIN to reviewers who may view evidence. Session-replay and DOM-capture
  tools (RUM, "session recording") must be disabled for `/ops/identity/`, and screenshots must
  follow the evidence rules (§3).
* Search contexts live in the server-side session store (database sessions). They hold case ids
  only, never the text. They expire after 30 minutes and are dropped at sign-out with the
  session.

**Timeouts.** `MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS` is an absolute bound for one request,
including name resolution, connect, TLS, headers and body. It must be at least the connect and
the read timeout (startup validation). A request that runs into it is retried like an outage.
Name resolution that hangs is abandoned on a daemon thread; if that happens often, fix the
resolver path (DNS reachability from the worker hosts).

## 5. Worker, scheduler and recovery

* Run at least one Celery **worker** and the Celery **beat** scheduler with the same settings as the
  web process. Beat runs `people.dispatch_due_identity_verification_jobs` every 60 s.
* A broker outage never loses work: the submission and its job are saved together; the job stays
  `PENDING` and is re-sent when the broker returns.
* Recovery without the broker: `python manage.py run_identity_verification_jobs --limit 50`
  (repeat until it reports 0). It is safe next to running workers: every job is claimed with a lease.
* A worker that dies mid-call releases its claim after `IDENTITY_VERIFICATION_LEASE_SECONDS`
  (180 s); the sweeper then retries it. Every claim counts as an attempt (IDV-C1, R-IDV-04).
  * Repeated deaths use up the same budget as handled failures: five attempts with the default
    delays.
  * After that the job is `EXHAUSTED`, the provider is not called again, and the case goes to
    manual review (`PROVIDER_ATTEMPTS_EXHAUSTED`), with audit event
    `IDV_PROVIDER_ATTEMPTS_EXHAUSTED`.
  * A busy provider slot is not an attempt.
* A withdrawal, an operational cancellation or a NOT_APPROVED decision discards the case's
  pending jobs (IDV-C1, R-IDV-06).
* Transient failures retry after 60 s, 5 min, 15 min and 1 h, then the case goes to manual review
  (`PROVIDER_UNAVAILABLE` / `PROVIDER_AUTH_ERROR`). After fixing an outage or the configuration,
  reviewers use **Correct NIN and recheck** with the same NIN to queue a new check.
* Worth watching (database counts, no personal data): jobs `PENDING` with `next_attempt_at` older
  than 10 minutes; jobs `EXHAUSTED` today (and how many have `last_outcome =
  ATTEMPTS_EXHAUSTED`, which means worker deaths, not service answers); cases in `MANUAL_REVIEW` by
  reason `PROVIDER_*`.

## 6. Real participant email (OTP) delivery

Participants still sign in with an email code. The project sends every email through Django's
SMTP backend, configured from `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`,
`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`, `EMAIL_TIMEOUT_SECONDS` and `DEFAULT_FROM_EMAIL`
(`docs/deployment/README.md` §4); staging and production refuse any other backend. Then:

1. configure the sending domain's SPF, DKIM and DMARC records with the provider;
2. request a code for a synthetic mailbox under the team's control; check delivery time, the
   sender, the language, and that the code works once;
3. record the result separately from synthetic tests (UAT-01, AS-15).

## 7. Rollback

* Stop using the ministry service at once: set
  `NIN_PROVIDER_BACKEND=apps.people.nin_provider.DisabledNinProvider` and restart; new and
  retried checks go to manual review. Nothing else changes.
* Schema rollback (destroys the identity history recorded since the migration; only with explicit
  approval), with the migration settings (`config.settings.staging_migration`):
  `python manage.py migrate people 0002` then `python manage.py migrate documents 0001`.
  `communications.0007` refuses to reverse once any `IDENTITY_REJECTION` message exists, and a
  message already sent cannot be recalled. Prefer rolling forward or restoring a backup.
