"""End-to-end static deploy check (accepted plan §5.6 A, §9).

Runs `manage.py check --deploy` in a genuine subprocess with documented
synthetic, non-secret environment values -- exercising the real settings
import + validation path without ever touching the real `.env` or a live
database/Redis/provider connection.
"""

from __future__ import annotations

from scripts.check import run_static_deploy_check


def test_staging_deploy_check_passes_with_synthetic_configuration() -> None:
    result = run_static_deploy_check("config.settings.staging")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_production_deploy_check_passes_with_synthetic_configuration() -> None:
    result = run_static_deploy_check("config.settings.production")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
