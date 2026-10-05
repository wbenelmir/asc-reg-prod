from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_private_storage_root(tmp_path, settings):
    """Every test in this package writes to an isolated per-test directory,
    never the real `var/private/` (mirrors `tests/conftest.py`'s `private_storage_root`)."""
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_ROOT = root
