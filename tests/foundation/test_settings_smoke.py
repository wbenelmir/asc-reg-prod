"""Smoke checks over the loaded `config.settings.test` module.

The fact that pytest-django could load DJANGO_SETTINGS_MODULE at all is
already a strong signal that base.py/test.py are importable and internally
consistent; these assertions pin down the specific Phase 1 Prompt 2
invariants the accepted plan calls out by name.
"""

from __future__ import annotations

from django.apps import apps
from django.conf import settings


def test_sixteen_project_apps_are_installed() -> None:
    # Phase 2 Prompt 2 adds `invitations`; Phase 2 Prompt 3 adds `reviews`;
    # Phase 2 Prompt 4 adds `accreditation`; Phase 2 Prompt 5 adds `exports`;
    # Phase 3 Prompt 2 adds `badges`; Phase 3 Prompt 4 adds `entry` (TRD
    # §13.1's own later-phase application names; ADR-0001, ADR-0017,
    # ADR-0021) -- the ten Phase 1 apps below are otherwise unchanged.
    expected = {
        "core",
        "accounts",
        "events",
        "people",
        "organizations",
        "registrations",
        "documents",
        "privacy",
        "communications",
        "audit",
        "invitations",
        "reviews",
        "accreditation",
        "exports",
        "badges",
        "entry",
    }
    installed = {cfg.label for cfg in apps.get_app_configs() if cfg.name.startswith("apps.")}
    assert installed == expected


def test_auth_user_model_is_the_custom_operational_user() -> None:
    # ADR-0011: accounts.OperationalUser is the project's custom user from
    # accounts.0001 onward, confirmed by Prompt 3's read-only pre-flight
    # gate before it was ever set.
    assert settings.AUTH_USER_MODEL == "accounts.OperationalUser"


def test_database_engine_is_postgresql_only() -> None:
    assert settings.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"


def test_celery_runs_eager_in_test_with_no_broker() -> None:
    assert settings.CELERY_TASK_ALWAYS_EAGER is True
    assert settings.CELERY_TASK_EAGER_PROPAGATES is True
    assert settings.CELERY_BROKER_URL == "memory://"


def test_email_backend_is_the_in_memory_test_sink() -> None:
    assert settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"


def test_private_storage_is_filesystem_backed_locally() -> None:
    assert settings.PRIVATE_STORAGE_BACKEND == "filesystem"


def test_every_project_app_has_at_least_one_model() -> None:
    # Prompt 3 introduces the Phase 1 data foundation across every app
    # (superseding the Prompt 2 "no models yet" invariant this test used
    # to pin).
    for cfg in apps.get_app_configs():
        if cfg.name.startswith("apps."):
            assert list(cfg.get_models()), f"{cfg.label} unexpectedly has no models"


def test_django_admin_is_not_installed_outside_local() -> None:
    assert not apps.is_installed("django.contrib.admin")
