"""Shared configuration of the deployed environments (staging and production).

Never selected directly. Four thin modules import it:

* `config.settings.staging` and `config.settings.production`: the RUNTIME
  modules for the web application, the Celery workers and the single beat
  scheduler. They connect with the restricted runtime database identity
  (`DATABASE_USER`, `DATABASE_PASSWORD`) and refuse to start when the
  migration-owner password is present in the process environment.
* `config.settings.staging_migration` and `config.settings.production_migration`:
  the MIGRATION modules, used only for `manage.py migrate` (and the read-only
  `showmigrations`/`migrate --plan`). They connect with the migration-owner
  identity (`DATABASE_MIGRATION_USER`, `DATABASE_MIGRATION_PASSWORD`) and do
  not need the runtime password.

There is no fallback in either direction: a runtime module never reads the
migration-owner password, and a migration module never uses the runtime
password. Real PostgreSQL ownership and grants are still verified after the
migration with the runtime identity (`scripts/check.py audit-isolation`,
ADR-0008); this module checks configuration shape only, without contacting
any service, at settings-import time.
"""

from __future__ import annotations

import os
import ssl
from urllib.parse import parse_qs, urlsplit

from ._env import env, env_bool, env_int, require
from .base import *  # noqa: F403
from .validation import (
    DeploymentSettingsSnapshot,
    check_staging_provisioning_flag,
    validate_deployment_configuration,
)

DEBUG = False

ALLOWED_HOSTS = [h.strip() for h in require("DJANGO_ALLOWED_HOSTS").split(",") if h.strip()]

# Validated, explicit configuration -- never the local default (absolute claim
# and invitation URLs are built from it).
PUBLIC_BASE_URL = require("DJANGO_PUBLIC_BASE_URL").rstrip("/")

# --- Database identities (accepted plan §5.6 A). The migration owner's role
# NAME is part of every process's configuration, so the distinctness check
# runs everywhere; its PASSWORD is read only by the migration modules. ---
DATABASE_MIGRATION_USER = env("DATABASE_MIGRATION_USER")
#: "runtime" in staging/production, "migration" in the *_migration modules.
DATABASE_PROCESS_ROLE = "runtime"

# --- Private object storage: S3-compatible backend (§5.11). ---
PRIVATE_STORAGE_BACKEND = "s3"

# Explicit, project-owned OPTIONS -- deliberately NOT the bare
# `{"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}` shape, which
# would let django-storages/boto3 fall back to its OWN ambient
# `AWS_*`-named Django settings or environment credentials if any ever
# appeared instead of failing closed on the project's own `S3_STORAGE_*`
# values. `default_acl`/`file_overwrite` mirror the private-by-default posture
# already enforced by `apps.documents.storage.S3PrivateStorage`.
# `querystring_auth=True`: Django's OWN default storage -- distinct from
# `S3PrivateStorage`, which the protected-document access actually uses and
# which exposes no URL method at all -- must never be able to hand out an
# unsigned, non-expiring object URL if anything ever calls its `.url()`.
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "bucket_name": S3_STORAGE_BUCKET_NAME,
            "endpoint_url": S3_STORAGE_ENDPOINT_URL,
            "access_key": S3_STORAGE_ACCESS_KEY_ID,
            "secret_key": S3_STORAGE_SECRET_ACCESS_KEY,
            "region_name": S3_STORAGE_REGION_NAME,
            "default_acl": "private",
            "querystring_auth": True,
            "file_overwrite": False,
        },
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}

# --- Malware scanning (SEC-006): the ClamAV clamd adapter only. ---
MALWARE_SCANNER_BACKEND = env("MALWARE_SCANNER_BACKEND", default=CLAMD_SCANNER_BACKEND)
MALWARE_SCANNER_ALLOW_LOCAL_STUBS = False

# --- Email: Django's SMTP backend, configured from the environment. Every
# participant and staff message (OTP codes included) goes through it. ---
SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_BACKEND = env("EMAIL_BACKEND", default=SMTP_EMAIL_BACKEND)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env_int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", default=False)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT_SECONDS", default=10)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# --- Celery broker over TLS. Without explicit options kombu connects to a
# `rediss://` broker WITHOUT certificate verification (it logs a warning and
# uses CERT_NONE). Verification is therefore requested here. When the URL
# itself carries `ssl_*` options (for example `ssl_ca_certs` for a private CA),
# kombu uses those instead, and validation.py refuses options that weaken
# verification. ---
_broker_parts = urlsplit(CELERY_BROKER_URL or "")
if _broker_parts.scheme == "rediss" and not any(
    key.startswith("ssl_") for key in parse_qs(_broker_parts.query)
):
    CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": ssl.CERT_REQUIRED}

# --- Cookies and transport security ---
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
# HSTS (P4-4). The defaults are the previously approved values. A staged
# rollout lowers them through the environment without a code change; the
# `preload` directive is sent only with includeSubDomains and at least one
# year, which validation.py enforces.
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
CONTENT_SECURITY_POLICY_UPGRADE_INSECURE_REQUESTS = True

# --- ALTCHA challenge-issuance counter (CACHE-01, ADR-0025): the shared Redis
# counter in the REDIS_URL database, never the per-process local counter.
# validation.py requires a TLS URL in a different database from the broker. ---
HUMAN_CHECK_COUNTER_STORE = "redis"


def use_migration_identity() -> dict:
    """The DATABASES setting of a migration process: the migration owner.

    Requires `DATABASE_MIGRATION_USER` and `DATABASE_MIGRATION_PASSWORD`; the
    runtime password is neither needed nor used.
    """
    migration_databases = {alias: dict(config) for alias, config in DATABASES.items()}
    migration_databases["default"]["USER"] = require("DATABASE_MIGRATION_USER")
    migration_databases["default"]["PASSWORD"] = require("DATABASE_MIGRATION_PASSWORD")
    return migration_databases


def validate_settings(namespace: dict, *, settings_module_name: str) -> None:
    """Static validation of a deployed settings module (no service contacted)."""
    check_staging_provisioning_flag(
        settings_module_name, bool(namespace.get("STAGING_PROVISIONING_ENABLED"))
    )
    role = namespace["DATABASE_PROCESS_ROLE"]
    connection = namespace["DATABASES"]["default"]
    validate_deployment_configuration(
        DeploymentSettingsSnapshot(
            settings_module_name=settings_module_name,
            database_process_role=role,
            # The runtime role's NAME is always DATABASE_USER, also in a
            # migration process (whose connection user is the owner).
            database_user=os.environ.get("DATABASE_USER"),
            database_migration_user=namespace["DATABASE_MIGRATION_USER"],
            database_password=(connection["PASSWORD"] if role == "runtime" else None),
            database_migration_password_present=bool(
                (os.environ.get("DATABASE_MIGRATION_PASSWORD") or "").strip()
            ),
            database_migration_password=(connection["PASSWORD"] if role == "migration" else None),
            redis_url=namespace["REDIS_URL"],
            celery_broker_url=namespace["CELERY_BROKER_URL"],
            celery_task_always_eager=namespace["CELERY_TASK_ALWAYS_EAGER"],
            mfa_backend=namespace["MFA_BACKEND"],
            human_check_counter_store=namespace["HUMAN_CHECK_COUNTER_STORE"],
            human_check_challenge_max_per_window=namespace["HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW"],
            human_check_challenge_fallback_max_per_window=namespace[
                "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW"
            ],
            human_check_counter_fallback_max_networks=namespace[
                "HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS"
            ],
            human_check_counter_connect_timeout_ms=namespace[
                "HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS"
            ],
            human_check_counter_socket_timeout_ms=namespace[
                "HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS"
            ],
            human_check_counter_retry_seconds=namespace["HUMAN_CHECK_COUNTER_RETRY_SECONDS"],
            human_check_counter_alert_interval_seconds=namespace[
                "HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS"
            ],
            malware_scanner_backend=namespace["MALWARE_SCANNER_BACKEND"],
            local_malware_scanner_backend=namespace["LOCAL_MALWARE_SCANNER_BACKEND"],
            clamd_scanner_backend=namespace["CLAMD_SCANNER_BACKEND"],
            clamd_socket_path=namespace["CLAMD_SOCKET_PATH"],
            clamd_host=namespace["CLAMD_HOST"],
            clamd_port=namespace["CLAMD_PORT"],
            clamd_connect_timeout_seconds=namespace["CLAMD_CONNECT_TIMEOUT_SECONDS"],
            clamd_scan_timeout_seconds=namespace["CLAMD_SCAN_TIMEOUT_SECONDS"],
            clamd_max_stream_bytes=namespace["CLAMD_MAX_STREAM_BYTES"],
            largest_upload_bytes=max(
                namespace["PROFILE_PHOTO_MAX_SIZE_BYTES"],
                namespace["PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES"],
                namespace["DELEGATION_CSV_MAX_SIZE_BYTES"],
            ),
            email_backend=namespace["EMAIL_BACKEND"],
            local_otp_sink_backend=namespace["LOCAL_OTP_SINK_EMAIL_BACKEND"],
            smtp_email_backend=namespace["SMTP_EMAIL_BACKEND"],
            email_host=namespace["EMAIL_HOST"],
            email_port=namespace["EMAIL_PORT"],
            email_host_user=namespace["EMAIL_HOST_USER"],
            email_host_password=namespace["EMAIL_HOST_PASSWORD"],
            email_use_tls=namespace["EMAIL_USE_TLS"],
            email_use_ssl=namespace["EMAIL_USE_SSL"],
            email_timeout=namespace["EMAIL_TIMEOUT"],
            default_from_email=namespace["DEFAULT_FROM_EMAIL"],
            public_base_url=namespace["PUBLIC_BASE_URL"],
            s3_bucket_name=namespace["S3_STORAGE_BUCKET_NAME"],
            s3_endpoint_url=namespace["S3_STORAGE_ENDPOINT_URL"],
            s3_access_key_id=namespace["S3_STORAGE_ACCESS_KEY_ID"],
            s3_secret_access_key=namespace["S3_STORAGE_SECRET_ACCESS_KEY"],
            s3_region_name=namespace["S3_STORAGE_REGION_NAME"],
            trusted_proxy_forwarding_enabled=namespace["TRUSTED_PROXY_FORWARDING_ENABLED"],
            trusted_proxy_cidrs=namespace["TRUSTED_PROXY_CIDRS"],
            hsts_seconds=namespace["SECURE_HSTS_SECONDS"],
            hsts_include_subdomains=namespace["SECURE_HSTS_INCLUDE_SUBDOMAINS"],
            hsts_preload=namespace["SECURE_HSTS_PRELOAD"],
            rate_limit_hmac_active_versions=namespace["RATE_LIMIT_HMAC_ACTIVE_VERSIONS"],
            rate_limit_hmac_write_version=namespace["RATE_LIMIT_HMAC_WRITE_VERSION"],
            rate_limit_key_overlap_seconds=namespace["RATE_LIMIT_KEY_OVERLAP_SECONDS"],
            rate_limit_minimum_required_overlap_seconds=namespace[
                "RATE_LIMIT_MINIMUM_REQUIRED_OVERLAP_SECONDS"
            ],
            rate_limit_hmac_keys_by_version=namespace["RATE_LIMIT_HMAC_KEYS_BY_VERSION"],
            otp_generator_backend=namespace["OTP_GENERATOR_BACKEND"],
            deterministic_otp_generator_backend=namespace["DETERMINISTIC_OTP_GENERATOR_BACKEND"],
            nin_provider_backend=namespace["NIN_PROVIDER_BACKEND"],
            official_nin_provider_backend=namespace["OFFICIAL_NIN_PROVIDER_BACKEND"],
            disabled_nin_provider_backend=namespace["DISABLED_NIN_PROVIDER_BACKEND"],
            identity_allow_simulated_provider=namespace["IDENTITY_ALLOW_SIMULATED_PROVIDER"],
            ministry_api={
                "base_url": namespace["MINISTRY_NIN_API_BASE_URL"] or "",
                "auth_path": namespace["MINISTRY_NIN_API_AUTH_PATH"] or "",
                "lookup_path_template": namespace["MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE"] or "",
                "username": namespace["MINISTRY_NIN_API_USERNAME"] or "",
                "password": namespace["MINISTRY_NIN_API_PASSWORD"] or "",
                "token_path": namespace["MINISTRY_NIN_API_AUTH_TOKEN_PATH"] or "",
                "expiry_path": namespace["MINISTRY_NIN_API_AUTH_EXPIRY_PATH"] or "",
                "expiry_format": namespace["MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT"] or "",
                "token_cache_seconds": namespace["MINISTRY_NIN_API_TOKEN_CACHE_SECONDS"],
                "connect_timeout_seconds": namespace["MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS"],
                "read_timeout_seconds": namespace["MINISTRY_NIN_API_READ_TIMEOUT_SECONDS"],
                "total_timeout_seconds": namespace["MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS"],
                "max_response_bytes": namespace["MINISTRY_NIN_API_MAX_RESPONSE_BYTES"],
                "ca_bundle": namespace["MINISTRY_NIN_API_CA_BUNDLE"] or "",
            },
            identity_encryption_active_versions=namespace["IDENTITY_ENCRYPTION_ACTIVE_VERSIONS"],
            identity_encryption_write_version=namespace["IDENTITY_ENCRYPTION_WRITE_VERSION"],
            identity_encryption_keys_by_version=namespace["IDENTITY_ENCRYPTION_KEYS_BY_VERSION"],
            identity_blind_index_hmac_active_versions=namespace[
                "IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS"
            ],
            identity_blind_index_hmac_write_version=namespace[
                "IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION"
            ],
            identity_blind_index_hmac_keys_by_version=namespace[
                "IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION"
            ],
        )
    )
