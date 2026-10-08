"""Production RUNTIME settings: web application, Celery workers and the beat scheduler.

Identical configuration shape and validation to `config.settings.staging`,
with no relaxation. Migrations run with `config.settings.production_migration`.
See `config/settings/_deployment.py`.

Production only: Google Analytics 4 on the public visitor pages, after the
visitor's analytics consent (`static/js/asc-analytics.js`). The measurement
ID can be overridden, or tracking switched off with an empty value, through
`ANALYTICS_GA4_MEASUREMENT_ID`. When it is set, the CSP allows exactly the
Google Tag and GA4 collection hosts; no 'unsafe-inline' is added.
"""

import re

from django.core.exceptions import ImproperlyConfigured

from ._deployment import *  # noqa: F403
from ._deployment import CONTENT_SECURITY_POLICY, validate_settings
from ._env import env

ANALYTICS_GA4_MEASUREMENT_ID = (
    env("ANALYTICS_GA4_MEASUREMENT_ID", default="G-FDXGG6H52P") or ""
).strip()
if ANALYTICS_GA4_MEASUREMENT_ID:
    if not re.fullmatch(r"G-[A-Z0-9]{4,20}", ANALYTICS_GA4_MEASUREMENT_ID):
        raise ImproperlyConfigured("ANALYTICS_GA4_MEASUREMENT_ID must look like G-XXXXXXXXXX.")
    _GOOGLE_TAG = "https://www.googletagmanager.com"
    _GA4_COLLECT = (
        "https://*.google-analytics.com",
        "https://*.analytics.google.com",
        "https://*.googletagmanager.com",
    )
    CONTENT_SECURITY_POLICY = {
        **CONTENT_SECURITY_POLICY,
        "script-src": (*CONTENT_SECURITY_POLICY["script-src"], _GOOGLE_TAG),
        "connect-src": (*CONTENT_SECURITY_POLICY["connect-src"], *_GA4_COLLECT),
        "img-src": (
            *CONTENT_SECURITY_POLICY["img-src"],
            "https://*.google-analytics.com",
            "https://*.googletagmanager.com",
        ),
    }

validate_settings(globals(), settings_module_name=__name__)
