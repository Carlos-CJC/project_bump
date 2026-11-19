"""Execute the same portable C++ core included by the Arduino sketch."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def firmware_binary(tmp_path_factory):
    configured = os.environ.get("BUMP_CXX")
    local_zig = ROOT / "evidence/firmware/tools/ziglang/zig.exe"
    compiler = configured or (
        str(local_zig) if local_zig.exists()
        else shutil.which("g++") or shutil.which("clang++")
    )
    if not compiler:
        pytest.skip("Set BUMP_CXX to g++, clang++, or zig to execute C++ core tests")
    build = tmp_path_factory.mktemp("firmware")
    executable = build / ("core-tests.exe" if os.name == "nt" else "core-tests")
    command = [compiler]
    if Path(compiler).name.lower() in ("zig", "zig.exe"):
        command += ["c++"]
    command += [
        "-std=c++11", "-Wall", "-Wextra", "-Werror",
        str(ROOT / "firmware/test_core.cpp"), "-o", str(executable),
    ]
    env = os.environ.copy()
    env["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / "evidence/firmware/build/zig-cache")
    env["ZIG_LOCAL_CACHE_DIR"] = str(build / "zig-local")
    compiled = subprocess.run(command, capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=240)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return executable


def test_portable_firmware_regressions(firmware_binary):
    result = subprocess.run([str(firmware_binary)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL_FIRMWARE_CORE_TESTS_PASSED" in result.stdout


def test_firmware_json_matches_backend_contract(firmware_binary):
    from bump.protocol import validate_event
    result = subprocess.run([str(firmware_binary), "json"], capture_output=True,
                            text=True, timeout=30, check=True)
    event = json.loads(result.stdout)
    settings = {
        "devices": {"badge-a": {"owner": "alice", "alias": "A00001"},
                    "badge-b": {"owner": "bob", "alias": "B00002"}},
        "sessions": {"demo-001": {"A00001": "badge-a", "B00002": "badge-b"}},
    }
    accepted = validate_event(event, "badge-a", settings)
    assert accepted["source"] == "simulated"
    assert accepted["time_quality"] == "unknown"
    assert accepted["start_utc"] is None and accepted["end_utc"] is None
