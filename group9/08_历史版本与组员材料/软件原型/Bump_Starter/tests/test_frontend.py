"""Optional Node check; the Python application does not require Node."""

from pathlib import Path
import os
import shutil
import subprocess

import pytest


def test_browser_session_races():
    node = os.environ.get("BUMP_NODE") or shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; install it to run frontend race checks")
    script = Path(__file__).with_name("frontend_session_test.cjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True,
        encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 frontend session race checks passed" in result.stdout
