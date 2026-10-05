"""Production RUNTIME settings: web application, Celery workers and the beat scheduler.

Identical configuration shape and validation to `config.settings.staging`,
with no relaxation. Migrations run with `config.settings.production_migration`.
See `config/settings/_deployment.py`.
"""

from ._deployment import *  # noqa: F403
from ._deployment import validate_settings

validate_settings(globals(), settings_module_name=__name__)
