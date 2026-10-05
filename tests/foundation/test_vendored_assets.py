"""Vendored asset (Bootstrap, htmx) checksum verification (§5.3)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.check import check_vendored_asset_checksums


def test_vendored_asset_checksums_match_provenance() -> None:
    problems = check_vendored_asset_checksums()
    assert problems == [], f"vendored asset checksum mismatch: {problems}"


def _make_fixture_vendor_dir(tmp_path: Path) -> Path:
    vendor_dir = tmp_path / "example-vendor"
    vendor_dir.mkdir()
    asset_content = b"body { color: red; }"
    (vendor_dir / "example.min.css").write_bytes(asset_content)
    digest = hashlib.sha256(asset_content).hexdigest()
    (vendor_dir / "PROVENANCE.md").write_text(
        "# Vendored asset provenance -- example\n\n"
        "## Vendored project files and SHA-256 checksums\n\n"
        "| File | SHA-256 |\n"
        "| --- | --- |\n"
        f"| `example.min.css` | `{digest}` |\n"
    )
    return vendor_dir


def test_reports_a_missing_listed_asset(tmp_path: Path) -> None:
    vendor_dir = _make_fixture_vendor_dir(tmp_path)
    (vendor_dir / "example.min.css").unlink()

    problems = check_vendored_asset_checksums([vendor_dir])

    assert any("is missing" in p for p in problems)


def test_reports_a_modified_asset(tmp_path: Path) -> None:
    vendor_dir = _make_fixture_vendor_dir(tmp_path)
    (vendor_dir / "example.min.css").write_bytes(b"body { color: blue; /* tampered */ }")

    problems = check_vendored_asset_checksums([vendor_dir])

    assert any("checksum mismatch" in p for p in problems)


def test_reports_an_unexpected_extra_asset(tmp_path: Path) -> None:
    vendor_dir = _make_fixture_vendor_dir(tmp_path)
    (vendor_dir / "unexpected-extra-file.js").write_text("console.log('not in PROVENANCE.md');")

    problems = check_vendored_asset_checksums([vendor_dir])

    assert any("unexpected-extra-file.js" in p and "unexpected file" in p for p in problems)


def test_a_correct_exact_directory_reports_no_problems(tmp_path: Path) -> None:
    vendor_dir = _make_fixture_vendor_dir(tmp_path)

    problems = check_vendored_asset_checksums([vendor_dir])

    assert problems == []
