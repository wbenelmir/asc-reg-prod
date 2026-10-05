"""Staging RUNTIME settings: web application, Celery workers and the beat scheduler.

Staging is where the deployment team's own infrastructure (PostgreSQL 17,
Redis for the broker and for the challenge counter, an S3-compatible object
store, a ClamAV clamd daemon, an SMTP service and, when one exists, an MFA
step-up provider for sensitive operations) is exercised. This module declares
the configuration shape that infrastructure must satisfy and fails closed, at
settings-import time and without contacting any service, when it does not.

Database identity: the restricted runtime role (`DATABASE_USER`,
`DATABASE_PASSWORD`). The migration-owner password must NOT be present in
these processes; migrations run with `config.settings.staging_migration`.
See `config/settings/_deployment.py` and `docs/deployment/README.md`.

This module does NOT and CANNOT prove real PostgreSQL ownership, grants or
privilege isolation -- `scripts/check.py audit-isolation` does, run after the
migration with the runtime credentials (ADR-0008).
"""

from ._deployment import *  # noqa: F403
from ._deployment import validate_settings

# The guarded UAT provisioning command (`manage.py provision_staging_uat`).
STAGING_PROVISIONING_ENABLED = True

validate_settings(globals(), settings_module_name=__name__)
