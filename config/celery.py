"""Celery application object.

Phase 1 local and test boundary (accepted plan §5.4): no broker is run and no
worker process is started. `CELERY_TASK_ALWAYS_EAGER = True` is set in
`config/settings/local.py` and `config/settings/test.py`, so
`transaction.on_commit`-scheduled tasks execute synchronously, in-process,
strictly after the enclosing transaction commits. This is a first-class,
documented Celery execution mode -- not a substitute component -- and it is
never described as a real broker/worker integration.

Staging and production configure a real broker (`CELERY_BROKER_URL`,
`REDIS_URL`) and `config/settings/validation.py` fails closed if that
configuration is absent, and asserts `CELERY_TASK_ALWAYS_EAGER is False`
there.

This module does not set a default `DJANGO_SETTINGS_MODULE`. It is imported
whenever the `config` package is (`config/__init__.py`), so a default here
would silently put every worker, beat scheduler or WSGI server started without
the variable on the local development settings. A worker started without it
now fails when it first reads the settings.
"""

from celery import Celery

app = Celery("asc2026")

# Read CELERY_* settings from Django settings (django.conf.settings), so the
# same environment-driven configuration boundary applies here as everywhere
# else in the project.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Discover tasks.py modules in every INSTALLED_APPS app automatically.
app.autodiscover_tasks()
