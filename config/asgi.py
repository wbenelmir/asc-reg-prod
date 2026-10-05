"""ASGI entrypoint.

Like `config/wsgi.py`, it requires an explicit `DJANGO_SETTINGS_MODULE` and
never falls back to the local development settings.
"""

import os

from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured

if not os.environ.get("DJANGO_SETTINGS_MODULE", "").strip():
    raise ImproperlyConfigured(
        "Set DJANGO_SETTINGS_MODULE explicitly (for example config.settings.staging)."
    )

application = get_asgi_application()
