"""Drive the white-button handler on two live boards via their existing serial collector."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time

from ir_baseline import BASE

def verify(session):
    serial_log = session / "serial.jsonl"
    initial = serial_log.stat().st_size
    side_names = {"A", "B"}
    def send(command, side="both"):
        subprocess.run([sys.executable, str(BASE.parent / "ops/board_session.py"),
                        "send", str(session), side, command], check=True)
    def events():
        result = []
        with serial_log.open("rb") as f:
            f.seek(initial)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                record = json.loads(line)
                if record["kind"] != "rx" or record["board"] not in side_names:
                    continue
                try:
                    payload = json.loads(record["data"])
                except ValueError:
                    continue
                result.append((record["board"], payload))
        return result
    def both(predicate, start=0):
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            found = {side: obj for side, obj in events()[start:] if predicate(side, obj)}
            if found.keys() == side_names:
                return found
            time.sleep(.06)
        raise RuntimeError("Missing response from one or both boards")
    def statuses(start, paused):
        send("status")
        return both(lambda side,d: d.get("type") == "status" and
                    d["paused"] == paused and not d["discovery"] and
                    d["ble_phase"] == "idle" and d["queued"] == 0, start)
    start = len(events())
    initial_status = statuses(start, False)
    send("discover")
    expected = {"A": "ble_scan_ready", "B": "ble_advertise_ready"}
    ready = both(lambda side,d: d.get("detail") == expected[side], start)
    sent_at = len(events())
    send("w")
    end = both(lambda side,d: d.get("type") == "discovery_end" and not d["complete"], sent_at)
    paused = both(lambda side,d: d.get("type") == "privacy" and d["detail"] == "discovery_paused", sent_at)
    pause_status = statuses(sent_at, True)
    blocked_at = len(events())
    send("discover")
    blocked = both(lambda side,d: d.get("type") == "discovery_ignored" and d["detail"] == "paused", blocked_at)
    # Only A transmits infrared. B stays idle and cannot form an invitation.
    ir_at = len(events())
    send("b", "A")
    send("w", "A")
    send("status")
    ir_status = both(lambda side,d: d.get("type") == "status" and d["paused"] and not d["discovery"] and not d["ir_paired"], ir_at)
    resumed_at = len(events())
    send("w")
    resumed = both(lambda side,d: d.get("type") == "privacy" and d["detail"] == "discovery_resumed", resumed_at)
    final = statuses(resumed_at, False)
    rows=events()
    assert all(d.get("type") not in ("ble_encounter_rgb","queued") for _,d in rows)
    assert all(initial_status[b]["boot_id"] == final[b]["boot_id"] for b in side_names)
    return dict(trigger="serial w uses the physical D2 handler; no manual press in this run",
                initial_status=initial_status, active_roles=ready, cancelled=end, pause_events=paused,
                paused_status=pause_status, ignored_discovery=blocked, privacy_after_local_ir_cancel=ir_status,
                resumed=resumed, final=final, no_encounter_rgb_after_cancel=True)

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("evidence", type=Path)
    args=parser.parse_args()
    with args.evidence.open("x", encoding="utf8") as f:
        json.dump(verify(args.session), f, indent=2)
    print("PASS: scan and advertising cancelled, privacy persisted through IR cancellation, both resumed.")
