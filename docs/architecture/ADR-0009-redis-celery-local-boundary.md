# ADR-0009: Redis/Celery local boundary and unverified-integration list

| Field | Value |
| --- | --- |
| Status | Accepted |
| Date | 2026-09-19 |
| Related requirements | TRD §6.1; accepted plan §5.4; conflict C6 |

## Context

TRD §6.1 marks Redis ("cache, rate-limit state and task broker where
appropriate") and Celery workers `[REQUIRED]` as **deployed runtime
components**. The approved local development environment runs neither: no
container runtime, no local Redis install, no Celery worker process.

## Decision

Every Phase 1 behavior that might plausibly need Redis was checked
individually against the specifications (accepted plan §5.4 table):

| Behavior | Real Redis needed? | Implementation |
| --- | --- | --- |
| OTP expiry, replay prevention, attempt count | No | `AuthenticationChallenge` rows in PostgreSQL (Schema §12.4 already specifies this) |
| Issuance/cooldown/lock/network throttling | No | PostgreSQL advisory locks (ADR-0007) |
| Anti-enumeration | No | View-layer response shape |
| Outbox atomicity, dispatch-after-commit | No | `OutboxEvent` row in the domain transaction + `transaction.on_commit` |
| Task idempotency | No | Idempotency keys in PostgreSQL |
| Sessions | No | Database-backed sessions |

**No Phase 1 acceptance criterion requires a running Redis instance or a
separate Celery worker.** `config/settings/local.py` and `test.py` set
`CELERY_TASK_ALWAYS_EAGER = True` with `CELERY_TASK_EAGER_PROPAGATES =
True` -- a first-class, documented Celery execution mode, not a
replacement component. Celery and Redis remain fully declared and
configured in `staging.py`/`production.py`, where
`validate_deployment_configuration` fails closed without
`CELERY_BROKER_URL`/`REDIS_URL` and asserts `CELERY_TASK_ALWAYS_EAGER is
False`.

**The Django cache framework is never used for security state.** Phase 1
places no throttle, lock, idempotency, or authorization state in
`django.core.cache` -- so `LocMemCache` (the local/test cache backend)
replaces no guarantee that Redis would otherwise provide.

## Consequences

Reported honestly, every time, never silently:

- **No real-Redis integration test exists** in this project.
- **No brokered-Celery integration test exists** in this project.
- **No mock or fake is ever labelled a Redis or Celery integration test.**

Both gaps are recorded against Implementation Plan Gate G1 in the Prompt 8
review pack. If a future review rejects this boundary, real Redis becomes
an explicit unresolved local prerequisite blocking only the broker-mode
outbox tests planned for Prompt 3 CP 3b and Prompt 5 -- installing WSL,
Memurai, Redis for Windows, or any other machine-level service remains
**not pre-authorized** without a separate explicit developer decision.
