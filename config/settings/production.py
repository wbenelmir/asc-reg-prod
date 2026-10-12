"""Production RUNTIME settings: web application, Celery workers and the beat scheduler.

Identical configuration shape and validation to `config.settings.staging`,
with no relaxation. Migrations run with `config.settings.production_migration`.
See `config/settings/_deployment.py`.

Production only: Google Analytics 4 on the public visitor pages, after the
visitor's analytics consent (`static/js/asc-analytics.js`). The measurement
ID defaults to the production web stream and can be overridden, or tracking
switched off with an empty value, through `ANALYTICS_GA4_MEASUREMENT_ID`
(`config/settings/_analytics.py`). When it is set, the CSP allows exactly the
Google tag and GA4 collection hosts; no 'unsafe-inline' is added.
"""

from ._analytics import ga4_content_security_policy, measurement_id_from
from ._deployment import *  # noqa: F403
from ._deployment import CONTENT_SECURITY_POLICY, validate_settings
from ._env import env

ANALYTICS_GA4_MEASUREMENT_ID = measurement_id_from(
    env("ANALYTICS_GA4_MEASUREMENT_ID", default=None)
)
CONTENT_SECURITY_POLICY = ga4_content_security_policy(
    CONTENT_SECURITY_POLICY, ANALYTICS_GA4_MEASUREMENT_ID
)

validate_settings(globals(), settings_module_name=__name__)
