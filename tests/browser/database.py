"""Explicit database boundary for synchronous Playwright tests.

Playwright objects stay on the test thread. Database callbacks use their own
thread and connection, with Django's async guard unchanged. Callers must resolve
querysets and lazy related values inside the callback. These browser tests use
transactional_db, so committed changes are visible to the real live server.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from functools import wraps
from threading import local

from django.db import connections
from django.db.models import QuerySet

_worker = local()


def _invoke(callback, args, kwargs):
    _worker.active = True
    try:
        result = callback(*args, **kwargs)
        if isinstance(result, QuerySet):
            raise TypeError("Evaluate querysets inside the database callback before returning.")
        return result
    finally:
        _worker.active = False


@contextmanager
def _database_worker():
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-database") as executor:

        def run(callback, *args, **kwargs):
            return executor.submit(context.run, _invoke, callback, args, kwargs).result()

        try:
            yield run
        finally:
            run(connections.close_all)


def database_call(callback, *args, **kwargs):
    """Run a complete database operation, including lazy evaluation, off-loop."""
    if getattr(_worker, "active", False):
        return callback(*args, **kwargs)
    with _database_worker() as run:
        return run(callback, *args, **kwargs)


def database_sync(callback):
    """Decorate a data-only helper or a non-yielding database fixture."""

    @wraps(callback)
    def wrapped(*args, **kwargs):
        return database_call(callback, *args, **kwargs)

    return wrapped


def database_fixture(callback):
    """Keep a yielding data fixture's setup and cleanup on the same DB thread."""

    @wraps(callback)
    def wrapped(*args, **kwargs):
        manager = contextmanager(callback)(*args, **kwargs)
        with _database_worker() as run:
            value = run(manager.__enter__)
            try:
                yield value
            finally:
                run(manager.__exit__, None, None, None)

    return wrapped


@database_sync
def operational_session_key(user):
    """Create the existing synthetic operational session without browser calls."""
    from django.conf import settings
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.sessions.backends.db import SessionStore
    from django.utils import timezone

    from apps.accounts import session_expiry

    now = timezone.now().isoformat()
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session[session_expiry.OPERATIONAL_ESTABLISHED_AT_KEY] = now
    session[session_expiry.OPERATIONAL_LAST_ACTIVITY_AT_KEY] = now
    session.create()
    return session.session_key


@database_sync
def participant_session_key(person):
    """Create the existing synthetic participant session without browser calls."""
    from django.contrib.sessions.backends.db import SessionStore
    from django.utils import timezone

    from apps.accounts import participant_auth, session_expiry

    now = timezone.now().isoformat()
    session = SessionStore()
    session[participant_auth.PARTICIPANT_SESSION_KEY] = str(person.pk)
    session[session_expiry.PARTICIPANT_ESTABLISHED_AT_KEY] = now
    session[session_expiry.PARTICIPANT_LAST_ACTIVITY_AT_KEY] = now
    session.create()
    return session.session_key
