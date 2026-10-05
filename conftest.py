"""Project-wide pytest fixtures for both test paths (`apps/` and `tests/`).

Only test isolation lives here; feature fixtures stay next to their tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _restore_active_translation():
    """Restore the test thread's active language after every test (P4-4-C1,
    review finding R-04 / OBS-01).

    A test-client request runs `LocaleMiddleware`, which activates the
    request's language in the calling thread and leaves it active afterwards.
    Without this, a test whose last request used Arabic made later
    language-sensitive tests (form error messages) run in Arabic, depending on
    the test order. Nothing is activated before the test, so a test still sees
    exactly the state it saw before; only leakage out of a test is removed.
    """
    from django.utils import translation

    language = translation.get_language()
    yield
    if translation.get_language() != language:
        if language is None:
            translation.deactivate_all()
        else:
            translation.activate(language)


@pytest.fixture(autouse=True)
def _fresh_issuance_counter():
    """Start and end every test with an empty challenge-issuance counter
    (P4-4-C3). The counter is process-wide, so counts from one test (all test
    clients share 127.0.0.1) must never reach the next one."""
    from apps.core.issuance_counter import reset_issuance_limiter

    reset_issuance_limiter()
    yield
    reset_issuance_limiter()
