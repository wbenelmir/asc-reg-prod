"""ASC 2026 Registration Platform - Django project configuration package.

Importing the Celery app object here ensures it is loaded whenever Django
starts, so that `@shared_task` decorators register against it.
"""

from .celery import app as celery_app

__all__ = ("celery_app",)
