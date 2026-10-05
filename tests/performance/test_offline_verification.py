"""Prompt 3 provisional functional browser benchmark; hardware confirmation pending.

10,000 synthetic entries, production package activation and QR verifier,
one cold lookup/key import followed by 100 warm scans. No CPU throttling.
This is not the formal Prompt 5 capacity/rehearsal benchmark.
"""

import json
import platform
from pathlib import Path

import pytest

from tests.offline_browser_support import OfflineBrowserWorld

pytestmark = pytest.mark.performance


def test_offline_verification_latency(page):
    world = OfflineBrowserWorld(page, count=10_000)
    measured = page.evaluate(
        """async qr => {
      const a=AscOffline; a.state.index=null; a.state.qrKeys={};
      const times=[];
      for(let i=0;i<101;i++) {
        const start=performance.now();const v=await a.verifyOfflineQr(qr);
        if(v.result!=='ALLOWED') throw Error('Synthetic benchmark did not verify');
        times.push(performance.now()-start);
      }
      const cold=times.shift();times.sort((a,b)=>a-b);
      return {cold_ms:cold,warm_runs:times.length,median_ms:(times[49]+times[50])/2,
        p95_ms:times[94],maximum_ms:times[99],user_agent:navigator.userAgent};
    }""",
        world.qr,
    )
    report = {
        "status": "MEASURED_ON_TEST_HOST",
        "hardware_confirmation": "NOT_EXECUTED",
        "entry_count": 10_000,
        "encrypted_package_bytes": world.package_bytes,
        "platform": platform.platform(),
        "cpu": platform.machine(),
        "cpu_throttle": 1,
        "method": "Actual Chromium WebCrypto verifier after production package activation",
        **measured,
    }
    path = (
        Path(__file__).resolve().parents[2]
        / "var/test_artifacts/phase4/prompt3-offline-performance.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert measured["cold_ms"] < 500 and measured["maximum_ms"] < 500
