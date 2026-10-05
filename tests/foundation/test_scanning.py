"""Malware-scanning adapter interface (TRD SEC-006, accepted plan risk R5)."""

from __future__ import annotations

import io

from apps.documents.scanning import DeterministicStubScanner


def test_deterministic_stub_scanner_always_reports_clean() -> None:
    scanner = DeterministicStubScanner()
    result = scanner.scan(io.BytesIO(b"anything"))
    assert result.clean is True
