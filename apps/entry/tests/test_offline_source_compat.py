"""Source-compatibility and import-level coverage for the Phase 4 Prompt 2
modules (independent re-review correction 1).

The project runs on Python 3.14, where PEP 758 allows `except A, B:`; the
py314 formatter even produces that form. An independent reviewer compiling
with an earlier parser rejects it. Every Prompt 2 Python module must
therefore parse on the Python 3.13 grammar, and the self-test vector loader
(where the form appeared) must be exercised on both of its error paths.
No database.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from django.conf import settings

PROMPT2_PYTHON_MODULES = (
    "apps/accounts/mfa.py",
    "apps/accounts/tests/mfa_double.py",
    "apps/audit/action_codes.py",
    "apps/core/credential_shapes.py",
    "apps/core/crypto/package_signing.py",
    "apps/core/redaction.py",
    "apps/entry/api/__init__.py",
    "apps/entry/api/errors.py",
    "apps/entry/api/permissions.py",
    "apps/entry/api/views.py",
    "apps/entry/apps.py",
    "apps/entry/checks.py",
    "apps/entry/forms/__init__.py",
    "apps/entry/management/commands/cleanup_offline_packages.py",
    "apps/entry/migrations/0003_offline_preparation.py",
    "apps/entry/migrations/0004_offline_build_lifecycle_and_journal.py",
    "apps/entry/models/__init__.py",
    "apps/entry/observability.py",
    "apps/entry/offline_contract.py",
    "apps/entry/offline_views.py",
    "apps/entry/presentation.py",
    "apps/entry/services/devices.py",
    "apps/entry/services/offline_crypto.py",
    "apps/entry/services/offline_devices.py",
    "apps/entry/services/offline_grants.py",
    "apps/entry/services/offline_journal.py",
    "apps/entry/services/offline_packages.py",
    "apps/entry/services/sessions.py",
    "apps/entry/tasks.py",
    "apps/entry/urls.py",
    "apps/entry/views.py",
    "apps/entry/tests/conftest.py",
    "apps/entry/tests/offline_factories.py",
    "apps/entry/tests/test_devices.py",
    "apps/entry/tests/test_offline_api.py",
    "apps/entry/tests/test_offline_contract.py",
    "apps/entry/tests/test_offline_devices.py",
    "apps/entry/tests/test_offline_journal.py",
    "apps/entry/tests/test_offline_migration.py",
    "apps/entry/tests/test_offline_packages.py",
    "apps/entry/tests/test_offline_source_compat.py",
    "apps/entry/tests/test_offline_views.py",
    "apps/events/models/__init__.py",
    "config/settings/base.py",
    "tests/browser/offline_urls.py",
    "tests/browser/test_offline_pwa.py",
    "tests/concurrency/test_offline_concurrency.py",
)


def _source(relative: str) -> str:
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", PROMPT2_PYTHON_MODULES)
def test_prompt2_module_parses_on_the_python_3_13_grammar(relative):
    tree = ast.parse(_source(relative), filename=relative, feature_version=(3, 13))
    # And compiles on the running interpreter.
    compile(tree, relative, "exec")


def test_the_pep_758_form_is_rejected_by_this_check():
    """Proves the check above is meaningful: the exact defect is caught."""
    source = "try:\n    pass\nexcept OSError, ValueError:\n    pass\n"
    with pytest.raises(SyntaxError):
        ast.parse(source, feature_version=(3, 13))


def test_prompt2_view_and_service_modules_import():
    for name in (
        "apps.entry.offline_views",
        "apps.entry.api.views",
        "apps.entry.services.offline_crypto",
        "apps.entry.services.offline_devices",
        "apps.entry.services.offline_grants",
        "apps.entry.services.offline_journal",
        "apps.entry.services.offline_packages",
        "apps.entry.offline_contract",
        "apps.entry.tasks",
    ):
        assert importlib.import_module(name) is not None


def test_self_test_vector_loads_the_committed_public_vector():
    from apps.entry.offline_views import _self_test_vector

    vector = _self_test_vector()
    assert set(vector) >= {"spki", "message", "signature"}


def test_self_test_vector_is_empty_when_the_file_is_missing(tmp_path, settings):
    from apps.entry.offline_views import _self_test_vector

    settings.BASE_DIR = tmp_path  # OSError path
    assert _self_test_vector() == {}


def test_self_test_vector_is_empty_when_the_file_is_malformed(tmp_path, settings):
    from apps.entry.offline_views import _self_test_vector

    target = tmp_path / "tests" / "assets" / "offline_vectors"
    target.mkdir(parents=True)
    (target / "es256_self_test.json").write_text("{not json", encoding="utf-8")
    settings.BASE_DIR = tmp_path  # ValueError path
    assert _self_test_vector() == {}


def test_no_code_reads_the_deprecated_gate_offline_policy():
    """independent re-review correction D: `Gate.offline_policy` is legacy and
    non-authoritative. Offline continuity is decided only by the global
    flag, the per-event setting and the current versioned device scope, so
    no application module (outside the events model and its migration) may
    reference the field."""
    root = Path(settings.BASE_DIR)
    allowed = {
        "apps/events/models/__init__.py",
        "apps/events/migrations/0004_venue_zone_gate.py",
        "apps/entry/tests/test_offline_source_compat.py",
    }
    offenders = []
    for path in (root / "apps").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in allowed:
            continue
        if "offline_policy" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    for path in [*(root / "templates").rglob("*.html"), *(root / "static/js").rglob("*.js")]:
        if "offline_policy" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(root).as_posix())
    assert offenders == []
