"""Staging/production `STORAGES["default"]` configuration (Prompt 2 correction §6, §1 v3).

`storages.backends.s3boto3.S3Boto3Storage` falls back to django-storages'
OWN ambient `AWS_*`-named Django settings, and from there to boto3's own
ambient environment/credential-file resolution, whenever it is not given
explicit `OPTIONS`. This module proves staging and production instead wire
EVERY required parameter explicitly from this project's own `S3_STORAGE_*`
settings, so no ambient AWS credential can ever be used by construction --
without importing Django or connecting to any live service.

`querystring_auth=True` (v3 correction) applies to Django's OWN default
storage only -- a signed, time-limited URL is the safe default for
whatever calls `default_storage.url()`, even though this project's
protected-document flow never does: it uses `S3PrivateStorage`
(`apps.documents.storage`) instead, which exposes no URL method of any
kind and is proven so here as well, so this file is a self-contained
statement of the full v3 §1 requirement.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from apps.documents.storage import S3PrivateStorage
from scripts.check import SYNTHETIC_DEPLOY_ENV

_REQUIRED_OPTION_KEYS = {
    "bucket_name",
    "endpoint_url",
    "access_key",
    "secret_key",
    "region_name",
    "default_acl",
    "querystring_auth",
    "file_overwrite",
}


def _import_fresh_settings_module(monkeypatch: pytest.MonkeyPatch, module_name: str):
    for key, value in SYNTHETIC_DEPLOY_ENV.items():
        if key == "DJANGO_SETTINGS_MODULE":
            continue
        monkeypatch.setenv(key, value)
    # `config.settings.base` reads the environment only once, at its OWN
    # import time, then caches the results as plain module attributes --
    # `from .base import *` in an already-imported `staging`/`production`
    # module would silently reuse whatever values were captured the
    # FIRST time `base` was imported (e.g. by pytest-django loading
    # `config.settings.test` before this test's `monkeypatch.setenv` ran),
    # not the values just set here. Both modules must be dropped from
    # `sys.modules` so the whole chain re-executes against the current
    # environment.
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.delitem(sys.modules, "config.settings.base", raising=False)
    monkeypatch.delitem(sys.modules, "config.settings._deployment", raising=False)
    return importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", ["config.settings.staging", "config.settings.production"])
def test_s3_storage_options_are_explicit_and_project_owned(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    settings_module = _import_fresh_settings_module(monkeypatch, module_name)

    default_storage = settings_module.STORAGES["default"]
    assert default_storage["BACKEND"] == "storages.backends.s3boto3.S3Boto3Storage"
    options = default_storage["OPTIONS"]

    assert set(options.keys()) == _REQUIRED_OPTION_KEYS
    assert options["bucket_name"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_BUCKET_NAME"]
    assert options["endpoint_url"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_ENDPOINT_URL"]
    assert options["access_key"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_ACCESS_KEY_ID"]
    assert options["secret_key"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_SECRET_ACCESS_KEY"]
    assert options["region_name"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_REGION_NAME"]

    # Private-by-default, non-negotiable regardless of environment.
    assert options["default_acl"] == "private"
    # v3 correction §1: Django's own default storage must sign its URLs
    # (never an unsigned, non-expiring one) if `.url()` is ever called.
    assert options["querystring_auth"] is True
    assert options["file_overwrite"] is False


@pytest.mark.parametrize("module_name", ["config.settings.staging", "config.settings.production"])
def test_s3_storage_options_are_not_influenced_by_ambient_aws_env_vars(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key-must-not-be-used")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret-must-not-be-used")
    monkeypatch.setenv("AWS_STORAGE_BUCKET_NAME", "ambient-bucket-must-not-be-used")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")

    settings_module = _import_fresh_settings_module(monkeypatch, module_name)

    options = settings_module.STORAGES["default"]["OPTIONS"]
    rendered = repr(options)
    assert "ambient-key-must-not-be-used" not in rendered
    assert "ambient-secret-must-not-be-used" not in rendered
    assert "ambient-bucket-must-not-be-used" not in rendered
    assert options["access_key"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_ACCESS_KEY_ID"]
    assert options["bucket_name"] == SYNTHETIC_DEPLOY_ENV["S3_STORAGE_BUCKET_NAME"]


@pytest.mark.parametrize("module_name", ["config.settings.staging", "config.settings.production"])
def test_storages_default_never_references_an_aws_named_setting(
    monkeypatch: pytest.MonkeyPatch, module_name: str
) -> None:
    """No django-storages `AWS_*`-named Django setting is ever set by this
    project -- OPTIONS is the only channel, so a bare-backend fallback to
    an ambient `AWS_*` Django setting has nothing to fall back to."""
    settings_module = _import_fresh_settings_module(monkeypatch, module_name)

    aws_named_settings = [name for name in vars(settings_module) if name.startswith("AWS_")]
    assert aws_named_settings == []


def test_s3_private_storage_still_exposes_no_url_method() -> None:
    """The v3 §1 `querystring_auth=True` change applies ONLY to Django's own
    default storage -- `S3PrivateStorage`, which this project's protected
    document access actually uses, must remain interface-incapable of
    producing a URL at all, signed or otherwise."""
    method_names = {name for name in dir(S3PrivateStorage) if not name.startswith("_")}
    assert method_names == {"save", "open", "exists", "delete", "size", "content_type"}
    assert "url" not in method_names
