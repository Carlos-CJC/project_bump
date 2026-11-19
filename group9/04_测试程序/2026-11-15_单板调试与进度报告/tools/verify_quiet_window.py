"""Follow up the successful Wi-Fi run without periodic serial output or PC BLE."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence/motion_mqtt/direct_quiet_followup"
OUT.mkdir(exist_ok=False)
previous = json.loads((ROOT / "evidence/motion_mqtt/direct_instrumented/summary.json").read_text())
summary = {"started_utc": datetime.now(timezone.utc).isoformat(),
           "boot_id": previous["boot_id"], "periodic_output": False,
           "pc_ble_scanner": False, "physical_motion_controlled": False}
pending = b""
ports = [p for p in list_ports.comports() if p.vid == 0x2341 and
         (p.serial_number or "").upper() == "416523501C74AAC8"]
assert len(ports) == 1
s = serial.Serial(ports[0].device, 115200, timeout=.15, write_timeout=2)

def capture(stage, duration, until):
    global pending
    found = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        pending += s.read(s.in_waiting or 1)
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if not line.strip():
                continue
            data = json.loads(line)
            record = {"stage": stage, "host_utc": datetime.now(timezone.utc).isoformat(), "data": data}
            with (OUT / "serial_records.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
            found.append(data)
            if data.get("type") == until:
                return found
    raise TimeoutError(stage + ": " + until + " not received")

def command(text, stage, until, duration=3):
    s.write((text + "\n").encode())
    return capture(stage, duration, until)

try:
    initial = command("status", "initial", "status")[-1]
    assert initial["firmware_version"] == 2 and initial["queued"] == 0
    assert initial["transport"] == "wifi_mqtt" and initial["state"] == "IDLE"
    command("q", "quiet", "telemetry")
    trigger = command("inject_shake", "injection", "injection_complete", 8)
    event = [d["payload"] for d in trigger if d.get("type") == "event_queued"][0]
    assert event["boot_id"] == summary["boot_id"] and event["source"] == "injected_test"
    summary["event"] = event
    print("Waiting for one quiet 60-second window and automatic Wi-Fi upload", flush=True)
    finish = capture("quiet_natural_expiry", 85, "sync_complete")
    summary["completion"] = finish
    summary["final_status"] = command("status", "final", "status")[-1]
    summary["final_dump"] = command("dump", "dump", "queue_dump_complete")
    database = ROOT / "private/motion_hotspot_runtime/telemetry.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT canonical_json FROM telemetry WHERE device_id=? AND boot_id=? AND seq=?",
                          ("badge-a", summary["boot_id"], event["seq"])).fetchall()
    summary["payload_stored_exactly"] = len(rows) == 1 and json.loads(rows[0][0]) == event
    summary["success"] = (summary["payload_stored_exactly"] and
                          summary["final_status"]["queued"] == 0 and
                          any(d.get("type") == "application_ack" and d.get("matched") for d in finish))
except Exception as error:
    summary["success"] = False
    summary["error"] = str(error)
finally:
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUT / "serial_pending.txt").write_text(pending.decode(errors="replace"), encoding="utf-8")
    s.close()
print(json.dumps(summary), flush=True)
raise SystemExit(0 if summary.get("success") else 1)
