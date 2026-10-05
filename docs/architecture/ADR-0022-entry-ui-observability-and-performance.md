# ADR-0022: Entry UI shell, observability, abuse limits, and online performance

| Field | Value |
| --- | --- |
| Status | Proposed (Phase 3 Prompt 5 implementation; awaiting local review) |
| Date | 2026-09-23 |
| Related requirements | TRD §7.1–7.3 (FE-001…006), §12, §22 NFR-PERF-003/004, §23.1–23.2, API-006; UI/UX §4.5–4.6, §9.4–9.7, §11, §12, §13, §14; PRD NFR-OBS-006; ADR-0007, ADR-0008, ADR-0021 |

## Context

Prompt 5 completes the Phase 3 user experience (entry, participant pass,
badge operations) in English, French and Arabic, and adds observability,
abuse limits, degraded-online messaging and a provisional performance
harness for online verification -- without any offline verification. The
developer made UI/UX quality a first-class acceptance requirement, named
Finder as the primary visual reference, the official logo and the Thmanyah
Arabic font, and required an independently reviewed design checkpoint before the
pattern was propagated.

## Decisions

### 1. A scoped, project-owned design system on the existing Bootstrap

`static/css/asc-ui.css` holds every token and component, scoped under
`body.asc-ui`, on top of the already vendored Bootstrap 5.3.8. Finder is a
pattern reference only (tokens and structures re-expressed; nothing
copied -- `docs/design/finder_inventory.md`). Three shells in
`templates/layouts/` (`base_entry`, `base_operations`, `base_workspace`, the
TRD §7.2 names) extend `base.html` through new blocks whose defaults are the
previous markup, so screens outside Prompt 5 are untouched and can migrate
later one by one. No new dependency, build step, or client framework.

### 2. Arabic font self-hosted under a developer-held extended licence

Thmanyah Sans (three WOFF2 weights plus its licence) is vendored in
`static/vendor/thmanyah/` with a checksummed `PROVENANCE.md`, verified by
`scripts/check.py assets`, and applied to Arabic only. The bundled licence
restricts web embedding; the developer confirmed an extended licence.

### 3. Truthful connection state through a passive status check

`GET /entry/status/` re-validates the whole checkpoint chain with
`resolve_checkpoint(touch=False)` and returns only a state word (`online`,
`degraded`, `session-ended`). The path is listed in
`OPERATIONAL_PASSIVE_PATHS`, which the operational session middleware
validates but does not count as activity -- so an unattended page can never
keep an operator or checkpoint session alive (tested). A system check
(`entry.E004`) fails if the path is removed from that setting. Offline
verification is explicitly *not enabled*: the offline state blocks lookups
and tells the operator to follow the continuity procedure (a readiness
placeholder, not Plan B).

### 4. Identifier-free telemetry: `entry.VerificationSample`

Every online lookup records one sample: event, gate, zone, device, method,
result, reason code, internal credential-verification code, server-side
latency. No person, registration, credential, token, identity value, search
text or operator. It also emits one structured `asc2026.metrics` log line
for an external pipeline (TRD §23.1). Recording is best-effort inside a
savepoint and can never change or fail a verification. Samples are pruned
after `ENTRY_METRICS_RETENTION_DAYS` (command `prune_entry_metrics`, Celery
task `entry.prune_verification_samples`; no beat schedule, as ADR-0009).
A bigint key is used for the same locality reason as `audit.AuditEvent`.

Read models (`apps.entry.observability.snapshot`) cover verification
latency percentiles (PostgreSQL `percentile_cont`), result codes, pass
failures, per-gate health, device health (status counts, stale devices,
open sessions), outbox queue depth, badge stock exceptions and anomaly
signals. They back the `/ops/entry/events/<event>/observability/` dashboard
(new permission `entry.view_entry_observability`, granted to Entry Device
Administrators, event-scoped) and the `entry_metrics` JSON command. The
Celery broker's own queue length is an infrastructure metric and is not read
by the application.

### 5. Degraded-online state from the gate's own recent samples

A gate is degraded when, over `ENTRY_DEGRADED_WINDOW_SECONDS` and with at
least `ENTRY_DEGRADED_MIN_SAMPLES`, its p95 exceeds
`ENTRY_VERIFICATION_TARGET_P95_MS` or technical errors reach
`ENTRY_DEGRADED_ERROR_PERCENT`. The verify screen then shows a warning and
the connection indicator reads "Online — degraded". It never changes a
result.

### 6. Abuse limits and anomaly signals from the audit trail

Following the approved fallback-reference budget (and ADR-0007's rejection
of cache-held limits), `apps.entry.services.limits` counts the operator's
own audited lookups through the indexed `(actor_user, occurred_at)` path:

* identity-reference lookups (NIN, passport, reference, manual search):
  anomaly at `ENTRY_LOOKUP_ANOMALY_THRESHOLD`, refusal at
  `ENTRY_LOOKUP_MAX_PER_WINDOW`;
* invalid or unsupported QR scans: anomaly at
  `ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD`, refusal at
  `ENTRY_INVALID_SCAN_MAX_PER_WINDOW`. Valid scans never count toward the
  budget; once it is exhausted, that operator's QR verification is paused
  for the rest of the window (other operators are unaffected).

The budget is checked after the permission check and before any lookup
work. A refusal is HTTP 429 with `Retry-After`, audited as
`ENT_LOOKUP_THROTTLED`, and produces **no** result (never a false DENIED).
An anomaly signal is one deduplicated `ENT_ANOMALY_SIGNAL` audit row plus a
WARNING log line, shown on the gate monitor and the dashboard. Limits are
per operator, so restarting a checkpoint session does not reset them. Like
the precedent, the ceiling is best-effort under concurrency by the same
operator (overshoot bounded by that operator's parallel requests). System
checks `entry.E001`–`E003` reject incoherent limit settings.

### 7. Indexed path, no synchronous external provider

The case-insensitive reference lookup (`public_reference__iexact`, i.e.
`UPPER(col) = UPPER(value)`) could not use the unique btree; migration
`registrations/0007` adds the expression index `reg_public_ref_upper_idx`.
`EXPLAIN` tests (with `enable_seqscan = off`) prove the reference, credential
(`jti`), rate-limit counter and gate-health queries are index scans, and a
test forbids any outbound network connection during all four lookup methods
(signature verification uses locally held keys; identity matching uses the
local blind index). Query-count ceilings guard the hot path (QR service 18,
reference 19, verify page 21, full QR request 34 queries as measured).

### 8. Provisional performance harness

`tests/performance/test_entry_verification_load.py` (`performance` marker)
drives the real HTTP endpoints of the live test server with one closed-loop
thread per enrolled device and a documented synthetic mix, asserts
correctness under load and client-observed p95 ≤ 1.5 s, and writes
`var/test_artifacts/phase3/prompt5-perf/entry_verification_load.json`. The
volumes are provisional because INFRA-001 is an open decision; the release
load test (NFR-PERF-004) must use the approved capacity profile.

## Consequences

* New migrations: `entry/0002_verificationsample`,
  `registrations/0007_registration_reg_public_ref_upper_idx` (both additive;
  the `AddIndex` briefly locks writes on the registration table, acceptable
  at pre-event volumes -- a concurrent index build would need
  `AddIndexConcurrently` in a non-atomic migration).
* New permission `entry.view_entry_observability`; Entry Device
  Administrators gain it. No other permission changes.
* `accounts.middleware` reads `OPERATIONAL_PASSIVE_PATHS`.
* Every lookup performs up to three extra small indexed queries (budget,
  anomaly count, optional dedup) plus one sample insert.
* Screens outside Prompt 5 remain on the legacy chrome until migrated.
