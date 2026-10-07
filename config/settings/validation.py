"""Fail-closed STATIC configuration validation.

Accepted Phase 1 plan, §5.6 A: this module performs configuration-shape
checks only. It NEVER queries a PostgreSQL catalog, NEVER requires that the
`audit_event` table (or any project table) exists, and therefore NEVER
blocks `manage.py migrate` run with the migration-owner configuration. It is
safe to call at Django settings-import time, before any migration has ever
been applied.

It does NOT and CANNOT prove real PostgreSQL ownership, grants, or privilege
isolation. That is a materially different, later check
(`scripts/check.py audit-isolation`, accepted plan §5.6 B) that connects to a
live, migrated database with the restricted runtime credentials and is run
by the deployment team after migrations. No claim that this module, or
`manage.py check --deploy`, proves database ownership or grants appears
anywhere in this project.

Called only through `config/settings/_deployment.py` (the staging and
production runtime and migration settings modules). `local.py` and `test.py` do not call this
-- Phase 1 local development is exempt from these production-grade
guarantees by design (accepted plan §5.4, §5.6, §5.7), and each such
exemption is documented at the point it is not enforced.

Every error message below names a variable; NONE of them ever includes a
configured value. This is asserted directly by
`tests/foundation/test_static_validation.py`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


class DeploymentConfigurationError(ImproperlyConfigured):
    """Raised when staging/production configuration fails static validation.

    The message aggregates every violation found in one pass, so a developer
    fixing configuration does not have to run the check repeatedly to
    discover each problem in turn. It never includes a secret value -- only
    variable names and the nature of the problem.
    """


@dataclass
class DeploymentSettingsSnapshot:
    """The subset of settings values that static validation inspects.

    Every field here is a *configuration shape* value (present/absent,
    equal/distinct, boolean, numeric ordering) -- never a value whose
    correctness could only be established by contacting PostgreSQL, Redis,
    S3, or a delivery provider. Those live checks belong to a different tool
    (`scripts/check.py db-probe`, `scripts/check.py audit-isolation`, and the
    deployment team's own smoke tests), not to settings-import-time
    validation.
    """

    settings_module_name: str

    # --- distinct migration-owner / runtime database configuration slots ---
    database_user: str | None
    database_migration_user: str | None
    database_password: str | None = None
    database_migration_password: str | None = None
    #: "runtime" (web, workers, beat) or "migration" (the migrate process only).
    database_process_role: str = "runtime"
    #: Whether DATABASE_MIGRATION_PASSWORD is present in the process environment.
    database_migration_password_present: bool = False

    # --- Redis / Celery broker configuration ---
    redis_url: str | None = None
    celery_broker_url: str | None = None
    celery_task_always_eager: bool = False

    # --- MFA step-up provider for sensitive operations (optional since the
    # MFA-01 revision; recorded, never required here) ---
    mfa_backend: str | None = None

    # --- ALTCHA challenge-issuance counter (P4-4-C3, CACHE-01). None means
    # "not supplied by this caller" and skips the check. ---
    human_check_counter_store: str | None = None
    human_check_challenge_max_per_window: int | None = None
    human_check_challenge_fallback_max_per_window: int | None = None
    human_check_counter_fallback_max_networks: int | None = None
    human_check_counter_connect_timeout_ms: int | None = None
    human_check_counter_socket_timeout_ms: int | None = None
    human_check_counter_retry_seconds: int | None = None
    human_check_counter_alert_interval_seconds: int | None = None

    # --- malware scanning. An empty `clamd_scanner_backend` means "not
    # supplied by this caller" and skips the clamd checks. ---
    malware_scanner_backend: str | None = None
    local_malware_scanner_backend: str = ""
    clamd_scanner_backend: str = ""
    clamd_socket_path: str | None = None
    clamd_host: str | None = None
    clamd_port: int | None = None
    clamd_connect_timeout_seconds: int | None = None
    clamd_scan_timeout_seconds: int | None = None
    clamd_max_stream_bytes: int | None = None
    largest_upload_bytes: int | None = None

    # --- real delivery (SMTP) / no local OTP sink. An empty
    # `smtp_email_backend` means "not supplied" and skips the SMTP checks. ---
    email_backend: str | None = None
    local_otp_sink_backend: str = ""
    smtp_email_backend: str = ""
    email_host: str | None = None
    email_port: int | None = None
    email_host_user: str | None = None
    email_host_password: str | None = None
    email_use_tls: bool = False
    email_use_ssl: bool = False
    email_timeout: int | None = None
    default_from_email: str | None = None
    public_base_url: str | None = None

    # --- private object storage: S3-compatible backend (Prompt 2 correction §1/§2) ---
    s3_bucket_name: str | None = None
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region_name: str | None = None

    # --- trusted-proxy / client-network identity ---
    trusted_proxy_forwarding_enabled: bool = False
    trusted_proxy_cidrs: list[str] = field(default_factory=list)

    # --- HSTS rollout (P4-4); None means "not supplied by this caller" ---
    hsts_seconds: int | None = None
    hsts_include_subdomains: bool | None = None
    hsts_preload: bool | None = None

    # --- rate-limit HMAC key rotation and overlap ---
    rate_limit_hmac_active_versions: list[int] = field(default_factory=list)
    rate_limit_hmac_write_version: int | None = None
    rate_limit_key_overlap_seconds: int | None = None
    rate_limit_minimum_required_overlap_seconds: int | None = None
    # {version: value_or_None} for every RATE_LIMIT_HMAC_KEY_V<version>
    # required by rate_limit_hmac_active_versions (Prompt 2 correction §2).
    rate_limit_hmac_keys_by_version: dict[int, str | None] = field(default_factory=dict)

    # --- OTP generator guard ---
    otp_generator_backend: str | None = None
    deterministic_otp_generator_backend: str = ""

    # --- NIN lookup provider (IDV-2, A13-06 / STUB-01). None means "not
    # supplied by this caller" and skips the check. `ministry_api` holds the
    # adapter's configuration values, checked for shape only. ---
    nin_provider_backend: str | None = None
    official_nin_provider_backend: str = ""
    disabled_nin_provider_backend: str = ""
    identity_allow_simulated_provider: bool = False
    ministry_api: dict[str, object] = field(default_factory=dict)

    # --- staff sign-in image CAPTCHA (apps.accounts.captcha_guard). None means
    # "not supplied by this caller" and skips the check. ---
    captcha_test_mode: bool | None = None
    atomic_requests: bool | None = None
    staff_captcha_length: int | None = None
    captcha_timeout_minutes: int | None = None

    # --- identity field-encryption and blind-index HMAC keys (ADR-0006, Prompt 3) ---
    identity_encryption_active_versions: list[int] = field(default_factory=list)
    identity_encryption_write_version: int | None = None
    identity_encryption_keys_by_version: dict[int, str | None] = field(default_factory=dict)
    identity_blind_index_hmac_active_versions: list[int] = field(default_factory=list)
    identity_blind_index_hmac_write_version: int | None = None
    identity_blind_index_hmac_keys_by_version: dict[int, str | None] = field(default_factory=dict)


_WEAK_CERT_REQS = {"none", "optional", "cert_none", "cert_optional"}
_FALSE_WORDS = {"0", "false", "no", "off", "n", "f"}


def _redis_location(url: str) -> tuple[str, str, int, int, dict[str, list[str]]] | None:
    """(scheme, host, port, database, query) of a redis:// or rediss:// URL,
    or None when it is not one. Never raises and never echoes the URL."""
    from urllib.parse import parse_qs

    try:
        parts = urlsplit(url)
        port = parts.port or 6379
        host = (parts.hostname or "").lower()
        query = parse_qs(parts.query)
        path = parts.path.strip("/")
        db_text = query.get("db", [path or "0"])[0]
        db = int(db_text)
    except ValueError:
        return None
    if parts.scheme not in ("redis", "rediss") or not host or db < 0:
        return None
    return parts.scheme, host, port, db, query


def _tls_verification_weakened(query: dict[str, list[str]]) -> bool:
    cert_reqs = [value.strip().lower() for value in query.get("ssl_cert_reqs", [])]
    check_hostname = [value.strip().lower() for value in query.get("ssl_check_hostname", [])]
    return any(value in _WEAK_CERT_REQS for value in cert_reqs) or any(
        value in _FALSE_WORDS for value in check_hostname
    )


def check_staging_provisioning_flag(settings_module_name: str, enabled: bool) -> None:
    """The staging UAT provisioning command is never enabled in production."""
    if enabled and ".production" in settings_module_name:
        raise DeploymentConfigurationError(
            "STAGING_PROVISIONING_ENABLED must be False in production."
        )


_LOOPBACK_NAMES = {"localhost"}


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _plausible_sender(address: str) -> bool:
    from email.utils import parseaddr

    if not address or any(char in address for char in "\r\n"):
        return False
    _name, mailbox = parseaddr(address)
    local, _, domain = mailbox.rpartition("@")
    return bool(local and domain and "." in domain and " " not in mailbox)


def validate_deployment_configuration(snapshot: DeploymentSettingsSnapshot) -> None:
    """Raise `DeploymentConfigurationError` if any static invariant is violated.

    Every check below is a presence/shape/distinctness check over already-
    read configuration values. No network call, no database connection, no
    dependency on any project model or migration.
    """
    errors: list[str] = []

    # 1. Distinct migration-owner and runtime database configuration slots
    # (accepted plan §5.6 A). This does NOT verify real PostgreSQL ownership
    # -- only that two distinct configuration identities are declared, which
    # is the prerequisite the deployment team's real role separation and the
    # post-migration `audit-isolation` check both depend on.
    if not snapshot.database_migration_user:
        errors.append(
            "DATABASE_MIGRATION_USER must be set to a distinct migration-owner "
            "database identity in staging/production (accepted plan §5.6 A)."
        )
    elif snapshot.database_migration_user == snapshot.database_user:
        errors.append(
            "DATABASE_MIGRATION_USER must be DISTINCT from DATABASE_USER (the "
            "restricted runtime identity) in staging/production. They are "
            "currently configured to the same value."
        )
    if snapshot.database_process_role == "runtime":
        if not snapshot.database_password:
            errors.append("DATABASE_PASSWORD must be configured for the runtime role.")
        if snapshot.database_migration_password_present:
            errors.append(
                "DATABASE_MIGRATION_PASSWORD must not be present in a runtime process (web, "
                "worker, beat). Only the migration process, run with the *_migration "
                "settings module, uses the migration-owner identity."
            )
    elif snapshot.database_process_role == "migration":
        if not snapshot.database_migration_password:
            errors.append(
                "DATABASE_MIGRATION_PASSWORD must be configured for the migration-owner role."
            )
    else:
        errors.append("The database process role must be 'runtime' or 'migration'.")

    # 2 & 3. Redis / Celery broker required; eager mode disabled.
    if not snapshot.redis_url:
        errors.append("REDIS_URL must be set in staging/production.")
    if not snapshot.celery_broker_url:
        errors.append("CELERY_BROKER_URL must be set in staging/production.")
    elif snapshot.celery_broker_url.startswith("memory://"):
        errors.append(
            "CELERY_BROKER_URL must not be the local in-memory transport "
            "('memory://') in staging/production."
        )
    else:
        broker_location = _redis_location(snapshot.celery_broker_url)
        if broker_location is None:
            errors.append(
                "CELERY_BROKER_URL must be a rediss:// URL with a host and a numeric "
                "database index."
            )
        else:
            if broker_location[0] != "rediss":
                errors.append("CELERY_BROKER_URL must use TLS (rediss://) in staging/production.")
            if _tls_verification_weakened(broker_location[4]):
                errors.append(
                    "CELERY_BROKER_URL must not disable TLS certificate or host-name "
                    "verification (ssl_cert_reqs, ssl_check_hostname)."
                )
    if snapshot.celery_task_always_eager:
        errors.append(
            "CELERY_TASK_ALWAYS_EAGER must be False in staging/production; "
            "eager execution is a local/test-only mode (accepted plan §5.4)."
        )

    # 3b. The Redis database of the challenge-issuance counter (P4-4-C3,
    #     CACHE-01): TLS with certificate and host-name verification (TRD
    #     §5.1: data services on a private network with encryption), and a different
    #     database from the Celery broker, so counter keys never mix with
    #     broker queues.
    if snapshot.redis_url:
        location = _redis_location(snapshot.redis_url)
        if location is None:
            errors.append(
                "REDIS_URL must be a rediss:// URL with a host and a numeric database "
                "index (P4-4-C3)."
            )
        else:
            scheme, _host, _port, _db, query = location
            if scheme != "rediss":
                errors.append("REDIS_URL must use TLS (rediss://) in staging/production (P4-4-C3).")
            if _tls_verification_weakened(query):
                errors.append(
                    "REDIS_URL must not disable TLS certificate or host-name verification "
                    "(ssl_cert_reqs, ssl_check_hostname) (P4-4-C3)."
                )
            broker = _redis_location(snapshot.celery_broker_url or "")
            if broker is not None and broker[1:4] == location[1:4]:
                errors.append(
                    "REDIS_URL and CELERY_BROKER_URL must name different Redis databases, so "
                    "the challenge counter is isolated from the broker (P4-4-C3)."
                )

    # 3c. Challenge-issuance counter settings (P4-4-C3, CACHE-01). Only the
    #     shared store is acceptable here; the fallback must be stricter than
    #     the normal limit; state, timeouts and intervals must be bounded.
    if snapshot.human_check_counter_store is not None:
        if snapshot.human_check_counter_store != "redis":
            errors.append(
                "HUMAN_CHECK_COUNTER_STORE must be 'redis' in staging/production; the "
                "per-process counter is for local development only (P4-4-C3)."
            )
    normal = snapshot.human_check_challenge_max_per_window
    fallback = snapshot.human_check_challenge_fallback_max_per_window
    if normal is not None and normal < 2:
        errors.append(
            "HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW must be at least 2, so a stricter "
            "fallback limit exists (P4-4-C3)."
        )
    if fallback is not None and (fallback < 1 or (normal is not None and fallback >= normal)):
        errors.append(
            "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW must be at least 1 and lower "
            "than HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW (P4-4-C3)."
        )
    for name, value, low, high in (
        (
            "HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS",
            snapshot.human_check_counter_fallback_max_networks,
            1,
            1_000_000,
        ),
        (
            "HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS",
            snapshot.human_check_counter_connect_timeout_ms,
            1,
            5_000,
        ),
        (
            "HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS",
            snapshot.human_check_counter_socket_timeout_ms,
            1,
            5_000,
        ),
        ("HUMAN_CHECK_COUNTER_RETRY_SECONDS", snapshot.human_check_counter_retry_seconds, 1, 3_600),
        (
            "HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS",
            snapshot.human_check_counter_alert_interval_seconds,
            1,
            86_400,
        ),
    ):
        if value is not None and not low <= value <= high:
            errors.append(f"{name} must be between {low} and {high} (P4-4-C3).")

    # 4. MFA. Owner decision MFA-01 was revised on 2026-10-01 (amendment
    #    A-11 in docs/execution/UX_REMARKS_DECISION_GATE.md): operational
    #    sign-in is email and password in staging and production, so no
    #    sign-in MFA provider is required here any more. `MFA_BACKEND` remains
    #    the step-up provider for the emergency device wipe, which fails
    #    closed without it; `entry.W001` reports its absence at
    #    `check --deploy`, and `manage.py release_readiness` reports the
    #    unavailable wipe. Never satisfy this with a stub.

    # 5. Scanner configuration (SEC-006).
    if not snapshot.malware_scanner_backend:
        errors.append("MALWARE_SCANNER_BACKEND must be configured in staging/production (SEC-006).")
    elif snapshot.malware_scanner_backend == snapshot.local_malware_scanner_backend:
        errors.append(
            "MALWARE_SCANNER_BACKEND must not be the local deterministic stub "
            "in staging/production (SEC-006)."
        )
    elif (
        snapshot.clamd_scanner_backend
        and snapshot.malware_scanner_backend != snapshot.clamd_scanner_backend
    ):
        errors.append(
            "MALWARE_SCANNER_BACKEND must be the ClamAV clamd adapter "
            "(apps.documents.scanning.ClamdScanner) in staging/production; no other "
            "scanner, and no local or test stub, is accepted (SEC-006)."
        )
    if (
        snapshot.clamd_scanner_backend
        and snapshot.malware_scanner_backend == snapshot.clamd_scanner_backend
    ):
        has_socket = bool((snapshot.clamd_socket_path or "").strip())
        has_host = bool((snapshot.clamd_host or "").strip())
        if has_socket == has_host:
            errors.append(
                "Set exactly one of CLAMD_SOCKET_PATH (a Unix socket) or CLAMD_HOST (a "
                "private TCP endpoint) for the clamd scanner (SEC-006)."
            )
        if has_host and not (snapshot.clamd_port is not None and 1 <= snapshot.clamd_port <= 65535):
            errors.append("CLAMD_PORT must be between 1 and 65535 (SEC-006).")
        for name, value, low, high in (
            ("CLAMD_CONNECT_TIMEOUT_SECONDS", snapshot.clamd_connect_timeout_seconds, 1, 60),
            ("CLAMD_SCAN_TIMEOUT_SECONDS", snapshot.clamd_scan_timeout_seconds, 1, 600),
        ):
            if value is None or not low <= value <= high:
                errors.append(f"{name} must be between {low} and {high} (SEC-006).")
        limit = snapshot.clamd_max_stream_bytes
        if limit is None or not 1 <= limit <= 2**32 - 1:
            errors.append("CLAMD_MAX_STREAM_BYTES must be between 1 and 4294967295 (SEC-006).")
        elif snapshot.largest_upload_bytes is not None and limit < snapshot.largest_upload_bytes:
            errors.append(
                "CLAMD_MAX_STREAM_BYTES must be at least the largest accepted upload size, "
                "so a valid upload is never refused by the scanner limit (SEC-006)."
            )

    # 6 & 7. Real delivery provider configured; no local OTP sink selectable.
    if not snapshot.email_backend:
        errors.append("EMAIL_BACKEND must be configured in staging/production.")
    elif snapshot.email_backend == snapshot.local_otp_sink_backend:
        errors.append(
            "EMAIL_BACKEND must not be the local fake delivery sink in "
            "staging/production (accepted plan §5.7). No OTP sink may exist "
            "or be selectable outside local and test."
        )
    elif snapshot.smtp_email_backend and snapshot.email_backend != snapshot.smtp_email_backend:
        errors.append(
            "EMAIL_BACKEND must be Django's SMTP backend "
            "(django.core.mail.backends.smtp.EmailBackend) in staging/production; a console, "
            "memory, file or dummy backend would silently drop participant codes."
        )
    if snapshot.smtp_email_backend:
        host = (snapshot.email_host or "").strip()
        if not host:
            errors.append("EMAIL_HOST must name the SMTP server in staging/production.")
        if snapshot.email_port is None or not 1 <= snapshot.email_port <= 65535:
            errors.append("EMAIL_PORT must be between 1 and 65535.")
        if snapshot.email_use_tls and snapshot.email_use_ssl:
            errors.append("EMAIL_USE_TLS and EMAIL_USE_SSL are mutually exclusive; set one.")
        encrypted = snapshot.email_use_tls or snapshot.email_use_ssl
        if host and not encrypted and not _is_loopback_host(host):
            errors.append(
                "SMTP to a non-loopback EMAIL_HOST must use EMAIL_USE_TLS (STARTTLS) or "
                "EMAIL_USE_SSL; unencrypted SMTP is accepted only to a relay on the same host."
            )
        has_user = bool((snapshot.email_host_user or "").strip())
        has_password = bool(snapshot.email_host_password)
        if has_user != has_password:
            errors.append("EMAIL_HOST_USER and EMAIL_HOST_PASSWORD must be set together.")
        if has_user and not encrypted:
            errors.append(
                "SMTP authentication requires EMAIL_USE_TLS or EMAIL_USE_SSL, so the "
                "credentials are never sent in clear text."
            )
        if snapshot.email_timeout is None or not 1 <= snapshot.email_timeout <= 120:
            errors.append(
                "EMAIL_TIMEOUT_SECONDS must be between 1 and 120, so a stalled SMTP server "
                "cannot hold a participant request open."
            )
        sender = (snapshot.default_from_email or "").strip()
        if not _plausible_sender(sender) or sender.lower().endswith("@localhost"):
            errors.append(
                "DEFAULT_FROM_EMAIL must be the sender address of the deployment "
                "(for example 'ASC 2026 <no-reply@your-domain>')."
            )

    # Absolute links sent to participants must resolve to the deployment's
    # own HTTPS origin. Presence alone is insufficient: malformed values,
    # embedded credentials and query/fragment suffixes can create broken or
    # misleading claim links.
    public_base_url_valid = False
    if snapshot.public_base_url:
        try:
            parsed_public_url = urlsplit(snapshot.public_base_url)
            _parsed_port = parsed_public_url.port  # force validation of a malformed port
        except ValueError:
            parsed_public_url = None
        if parsed_public_url is not None:
            public_base_url_valid = bool(
                parsed_public_url.scheme == "https"
                and parsed_public_url.hostname
                and not any(character.isspace() for character in parsed_public_url.hostname)
                and parsed_public_url.username is None
                and parsed_public_url.password is None
                and not parsed_public_url.query
                and not parsed_public_url.fragment
            )
    if not public_base_url_valid:
        errors.append(
            "DJANGO_PUBLIC_BASE_URL must be an absolute HTTPS URL without embedded "
            "credentials, query parameters, or a fragment in staging/production."
        )

    # 7b. Private object storage: S3-compatible backend (Prompt 2 correction
    # §1/§2). Configuration-shape only -- never contacts S3.
    if not snapshot.s3_bucket_name:
        errors.append("S3_STORAGE_BUCKET_NAME must be configured in staging/production.")
    if not snapshot.s3_endpoint_url:
        errors.append("S3_STORAGE_ENDPOINT_URL must be configured in staging/production.")
    if not snapshot.s3_access_key_id:
        errors.append("S3_STORAGE_ACCESS_KEY_ID must be configured in staging/production.")
    if not snapshot.s3_secret_access_key:
        errors.append("S3_STORAGE_SECRET_ACCESS_KEY must be configured in staging/production.")
    if not snapshot.s3_region_name:
        errors.append(
            "S3_STORAGE_REGION_NAME must be configured in staging/production "
            "(required by the S3-compatible adapter for request signing)."
        )

    # 8. Trusted-proxy configuration whenever forwarded-address processing
    #    is enabled (accepted plan §5.5, client-network identity contract).
    if snapshot.trusted_proxy_forwarding_enabled and not snapshot.trusted_proxy_cidrs:
        errors.append(
            "TRUSTED_PROXY_FORWARDING_ENABLED is true but TRUSTED_PROXY_CIDRS "
            "is empty. Forwarded-address processing must fail closed without "
            "an explicit trusted-proxy boundary (accepted plan §5.5)."
        )
    for index, cidr in enumerate(snapshot.trusted_proxy_cidrs, start=1):
        try:
            ipaddress.ip_network(str(cidr).strip(), strict=False)
        except ValueError:
            # The entry position only, never its value.
            errors.append(f"TRUSTED_PROXY_CIDRS entry {index} is not a valid network (P4-4).")

    # 8b. HSTS rollout (P4-4). A staged rollout may lower the max-age, but it
    #     is never disabled here, and `preload` is only sent when the browser
    #     preload list would accept it.
    if snapshot.hsts_seconds is not None and snapshot.hsts_seconds <= 0:
        errors.append("DJANGO_SECURE_HSTS_SECONDS must be a positive number of seconds (P4-4).")
    if snapshot.hsts_preload:
        if not snapshot.hsts_include_subdomains:
            errors.append(
                "DJANGO_SECURE_HSTS_PRELOAD requires DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS (P4-4)."
            )
        if snapshot.hsts_seconds is not None and snapshot.hsts_seconds < 31536000:
            errors.append(
                "DJANGO_SECURE_HSTS_PRELOAD requires DJANGO_SECURE_HSTS_SECONDS of at least "
                "31536000 (one year) (P4-4)."
            )

    # 9. Rate-limit HMAC key-version and overlap configuration
    #    (accepted plan §5.5, rotation-safe throttling; Prompt 2 correction §2).
    active_versions = snapshot.rate_limit_hmac_active_versions
    if not active_versions:
        errors.append("RATE_LIMIT_HMAC_ACTIVE_VERSIONS must contain at least one version.")
    else:
        non_positive = [v for v in active_versions if v <= 0]
        if non_positive:
            errors.append(
                "Every version in RATE_LIMIT_HMAC_ACTIVE_VERSIONS must be a "
                f"positive integer; found non-positive or unparseable value(s) {non_positive!r}."
            )
        if len(active_versions) != len(set(active_versions)):
            errors.append("RATE_LIMIT_HMAC_ACTIVE_VERSIONS must not contain duplicate versions.")

    if snapshot.rate_limit_hmac_write_version is None:
        errors.append("RATE_LIMIT_HMAC_WRITE_VERSION must be configured.")
    elif snapshot.rate_limit_hmac_write_version not in active_versions:
        errors.append(
            "RATE_LIMIT_HMAC_WRITE_VERSION must be one of RATE_LIMIT_HMAC_ACTIVE_VERSIONS."
        )

    # One RATE_LIMIT_HMAC_KEY_V<version> per active version (accepted plan
    # §5.5). Named individually so a developer sees exactly which version's
    # key is missing -- never the key's value.
    for version in active_versions:
        if version <= 0:
            continue  # already reported above; avoid a confusing duplicate name
        if not snapshot.rate_limit_hmac_keys_by_version.get(version):
            errors.append(f"RATE_LIMIT_HMAC_KEY_V{version} must be configured.")

    if (
        snapshot.rate_limit_key_overlap_seconds is not None
        and snapshot.rate_limit_minimum_required_overlap_seconds is not None
        and snapshot.rate_limit_key_overlap_seconds
        < snapshot.rate_limit_minimum_required_overlap_seconds
    ):
        errors.append(
            "RATE_LIMIT_KEY_OVERLAP_SECONDS "
            f"({snapshot.rate_limit_key_overlap_seconds}) is shorter than the "
            "computed minimum retirement overlap "
            f"({snapshot.rate_limit_minimum_required_overlap_seconds}s = max(OTP "
            "lifetime, resend cooldown, temporary-lock duration, every rate-limit "
            "window)). A retired key version must remain active at least that "
            "long (accepted plan §5.5)."
        )

    # OTP generator guard: the deterministic generator must never be
    # selectable outside test settings (accepted plan §5.7).
    if (
        snapshot.otp_generator_backend
        and snapshot.deterministic_otp_generator_backend
        and snapshot.otp_generator_backend == snapshot.deterministic_otp_generator_backend
    ):
        errors.append(
            "OTP_GENERATOR_BACKEND is set to the deterministic generator in "
            f"{snapshot.settings_module_name!r}. The deterministic generator "
            "may be injected only under config.settings.test (accepted plan §5.7)."
        )

    # 10. Identity field-encryption and blind-index HMAC key configuration
    #     (ADR-0006, Prompt 3) -- same shape rules as the rate-limit family.
    def _check_versioned_key_family(
        *,
        family_label: str,
        active_versions: list[int],
        write_version: int | None,
        keys_by_version: dict[int, str | None],
        key_var_prefix: str,
    ) -> None:
        if not active_versions:
            errors.append(f"{family_label}_ACTIVE_VERSIONS must contain at least one version.")
            return
        non_positive = [v for v in active_versions if v <= 0]
        if non_positive:
            errors.append(
                f"Every version in {family_label}_ACTIVE_VERSIONS must be a positive "
                f"integer; found non-positive or unparseable value(s) {non_positive!r}."
            )
        if len(active_versions) != len(set(active_versions)):
            errors.append(f"{family_label}_ACTIVE_VERSIONS must not contain duplicate versions.")
        if write_version is None:
            errors.append(f"{family_label}_WRITE_VERSION must be configured.")
        elif write_version not in active_versions:
            errors.append(
                f"{family_label}_WRITE_VERSION must be one of {family_label}_ACTIVE_VERSIONS."
            )
        for version in active_versions:
            if version <= 0:
                continue
            if not keys_by_version.get(version):
                errors.append(f"{key_var_prefix}{version} must be configured.")

    _check_versioned_key_family(
        family_label="IDENTITY_ENCRYPTION",
        active_versions=snapshot.identity_encryption_active_versions,
        write_version=snapshot.identity_encryption_write_version,
        keys_by_version=snapshot.identity_encryption_keys_by_version,
        key_var_prefix="IDENTITY_ENCRYPTION_KEY_V",
    )
    # 11. NIN lookup provider (IDV-2, amendment A-13, A13-06 / STUB-01). Only
    #     the official ministry adapter or the disabled backend (manual review
    #     only) may run here; every stub and development simulation is
    #     refused, so a simulated result can never pass as official
    #     verification. A malformed adapter value fails startup; a MISSING
    #     value only keeps the adapter unavailable (the authentication
    #     contract is still open, API-01), which release_readiness reports.
    if snapshot.nin_provider_backend is not None:
        allowed = {
            snapshot.official_nin_provider_backend,
            snapshot.disabled_nin_provider_backend,
        } - {""}
        if snapshot.nin_provider_backend not in allowed:
            errors.append(
                "NIN_PROVIDER_BACKEND must be the official ministry adapter or the disabled "
                "backend in staging/production; every stub and development simulation is "
                "refused (A13-06)."
            )
        if snapshot.identity_allow_simulated_provider:
            errors.append(
                "IDENTITY_ALLOW_SIMULATED_PROVIDER must be False in staging/production (A13-06)."
            )
        if snapshot.nin_provider_backend == snapshot.official_nin_provider_backend:
            from apps.people.nin_provider import MinistryApiConfig

            try:
                config = MinistryApiConfig(**snapshot.ministry_api)
            except TypeError:
                errors.append("The ministry adapter configuration is incomplete (IDV-2).")
            else:
                errors.extend(config.malformed_problems())

    # Staff sign-in CAPTCHA: never the package's test answer, a consumption
    # that commits before authentication (no ATOMIC_REQUESTS), and bounded
    # length and lifetime.
    if snapshot.captcha_test_mode:
        errors.append("CAPTCHA_TEST_MODE must be False in staging/production.")
    if snapshot.atomic_requests:
        errors.append(
            "DATABASES['default']['ATOMIC_REQUESTS'] must be False: the staff sign-in CAPTCHA "
            "consumption must commit before the password is checked."
        )
    if snapshot.staff_captcha_length is not None and not 4 <= snapshot.staff_captcha_length <= 8:
        errors.append("STAFF_CAPTCHA_LENGTH must be between 4 and 8 characters.")
    if (
        snapshot.captcha_timeout_minutes is not None
        and not 1 <= snapshot.captcha_timeout_minutes <= 30
    ):
        errors.append("STAFF_CAPTCHA_TIMEOUT_MINUTES must be between 1 and 30.")

    _check_versioned_key_family(
        family_label="IDENTITY_BLIND_INDEX_HMAC",
        active_versions=snapshot.identity_blind_index_hmac_active_versions,
        write_version=snapshot.identity_blind_index_hmac_write_version,
        keys_by_version=snapshot.identity_blind_index_hmac_keys_by_version,
        key_var_prefix="IDENTITY_BLIND_INDEX_HMAC_KEY_V",
    )

    if errors:
        header = (
            f"Static deployment configuration validation failed for "
            f"{snapshot.settings_module_name!r} with {len(errors)} problem(s). "
            "This check inspects configuration shape only -- it does not "
            "connect to PostgreSQL, Redis, S3, or any live service, and it "
            "does not prove database ownership or grants. No secret value is "
            "included below.\n"
        )
        raise DeploymentConfigurationError(header + "\n".join(f"  - {e}" for e in errors))
