"""Local-safety contract: `.gitignore` text, `.env` exclusion, `.env.example`
placeholders (developer execution-control decision: no Git dependency).

Replaces the earlier `git check-ignore`-based test. Nothing here invokes
Git or requires a `.git` directory to exist.
"""

from __future__ import annotations

from scripts.check import (
    check_env_example_placeholders_only,
    check_env_file_is_excluded,
    check_gitignore_text_contract,
)


def test_gitignore_text_retains_every_required_rule() -> None:
    problems = check_gitignore_text_contract()
    assert problems == [], f".gitignore text contract violated: {problems}"


def test_env_exists_and_is_structurally_excluded_from_project_scans() -> None:
    problems = check_env_file_is_excluded()
    assert problems == [], f".env exclusion contract violated: {problems}"


def test_env_example_holds_placeholders_only() -> None:
    problems = check_env_example_placeholders_only()
    assert problems == [], f".env.example placeholder contract violated: {problems}"
