"""Typed `os.environ` accessors (accepted plan §5.8) -- not a `.env` parser."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings._env import env, env_bool, env_int, env_list, require


def test_env_returns_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SOME_PROBE_VAR", raising=False)
    assert env("SOME_PROBE_VAR", "fallback") == "fallback"


def test_require_raises_a_clear_non_secret_error_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("SOME_REQUIRED_VAR", raising=False)
    with pytest.raises(ImproperlyConfigured) as excinfo:
        require("SOME_REQUIRED_VAR")
    assert "SOME_REQUIRED_VAR" in str(excinfo.value)


def test_require_raises_on_blank_value(monkeypatch) -> None:
    monkeypatch.setenv("SOME_REQUIRED_VAR", "   ")
    with pytest.raises(ImproperlyConfigured):
        require("SOME_REQUIRED_VAR")


def test_require_returns_the_value_when_present(monkeypatch) -> None:
    monkeypatch.setenv("SOME_REQUIRED_VAR", "value-123")
    assert require("SOME_REQUIRED_VAR") == "value-123"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_env_bool_parses_recognized_values(monkeypatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("SOME_BOOL_VAR", raw)
    assert env_bool("SOME_BOOL_VAR") is expected


def test_env_bool_rejects_unrecognized_value(monkeypatch) -> None:
    monkeypatch.setenv("SOME_BOOL_VAR", "maybe")
    with pytest.raises(ImproperlyConfigured):
        env_bool("SOME_BOOL_VAR")


def test_env_bool_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SOME_BOOL_VAR", raising=False)
    assert env_bool("SOME_BOOL_VAR", default=True) is True


def test_env_int_parses_and_defaults(monkeypatch) -> None:
    monkeypatch.setenv("SOME_INT_VAR", "42")
    assert env_int("SOME_INT_VAR") == 42
    monkeypatch.delenv("SOME_INT_VAR", raising=False)
    assert env_int("SOME_INT_VAR", default=7) == 7


def test_env_int_raises_on_unparseable_value(monkeypatch) -> None:
    monkeypatch.setenv("SOME_INT_VAR", "not-a-number")
    with pytest.raises(ImproperlyConfigured):
        env_int("SOME_INT_VAR")


def test_env_list_splits_on_comma_and_trims(monkeypatch) -> None:
    monkeypatch.setenv("SOME_LIST_VAR", "a, b ,c")
    assert env_list("SOME_LIST_VAR") == ["a", "b", "c"]


def test_env_list_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SOME_LIST_VAR", raising=False)
    assert env_list("SOME_LIST_VAR", default=("x", "y")) == ["x", "y"]
