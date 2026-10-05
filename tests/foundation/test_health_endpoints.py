"""Health/readiness endpoints (accepted plan Prompt 2 CP 2f).

The readiness view's database probe is exercised here with a mocked
connection, not a real one -- this test needs no migration and no live
database. Genuine live-database connectivity is verified by
`scripts/check.py db-probe`, which is a completely separate, raw-psycopg
code path (see tests/foundation/test_db_probe.py) with no dependency on
Django's own database/test-database machinery.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory

from apps.core import views as core_views


def test_liveness_never_touches_the_database() -> None:
    request = RequestFactory().get("/healthz")
    response = core_views.liveness(request)
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok"}


def test_readiness_reports_ok_when_select_1_succeeds(settings, private_storage_root) -> None:
    # P4-4-C1: the storage probe checks the configured root, so it is isolated.
    settings.PRIVATE_STORAGE_BACKEND = "filesystem"
    settings.PRIVATE_STORAGE_ROOT = private_storage_root
    request = RequestFactory().get("/readyz")
    mock_cursor = MagicMock()
    mock_cursor.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = (1,)

    with patch.object(core_views.connection, "cursor", return_value=mock_cursor):
        response = core_views.readiness(request)

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["status"] == "ok"
    mock_cursor.execute.assert_called_once_with("SELECT 1")


def test_readiness_reports_unavailable_without_leaking_the_driver_error() -> None:
    request = RequestFactory().get("/readyz")

    driver_error_message = "dsn=postgresql://user:secret@host/db"  # secret-scan: allow
    synthetic_driver_error = RuntimeError(driver_error_message)

    with patch.object(core_views.connection, "cursor", side_effect=synthetic_driver_error):
        response = core_views.readiness(request)

    assert response.status_code == 503
    body_text = response.content.decode()
    assert "secret" not in body_text
    assert json.loads(body_text)["status"] == "unavailable"
