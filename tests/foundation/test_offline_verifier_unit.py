"""Run the actual offline verifier with real WebCrypto, without a browser/DB.

Uses the Node executable already shipped with the pinned Playwright package.
It does not measure supported-browser performance or emulate IndexedDB.
"""

import json
import os
import subprocess
from pathlib import Path

import playwright

from apps.entry.offline_contract import client_config

ROOT = Path(__file__).resolve().parents[2]


def test_offline_verifier_security_and_state_regressions():
    node = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
    result = subprocess.run(  # noqa: S603 - pinned local driver and project test only
        [
            str(node),
            str(ROOT / "tests/assets/offline_verifier_unit.cjs"),
            str(ROOT / "static/js/entry-offline.js"),
        ],
        input=json.dumps(client_config()),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] == 18
