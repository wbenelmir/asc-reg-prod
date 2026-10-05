"""Local development settings -- native Windows, native PostgreSQL 17.11.

This is the default `manage.py` settings module. It is intentionally the
most permissive environment: HTTP is fine, the Django admin is enabled, and
Celery runs in eager mode with no broker and no worker process (accepted
plan §5.4). None of the production-grade guarantees validated by
`config/settings/validation.py` are enforced here -- that exemption is
deliberate and documented at every point it applies, never silent.
"""

from ._env import env, env_list, require
from .base import *  # noqa: F403

# The runtime password is required for every local and test connection.
DATABASES["default"]["PASSWORD"] = require("DATABASE_PASSWORD")

DEBUG = True

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", default=("localhost", "127.0.0.1"))

# Django admin is enabled in local settings only (accepted plan §10);
# disabled in test/staging/production.
INSTALLED_APPS = [*INSTALLED_APPS, "django.contrib.admin"]

# --- Celery: eager execution, no broker, no worker process (§5.4) ---
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"

# --- Local fake delivery sink (§5.7): Django's own file-based backend,
# rooted under gitignored var/mail/, outside every served path. ---
EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
EMAIL_FILE_PATH = str(LOCAL_MAIL_SINK_ROOT)

# --- Private file storage: local filesystem, rooted outside every served
# path (§5.11). ---
PRIVATE_STORAGE_BACKEND = "filesystem"

# --- Malware scanner: the deterministic local stub is allowed only here and in
# test.py (`apps.documents.scanning.get_scanner`). ---
MALWARE_SCANNER_ALLOW_LOCAL_STUBS = True

# --- Cookies over plain HTTP in local development ---
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False

# --- Client-network identity: ignore forwarded headers entirely, use the
# direct peer only (accepted plan §5.5). ---
TRUSTED_PROXY_FORWARDING_ENABLED = False
TRUSTED_PROXY_CIDRS: list[str] = []
SECURE_PROXY_SSL_HEADER = None

# --- Deterministic email-failure simulation for local manual testing only
# (Phase 2 Prompt 5 §4.4) -- never in staging/production. ---
COMMUNICATIONS_ALLOW_TEST_FAILURE_SIMULATION = True

# --- Identity verification (IDV-2, A13-06): the DEVELOPMENT SIMULATION by
# default, which answers from synthetic bodies and is never official
# verification. After-commit dispatch is off; run
# `manage.py run_identity_verification_jobs` to process submitted cases. ---
NIN_PROVIDER_BACKEND = env("NIN_PROVIDER_BACKEND", default=LOCAL_NIN_PROVIDER_BACKEND)
IDENTITY_ALLOW_SIMULATED_PROVIDER = True
IDENTITY_VERIFICATION_DISPATCH_ON_COMMIT = False
