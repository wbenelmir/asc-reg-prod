"""Production MIGRATION settings: used only by the database migration process.

The production counterpart of `config.settings.staging_migration`.
"""

from ._deployment import *  # noqa: F403
from ._deployment import use_migration_identity, validate_settings

DATABASE_PROCESS_ROLE = "migration"
DATABASES = use_migration_identity()

validate_settings(globals(), settings_module_name=__name__)
