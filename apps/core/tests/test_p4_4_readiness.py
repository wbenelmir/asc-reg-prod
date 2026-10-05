"""P4-4 (DEP-005, partial): the readiness probe covers the database, the cache
and the private storage adapter, and never reveals why a probe failed.

P4-4-C3 (CACHE-01): the process-local cache probe, which proved nothing about
Redis, is replaced by the challenge-issuance counter state; the counter's own
healthy, degraded and failed cases are in `test_p4_4_c3_readiness.py`."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_private_storage(settings, tmp_path):
    # P4-4-C1: the storage probe now checks the configured root, so every test
    # here uses its own synthetic root, never `var/private/`.
    root = tmp_path / "private"
    root.mkdir()
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = root


def test_every_probe_reachable_is_ready_and_never_cached() -> None:
    response = Client().get("/readyz")
    assert response.status_code == 200
    assert json.loads(response.content) == {
        "status": "ok",
        "database": "reachable",
        "storage": "reachable",
        "challenge_counter": "local",
    }
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize(
    ("target", "component"),
    [
        ("apps.documents.storage.PrivateFileSystemStorage.exists", "storage"),
        ("apps.core.views._database_ready", "database"),
    ],
)
def test_a_failing_probe_is_unavailable_without_detail(target, component) -> None:
    detail = "bucket=p44-private host=storage.internal key=abc"
    with patch(target, side_effect=RuntimeError(detail)):
        response = Client().get("/readyz")
    assert response.status_code == 503
    body = json.loads(response.content)
    assert body["status"] == "unavailable"
    assert body[component] == "unreachable"
    assert [name for name, value in body.items() if value == "unreachable"] == [component]
    text = response.content.decode()
    for fragment in ("p44-private", "storage.internal", "RuntimeError"):
        assert fragment not in text


def test_a_counter_that_cannot_count_is_unavailable() -> None:
    # P4-4-C3: replaces the LocMemCache round-trip case.
    with patch("apps.core.issuance_counter.IssuanceLimiter.probe", side_effect=RuntimeError("p44")):
        response = Client().get("/readyz")
    assert response.status_code == 503
    assert json.loads(response.content)["challenge_counter"] == "unavailable"
