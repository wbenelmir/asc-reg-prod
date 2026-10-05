# ADR-0007: PostgreSQL advisory-lock OTP throttling, rate-limit key rotation, and trusted-proxy client identity

| Field | Value |
| --- | --- |
| Status | Accepted; implemented in Prompt 3 |
| Date | 2026-09-19 |
| Related requirements | `AF-AUTH-01`, Schema §12.4, `API-006`; accepted plan §5.5 |

## Context

Counting rows inside an ordinary `READ COMMITTED` transaction is not
sufficient to enforce an OTP issuance/verification/lock rate limit:
concurrent first requests for the same recipient can each observe zero
prior rows and all proceed, exceeding the configured limit. The limiting
mechanism must also survive HMAC key rotation without resetting anyone's
window, and per-network throttling is worthless if a caller can set its own
apparent IP address.

## Decision

### Serialization: transaction-scoped PostgreSQL advisory locks

`pg_advisory_xact_lock(classid, objid)` is used because it works when no
row yet exists (the lock key derives from the identity, not from a row),
needs no PostgreSQL extension, and releases automatically at commit or
rollback so no lock can leak. Fixed `classid` namespaces: `1` recipient
issuance, `2` network issuance, `3` verification (row-level `SELECT ... FOR
UPDATE`, since the row exists by definition there), `4` recipient temporary
lock.

`objid` is derived from the first 4 bytes of a keyed HMAC fingerprint of
the normalized identity, interpreted as a signed 32-bit integer. A 32-bit
slice can collide; a collision only causes benign extra serialization
between two unrelated keys, never a missed limit -- this is an accepted,
documented trade-off, not an oversight.

**Deterministic lock ordering.** Every transaction that needs more than one
lock collects all required `(classid, objid)` pairs -- across every lock
class *and* every active key version -- sorts them ascending by `classid`
then `objid`, and acquires them in that order through one shared helper.
Row locks are taken after advisory locks, ordered by primary key.
Deadlock-freedom follows from this total ordering; no call site may invent
its own acquisition order.

### Rotation-safe rate-limit fingerprints

A rate limit derived from the *identity* blind-index key (ADR-0006) would
silently reset on every key rotation, since the same recipient would hash
differently and match no prior rows. This is fixed with a **dedicated**
`RATE_LIMIT_HMAC_KEY` family, entirely separate from the identity blind
indexes:

- An **ordered set of active versions** (`RATE_LIMIT_HMAC_ACTIVE_VERSIONS`)
  and a single **write version** (`RATE_LIMIT_HMAC_WRITE_VERSION`),
  configured in `config/settings/base.py` and validated in
  staging/production by `config/settings/validation.py`.
- Every limited operation derives one fingerprint per active version,
  locks and counts across **all** of them, so a recipient stays subject to
  one combined limit throughout a rotation's overlap window.
- A retired version is not dropped from `ACTIVE_VERSIONS` until at least
  `max(OTP lifetime, resend cooldown, temporary-lock duration, every
  rate-limit window)` has elapsed since it stopped being the write
  version. `RATE_LIMIT_MINIMUM_REQUIRED_OVERLAP_SECONDS` computes this
  automatically from the component settings, and
  `validate_deployment_configuration` refuses a shorter configured overlap
  in staging/production.
- Only versioned keyed fingerprints are ever stored, logged, or audited --
  never a plaintext email address, OTP value, or IP address.

### Trusted-proxy client-network identity

The client address used for per-network throttling is resolved by one
project-owned function, never by trusting an arbitrary header:

- IPv4/IPv6 values are normalized (canonical compressed IPv6, IPv4-mapped
  IPv6 folded to IPv4, zone identifiers and ports stripped) before hashing,
  so one host cannot present as several.
- **Local and test**: the direct peer (`REMOTE_ADDR`) is used;
  `X-Forwarded-For` and every other forwarded header are ignored entirely
  (`TRUSTED_PROXY_FORWARDING_ENABLED = False`).
- **Staging/production**: forwarded headers are honored only when the
  direct peer is inside a configured trusted-proxy CIDR set
  (`TRUSTED_PROXY_CIDRS`). Hop selection walks the forwarded chain from the
  right, accepting each entry only while it is itself a trusted proxy, and
  the first non-trusted entry is the client -- never the bare leftmost or
  rightmost value. An over-long or malformed chain falls back to the
  direct peer.
- If proxy forwarding is declared enabled without a trusted-proxy
  configuration, `validate_deployment_configuration` fails closed.
- The plaintext address is never logged, audited, or stored in an ordinary
  column -- only the versioned keyed `network_fingerprint`.

## Consequences

- No Redis is required for any of this; PostgreSQL is authoritative and
  strictly stronger (durable, transactional) than a cache-based rate
  limiter would be (see ADR-0009).
- Prompt 3's real multi-connection concurrency test suite
  (`tests/concurrency/`) must prove: correct limits under simultaneous
  first-request issuance with no pre-existing row; limits surviving key
  rotation; no deadlock under a mixed old/new-version workload; and correct
  behavior for spoofed/multi-hop/malformed `X-Forwarded-For` input.
- `config/settings/base.py` already declares the settings surface
  (`RATE_LIMIT_HMAC_*`, `TRUSTED_PROXY_*`, `OTP_*`) so that Prompt 2's
  static configuration validation has something concrete to check in
  staging/production, even though the OTP domain itself is Prompt 3 work.
