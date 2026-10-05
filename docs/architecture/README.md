# Architecture Decisions

Store concise Architecture Decision Records here. Each record must state context, decision, consequences, status, and related requirements.

## Index

| ADR | Title |
| --- | --- |
| [ADR-0001](ADR-0001-app-boundaries.md) | Django application boundaries |
| [ADR-0002](ADR-0002-uuid-primary-keys.md) | UUID primary keys for business entities |
| [ADR-0003](ADR-0003-uv-dependency-management.md) | `uv` dependency management and lock policy |
| [ADR-0004](ADR-0004-env-file-secret-delivery.md) | Environment-file and secret-delivery contract |
| [ADR-0005](ADR-0005-provider-neutral-quality-commands.md) | Provider-neutral quality commands |
| [ADR-0006](ADR-0006-encryption-and-blind-indexes.md) | Field encryption and versioned HMAC blind indexes |
| [ADR-0007](ADR-0007-advisory-lock-otp-throttling.md) | PostgreSQL advisory-lock OTP throttling, rate-limit key rotation, and trusted-proxy client identity |
| [ADR-0008](ADR-0008-audit-append-only-protection.md) | Audit append-only protection -- honest two-level contract |
| [ADR-0009](ADR-0009-redis-celery-local-boundary.md) | Redis/Celery local boundary and unverified-integration list |
| [ADR-0010](ADR-0010-venue-zone-gate-deferral.md) | Venue/Zone/Gate deferral |
| [ADR-0011](ADR-0011-custom-user-before-migrations.md) | Custom-user-before-migrations sequencing |
| [ADR-0012](ADR-0012-native-local-environment.md) | Native local environment, no containers |
| [ADR-0013](ADR-0013-private-filesystem-storage.md) | Private filesystem storage behind the S3-compatible interface |
| [ADR-0014](ADR-0014-vendored-asset-provenance.md) | Vendored third-party asset provenance policy |
| [ADR-0015](ADR-0015-no-git-dependency.md) | No Git dependency at this stage; provider-neutral filesystem verification |
| [ADR-0016](ADR-0016-invitation-token-design.md) | Independent invitation and on-behalf claim token design |
| [ADR-0017](ADR-0017-badges-app-boundary.md) | `badges` app boundary while assignments stay in `accreditation` |
| [ADR-0018](ADR-0018-credential-version-vs-lock-version.md) | `credential_version` versus `VersionedModel.version` |
| [ADR-0019](ADR-0019-es256-compact-jws-and-signing-key-boundary.md) | ES256 Compact JWS, `joserfc` verification, and the opaque signing-key boundary |
| [ADR-0020](ADR-0020-physical-badge-stock-design.md) | Generic physical badge stock: app boundary, entry-type set, idempotency ledger, and organization-scoping rules |
| [ADR-0021](ADR-0021-online-entry-devices-and-verification.md) | Online entry: venue structure, device credential and sessions, gate-scoped authorization, admission evaluator, and override boundary |
| [ADR-0022](ADR-0022-entry-ui-observability-and-performance.md) | Entry UI shell and design system, passive connection status, identifier-free telemetry, abuse limits and anomaly signals, indexed path, provisional performance harness |
| [ADR-0023](ADR-0023-pwa-offline-package-preparation.md) | Enrolled-device PWA shell and Offline Package preparation: device API boundary (DRF), layered off-by-default availability, pinned separate package-signing key family, immutable device-encrypted packages, approved validity bands and the seven states, evidence-preserving local storage and the distinct emergency wipe |
| [ADR-0024](ADR-0024-offline-operations-and-synchronization.md) | Offline operations, synchronization and reconciliation |
| [ADR-0025](ADR-0025-shared-challenge-issuance-counter.md) | Shared challenge-issuance counter with a bounded local fallback (CACHE-01) |
| [ADR-0026](ADR-0026-post-submission-identity-verification.md) | Post-submission identity verification: separate state machine, identity revisions, durable jobs, worker leases and slots, duplicate locking, provider seam and staff review |
| [ADR-0027](ADR-0027-staging-beta-deployment-contract.md) | Staging Beta deployment contract: application server, runtime and migration database identities, ClamAV scanning, SMTP, broker TLS, staging provisioning and the runtime artifact |
| [ADR-0028](ADR-0028-approved-country-catalog.md) | Approved country catalog: frozen data, reconciling migration and operator command |
