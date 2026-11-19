"""Exercises the actual v1 panel scripts without touching any runtime."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_protocol_lab_frontend():
    node = os.environ.get("BUMP_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node is optional; install it to run the protocol panel checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("frontend_compat_lab_test.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 protocol lab frontend checks passed" in result.stdout
