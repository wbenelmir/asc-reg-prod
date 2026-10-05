"""WSGI entrypoint.

`DJANGO_SETTINGS_MODULE` must be set explicitly. There is no fallback to the
local development settings here: a server or worker started without the
variable fails at once instead of silently running with the local mail sink,
local file storage and development keys. `manage.py` still defaults to
`config.settings.local` for developer commands, and `runserver` imports this
module only after that default is in place.
"""

import os

from django.core.exceptions import ImproperlyConfigured
from django.core.wsgi import get_wsgi_application

if not os.environ.get("DJANGO_SETTINGS_MODULE", "").strip():
    raise ImproperlyConfigured(
        "Set DJANGO_SETTINGS_MODULE explicitly (for example config.settings.staging)."
    )

application = get_wsgi_application()
