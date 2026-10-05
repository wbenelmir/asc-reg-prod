# ADR-0025: Shared challenge-issuance counter with a bounded local fallback

| Field | Value |
| --- | --- |
| Status | Accepted (owner decision CACHE-01, 2026-10-01; implemented in P4-4-C3; state evidence corrected in P4-4-C4) |
| Date | 2026-10-01 |
| Related requirements | TRD §5.1 and §6.1 (Redis for rate-limit state, private and encrypted); P4-4 finding P44-F09 and review finding R-05; ADR-0007, ADR-0009 |

## Context

The ALTCHA challenge endpoint (`accounts:human-check-challenge`) creates an anonymous session for
each new visitor. P4-4 limited it per client network before any session write. The counter lived in
Django's `LocMemCache`, which staging and production inherited. With *N* worker processes, a network
could therefore obtain *N* times the limit in each window, and a cache error allowed the request
(fail open). P4-4-C1 reclassified the finding as PARTIAL. On 2026-10-01 the owner approved the
direction of decision CACHE-01: a shared Redis counter, with a stricter, bounded per-process fallback
when Redis is unavailable, and sanitized, rate-limited degradation and recovery alerts.

This counter limits challenge **issuance** and anonymous session growth only. The OTP action keeps
its own exact, database-backed controls, which are unchanged: ALTCHA verification, session and action
binding, expiry, atomic one-time consumption, and the OTP limits per recipient and per network
(ADR-0007).

## Decision

`apps/core/issuance_counter.py`, used by `apps.core.human_check.challenge_issuance_allowed`.

**Store selection** (`HUMAN_CHECK_COUNTER_STORE`, a setting, not an environment variable):

* `local` in `base.py`, `local.py` and `test.py`: a per-process counter with the normal limit. Local
  development and tests need no Redis (ADR-0009 boundary).
* `redis` in `staging.py` and `production.py`. Static validation refuses any other value there, so
  process-local counting can never be the normal production path.

**Shared counter.** The database named by `REDIS_URL` (TRD §6.1: "cache, rate-limit state"). Each
request runs `INCR key` and `EXPIRE key window+5` inside one MULTI/EXEC transaction (`redis-py`
`pipeline(transaction=True)`). The count is atomic across processes and hosts, and a key can never
exist without an expiry.

* Django's `RedisCache` was not used. Its `incr` (Django 5.2.17, verified in the installed source)
  runs `EXISTS` and then `INCR` as two separate commands. If the key expires between them, `INCR`
  re-creates it with no expiry. Separate cache operations do not form an atomic, expiring limit.
* Key: `asc:abuse:v1:hc-issue:<digest>:<window index>`. The digest is a salted HMAC (from
  `SECRET_KEY`) of the IPv4 address or IPv6 /64, cut to 32 hex characters. The address is never
  stored. The namespace is explicit, and the HMAC separates deployments that share a server.
* Window: fixed, `floor(epoch / HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS)`. Every process and host derives
  the same index from the wall clock.
* Client: `socket_connect_timeout` and `socket_timeout` from `HUMAN_CHECK_COUNTER_*_TIMEOUT_MS`.
  Retries are disabled (`Retry(NoBackoff(), 0)`): a retried `INCR` could count one request twice, and
  every retry adds latency to a public request. No health-check pings. Pool and TLS options come
  from the URL; `rediss://` verifies the certificate and the host name by default.

**Isolation and connection security** (static validation, staging and production):

* `REDIS_URL` must be `rediss://`.
* It must not disable certificate or host-name verification (`ssl_cert_reqs`,
  `ssl_check_hostname` in the query).
* Its host, port and database must differ from `CELERY_BROKER_URL`, so counter keys never mix with
  broker queues.
* The counter has its own connection pool. No other project code uses that database today.

**Fallback.** Any failure of the shared counter counts as unavailability: a timeout, a refused
connection, a Redis error such as an out-of-memory refusal or a read-only replica, or a reply that
is not a positive integer. The request is then counted per process with
`HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW`, which validation requires to be at least 1 and lower
than the normal limit. It is never allowed merely because counting failed.

* The fallback (`LocalWindowCounter`) is lock-protected and keeps only the current window.
* It tracks at most `HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS` networks. When the table is full,
  every further network shares one overflow counter with the same limit. A flood of new addresses
  therefore cannot grow memory, cannot evict another network's count, and gains at most one extra
  fallback allowance.
* If even the fallback cannot count, issuance is refused (fail closed) and the state is
  `unavailable`.

**Retry and recovery.** After a failure, the shared counter is tried again by one request per
`HUMAN_CHECK_COUNTER_RETRY_SECONDS`; the other requests meanwhile use the fallback. As soon as one
attempt succeeds, the shared counter is used again. P4-4-C3 also let a readiness `PING` update this
state; **P4-4-C4 removed that** (review finding R-C3-01, see the last section): only a completed
counter write restores the shared state, and readiness probes follow the same retry schedule.

**Alerts.** The `asc2026.ops.alerts` logger, one structured line per event:

| Alert | Level | When |
| --- | --- | --- |
| `HUMAN_CHECK_COUNTER_DEGRADED` | ERROR | At the transition, then at most once per `HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS` while retries keep failing |
| `HUMAN_CHECK_COUNTER_RECOVERED` | WARNING | When a shared counter write (an issuance count or the readiness capability write) completes after a known failure; never after a `PING` (P4-4-C4) |
| `HUMAN_CHECK_COUNTER_UNAVAILABLE` | CRITICAL | When no counter can count |

* The fields are fixed: `failure` (one of `timeout`, `connection`, `rejected`, `unexpected-reply`,
  `configuration`, `error`), `transition`, `degraded_seconds`, `fallback_decisions`,
  `fallback_refusals`, `fallback_limit`, `window_seconds`, and `suppressed_since_last`.
* No alert carries an address, a digest, a URL, a host, a credential or exception text.
* Alerts are rate-limited per alert code, and suppressed ones are counted. During rapid flapping, an
  alert can therefore trail the live state by up to one interval; `/readyz` shows the live state.

**Readiness** (`/readyz`): `challenge_counter` is `shared`, `local`, `degraded` or `unavailable`.
Since P4-4-C4, `shared` means that a shared counter write completed within the last
`HUMAN_CHECK_COUNTER_RETRY_SECONDS` (or was just made by the probe); see the last section.

* `degraded` gives HTTP 200 with `status: degraded`. A degraded node still serves bounded traffic,
  and failing every node at once on a shared Redis outage would take the service down.
* `unavailable` gives 503.
* A failed database or storage probe gives 503 with `status: unavailable` whatever the counter state,
  so the fallback never hides another failure.
* The former `cache` field, a round trip through the process-local `LocMemCache`, was removed. It
  proved nothing about Redis.

## Bounds

With *N* web processes alive in a window (a restarted process counts as a new one, because the
fallback state is in memory):

| State | Admitted per network and window | Notes |
| --- | --- | --- |
| Healthy | ≤ `MAX_PER_WINDOW` (60) | Across all processes and hosts |
| Degraded for the whole window | ≤ *N* × `FALLBACK_MAX_PER_WINDOW` (20 *N*) | At or below the normal limit only while *N* ≤ 3; set the fallback limit from the real process count |
| Window that overlaps an outage | ≤ 60 + 20 *N* | Requests admitted by the fallback are not written to Redis afterwards |
| Process restarts during an outage | + 20 per restarted process | A new process starts with an empty fallback |
| Fallback table full | ≤ 20 per process for all overflowing networks together | Never a reset of a tracked network |

Fallback memory per process is about `MAX_NETWORKS` entries of a 32-character key and an integer:
roughly 2 MB at 10,000.

Redis memory is one small key per active network and window. A key lives at most two windows plus
5 s, because each hit renews its expiry.

The Redis database must use `maxmemory-policy noeviction`. With an evicting policy, Redis could
silently drop counter keys and reset counts. With `noeviction`, a full Redis refuses the write, the
fallback applies, and the degradation alert fires.

## Provisional engineering defaults (not owner-approved capacity figures)

| Setting | Default |
| --- | --- |
| `HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW` (existing) | 60 |
| `HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS` (existing) | 600 |
| `HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW` | 20 |
| `HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS` | 10,000 |
| `HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS` | 500 |
| `HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS` | 500 |
| `HUMAN_CHECK_COUNTER_RETRY_SECONDS` | 15 |
| `HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS` | 300 |

Assumptions behind these defaults:

* A normal visitor needs one challenge per page load plus one per 15-minute expiry.
* A shared network address (a campus NAT, a mobile carrier) may carry many visitors.
* The fallback limit of 20 is one third of the normal limit, so up to three processes stay at or
  below the healthy bound.
* A request waits at most about one connect timeout plus one socket timeout (about 1 s) when Redis
  hangs, and only one request per process does so per retry interval.

All of these are reviewable, environment-configurable and bounded by validation.

## Consequences

* Redis is now on the public request path of challenge issuance in staging and production. Its
  failure degrades issuance to the bounded fallback; it does not stop registration.
* `/readyz` changes its body: `cache` is replaced by `challenge_counter`.
* No new dependency: `redis` 8.1.0 is already locked.
* Not proven locally: no Redis server is available in the development environment.
  * The decision logic is tested with an in-process stand-in. The adapter's command sequence is
    tested with a recording pipeline, and a real `redis-py` client is tested against a closed
    loopback port.
  * Cross-process atomicity, server-side expiry, outage and recovery against a real server are
    covered by the opt-in `apps/core/tests/test_p4_4_c3_redis_integration.py`, which is skipped
    without an isolated Redis, and by the manual staging guide
    (`docs/testing/phase_04_p4_4_c3_staging_guide.md`).

## P4-4-C4 correction: counter-state evidence (2026-10-01)

The independent review of P4-4-C3 reproduced two defects. P4-4-C4 corrects both.

**R-C3-01: PING is not evidence of counting.**

In P4-4-C3, `probe()` treated a successful `PING` as recovery. Redis answers `PING` while refusing
`INCR` or `EXPIRE`, for example under an ACL, on a read-only replica, or when out of memory. Readiness
could therefore report `shared`, and `release_readiness` READY, while every count still fell back.
Each probe also cleared the retry backoff and emitted a recovery alert.

* **Evidence.** Only a completed counter write proves the shared counter. That write is either an
  issuance count, or the capability write below. The adapter has no `ping()` any more.
* **Startup.** Before any write, a configured shared counter is `unverified` (`state()`), never
  `shared`. The first `/readyz` or `release_readiness` probe resolves it with one capability write:
  `shared` if the write completes, `degraded` if it fails. `/readyz` and `release_readiness`
  therefore never report an untested write path as usable.
* **Schedule.**
  * While degraded, the probe writes only when a shared attempt is due under
    `HUMAN_CHECK_COUNTER_RETRY_SECONDS`, and it uses the same slot as requests. Probing can neither
    shorten the backoff nor add write attempts.
  * While healthy, a write completed within the last retry interval is reused, so a probe does not
    write on every call.
  * The reported `shared` is therefore at most one retry interval old.
* **The capability write.**
  * Key: `asc:abuse:v1:hc-issue:readiness-probe`. It is one key for all processes, under the
    issuance namespace, so an ACL limited to `asc:abuse:v1:hc-issue:*` covers both the probe and
    issuance.
  * Operation: the same atomic MULTI/EXEC of `INCR` and `EXPIRE` as an issuance count, with a TTL of
    `PROBE_TTL_SECONDS` = 60 s. The permissions are therefore exactly those of issuance; `PING` is not
    needed.
  * Concurrency: `INCR` is atomic, and the value is never compared with a limit, so concurrent probes
    from many processes are harmless.
  * Cleanup: none. The key expires 60 s after the last probe.
  * Separation from user budgets: an issuance key is `<namespace>:<32-hex digest>:<window index>`.
    The probe key has no window suffix and a non-hex label, so it can never be one. No issuance
    decision reads it.
  * Limitation: a misconfigured ACL that allowed only the probe key would pass the probe, and the
    next issuance count would then fail and degrade normally.
* **Alerts.** `HUMAN_CHECK_COUNTER_RECOVERED` follows only a completed write after a known failure.

**R-C3-02: a stale unavailable state after shared recovery.**

In P4-4-C3, `state()` returned `unavailable` whenever the fallback had failed, even after shared
counts succeeded again. A node could then count and admit requests while `/readyz` answered 503.

* A working shared counter is decisive: `shared` whatever happened to the fallback earlier.
* The fallback's own health is kept separately (`fallback_healthy()`). Only the fallback's own
  successful count clears it; a Redis recovery never does.
* That health matters again as soon as the shared counter fails. A later shared failure with a
  still-broken fallback refuses issuance and reports `unavailable` (503).
* A working counter that refuses a request because the network reached its limit stays `shared`.
  Reaching the limit is not a dependency failure.

**Unchanged:** the store selection, the atomic count, the bounds, the fallback and its limits, the
HTTP status rules (200 `degraded`, 503 `unavailable`), the alerts' fields, the human-check recovery
switch, and the OTP-action protections.

**Evidence:** `apps/core/tests/test_p4_4_c4_counter_state.py` with in-process stand-ins, not Redis.
The opt-in `test_the_capability_write_proves_counting_on_the_server` was added to the real-Redis
module and is skipped without `ASC_TEST_REDIS_URL`, so real behaviour stays NOT_PROVEN.
