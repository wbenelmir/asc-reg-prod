"""Shared pytest fixtures.

No fixture here uses the `django_db` marker/fixture. Phase 1 Prompt 2 has
zero project migrations, and this project's hard boundary is that Prompt 2
never runs `manage.py migrate` against any database, including implicitly
through pytest-django's automatic test-database creation. Every foundation
test in `tests/foundation/` and `apps/*/tests/` for Prompt 2 is written to
need no database access at all.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def private_storage_root(tmp_path):
    """An isolated private-storage root for a single test -- never `var/private/`."""
    root = tmp_path / "private"
    root.mkdir()
    return root
