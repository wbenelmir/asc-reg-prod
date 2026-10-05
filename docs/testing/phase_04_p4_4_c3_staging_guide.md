# P4-4-C3 manual staging guide: configuration and verification

Package: P4-4-C3, corrected by P4-4-C4 (counter-state evidence, review findings R-C3-01 and
R-C3-02). Date: 2026-10-01. Language: English. Audience: the owner, who deploys manually.

**What this guide is.** The configuration and the checks for what P4-4-C3 changed:

* password-only operational sign-in (owner decision MFA-01 revised, amendment A-11);
* the unchanged participant email OTP;
* the MFA step-up that sensitive operations still require;
* the shared ALTCHA challenge-issuance counter with its bounded fallback (CACHE-01, ADR-0025);
* the new `/readyz` states.

**What it is not.** It is not a deployment approval, a go-live checklist, a complete all-role
acceptance guide, or a security certification. A staging run that passes every step below proves only
those steps. §9 lists what remains before staging acceptance and before production release.

**Where this fits.** Deploy with `docs/deployment/README.md` first; the live checks that use this
guide are listed in `docs/testing/phase_04_staging_uat_handoff.md`, which cross-references §1,
§3, §4, §6, §7 and §8 here instead of repeating them.

**Ground rules.**

* Use synthetic data and synthetic accounts only.
* Use an isolated or explicitly approved Redis database. Never a production service, and never a
  shared service you are not authorized to change.
* Never paste a real URL that contains a credential, a password, a key or a token into a ticket, a
  chat or this document.

## 1. Environment variables

The values below are placeholders. Set real values only in the deployment's secret store.
Staging and production refuse to start, at settings import, when any static rule in
`config/settings/validation.py` fails. The error names the variable, never its value.

**Changed or new in P4-4-C3:**

| Variable | Example placeholder | Rule |
| --- | --- | --- |
| `REDIS_URL` | `rediss://__COUNTER_REDIS_HOST__:6379/2` | **Now used** (the challenge counter). Must be `rediss://`; must not set `ssl_cert_reqs=none` or `ssl_check_hostname=false`; its host, port and database must differ from `CELERY_BROKER_URL`. If the server needs credentials, put them in the URL's user-information part through the secret store only. |
| `CELERY_BROKER_URL` | `rediss://__BROKER_REDIS_HOST__:6379/1` | Unchanged. It must name a different database from `REDIS_URL`. |
| `MFA_BACKEND` | (unset) or `__APPROVED_STEP_UP_PROVIDER_CLASS__` | **Now optional.** Only the step-up provider for sensitive operations. Unset means the emergency wipe is unavailable (§4). Never point it at a test double or a stub. |
| `HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW` | `60` (default) | Normal limit per network per window, across all processes; at least 2. |
| `HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS` | `600` (default) | Fixed window. |
| `HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW` | `20` (default) | Per-process limit during a Redis failure. Must be at least 1 and lower than the normal limit. Choose it so that web processes × fallback ≤ normal limit, if you want degradation never to exceed the healthy bound. |
| `HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS` | `10000` (default) | Fallback memory bound per process (1 to 1,000,000). |
| `HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS`, `HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS` | `500` (defaults) | 1 to 5000. |
| `HUMAN_CHECK_COUNTER_RETRY_SECONDS` | `15` (default) | How often one request per process retries Redis while degraded (1 to 3600). |
| `HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS` | `300` (default) | Minimum gap between alerts of one kind (1 to 86400). |

`HUMAN_CHECK_COUNTER_STORE` is not an environment variable. `staging.py` and `production.py` set
it to `redis`; local development keeps `local`.

**Unchanged and still required** (names only; see `validation.py`):

* `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_PUBLIC_BASE_URL`;
* the runtime database identity (the migration-owner password only in the migration process);
* `CELERY_BROKER_URL`;
* the clamd scanner (`CLAMD_SOCKET_PATH` or `CLAMD_HOST`) and SMTP (`EMAIL_HOST`, `DEFAULT_FROM_EMAIL`, ...);
* the `S3_STORAGE_*` values;
* the rate-limit, identity-encryption and blind-index key families;
* trusted-proxy settings when forwarding is enabled;
* the HSTS rollout values (HSTS-01).

## 2. Redis setup for the counter

1. **Use a dedicated logical database, or a dedicated instance,** for the counter. It must be
   separate from the Celery broker; validation refuses the same host, port and database.
2. **TLS** with certificate and host-name verification (the `rediss://` default). The CA must be
   trusted by the web hosts.
3. **Eviction policy `noeviction`** for the counter database's instance. With an evicting policy,
   Redis can silently drop counter keys and reset counts. With `noeviction`, a full Redis refuses
   writes, the bounded fallback applies, and the degradation alert fires. The application cannot
   check this on managed services where `CONFIG` is disabled; record the provider setting instead.
4. **Optional ACL** (Redis 6 or later): a user limited to the keys `asc:abuse:v1:hc-issue:*`, with
   the commands `incr`, `expire`, `multi` and `exec`. Since P4-4-C4 the application never sends
   `PING`, because an answer to `PING` does not show that writes are accepted.
5. **Network.** Every web node must reach the counter Redis. Do not expose it publicly.
6. **Namespace.**
   * The key form is `asc:abuse:v1:hc-issue:<32-hex keyed digest>:<window index>`.
   * Each key expires within two windows plus 5 s, about 20 minutes with the defaults.
   * Keys are HMAC digests (from `DJANGO_SECRET_KEY`), never addresses.
   * **Readiness capability key (P4-4-C4):** `asc:abuse:v1:hc-issue:readiness-probe`. It is one
     key for all processes, written with the same `INCR` + `EXPIRE` as a count, with a TTL of 60 s.
     It is never compared with a limit and never used as a network budget, and it expires by
     itself.

Read-only inspection, with the URL taken from the environment so that it is never typed or shown.
`PING` here shows connectivity only, not that counting works:

```bash
redis-cli -u "$REDIS_URL" --tls PING
```

```bash
redis-cli -u "$REDIS_URL" --tls CONFIG GET maxmemory-policy
```

```bash
redis-cli -u "$REDIS_URL" --tls --scan --pattern 'asc:abuse:v1:hc-issue:*' --count 100
```

## 3. Startup and static checks (no live service contacted)

```bash
python manage.py check --deploy --settings=config.settings.staging
```

Expected:

* no error;
* without `MFA_BACKEND`, one warning `entry.W001`, which says the emergency device wipe is
  unavailable.

```bash
python manage.py release_readiness --settings=config.settings.staging
```

Expected: an exit code and an overall result that follow the real facts.

* `operational_sign_in` is READY, and the fact `operational_sign_in_mfa_enforced: False` is
  printed. This is the revised requirement, not an MFA implementation.
* `sensitive_operation_step_up` is BLOCKED without a genuine provider.
* `challenge_issuance_counter` is READY only if a shared counter write completed (P4-4-C4: the
  command makes one capability write if no recent write exists; a `PING` is never enough).
* The legal, retention and schema items show their own state.
* The overall result is BLOCKED while any item is.

Record the output. Do not treat READY as a release approval.

## 4. Operational sign-in (password only) and sensitive operations

**Accounts needed.** At least one synthetic account per kind:

* an administrator;
* a temporary operator with `active_until` set;
* an external security account;
* a Security Restriction Manager for the wipe check.

Every account needs a synthetic email address under the team's control.

Create them with `python manage.py provision_staging_uat` and set each password with
`python manage.py changepassword <email>` (deployment guide §9,
`docs/deployment/README.md`).

| Check | Expected |
| --- | --- |
| Sign in at `/accounts/ops/sign-in/` with email and password | A redirect to the operational area; no MFA prompt; audit `ACC_OPERATIONAL_SIGN_IN_SUCCEEDED` |
| Wrong password | "Incorrect email or password."; audit `ACC_OPERATIONAL_SIGN_IN_FAILED` |
| More than 10 wrong passwords for one email within 15 min | HTTP 429 with the generic message, also for a later correct password; audit `ACC_OPERATIONAL_SIGN_IN_THROTTLED` |
| A suspended account, or an account past `active_until` | No session, with the same message as a wrong password |
| A page outside the account's permission or scope | Refused (403 or 404), as before |
| Sign-out (POST) | The session ends; a participant session in the same browser is kept |
| 15 min inactive, 8 h absolute | Sign-in again is required |
| Emergency wipe without `MFA_BACKEND` (`/ops/entry/devices/<id>/emergency-wipe/`) | "MFA step-up is not available"; no wipe order; audit `ENT_DEVICE_EMERGENCY_WIPE_REFUSED` with reason `MFA_UNAVAILABLE` |

## 5. Participant email OTP (unchanged)

This needs the staging SMTP configuration (`EMAIL_*`, `DEFAULT_FROM_EMAIL`; deployment guide §4),
a sending domain with its authentication records, and a synthetic test mailbox.

| Check | Expected |
| --- | --- |
| Open `/accounts/start/` | The form with the ALTCHA check; the challenge request is `200`, `private, no-store` |
| Submit a synthetic address | The same neutral message for every address; one code by email |
| Enter the code | Signed in as a participant; a wrong code is refused with bounded attempts |
| Resubmit the same solved check (browser back and resend) | Refused; the email address is kept |

## 6. `/readyz` expectations

```bash
curl -s -i https://__STAGING_HOST__/readyz
```

| State | HTTP | Body |
| --- | --- | --- |
| Healthy | 200 | `{"status": "ok", "database": "reachable", "storage": "reachable", "challenge_counter": "shared"}` |
| Counter Redis unavailable, fallback working | 200 | `"status": "degraded"`, `"challenge_counter": "degraded"` |
| No counter can count | 503 | `"status": "unavailable"`, `"challenge_counter": "unavailable"` |
| Database or storage failing (any counter state) | 503 | `"status": "unavailable"`, the failing item `unreachable` |

* Every response is `Cache-Control: no-store`.
* No response contains a host, a URL, a credential, a bucket or an error text.
* `local` must never appear in staging.
* `shared` means a counter write completed within the last 15 s, or was just made by this probe
  (P4-4-C4). The internal `unverified` start state is resolved by the first probe and never
  appears in the response.
* The broker is not probed by `/readyz`, and the counter's capability write does not stand for
  it.
* Restrict `/readyz` to the load balancer's health checker.

## 7. Counter verification: shared count, outage and recovery

Do these checks only against the isolated or approved counter Redis database of the staging
environment, with at least **two web processes** running. Use one dedicated synthetic client
address, because that address is throttled for the rest of the window.

1. **Shared count across processes.**
   * Within one 10-minute window, send 61 challenge requests from the test address:

     ```bash
     for i in $(seq 61); do curl -s -o /dev/null -w '%{http_code}\n' https://__STAGING_HOST__/accounts/start/human-check/; done | sort | uniq -c
     ```

   * Expected: 60 × `200` and 1 × `429`, wherever the load balancer sent each request. Per-process
     counting would allow more.
   * The `--scan` command in §2 shows one key for the test digest.
2. **Opt-in automated evidence (optional).** The integration module needs a host with the project's
   development tools and an isolated, disposable Redis database:

   ```bash
   ASC_TEST_REDIS_URL="$ISOLATED_TEST_REDIS_URL" uv run --env-file .env pytest apps/core/tests/test_p4_4_c3_redis_integration.py -rs
   ```

   It covers concurrent initialization, 4 processes sharing one exact count, server-side expiry and
   window boundaries, and an outage and recovery through a local forwarder. The outage test needs a
   plain `redis://` test URL; with `rediss://` it is skipped. It writes only under a random
   namespace and deletes only those keys.
3. **Outage.**
   * On the approved test service only, cut the web nodes' path to the counter Redis: a temporary
     firewall or security-group rule, or stopping a dedicated test instance. Never a broker or a
     shared instance.
   * `/readyz`: 200 `degraded`.
   * Logs: one `HUMAN_CHECK_COUNTER_DEGRADED` (level ERROR, logger `asc2026.ops.alerts`) per web
     process, then at most one per 5 minutes per process. Each carries `failure`, `fallback_limit`
     and `window_seconds`, and no address, URL or error text.
   * Challenge requests from a fresh test address: each process allows at most 20 per window, then
     answers `429`; no session row is created by a refused request.
   * Requests wait at most about 1 s for one request per process per 15 s.
4. **Recovery.**
   * Remove the rule or restart the instance.
   * At the next scheduled shared attempt, at most about 15 s later (the retry interval). That
     attempt is a request or a `/readyz` probe, whichever comes first; probes do not make it
     sooner (P4-4-C4):
     * `/readyz` returns to 200 `ok` with `shared`;
     * one `HUMAN_CHECK_COUNTER_RECOVERED` (level WARNING) per process, with `degraded_seconds` and
       `fallback_decisions`.
   * Requests admitted during the outage are not added to Redis afterwards. In that window, the
     test address can therefore get up to 60 + 20 × processes; this is the documented bound.
5. **Writes refused while `PING` works (P4-4-C4, R-C3-01).**
   * On the approved test service only, make the counter user refuse writes while it stays
     reachable, for example by temporarily removing `+incr` from its ACL. Never do this on a
     shared or production user.
   * Expected, within one retry interval:
     * `/readyz` is 200 `degraded`, never `ok`/`shared`, however often it is polled;
     * `HUMAN_CHECK_COUNTER_DEGRADED` alerts appear with `failure: rejected`;
     * no `HUMAN_CHECK_COUNTER_RECOVERED` alert appears;
     * `release_readiness` reports `challenge_issuance_counter` BLOCKED.
   * Restore the ACL. The next scheduled write, at most about 15 s later, gives `shared` and
     exactly one `RECOVERED` alert per process.
6. **Unavailable.** The `unavailable` state needs the in-process fallback itself to fail. It is not
   reproducible from outside without fault injection. It is covered by the automated tests,
   including R-C3-02 (recovery after both counters failed, then a renewed shared failure). Do not
   try to force it in staging.

## 8. Cleanup (synthetic artifacts only)

* Remove the temporary firewall or security-group rule, and restore any ACL you changed; confirm
  `/readyz` shows `shared`.
* The readiness capability key expires 60 s after the last probe; nothing to delete.
* Counter keys expire by themselves within about 20 minutes. Do not run `FLUSHDB` or `FLUSHALL`.
  If an early removal is needed, delete only the keys of the synthetic test digest, never the whole
  namespace on a shared instance.
* Deactivate or remove the synthetic staff accounts and the synthetic participant records created
  for this guide, by the approved procedure.
* Audit events are append-only and stay.
* Delete the test emails from the synthetic mailbox.

## 9. Remaining prerequisites

**Before staging acceptance:**

* Provision the counter Redis (REDIS-01): TLS, a separate database, `noeviction`, and alert routing
  for `asc2026.ops.alerts` to someone on call.
* Run §7 and record the results. Until then, real shared-counter concurrency, outage and recovery
  are **NOT_PROVEN**.
* Real email delivery for the participant OTP: the SMTP service, a sending domain and its
  authentication records, and a synthetic mailbox (UAT handoff INT-03).
* The UAT accounts (`provision_staging_uat`) and the role checks of the UAT handoff.
* Decide STEPUP-01: a genuine step-up provider, or acceptance that the emergency wipe is
  unavailable.
* Confirm or replace the provisional counter figures (CACHE-01 follow-up, CACHE-02) against the real
  process count.

**Before production release** (from `release_readiness` and the P4-4-C1 owner brief; not changed
by this package):

* approved legal facts (C-09) and retention periods with a purge job (OD-007);
* the country catalog (C-01);
* hosting, keys and backups (OD-006);
* capacity figures (OD-001, INFRA-001, OD-008);
* database-role isolation for the audit table;
* proxies, HSTS (HSTS-01), restore drill and rehearsals;
* COMM-01;
* the step-up decision above;
* the deployment organization's own go/no-go decision.
