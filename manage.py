#!/usr/bin/env python
"""Django's command-line utility for administrative tasks.

DJANGO_SETTINGS_MODULE defaults to `config.settings.local` so that ordinary
development commands work without extra flags. Every other environment
(test, staging, production) must be selected explicitly, either by setting
DJANGO_SETTINGS_MODULE in the environment or passing --settings=... .
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover - environment misconfiguration
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment, or run this through "
            "`uv run`?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
