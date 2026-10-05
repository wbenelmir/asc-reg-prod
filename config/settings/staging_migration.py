"""Staging MIGRATION settings: used only by the database migration process.

    DJANGO_SETTINGS_MODULE=config.settings.staging_migration python manage.py migrate

Connects with the migration-owner identity (`DATABASE_MIGRATION_USER`,
`DATABASE_MIGRATION_PASSWORD`). The runtime password is not needed. The web
application, workers and beat never use this module: they run
`config.settings.staging`, which refuses to start when the migration-owner
password is in its environment. See `docs/deployment/README.md`.
"""

from ._deployment import *  # noqa: F403
from ._deployment import use_migration_identity, validate_settings

DATABASE_PROCESS_ROLE = "migration"
DATABASES = use_migration_identity()

validate_settings(globals(), settings_module_name=__name__)
