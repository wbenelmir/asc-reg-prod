"""Test settings -- native PostgreSQL 17.11, `test_asc2026_dev`.

Django/pytest-django create the test database automatically by prefixing
`DATABASE_NAME` with `test_` (the Postgres default), using the `CREATEDB`
right already granted to `asc2026_app` -- no superuser needed (accepted
plan §5.10). No SQLite anywhere in this project.
"""

from ._env import require
from .base import *  # noqa: F403

# The runtime password is required for every local and test connection.
DATABASES["default"]["PASSWORD"] = require("DATABASE_PASSWORD")

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# --- Celery: eager execution, no broker, no worker process (§5.4) ---
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"

# --- In-memory delivery sink for tests (§5.7): Django's own locmem backend,
# collecting messages in `django.core.mail.outbox` -- no disk I/O. ---
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# --- Private file storage: local filesystem in a temporary directory,
# rooted outside every served path (§5.11). Individual tests may override
# PRIVATE_STORAGE_ROOT to an isolated tmp_path. ---
PRIVATE_STORAGE_BACKEND = "filesystem"

# --- Malware scanner / NIN provider: forced to their deterministic local
# stubs here, NOT inherited from `.env` -- these are optional settings with
# a code default (`env(..., default=...)`), so a developer's real `.env`
# leaving them unset, empty, or pointed at a real provider must never change
# what the test suite exercises. Individual tests opt into the
# scanner-failure/provider-failure stubs with `override_settings`. ---
MALWARE_SCANNER_BACKEND = "apps.documents.scanning.DeterministicStubScanner"
MALWARE_SCANNER_ALLOW_LOCAL_STUBS = True
# The staging provisioning command is exercised by its own tests.
STAGING_PROVISIONING_ENABLED = True
NIN_PROVIDER_BACKEND = "apps.people.nin_provider.LocalSimulationNinProvider"
# IDV-2: tests may use the development simulation and drive the worker
# explicitly; dispatch-on-commit tests opt in with override_settings.
IDENTITY_ALLOW_SIMULATED_PROVIDER = True
IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT = False

# --- Human check (UX-4, UX-C2 ALTCHA): still ENABLED in tests, with the lowest
# accepted cost so every test that requests an OTP solves a real challenge
# quickly (the browser suite runs the real ALTCHA widget and worker; client
# tests use `apps.core.testing`). ---
HUMAN_CHECK_ENABLED = True
HUMAN_CHECK_COST = 100
# P4-4: every test client and the live browser server share 127.0.0.1 and one
# process-local counter (HUMAN_CHECK_COUNTER_STORE "local" since P4-4-C3), so
# the challenge-endpoint limit is effectively off by default here;
# `apps/accounts/tests/test_p4_4_challenge_and_cache.py` and the P4-4-C3
# counter tests set low limits explicitly to test it.
HUMAN_CHECK_CHALLENGE_MAX_PER_WINDOW = 1_000_000

# --- Password hashing: fast hasher in tests only, never local/staging/prod. ---
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# --- Client-network identity: ignore forwarded headers by default; specific
# tests exercise the trusted-proxy resolver directly with explicit
# configuration (§5.5). ---
TRUSTED_PROXY_FORWARDING_ENABLED = False
TRUSTED_PROXY_CIDRS: list[str] = []
SECURE_PROXY_SSL_HEADER = None

# The deterministic OTP generator (accepted plan §5.7) may be selected ONLY
# under this settings module -- individual tests opt in with
# @override_settings(OTP_GENERATOR_BACKEND=DETERMINISTIC_OTP_GENERATOR_BACKEND).
# The default here remains the CSPRNG generator, matching every other
# environment, so that a test which forgets to opt in still exercises real
# randomness rather than silently becoming deterministic.

# --- Test-only synthetic key material for ADR-0006 field encryption and
# blind indexes (Prompt 3). Hardcoded here, NOT read from `.env` --
# the project rules forbid writing real secret values into `.env`, and the test
# suite must be hermetic regardless of what a developer has (or has not)
# configured locally. These values are never used outside this settings
# module and are obviously not production key material. Individual
# rotation tests add a second version with `override_settings`.
IDENTITY_ENCRYPTION_ACTIVE_VERSIONS = [1]
IDENTITY_ENCRYPTION_WRITE_VERSION = 1
IDENTITY_ENCRYPTION_KEYS_BY_VERSION = {1: "test-only-identity-encryption-key-v1-not-real"}  # noqa: S105

IDENTITY_BLIND_INDEX_HMAC_ACTIVE_VERSIONS = [1]
IDENTITY_BLIND_INDEX_HMAC_WRITE_VERSION = 1
IDENTITY_BLIND_INDEX_HMAC_KEYS_BY_VERSION = {1: "test-only-blind-index-key-v1-not-real"}  # noqa: S105

# Rate-limit HMAC keys are also forced here for the same hermeticity reason
# -- a developer's `.env` may not have RATE_LIMIT_HMAC_KEY_V1 set (it is a
# real secret they configure themselves), and OTP-domain tests must not
# depend on that.
RATE_LIMIT_HMAC_ACTIVE_VERSIONS = [1]
RATE_LIMIT_HMAC_WRITE_VERSION = 1
RATE_LIMIT_HMAC_KEYS_BY_VERSION = {1: "test-only-rate-limit-key-v1-not-real"}  # noqa: S105

# --- Deterministic email-failure simulation (Phase 2 Prompt 5 §4.4): lets
# tests exercise retryable/permanent/retry-exhaustion delivery paths via a
# marker destination, without depending on monkeypatching every call site. ---
COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION = True
