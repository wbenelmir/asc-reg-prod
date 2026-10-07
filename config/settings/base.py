"""Shared Django settings.

Every environment-specific module (`local.py`, `test.py`, `staging.py`,
`production.py`) imports `*` from this module and then overrides only what
genuinely differs. Values are read exclusively from `os.environ` through the
helpers in `config/settings/_env.py` -- Django never reads `.env` itself,
and no dotenv dependency exists in this project (accepted plan §5.8).

`AUTH_USER_MODEL` is set to `accounts.OperationalUser` as of Prompt 3
(ADR-0011). `accounts.0001` is the first project migration in the whole
graph, introducing `OperationalUser` alone, before any other project
migration exists.
"""

from __future__ import annotations

from pathlib import Path

from ._env import env, env_bool, env_int, env_list, env_versioned_keys, require

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------

SECRET_KEY = require("DJANGO_SECRET_KEY")

DEBUG = False  # each environment module sets this explicitly; base fails closed

ALLOWED_HOSTS: list[str] = env_list("DJANGO_ALLOWED_HOSTS")

# Custom operational user (Schema §12.1, ADR-0011). The read-only Prompt 3
# pre-flight gate ran and confirmed a clean pre-Prompt-3 database state
# BEFORE this was set and before `apps.accounts.OperationalUser` was
# defined. `accounts.0001` (OperationalUser alone) is the first project
# migration in the whole graph.
AUTH_USER_MODEL = "accounts.OperationalUser"

# Business entities use application-generated UUIDv7 primary keys (Schema
# §3.1, accepted plan §6 C2), assigned explicitly on each model in Prompt 3.
# This setting only silences Django's system-check warning for the rare
# model that has no explicit primary key declared; no Phase 1 model relies
# on it.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Project apps (TRD §13.1 application boundaries; accepted plan §6.1).
    # Every app below currently has an empty `models/` package: no project
    # model and no project migration exists until Prompt 3.
    "apps.core",
    "apps.accounts",
    "apps.events",
    "apps.people",
    "apps.organizations",
    "apps.registrations",
    "apps.documents",
    "apps.privacy",
    "apps.communications",
    "apps.audit",
    # Phase 2 Prompt 2: organization invitations, delegation import, and
    # authorized on-behalf registration (TRD §13.1 names this application).
    "apps.invitations",
    # Phase 2 Prompt 3: operations back-office review, additional-information
    # requests, duplicate-candidate review, and decision history (TRD §13.1
    # names this application).
    "apps.reviews",
    # Phase 2 Prompt 4: configurable participant-role/badge-type/access-
    # profile/access-rule reference data and registration-scoped assignment
    # history (TRD §13.1 names this application).
    "apps.accreditation",
    # Phase 2 Prompt 5: purpose-bound controlled exports.
    "apps.exports",
    # Phase 3 Prompt 2: Digital Entry Pass credentials, their signed QR
    # contract, and the public verification-key set (ADR-0017). Physical
    # badge stock, entry devices, and entry events are NOT part of this app
    # yet -- they belong to later Phase 3 prompts.
    "apps.badges",
    # Phase 3 Prompt 4: entry devices, checkpoint sessions, online
    # verification, security restrictions, overrides, and Entry Events
    # (TRD §13.1 names this application; ADR-0021). Phase 4 Prompt 2 adds
    # enrolled-device offline PREPARATION (ADR-0023): Offline Packages, device
    # keys, operator grants. No synchronization and no Event Edge concept.
    "apps.entry",
    # Phase 4 Prompt 2 (binding decision P2-A): Django REST Framework, used
    # ONLY for the device API boundary (`/entry/api/v1/`). HTML views stay
    # ordinary Django views.
    "rest_framework",
    # Staff sign-in image CAPTCHA: django-simple-captcha provides only the
    # challenge store (`captcha_captchastore`) and the image renderer; its
    # URLconf is NOT included (apps.accounts.captcha_guard verifies).
    "captcha",
]

MIDDLEWARE = [
    # P4-4: drops `X-Forwarded-Proto` unless the direct peer is a trusted
    # proxy, before SecurityMiddleware reads it (SECURE_PROXY_SSL_HEADER).
    "apps.core.middleware.security_headers.ForwardedProtoGuardMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.security_headers.ContentSecurityPolicyMiddleware",
    "apps.core.middleware.private_pages.PrivatePageProtectionMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "apps.core.middleware.correlation.CorrelationIdMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "apps.accounts.middleware.OperationalSessionExpiryMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "apps.core.context_processors.participant_session",
                "apps.core.context_processors.site_metadata",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database -- native PostgreSQL 17.11, localhost:5433, asc2026_dev
# (accepted plan §5.10). No SQLite anywhere in this project: partial unique
# indexes, JSONB and advisory locks cannot be represented faithfully by it.
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": require("DATABASE_HOST"),
        "PORT": require("DATABASE_PORT"),
        "NAME": require("DATABASE_NAME"),
        "USER": require("DATABASE_USER"),
        # Not required here: a migration process (config.settings.*_migration)
        # connects with the migration owner and has no runtime password.
        # local.py and test.py require it; the deployed runtime settings
        # validate it (config/settings/validation.py).
        "PASSWORD": env("DATABASE_PASSWORD", default=""),
        "CONN_MAX_AGE": env_int("DATABASE_CONN_MAX_AGE", default=0),
    }
}

# ---------------------------------------------------------------------------
# Password validation (operational users only; participants use passwordless
# email OTP -- TRD §16.1, Prompt 3+).
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalization -- English, French, Arabic with full RTL support
# (PRD/TRD/UI-UX baseline; Schema §3.4).
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"  # all timestamps stored UTC; rendered in event-local timezone (Schema DATA-007)
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("en", "English"),
    ("fr", "Français"),
    ("ar", "العربية"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]

# ---------------------------------------------------------------------------
# Static files. No JavaScript build step exists in Phase 1 (accepted plan
# §4), so there is no `static/dist/`. `staticfiles/` (STATIC_ROOT) is the
# generated `collectstatic` output and is gitignored.
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# ---------------------------------------------------------------------------
# Private file storage (Restricted classification -- Schema §17.2, SEC-006).
# `PrivateStorage` has no public-URL method; access is authorization-
# controlled exclusively through application views (Prompt 4). See
# apps/documents/storage.py and ADR-0013.
# ---------------------------------------------------------------------------

PRIVATE_STORAGE_BACKEND = "filesystem"  # "filesystem" (local/test) | "s3" (staging/production)
PRIVATE_STORAGE_ROOT = BASE_DIR / "var" / "private"

S3_STORAGE_BUCKET_NAME = env("S3_STORAGE_BUCKET_NAME")
S3_STORAGE_ENDPOINT_URL = env("S3_STORAGE_ENDPOINT_URL")
S3_STORAGE_ACCESS_KEY_ID = env("S3_STORAGE_ACCESS_KEY_ID")
S3_STORAGE_SECRET_ACCESS_KEY = env("S3_STORAGE_SECRET_ACCESS_KEY")
# boto3 requires a region for SigV4 request signing even against an
# S3-compatible provider that otherwise ignores it -- required by the
# adapter, so it is validated in staging/production rather than assumed.
S3_STORAGE_REGION_NAME = env("S3_STORAGE_REGION_NAME")

# ---------------------------------------------------------------------------
# Local fake delivery sink (accepted plan §5.7). Django's own file-based
# email backend, rooted OUTSIDE every served path, under gitignored
# `var/mail/`. Selected only by local.py/test.py; validation.py (staging and
# production) fails closed if EMAIL_BACKEND equals this value. No OTP domain
# exists yet in Prompt 2 -- this is the delivery boundary only.
# ---------------------------------------------------------------------------

LOCAL_OTP_SINK_EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
LOCAL_MAIL_SINK_ROOT = BASE_DIR / "var" / "mail"

# ---------------------------------------------------------------------------
# Communications adapters (Phase 2 Prompt 5 §4.4). SMS is disabled by
# default everywhere -- no provider has been selected or approved; enabling
# it here would still not select a real provider (`apps.communications.
# adapters.sms.NullSmsAdapter` always fails). Deterministic email-failure
# simulation is a TEST-ONLY convenience (`apps.communications.adapters.
# email.DjangoEmailAdapter`) and is explicitly `False` here so staging/
# production never inherit it by omission -- only `local.py`/`test.py`
# override it to `True`.
# ---------------------------------------------------------------------------

SMS_ENABLED = env_bool("SMS_ENABLED", default=False)
COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION = False

# Each environment module sets the backend explicitly; the deployed settings
# use Django's SMTP backend configured from EMAIL_* variables (_deployment.py).
EMAIL_BACKEND = env("EMAIL_BACKEND")

# ---------------------------------------------------------------------------
# Celery. Local/test run in eager mode with no broker and no worker process
# (accepted plan §5.4, ADR-0009). Staging/production require a real broker;
# validation.py fails closed without it.
# ---------------------------------------------------------------------------

CELERY_TASK_ALWAYS_EAGER = False  # local.py / test.py override to True
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_BROKER_URL = env("CELERY_BROKER_URL")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"

REDIS_URL = env("REDIS_URL")

# Periodic maintenance (P4-4). Declarative only: it takes effect where the
# deployment runs `celery beat`, which is a P4-5 runbook decision; locally
# nothing is scheduled (ADR-0009) and the management commands remain the
# manual triggers. Every task listed is idempotent and deletes only expired,
# non-participant rows or persists already-effective expiry. No retention job
# for participant data is scheduled: the periods are not approved (OD-007,
# C-09), and the delegation purge keeps its manual command for that reason.
CELERY_BEAT_SCHEDULE = {
    "entry-expire-access": {"task": "entry.expire_entry_access", "schedule": 5 * 60},
    "entry-cleanup-offline-packages": {
        "task": "entry.cleanup_offline_packages",
        "schedule": 15 * 60,
    },
    "entry-prune-verification-samples": {
        "task": "entry.prune_verification_samples",
        "schedule": 60 * 60,
    },
    "communications-dispatch-pending": {
        "task": "apps.communications.tasks.dispatch_pending_communication_events",
        "schedule": 5 * 60,
    },
    "communications-redeliver-overdue-deferred": {
        "task": "communications.redeliver_overdue_deferred_messages",
        "schedule": 5 * 60,
    },
    "core-clear-expired-sessions": {"task": "core.clear_expired_sessions", "schedule": 60 * 60},
    "core-purge-human-challenge-uses": {
        "task": "core.purge_human_challenge_uses",
        "schedule": 60 * 60,
    },
    # Expired staff sign-in CAPTCHA challenges (bounded batches).
    "accounts-purge-expired-staff-captchas": {
        "task": "accounts.purge_expired_staff_captchas",
        "schedule": 15 * 60,
    },
    # IDV-2 (A13-14): recovers identity jobs left by a broker outage or a dead
    # worker. Idempotent: a job is claimed with a lease before any provider call.
    "people-dispatch-due-identity-verification-jobs": {
        "task": "people.dispatch_due_identity_verification_jobs",
        "schedule": 60,
    },
}

# ---------------------------------------------------------------------------
# Django cache framework. No lock, idempotency or authorization state lives
# in the cache (accepted plan §5.4), and the OTP and operational sign-in
# limits are database-backed (§5.5). P4-4 kept the ALTCHA challenge-endpoint
# counter here, per process and failing open; since P4-4-C3 (CACHE-01) it
# has its own store (`apps.core.issuance_counter`, HUMAN_CHECK_COUNTER_STORE),
# so no project code uses this cache, and nothing reports it as Redis.
# ---------------------------------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# ---------------------------------------------------------------------------
# Operational authentication. Owner decision MFA-01 (revised 2026-10-01,
# amendment A-11, superseding the sign-in MFA of TRD §16.1 / FR-AUTH-009):
# operational users sign in with email and password in staging and
# production. `MFA_BACKEND` is now only the step-up provider for sensitive
# operations (the emergency device wipe), which fail closed without it.
# It is optional everywhere; `entry.W001` (`check --deploy`) says when it is
# missing and the wipe is therefore unavailable.
# ---------------------------------------------------------------------------

MFA_BACKEND = env("MFA_BACKEND")

# Operational sign-in attempt limits (P4-4, `apps.accounts.operational_sign_in`).
# Failed attempts per typed email (since its last success) and per client
# network, in one fixed look-back window. Counted from the audit trail.
OPERATIONAL_SIGN_IN_WINDOW_SECONDS = env_int("OPERATIONAL_SIGN_IN_WINDOW_SECONDS", default=900)
OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL = env_int(
    "OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_EMAIL", default=10
)
OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK = env_int(
    "OPERATIONAL_SIGN_IN_MAX_FAILURES_PER_NETWORK", default=50
)

# Staff credential setup and password reset links (`apps.accounts.
# administration`): single use, only a digest stored, replaced by a newer
# link, and valid for this many seconds.
OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS = env_int(
    "OPERATIONAL_CREDENTIAL_SETUP_TTL_SECONDS", default=48 * 60 * 60
)

# Staff sign-in image CAPTCHA (`apps.accounts.captcha_guard`): self-hosted
# django-simple-captcha, session- and purpose-bound, one attempt per image,
# atomic consumption. Answers come from `secrets`; there is no test answer and
# no audio variant. The participant OTP keeps its ALTCHA proof of work.
STAFF_CAPTCHA_LENGTH = env_int("STAFF_CAPTCHA_LENGTH", default=5)
CAPTCHA_TIMEOUT = env_int("STAFF_CAPTCHA_TIMEOUT_MINUTES", default=5)  # minutes
CAPTCHA_CHALLENGE_FUNCT = "apps.accounts.captcha_guard.challenge"
CAPTCHA_IMAGE_SIZE = (180, 56)
CAPTCHA_FONT_SIZE = 32
CAPTCHA_LETTER_ROTATION = (-20, 20)
CAPTCHA_FOREGROUND_COLOR = "#102b3a"
CAPTCHA_2X_IMAGE = False
CAPTCHA_TEST_MODE = False  # never accept the package's test answer
CAPTCHA_FLITE_PATH = None  # no audio variant (no external tool)
CAPTCHA_SOX_PATH = None

# ---------------------------------------------------------------------------
# Malware scanning (SEC-006). `ScannerAdapter` interface with a deterministic
# stub locally/test; staging and production require the ClamAV clamd adapter
# (`apps.documents.scanning.ClamdScanner`, validated in validation.py). The
# stubs are refused at runtime unless MALWARE_SCANNER_ALLOW_LOCAL_STUBS is
# True, which only local.py and test.py set.
# ---------------------------------------------------------------------------

LOCAL_MALWARE_SCANNER_BACKEND = "apps.documents.scanning.DeterministicStubScanner"
CLAMD_SCANNER_BACKEND = "apps.documents.scanning.ClamdScanner"
MALWARE_SCANNER_BACKEND = env("MALWARE_SCANNER_BACKEND", default=LOCAL_MALWARE_SCANNER_BACKEND)
MALWARE_SCANNER_ALLOW_LOCAL_STUBS = False

# `manage.py provision_staging_uat` runs only where this is True: the staging
# runtime settings and the test settings. Production refuses it at startup.
STAGING_PROVISIONING_ENABLED = False

# clamd connection: a Unix socket path, or a private TCP host and port (exactly
# one). The scan timeout bounds the whole exchange once connected. The stream
# limit must not exceed clamd's own StreamMaxLength and must cover the largest
# accepted upload (validation.py checks the second condition).
CLAMD_SOCKET_PATH = env("CLAMD_SOCKET_PATH", default="")
CLAMD_HOST = env("CLAMD_HOST", default="")
CLAMD_PORT = env_int("CLAMD_PORT", default=3310)
CLAMD_CONNECT_TIMEOUT_SECONDS = env_int("CLAMD_CONNECT_TIMEOUT_SECONDS", default=5)
CLAMD_SCAN_TIMEOUT_SECONDS = env_int("CLAMD_SCAN_TIMEOUT_SECONDS", default=60)
CLAMD_MAX_STREAM_BYTES = env_int("CLAMD_MAX_STREAM_BYTES", default=25 * 1024 * 1024)

# ---------------------------------------------------------------------------
# Profile-photograph validation (Prompt 4 final closure pass §4). JPEG/PNG
# only, bounded size, and a sane minimum resolution -- enforced in
# `apps.documents.services.save_profile_photo` before the malware scan and
# before anything is written to `PrivateStorage`.
# ---------------------------------------------------------------------------

PROFILE_PHOTO_MAX_SIZE_BYTES = env_int("PROFILE_PHOTO_MAX_SIZE_BYTES", default=5 * 1024 * 1024)
PROFILE_PHOTO_MIN_WIDTH_PX = env_int("PROFILE_PHOTO_MIN_WIDTH_PX", default=200)
PROFILE_PHOTO_MIN_HEIGHT_PX = env_int("PROFILE_PHOTO_MIN_HEIGHT_PX", default=200)

# ---------------------------------------------------------------------------
# Passport identity-page upload (Prompt 5 correction pass §2, REG-03).
# Disabled by default at the EventEdition level (see
# `EventEdition.passport_identity_page_upload_enabled`) -- these bounds
# apply only when that policy switch is on. A scanned document page is
# typically larger than a face photo, hence the larger default ceiling; no
# minimum-dimension check applies (unlike the profile photo), since a
# legitimate identity-page scan/photo can legitimately be small or large.
# ---------------------------------------------------------------------------

PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES = env_int(
    "PASSPORT_IDENTITY_PAGE_MAX_SIZE_BYTES", default=8 * 1024 * 1024
)

# ---------------------------------------------------------------------------
# Delegation CSV import (Phase 2 Prompt 2, Schema §6.5, FR-DEL-003).
# ---------------------------------------------------------------------------

DELEGATION_CSV_MAX_SIZE_BYTES = env_int("DELEGATION_CSV_MAX_SIZE_BYTES", default=2 * 1024 * 1024)
DELEGATION_CSV_MAX_ROWS = env_int("DELEGATION_CSV_MAX_ROWS", default=2000)
# Retention/purge lifecycle (Phase 2 Prompt 2 correction pass): the
# uploaded source CSV's bytes and each row's staged encrypted candidate
# email are purged this long after upload -- durations are implementation
# defaults, not a legal/retention-policy decision (that remains Phase 2+
# `RetentionPolicy` scope per docs/specifications §14.5).
DELEGATION_SOURCE_RETENTION_SECONDS = env_int(
    "DELEGATION_SOURCE_RETENTION_SECONDS", default=30 * 24 * 60 * 60
)
DELEGATION_ROW_RETENTION_SECONDS = env_int(
    "DELEGATION_ROW_RETENTION_SECONDS", default=90 * 24 * 60 * 60
)

# On-behalf claim link lifetime (AF-ORG-03/04, Phase 2 Prompt 2).
ON_BEHALF_CLAIM_TTL_SECONDS = env_int("ON_BEHALF_CLAIM_TTL_SECONDS", default=14 * 24 * 60 * 60)

# Absolute base URL for links generated OUTSIDE an HTTP request context
# (Phase 2 Prompt 2 V2 correction pass "make delegated-claim delivery
# durable and recoverable"): `apply_delegation_batch` is a service
# function with no `request` to call `build_absolute_uri` against, since
# delegation delivery/retry can run from a management command. No
# trailing slash. Local default matches the local dev server; staging/
# production MUST set this explicitly via `DJANGO_PUBLIC_BASE_URL`.
PUBLIC_BASE_URL = env("DJANGO_PUBLIC_BASE_URL", default="http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------------------
# Participant and operational session expiry (Prompt 5 correction pass §4,
# AF-AUTH-04, UI/UX §6.5). Independently configurable per audience:
# inactivity (idle) lifetime, absolute lifetime (never extended by
# activity), and the warning interval before either deadline. See
# `apps/accounts/session_expiry.py`.
# ---------------------------------------------------------------------------

PARTICIPANT_SESSION_INACTIVITY_SECONDS = env_int(
    "PARTICIPANT_SESSION_INACTIVITY_SECONDS", default=30 * 60
)
PARTICIPANT_SESSION_ABSOLUTE_SECONDS = env_int(
    "PARTICIPANT_SESSION_ABSOLUTE_SECONDS", default=8 * 60 * 60
)
PARTICIPANT_SESSION_WARNING_SECONDS = env_int("PARTICIPANT_SESSION_WARNING_SECONDS", default=120)

OPERATIONAL_SESSION_INACTIVITY_SECONDS = env_int(
    "OPERATIONAL_SESSION_INACTIVITY_SECONDS", default=15 * 60
)
OPERATIONAL_SESSION_ABSOLUTE_SECONDS = env_int(
    "OPERATIONAL_SESSION_ABSOLUTE_SECONDS", default=8 * 60 * 60
)
OPERATIONAL_SESSION_WARNING_SECONDS = env_int("OPERATIONAL_SESSION_WARNING_SECONDS", default=120)

# ---------------------------------------------------------------------------
# National identity number (NIN) lookup provider (TRD §15.3; IDV-2, amendment
# A-13, ADR-0026). Verification runs after final submission, from a durable
# job, in the worker -- never in a request or inside a database transaction.
#
# Backends: the official ministry adapter; the DISABLED backend (the default
# here: every Algerian case goes to manual review); and the development
# simulation (local and test only). Staging and production refuse every
# backend other than the official and the disabled one at startup
# (validation.py, A13-06), and the worker refuses a simulation at run time
# unless IDENTITY_ALLOW_SIMULATED_PROVIDER is on.
# ---------------------------------------------------------------------------

OFFICIAL_NIN_PROVIDER_BACKEND = "apps.people.nin_provider.MinistryNinProvider"
DISABLED_NIN_PROVIDER_BACKEND = "apps.people.nin_provider.DisabledNinProvider"
LOCAL_NIN_PROVIDER_BACKEND = "apps.people.nin_provider.LocalSimulationNinProvider"
NIN_PROVIDER_BACKEND = env("NIN_PROVIDER_BACKEND", default=DISABLED_NIN_PROVIDER_BACKEND)
IDENTITY_ALLOW_SIMULATED_PROVIDER = False

# Ministry adapter (docs/security/identity_provider_contract.md). The request
# paths and the bearer scheme are observed facts. The authentication RESPONSE
# is not supplied (API-01): the token path, and optionally an expiry path and
# format, are configuration, validated explicitly. Until every required value
# is present the adapter is unavailable and cases go to manual review.
MINISTRY_NIN_API_BASE_URL = env("MINISTRY_NIN_API_BASE_URL", default="")
MINISTRY_NIN_API_AUTH_PATH = env("MINISTRY_NIN_API_AUTH_PATH", default="/api/auth/")
MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE = env(
    "MINISTRY_NIN_API_LOOKUP_PATH_TEMPLATE", default="/api/get/{nin}"
)
MINISTRY_NIN_API_USERNAME = env("MINISTRY_NIN_API_USERNAME", default="")
MINISTRY_NIN_API_PASSWORD = env("MINISTRY_NIN_API_PASSWORD", default="")
MINISTRY_NIN_API_AUTH_TOKEN_PATH = env("MINISTRY_NIN_API_AUTH_TOKEN_PATH", default="")
MINISTRY_NIN_API_AUTH_EXPIRY_PATH = env("MINISTRY_NIN_API_AUTH_EXPIRY_PATH", default="")
MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT = env("MINISTRY_NIN_API_AUTH_EXPIRY_FORMAT", default="")
# A conservative LOCAL client policy, not a provider expiry claim: the token
# is reused for at most this long (and less when an expiry is configured).
MINISTRY_NIN_API_TOKEN_CACHE_SECONDS = env_int("MINISTRY_NIN_API_TOKEN_CACHE_SECONDS", default=300)
MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS = env_int(
    "MINISTRY_NIN_API_CONNECT_TIMEOUT_SECONDS", default=5
)
MINISTRY_NIN_API_READ_TIMEOUT_SECONDS = env_int("MINISTRY_NIN_API_READ_TIMEOUT_SECONDS", default=10)
MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS = env_int(
    "MINISTRY_NIN_API_TOTAL_TIMEOUT_SECONDS", default=20
)
MINISTRY_NIN_API_MAX_RESPONSE_BYTES = env_int("MINISTRY_NIN_API_MAX_RESPONSE_BYTES", default=65536)
MINISTRY_NIN_API_CA_BUNDLE = env("MINISTRY_NIN_API_CA_BUNDLE", default="")
# Ministry NIN diagnostics page (`apps.people.nin_diagnostics`): network checks
# per operator and per environment in each window, counted on the shared
# counter; one check runs at a time.
NIN_DIAGNOSTICS_MAX_PER_OPERATOR = env_int("NIN_DIAGNOSTICS_MAX_PER_OPERATOR", default=5)
NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT = env_int("NIN_DIAGNOSTICS_MAX_PER_ENVIRONMENT", default=20)
NIN_DIAGNOSTICS_WINDOW_SECONDS = env_int("NIN_DIAGNOSTICS_WINDOW_SECONDS", default=600)

# Identity verification worker policy (engineering defaults, ADR-0026). The
# owner reports no provider usage limits (A13-11); these bounds apply anyway.
IDENTITY_PROVIDER_MAX_CONCURRENCY = env_int("IDENTITY_PROVIDER_MAX_CONCURRENCY", default=4)
IDENTITY_PROVIDER_SLOT_RETRY_SECONDS = 15
IDENTITY_VERIFICATION_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600)
IDENTITY_VERIFICATION_LEASE_SECONDS = 180
# After-commit enqueueing on the broker. Off in local/test eager mode, where
# `manage.py run_identity_verification_jobs` processes jobs, so the provider is
# never called inside a participant's request even there.
IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT = True

# ---------------------------------------------------------------------------
# Trusted-proxy / client-network identity (accepted plan §5.5). Local/test
# ignore forwarded headers entirely and use the direct peer. Staging/
# production may honor X-Forwarded-For only behind a configured trusted-
# proxy CIDR boundary; validation.py fails closed otherwise.
# ---------------------------------------------------------------------------

TRUSTED_PROXY_FORWARDING_ENABLED = env_bool("TRUSTED_PROXY_FORWARDING_ENABLED", default=False)
TRUSTED_PROXY_CIDRS = env_list("TRUSTED_PROXY_CIDRS")
TRUSTED_PROXY_MAX_FORWARDED_HOPS = env_int("TRUSTED_PROXY_MAX_FORWARDED_HOPS", default=5)

# ---------------------------------------------------------------------------
# Rate-limit HMAC key rotation (accepted plan §5.5). Separate from the
# identity blind-index keys of ContactPoint/IdentityIdentifier (Prompt 3).
# The OTP domain itself (AuthenticationChallenge, advisory-lock throttling)
# is implemented in Prompt 3; these settings exist now so that static
# configuration validation (§5.6 A) has something concrete to check in
# staging/production from Prompt 2 onward.
# ---------------------------------------------------------------------------


def _parse_rate_limit_version(raw: str) -> int:
    """Parse one configured version, never raising.

    A non-numeric entry becomes the sentinel `-1`, which the "must be a
    positive integer" static-validation check (accepted plan §5.5,
    Prompt 2 correction §2) then reports cleanly by name -- rather than
    this module-level list comprehension crashing with a raw `ValueError`
    for every settings module, including local/test, before validation
    ever gets a chance to produce a clear, non-secret error.
    """
    try:
        return int(raw)
    except ValueError:
        return -1


RATE_LIMIT_HMAC_ACTIVE_VERSIONS = [
    _parse_rate_limit_version(v)
    for v in env_list("RATE_LIMIT_HMAC_ACTIVE_VERSIONS", default=("1",))
]
RATE_LIMIT_HMAC_WRITE_VERSION = env_int("RATE_LIMIT_HMAC_WRITE_VERSION", default=1)
RATE_LIMIT_KEY_OVERLAP_SECONDS = env_int("RATE_LIMIT_KEY_OVERLAP_SECONDS", default=3600)

# One key variable per active version (`RATE_LIMIT_HMAC_KEY_V1`,
# `RATE_LIMIT_HMAC_KEY_V2`, ...) -- the exact set of names to look up
# depends on the dynamic, configured version list above. A missing value
# is `None` here; static validation reports it by name in staging/
# production (Prompt 2 correction §2). Never logged or printed as a dict.
RATE_LIMIT_HMAC_KEYS_BY_VERSION = env_versioned_keys(
    "RATE_LIMIT_HMAC_KEY_V", RATE_LIMIT_HMAC_ACTIVE_VERSIONS
)

# Components of the minimum required retirement overlap (accepted plan
# §5.5): max(OTP lifetime, resend cooldown, temporary-lock duration, every
# rate-limit window). Concrete values are owned by the OTP domain (Prompt 3);
# conservative defaults are declared here so validation has a real minimum
# to compare against starting in Prompt 2.
OTP_LIFETIME_SECONDS = env_int("OTP_LIFETIME_SECONDS", default=600)
OTP_RESEND_COOLDOWN_SECONDS = env_int("OTP_RESEND_COOLDOWN_SECONDS", default=60)
OTP_TEMPORARY_LOCK_SECONDS = env_int("OTP_TEMPORARY_LOCK_SECONDS", default=900)
RATE_LIMIT_WINDOW_SECONDS = env_int("RATE_LIMIT_WINDOW_SECONDS", default=3600)

# Issuance throttle ceilings within one RATE_LIMIT_WINDOW_SECONDS window
# (ADR-0007). Recipient and network throttles are independent counters.
OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW = env_int(
    "OTP_MAX_ISSUANCES_PER_RECIPIENT_WINDOW", default=5
)
OTP_MAX_ISSUANCES_PER_NETWORK_WINDOW = env_int("OTP_MAX_ISSUANCES_PER_NETWORK_WINDOW", default=20)

# Self-hosted ALTCHA human check on the OTP request form (UX-4, M01, D-13,
# S-16; UX-C2 decision UX-D01 option M; `apps.core.human_check`). It
# supplements the throttles above. The widget searches counters whose
# PBKDF2/SHA-256 key (HUMAN_CHECK_COST iterations) starts with one zero byte:
# about 256 derivations on average. The default cost comes from the UX-C2
# measurement on local headless Chromium (the UX-C2 measurement record);
# it is NOT a low-end-phone measurement. The challenge expires after
# HUMAN_CHECK_TTL_SECONDS. HUMAN_CHECK_ENABLED=false is the documented
# recovery switch only: it needs deployment access, logs a warning on every
# skipped check and raises system check `core.W001`.
HUMAN_CHECK_ENABLED = env_bool("HUMAN_CHECK_ENABLED", default=True)
HUMAN_CHECK_ALGORITHM = "PBKDF2/SHA-256"  # the only vendored worker
HUMAN_CHECK_COST = env_int("HUMAN_CHECK_COST", default=5_000)
HUMAN_CHECK_TTL_SECONDS = env_int("HUMAN_CHECK_TTL_SECONDS", default=900)
# P4-4 challenge-endpoint limits. At most this many challenges per client
# network (IPv4 address or IPv6 /64) per window; a refused request touches no
# session. A normal visitor needs one per page load plus one per expiry. See
# `apps.core.human_check.challenge_issuance_allowed`.
HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS = env_int("HUMAN_CHECK_CHALLENGE_WINDOW_SECONDS", default=600)
HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW = env_int("HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW", default=60)
# P4-4-C3 (CACHE-01, ADR-0025): where that counter lives. "local" is a
# per-process counter for local development and tests, which need no Redis;
# staging and production set "redis" (the shared counter in the `REDIS_URL`
# database) and validation refuses anything else there. When the shared
# counter fails, each process counts with the stricter fallback limit below
# (at most this many per network per window per process) and tries Redis
# again after the retry interval. The fallback tracks at most
# HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS networks, then counts every further
# network in one shared overflow bucket. These are provisional engineering
# defaults, not owner-approved capacity figures.
HUMAN_CHECK_COUNTER_STORE = "local"
HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW = env_int(
    "HUMAN_CHECK_CHALLENGE_FALLBACK_MAX_PER_WINDOW", default=20
)
HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS = env_int(
    "HUMAN_CHECK_COUNTER_FALLBACK_MAX_NETWORKS", default=10_000
)
HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS = env_int(
    "HUMAN_CHECK_COUNTER_CONNECT_TIMEOUT_MS", default=500
)
HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS = env_int(
    "HUMAN_CHECK_COUNTER_SOCKET_TIMEOUT_MS", default=500
)
HUMAN_CHECK_COUNTER_RETRY_SECONDS = env_int("HUMAN_CHECK_COUNTER_RETRY_SECONDS", default=15)
HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS = env_int(
    "HUMAN_CHECK_COUNTER_ALERT_INTERVAL_SECONDS", default=300
)
# A session created only for a challenge nonce expires after this much
# inactivity: long enough to finish the check and use a code.
HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS = env_int(
    "HUMAN_CHECK_ANONYMOUS_SESSION_SECONDS",
    default=HUMAN_CHECK_TTL_SECONDS + OTP_LIFETIME_SECONDS + 5 * 60,
)

RATE_LIMIT_MINIMUM_REQUIRED_OVERLAP_SECONDS = max(
    OTP_LIFETIME_SECONDS,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_TEMPORARY_LOCK_SECONDS,
    RATE_LIMIT_WINDOW_SECONDS,
)

# ---------------------------------------------------------------------------
# Identity field encryption and blind-index HMAC keys (ADR-0006, Prompt 3).
# Two independent versioned-key families, both entirely separate from the
# rate-limit HMAC key family above: rotating one never disturbs another.
# Development/staging keys are supplied through the environment only
# (`apps.core.crypto.EnvKeyProvider`); no key ever enters application
# tables or source control (Schema §17.3).
# ---------------------------------------------------------------------------

IDENTITY_ENCRYPTION_ACTIVE_VERSIONS = [
    _parse_rate_limit_version(v)
    for v in env_list("IDENTITY_ENCRYPTION_ACTIVE_VERSIONS", default=("1",))
]
IDENTITY_ENCRYPTION_WRITE_VERSION = env_int("IDENTITY_ENCRYPTION_WRITE_VERSION", default=1)
IDENTITY_ENCRYPTION_KEYS_BY_VERSION = env_versioned_keys(
    "IDENTITY_ENCRYPTION_KEY_V", IDENTITY_ENCRYPTION_ACTIVE_VERSIONS
)

IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS = [
    _parse_rate_limit_version(v)
    for v in env_list("IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS", default=("1",))
]
IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION = env_int(
    "IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION", default=1
)
IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION = env_versioned_keys(
    "IDENTITY_BLIND_INDEX_HMAC_KEY_V", IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS
)

# ---------------------------------------------------------------------------
# Digital Entry Pass ES256 signing keys (Phase 3 Prompt 2, ADR-0019). A
# FOURTH independent versioned-key family, deliberately separate from the
# three above: rotating a QR signing key must never disturb identity
# encryption, identity blind indexes, or rate-limit fingerprints, and vice
# versa (the same separation rationale as ADR-0006/ADR-0007).
#
# `QR_SIGNING_KEY_V<n>` holds a PEM-encoded P-256 PRIVATE key supplied
# through the environment only. It is read exclusively by
# `apps.core.crypto.signing.EnvSigningKeyProvider`, which never returns
# private bytes to a caller. Production is expected to substitute a managed
# KMS/HSM provider implementing the same interface; that substitution is
# deployment-only and is not exercised locally.
#
# The public half of each key is published through `badges.VerificationKey`,
# which stores public key material only.
# ---------------------------------------------------------------------------

QR_SIGNING_ACTIVE_KEY_VERSIONS = [
    _parse_rate_limit_version(v) for v in env_list("QR_SIGNING_ACTIVE_KEY_VERSIONS", default=("1",))
]
QR_SIGNING_WRITE_KEY_VERSION = env_int("QR_SIGNING_WRITE_KEY_VERSION", default=1)
QR_SIGNING_KEYS_BY_VERSION = env_versioned_keys("QR_SIGNING_KEY_V", QR_SIGNING_ACTIVE_KEY_VERSIONS)

# ---------------------------------------------------------------------------
# Digital Entry Pass credential policy (Phase 3 Prompt 2). These are
# configuration boundaries, not admission rules: they bound how a
# credential is issued and parsed, never who may enter.
# ---------------------------------------------------------------------------

#: Payload schema version written into new credentials, and the bounded set
#: a verifier will accept. An unsupported version is rejected outright.
QR_PAYLOAD_VERSION = env_int("QR_PAYLOAD_VERSION", default=1)
QR_SUPPORTED_PAYLOAD_VERSIONS = [
    _parse_rate_limit_version(v) for v in env_list("QR_SUPPORTED_PAYLOAD_VERSIONS", default=("1",))
]

#: Hard upper bound on an accepted QR string, enforced before any decoding
#: so an oversized input can never reach the parser.
QR_MAX_TOKEN_BYTES = env_int("QR_MAX_TOKEN_BYTES", default=1024)

#: Bounded tolerance for device/server clock differences when evaluating
#: `nbf`/`exp`. Small by default; never large enough to resurrect a
#: meaningfully expired credential.
QR_CLOCK_SKEW_SECONDS = env_int("QR_CLOCK_SKEW_SECONDS", default=60)

#: Validity window margins applied to the EventEdition window when a
#: credential is issued. Default zero on both sides (approved Phase 3
#: default): the credential is valid exactly for the configured event
#: window unless an operator configures otherwise.
PASS_VALIDITY_MARGIN_BEFORE_SECONDS = env_int("PASS_VALIDITY_MARGIN_BEFORE_SECONDS", default=0)
PASS_VALIDITY_MARGIN_AFTER_SECONDS = env_int("PASS_VALIDITY_MARGIN_AFTER_SECONDS", default=0)

#: ACTIVE-only QR exposure is a HARD INVARIANT enforced in
#: `apps.badges.services.issue_pass_token`, not a configurable policy. The
#: former `PASS_PRE_ACTIVATION_QR_ENABLED` switch was removed in the Phase 3
#: Prompt 2 correction pass: a setting that could hand a participant a usable
#: credential before activation is exactly the kind of escape hatch that
#: should not exist.

#: Controlled fallback-reference lookup limits. The reference is a locator,
#: never an authenticator, so exact-match-only lookup is additionally
#: rate-limited per operator and per device-network identity.
FALLBACK_REFERENCE_LOOKUP_WINDOW_SECONDS = env_int(
    "FALLBACK_REFERENCE_LOOKUP_WINDOW_SECONDS", default=300
)
FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW = env_int(
    "FALLBACK_REFERENCE_LOOKUP_MAX_PER_WINDOW", default=30
)

# ---------------------------------------------------------------------------
# Online entry (Phase 3 Prompt 4, ADR-0021). Session and freshness bounds
# only -- none of these values decides who may enter.
# ---------------------------------------------------------------------------

#: Lifetime of a one-time device activation code shown to the enrolling
#: administrator. Short: the code is a bearer secret until it is used.
ENTRY_DEVICE_ACTIVATION_CODE_SECONDS = env_int(
    "ENTRY_DEVICE_ACTIVATION_CODE_SECONDS", default=15 * 60
)
#: Upper bound on a device's enrollment lifetime; the per-device
#: `expires_at` is additionally capped at the event edition's end.
ENTRY_DEVICE_MAX_ENROLLMENT_DAYS = env_int("ENTRY_DEVICE_MAX_ENROLLMENT_DAYS", default=30)
#: One checkpoint set-up (gate + zone) on a device.
ENTRY_DEVICE_SESSION_SECONDS = env_int("ENTRY_DEVICE_SESSION_SECONDS", default=10 * 60 * 60)
#: One named operator's shift on a device session (absolute and idle).
ENTRY_OPERATOR_SESSION_SECONDS = env_int("ENTRY_OPERATOR_SESSION_SECONDS", default=4 * 60 * 60)
ENTRY_OPERATOR_INACTIVITY_SECONDS = env_int("ENTRY_OPERATOR_INACTIVITY_SECONDS", default=10 * 60)
#: How long a verification result may be acted on. After this, the decision
#: is refused as STALE and the operator must verify again.
ENTRY_DECISION_TICKET_SECONDS = env_int("ENTRY_DECISION_TICKET_SECONDS", default=120)
#: Participant details on a result screen are cleared after this much
#: operator inactivity (Flow §15.3).
ENTRY_RESULT_CLEAR_SECONDS = env_int("ENTRY_RESULT_CLEAR_SECONDS", default=45)
#: A previous admission within this window is flagged as "very recent".
ENTRY_RECENT_REENTRY_SECONDS = env_int("ENTRY_RECENT_REENTRY_SECONDS", default=5 * 60)
#: Hard cap on controlled manual-search results.
ENTRY_MANUAL_SEARCH_MAX_RESULTS = env_int("ENTRY_MANUAL_SEARCH_MAX_RESULTS", default=10)
ENTRY_MANUAL_SEARCH_MIN_CHARS = env_int("ENTRY_MANUAL_SEARCH_MIN_CHARS", default=3)
#: Name of the HttpOnly device-credential cookie.
ENTRY_DEVICE_COOKIE_NAME = "asc_entry_device"

# ---------------------------------------------------------------------------
# Entry UI, observability and online performance (Phase 3 Prompt 5,
# ADR-0022). Presentation, measurement and abuse-limiting bounds only --
# none of these values decides who may enter.
# ---------------------------------------------------------------------------

#: Exact request paths that are validated like any other request but are
#: NOT operator activity: they never refresh the operational or checkpoint
#: inactivity deadline (the checkpoint connection status check).
OPERATIONAL_PASSIVE_PATHS = ("/entry/status/", "/entry/api/v1/offline/heartbeat/")
#: How often an open checkpoint page re-checks its connection.
ENTRY_CONNECTION_POLL_SECONDS = env_int("ENTRY_CONNECTION_POLL_SECONDS", default=20)
#: Identity-reference lookups (NIN, passport, registration reference, manual
#: search) per operator: an anomaly signal at the threshold, refusal at the
#: maximum, within one sliding window. QR scans do not count here.
ENTRY_LOOKUP_WINDOW_SECONDS = env_int("ENTRY_LOOKUP_WINDOW_SECONDS", default=5 * 60)
ENTRY_LOOKUP_ANOMALY_THRESHOLD = env_int("ENTRY_LOOKUP_ANOMALY_THRESHOLD", default=20)
ENTRY_LOOKUP_MAX_PER_WINDOW = env_int("ENTRY_LOOKUP_MAX_PER_WINDOW", default=30)
#: Repeated invalid or unsupported QR scans per operator (valid scans are
#: never counted; once the maximum is reached, that operator's QR
#: verification pauses until the window moves on).
ENTRY_INVALID_SCAN_WINDOW_SECONDS = env_int("ENTRY_INVALID_SCAN_WINDOW_SECONDS", default=5 * 60)
ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD = env_int("ENTRY_INVALID_SCAN_ANOMALY_THRESHOLD", default=5)
ENTRY_INVALID_SCAN_MAX_PER_WINDOW = env_int("ENTRY_INVALID_SCAN_MAX_PER_WINDOW", default=15)
#: NFR-PERF-003: p95 online validation within 1.5 s. Used by the degraded
#: state, the dashboard, and the performance harness.
ENTRY_VERIFICATION_TARGET_P95_MS = env_int("ENTRY_VERIFICATION_TARGET_P95_MS", default=1500)
#: A gate is "degraded" when, over this window and with at least the minimum
#: number of samples, its p95 exceeds the target or its technical-error share
#: reaches the percentage.
ENTRY_DEGRADED_WINDOW_SECONDS = env_int("ENTRY_DEGRADED_WINDOW_SECONDS", default=5 * 60)
ENTRY_DEGRADED_MIN_SAMPLES = env_int("ENTRY_DEGRADED_MIN_SAMPLES", default=5)
ENTRY_DEGRADED_ERROR_PERCENT = env_int("ENTRY_DEGRADED_ERROR_PERCENT", default=20)
#: An enrolled device not seen for this long is reported as stale.
ENTRY_DEVICE_STALE_SECONDS = env_int("ENTRY_DEVICE_STALE_SECONDS", default=5 * 60)
#: Telemetry retention and the dashboard's default look-back window.
ENTRY_METRICS_RETENTION_DAYS = env_int("ENTRY_METRICS_RETENTION_DAYS", default=30)
ENTRY_METRICS_DEFAULT_WINDOW_SECONDS = env_int(
    "ENTRY_METRICS_DEFAULT_WINDOW_SECONDS", default=60 * 60
)

# ---------------------------------------------------------------------------
# Enrolled-device offline preparation, Plan B (Phase 4 Prompt 2, ADR-0023).
# Preparation only: no offline admission, synchronization, or Event Edge.
# ---------------------------------------------------------------------------

#: Global kill switch, OFF by default in every environment (binding decision
#: P2-E). Offline also requires per-event enablement, an offline-capable
#: device scope, completed preparation, and a passed self-test.
ENTRY_OFFLINE_ENABLED = env_bool("ENTRY_OFFLINE_ENABLED", default=False)

#: Offline Package ES256 signing keys: a FIFTH independent versioned-key
#: family (binding decision P2-D), never the QR keys. `OFFLINE_PACKAGE_
#: SIGNING_KEY_V<n>` holds a PEM P-256 PRIVATE key from the environment only,
#: read exclusively by `apps.core.crypto.package_signing`. Key identifiers
#: are `p<n>` (QR keys are `v<n>`).
OFFLINE_PACKAGE_SIGNING_ACTIVE_KEY_VERSIONS = [
    _parse_rate_limit_version(v)
    for v in env_list("OFFLINE_PACKAGE_SIGNING_ACTIVE_KEY_VERSIONS", default=("1",))
]
OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION = env_int(
    "OFFLINE_PACKAGE_SIGNING_WRITE_KEY_VERSION", default=1
)
OFFLINE_PACKAGE_SIGNING_KEYS_BY_VERSION = env_versioned_keys(
    "OFFLINE_PACKAGE_SIGNING_KEY_V", OFFLINE_PACKAGE_SIGNING_ACTIVE_KEY_VERSIONS
)

#: Approved validity values (binding decision P2-B), in seconds after
#: `package_data_cutoff_at`. Deliberately NOT read from the environment:
#: they are approved security values, and `apps.entry.checks` refuses any
#: configuration weaker than the approved maxima (`entry.E0xx`). "Aging to
#: Stale" is read conservatively as measured from the data cutoff too.
ENTRY_OFFLINE_VALIDITY = {
    "STANDARD": {
        "aging_after_seconds": 15 * 60,
        "stale_after_seconds": 2 * 60 * 60,
        "expires_after_seconds": 8 * 60 * 60,
        "grant_after_contact_seconds": 2 * 60 * 60,
        "clock_discontinuity_seconds": 5 * 60,
    },
    "SENSITIVE": {
        "aging_after_seconds": 5 * 60,
        "stale_after_seconds": 30 * 60,
        "expires_after_seconds": 2 * 60 * 60,
        "grant_after_contact_seconds": 60 * 60,
        "clock_discontinuity_seconds": 2 * 60,
    },
}
#: Approved common values (binding decision P2-B).
ENTRY_OFFLINE_OPERATOR_INACTIVITY_LOCK_SECONDS = 10 * 60
ENTRY_OFFLINE_HEALTH = {
    "failures_to_offline": 3,
    "failure_span_seconds": 20,
    "successes_to_sync": 2,
    "success_spacing_seconds": 10,
}

#: Operational tuning, NOT approved security thresholds. They change only
#: how often a device asks for newer data and how long ciphertext is kept;
#: none of them can extend a package's validity band.
#: Retry cadence of the health check while the connection is unhealthy.
ENTRY_OFFLINE_HEALTH_RETRY_SECONDS = env_int("ENTRY_OFFLINE_HEALTH_RETRY_SECONDS", default=5)
#: A READY package older than this (data cutoff age) is rebuilt as a NEW
#: immutable version when the device asks for one.
ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS = env_int(
    "ENTRY_OFFLINE_PACKAGE_REFRESH_SECONDS", default=5 * 60
)
#: How often an online device asks for a signed critical delta.
ENTRY_OFFLINE_DELTA_REFRESH_SECONDS = env_int("ENTRY_OFFLINE_DELTA_REFRESH_SECONDS", default=60)
#: Temporary retention of immutable device-encrypted ciphertext after its
#: package is superseded, expired, or revoked (binding decision P2-C). The
#: metadata row is never deleted by this retention.
ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS = env_int(
    "ENTRY_OFFLINE_CIPHERTEXT_RETENTION_SECONDS", default=60 * 60
)
#: Per-device package/delta download budget (binding decision P2-C).
ENTRY_OFFLINE_DOWNLOAD_WINDOW_SECONDS = env_int(
    "ENTRY_OFFLINE_DOWNLOAD_WINDOW_SECONDS", default=5 * 60
)
ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW = env_int("ENTRY_OFFLINE_DOWNLOADS_PER_WINDOW", default=30)
#: Lifetime of the single-use server nonce a device signs to prove key
#: possession on every signed API call.
ENTRY_OFFLINE_NONCE_SECONDS = env_int("ENTRY_OFFLINE_NONCE_SECONDS", default=120)
#: The self-test fails when the browser reports less free storage than this.
ENTRY_OFFLINE_MIN_FREE_STORAGE_BYTES = env_int(
    "ENTRY_OFFLINE_MIN_FREE_STORAGE_BYTES", default=20 * 1024 * 1024
)
#: Hard upper bound on entries in one package (fail closed above it).
ENTRY_OFFLINE_MAX_PACKAGE_ENTRIES = env_int("ENTRY_OFFLINE_MAX_PACKAGE_ENTRIES", default=100_000)
#: Package build lifecycle (independent re-review correction B). A worker holds a
#: build for at most the lease; an expired lease lets another worker (or a
#: Celery redelivery) claim the SAME build and version. After the maximum
#: attempts the build fails and a later request starts a new one.
ENTRY_OFFLINE_BUILD_LEASE_SECONDS = env_int("ENTRY_OFFLINE_BUILD_LEASE_SECONDS", default=300)
ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS = env_int("ENTRY_OFFLINE_BUILD_MAX_ATTEMPTS", default=3)
#: The "building" answer tells the device to ask again after this long; the
#: device gives up after this many consecutive "building" answers.
ENTRY_OFFLINE_BUILD_RETRY_AFTER_SECONDS = env_int(
    "ENTRY_OFFLINE_BUILD_RETRY_AFTER_SECONDS", default=2
)
ENTRY_OFFLINE_BUILD_POLL_LIMIT = env_int("ENTRY_OFFLINE_BUILD_POLL_LIMIT", default=30)
#: Change journal (independent re-review correction C). A delta that would carry
#: more journaled changes than this fails closed with a full rebuild
#: (BULK_CHANGE). Journal rows older than the retention -- which must exceed
#: every package's lifetime (system check entry.E011) -- are pruned.
ENTRY_OFFLINE_DELTA_MAX_CHANGES = env_int("ENTRY_OFFLINE_DELTA_MAX_CHANGES", default=5_000)
ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS = env_int(
    "ENTRY_OFFLINE_JOURNAL_RETENTION_SECONDS", default=24 * 60 * 60
)

# ---------------------------------------------------------------------------
# Offline verification, operation queue and synchronization (Phase 4 Prompt
# 3, ADR-0024). Operational tuning only, like the values above: none of
# these can extend a validity band, a grant, or what an offline device may
# admit. The approved Stale/Expired behaviour is fixed in
# `apps.entry.offline_contract` (STALE_POLICY / EXPIRED_POLICY, entry.E012).
# ---------------------------------------------------------------------------

#: Bounded upload batches: at most this many operations and this many bytes
#: per signed request (the device API body limit is 64 KiB).
ENTRY_OFFLINE_SYNC_BATCH_SIZE = env_int("ENTRY_OFFLINE_SYNC_BATCH_SIZE", default=20)
ENTRY_OFFLINE_SYNC_BATCH_BYTES = env_int("ENTRY_OFFLINE_SYNC_BATCH_BYTES", default=48 * 1024)
#: Device retry after a failed upload: exponential backoff with jitter,
#: starting at the base and never above the maximum.
ENTRY_OFFLINE_SYNC_RETRY_BASE_SECONDS = env_int("ENTRY_OFFLINE_SYNC_RETRY_BASE_SECONDS", default=5)
ENTRY_OFFLINE_SYNC_RETRY_MAX_SECONDS = env_int("ENTRY_OFFLINE_SYNC_RETRY_MAX_SECONDS", default=120)
#: Per-device upload budget (reconnect storms): signed batches per window.
ENTRY_OFFLINE_SYNC_WINDOW_SECONDS = env_int("ENTRY_OFFLINE_SYNC_WINDOW_SECONDS", default=5 * 60)
ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW = env_int(
    "ENTRY_OFFLINE_SYNC_BATCHES_PER_WINDOW", default=120
)
#: A device keeps a durably acknowledged record this long (duplicate display,
#: local re-entry advisories) before ordinary cleanup may delete it.
ENTRY_OFFLINE_ACK_RETENTION_SECONDS = env_int(
    "ENTRY_OFFLINE_ACK_RETENTION_SECONDS", default=60 * 60
)
#: Server-side clock-anomaly tolerance: an operation dated in the future,
#: before its package, or earlier than its predecessor by more than this is
#: flagged for reconciliation (it is never re-dated).
ENTRY_OFFLINE_CLOCK_TOLERANCE_SECONDS = env_int(
    "ENTRY_OFFLINE_CLOCK_TOLERANCE_SECONDS", default=5 * 60
)

# ---------------------------------------------------------------------------
# Django REST Framework -- device API boundary only (binding decision P2-A).
# Deny by default: every API view declares its own authentication and
# permission. JSON in, JSON out; no browsable API; no schema generator.
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["apps.entry.api.permissions.DenyAll"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_THROTTLE_CLASSES": [],
    "EXCEPTION_HANDLER": "apps.entry.api.errors.api_exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# ---------------------------------------------------------------------------
# OTP generator (accepted plan §5.7). CSPRNG everywhere by default; the
# deterministic generator may be injected only under config.settings.test.
# The generator implementation itself belongs to Prompt 3's OTP domain
# (accounts.AuthenticationChallenge); this setting is the injection point
# and is guarded by validation.py from Prompt 2 onward.
# ---------------------------------------------------------------------------

CSPRNG_OTP_GENERATOR_BACKEND = "apps.accounts.otp.CsprngOtpGenerator"
DETERMINISTIC_OTP_GENERATOR_BACKEND = "apps.accounts.otp.DeterministicTestOtpGenerator"
OTP_GENERATOR_BACKEND = env("OTP_GENERATOR_BACKEND", default=CSPRNG_OTP_GENERATOR_BACKEND)

# ---------------------------------------------------------------------------
# Security headers. Cookie Secure/SameSite and SSL redirect are strengthened
# per environment (SEC-002..SEC-004).
# ---------------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
# Localized, styled rejection page only; `CsrfViewMiddleware` still decides
# every rejection and the technical reason is never shown (UI/UX gate F4).
CSRF_FAILURE_VIEW = "apps.core.views.csrf_failure"

# Enforced Content Security Policy (P4-4, plan §8, SEC-004); see
# `apps/core/middleware/security_headers.py` for why each source is needed.
# Same-origin only, no 'unsafe-inline', no 'unsafe-eval', no nonce.
CONTENT_SECURITY_POLICY = {
    "default-src": ("'self'",),
    "script-src": ("'self'",),
    "style-src": ("'self'",),
    "img-src": ("'self'", "data:"),
    "font-src": ("'self'",),
    "connect-src": ("'self'",),
    "worker-src": ("'self'",),
    "manifest-src": ("'self'",),
    "media-src": ("'none'",),
    "object-src": ("'none'",),
    "frame-src": ("'none'",),
    "base-uri": ("'none'",),
    "form-action": ("'self'",),
    "frame-ancestors": ("'none'",),
}
# Staging and production (HTTPS only) add `upgrade-insecure-requests`.
CONTENT_SECURITY_POLICY_UPGRADE_INSECURE_REQUESTS = False
# No page uses these browser features. Browser camera scanning (plan R4) is a
# requirement gap; enabling it must change `camera` deliberately.
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), "
    "hid=(), display-capture=()"
)

# HTTPS seen through a TLS-terminating proxy (P4-4). Honoured only when
# forwarding is enabled; `ForwardedProtoGuardMiddleware` removes the header
# from any request whose direct peer is not in TRUSTED_PROXY_CIDRS, and
# validation.py refuses forwarding without CIDRs.
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if TRUSTED_PROXY_FORWARDING_ENABLED else None
)

# Server-side session rows expire with the longest approved session lifetime
# instead of Django's two-week default (P4-4). The per-audience inactivity
# and absolute deadlines in `apps/accounts/session_expiry.py` still decide
# access; this only bounds how long an abandoned (for example anonymous)
# session row may exist before `clearsessions` can remove it.
SESSION_COOKIE_AGE = max(
    PARTICIPANT_SESSION_ABSOLUTE_SECONDS,
    OPERATIONAL_SESSION_ABSOLUTE_SECONDS,
    ENTRY_DEVICE_SESSION_SECONDS,
)

# ---------------------------------------------------------------------------
# Logging -- structured, with redaction of NIN, passport, OTP, tokens,
# document URLs, database credentials and connection URLs (accepted plan,
# Prompt 2 CP 2g). See apps/core/redaction.py.
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {"()": "apps.core.middleware.correlation.CorrelationIdLogFilter"},
        "redact_sensitive": {"()": "apps.core.redaction.RedactingLogFilter"},
    },
    "formatters": {
        "structured": {
            "()": "apps.core.logging_config.StructuredFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
            "filters": ["correlation_id", "redact_sensitive"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "asc2026": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
