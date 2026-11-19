"""Verify ownership via the process's real working directory and complete argv."""
import json
import os
from pathlib import Path
import sys

import psutil

OPS = Path(__file__).resolve().parent
KEEPER = OPS / "Resume-TestHotspot.ps1"
POWERSHELL = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"

def exact_process(proc, script, state=None):
    argv = proc.cmdline()
    if not argv or Path(argv[0]).name.casefold() != "powershell.exe" or Path(proc.exe()) != POWERSHELL:
        return False
    args = argv[1:]
    prefix = ["-noprofile", "-executionpolicy", "bypass", "-file"]
    if [s.casefold() for s in args[:4]] != prefix or len(args) != (8 if state else 5):
        return False
    if state and (args[5].casefold() != "-statedirectory" or args[7].casefold() != "-keepon"):
        return False
    # Fast name filters also avoid inspecting unrelated PowerShell working directories.
    if Path(args[4]).name.casefold() != script.name.casefold():
        return False
    cwd = Path(proc.cwd())
    if (cwd / args[4]).resolve() != script:
        return False
    return state is None or (cwd / args[6]).resolve() == state

def run(action, path):
    state = Path(path).resolve(strict=True)
    session = json.loads((state / "session.json").read_text(encoding="utf-8-sig"))
    owner = psutil.Process(int(session["pid"]))
    user = psutil.Process().username().casefold()
    owner_script = (state.parents[1] / "tools/temporary_board_hotspot.ps1").resolve(strict=True)
    if owner.username().casefold() != user or not exact_process(owner, owner_script):
        raise RuntimeError("Original owner identity differs from this session")
    found = []
    for proc in psutil.process_iter(["name", "username"]):
        if proc.pid == owner.pid or (proc.info["name"] or "").casefold() != "powershell.exe":
            continue
        if (proc.info["username"] or "").casefold() != user:
            continue
        try:
            if exact_process(proc, KEEPER, state):
                found.append(proc)
        except psutil.NoSuchProcess:
            continue
    if len(found) > 1:
        raise RuntimeError("More than one exact session keeper is active")
    if action == "stop" and found:
        keeper = found[0]
        if keeper.pid == owner.pid or not exact_process(keeper, KEEPER, state):
            raise RuntimeError("Session keeper identity changed")
        keeper.terminate()  # psutil checks process identity against PID reuse.
        keeper.wait(5)
    return {"owner": owner.pid, "keepers": [p.pid for p in found]}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("inspect", "stop"):
        raise SystemExit("Usage: hotspot_keeper.py inspect|stop <state-directory>")
    print(json.dumps(run(sys.argv[1], sys.argv[2])))

