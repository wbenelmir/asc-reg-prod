"""`deploy/staging.env.example` stays placeholders-only and in step with the settings."""

from __future__ import annotations

import shutil
from pathlib import Path

from scripts.check import BASE_DIR, DEPLOYMENT_ENV_EXAMPLE, check_deployment_env_example


def _tree(tmp_path: Path, template: str) -> Path:
    shutil.copytree(BASE_DIR / "config", tmp_path / "config")
    (tmp_path / "deploy").mkdir()
    (tmp_path / DEPLOYMENT_ENV_EXAMPLE).write_text(template, encoding="utf-8")
    return tmp_path


def test_the_committed_template_is_safe_and_complete() -> None:
    assert check_deployment_env_example() == []


def test_a_real_looking_secret_is_reported_without_its_value(tmp_path) -> None:
    template = (
        (BASE_DIR / DEPLOYMENT_ENV_EXAMPLE)
        .read_text(encoding="utf-8")
        .replace(
            "DJANGO_SECRET_KEY=__SET_BY_OPERATOR__", "DJANGO_SECRET_KEY=a-real-looking-value-123"
        )
    )
    problems = check_deployment_env_example(_tree(tmp_path, template))
    assert any("DJANGO_SECRET_KEY" in problem for problem in problems)
    assert not any("a-real-looking-value-123" in problem for problem in problems)


def test_a_name_the_settings_do_not_read_is_reported(tmp_path) -> None:
    template = (BASE_DIR / DEPLOYMENT_ENV_EXAMPLE).read_text(encoding="utf-8")
    problems = check_deployment_env_example(
        _tree(tmp_path, template + "\nEMAIL_PROVIDER_API_KEY=\n")
    )
    assert any("EMAIL_PROVIDER_API_KEY" in problem for problem in problems)


def test_a_missing_required_name_is_reported(tmp_path) -> None:
    template = "\n".join(
        line
        for line in (BASE_DIR / DEPLOYMENT_ENV_EXAMPLE).read_text(encoding="utf-8").splitlines()
        if not line.startswith("DEFAULT_FROM_EMAIL=")
    )
    problems = check_deployment_env_example(_tree(tmp_path, template))
    assert any("does not list DEFAULT_FROM_EMAIL" in problem for problem in problems)
