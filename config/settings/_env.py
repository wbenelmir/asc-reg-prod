"""Typed accessors over `os.environ`.

This is NOT a `.env` file parser and does not read `.env` itself -- Django
never reads `.env` automatically, and no dotenv dependency is used anywhere
in this project (accepted plan §5.8). Values reach the process environment
through `uv run --env-file .env ...` (developer machine) or through the
deployment platform's own environment injection (staging/production); by the
time these helpers run, everything they read is already a plain OS
environment variable.

These helpers only perform type coercion and, for `require`, a clear
fail-closed error when a mandatory value is absent -- never a default secret
and never a printed value.
"""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def env(name: str, default: str | None = None) -> str | None:
    """Return the raw string value of environment variable `name`, or `default`."""
    return os.environ.get(name, default)


def require(name: str) -> str:
    """Return the value of `name`, raising a clear, non-secret error if unset or blank.

    The error message never includes the configured value -- only the
    variable name -- so a misconfiguration report can never leak a secret.
    """
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise ImproperlyConfigured(
            f"Required environment variable {name!r} is not set. "
            "Set it in the local .env file (loaded via `uv run --env-file .env ...`) "
            "or in the deployment platform's environment configuration. "
            "This message never includes the value itself."
        )
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"Environment variable {name!r} must be one of "
        f"{sorted(_TRUE_VALUES | _FALSE_VALUES)!r} (case-insensitive); got an "
        "unrecognized value. The value itself is not included in this message."
    )


def env_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ImproperlyConfigured(
                f"Required integer environment variable {name!r} is not set."
            )
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name!r} must be an integer; got a value "
            "that could not be parsed. The value itself is not included in "
            "this message."
        ) from exc


def env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    """Comma-separated list, e.g. `DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1`."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_versioned_keys(prefix: str, versions: list[int]) -> dict[int, str | None]:
    """Return {version: value_or_None} for `f"{prefix}{version}"` per `version`.

    Used for the rate-limit HMAC key family, where the exact set of
    variable names to look up (`RATE_LIMIT_HMAC_KEY_V1`,
    `RATE_LIMIT_HMAC_KEY_V2`, ...) depends on the dynamic, configured list
    of active versions (accepted plan §5.5). Never raises -- a missing key
    is reported as `None` so the caller (static validation) can produce one
    aggregated, non-secret error message naming every missing variable.
    """
    return {version: os.environ.get(f"{prefix}{version}") for version in versions}
