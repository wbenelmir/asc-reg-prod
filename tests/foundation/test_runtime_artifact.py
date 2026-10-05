"""Runtime-artifact tooling: the static build, the manifest and its verification.

The static build runs in a temporary copy of the source tree with the
documented synthetic, non-secret values; no real secret and no service is
used, and the repository tree itself is not written.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scripts.check import (
    BASE_DIR,
    compute_artifact_manifest,
    main,
    run_static_build,
    verify_artifact,
)


def _copy_source(target: Path) -> Path:
    for name in ("apps", "config", "static", "templates", "locale", "scripts"):
        shutil.copytree(
            BASE_DIR / name,
            target / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"),
        )
    shutil.copy2(BASE_DIR / "manage.py", target / "manage.py")
    return target


def test_the_static_build_needs_no_secret_and_writes_only_staticfiles(tmp_path) -> None:
    tree = _copy_source(tmp_path / "artifact")
    before = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    result = run_static_build(tree)
    assert result.returncode == 0, result.stderr[-2000:]
    assert (tree / "staticfiles" / "staticfiles.json").is_file()
    after = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    added = after - before
    assert added and all(path.startswith("staticfiles/") for path in added)
    assert not list(tree.rglob("__pycache__"))


def _artifact(tmp_path: Path) -> Path:
    root = tmp_path / "frozen"
    (root / "app").mkdir(parents=True)
    (root / "app" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "staticfiles").mkdir()
    (root / "staticfiles" / "staticfiles.json").write_text("{}", encoding="utf-8")
    return root


def test_manifest_and_verify_detect_every_kind_of_change(tmp_path) -> None:
    root = _artifact(tmp_path)
    manifest = tmp_path / "artifact.sha256"
    manifest.write_text("\n".join(compute_artifact_manifest(root)) + "\n", encoding="utf-8")
    assert verify_artifact(root, manifest) == {"missing": [], "added": [], "changed": []}

    (root / "app" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "module.cpython-314.pyc").write_bytes(b"x")
    (root / "staticfiles" / "staticfiles.json").unlink()
    differences = verify_artifact(root, manifest)
    assert differences["changed"] == ["app/module.py"]
    assert differences["added"] == ["app/__pycache__/module.cpython-314.pyc"]
    assert differences["missing"] == ["staticfiles/staticfiles.json"]


def test_a_symbolic_link_is_recorded_by_its_target(tmp_path) -> None:
    root = _artifact(tmp_path)
    try:
        os.symlink("module.py", root / "app" / "alias.py")
    except (OSError, NotImplementedError):  # fmt: skip
        pytest.skip("this account cannot create symbolic links")
    lines = compute_artifact_manifest(root)
    assert "symlink:module.py  app/alias.py" in lines


def test_the_cli_writes_outside_the_artifact_and_verifies_read_only(tmp_path, capsys) -> None:
    root = _artifact(tmp_path)
    inside = root / "manifest.sha256"
    assert main(["artifact-manifest", "--root", str(root), "--output", str(inside)]) == 2
    assert not inside.exists()
    manifest = tmp_path / "manifest.sha256"
    assert main(["artifact-manifest", "--root", str(root), "--output", str(manifest)]) == 0
    before = sorted(p.as_posix() for p in root.rglob("*"))
    assert main(["artifact-verify", "--root", str(root), "--manifest", str(manifest)]) == 0
    assert sorted(p.as_posix() for p in root.rglob("*")) == before
    (root / "app" / "module.py").write_text("tampered\n", encoding="utf-8")
    assert main(["artifact-verify", "--root", str(root), "--manifest", str(manifest)]) == 1
    output = capsys.readouterr().out
    assert "changed: app/module.py" in output


def test_other_subcommands_still_refuse_unknown_arguments() -> None:
    with pytest.raises(SystemExit):
        main(["secret-scan", "--root", "x"])
