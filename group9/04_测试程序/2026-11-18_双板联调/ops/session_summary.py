"""Summarize raw USB evidence without changing either badge or the database."""
import argparse
import json
from pathlib import Path


def summarize(path):
    windows = {"A": [], "B": []}
    states = {}
    noteworthy = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row["kind"] != "rx" or not row["data"].startswith("{"):
                continue
            body = json.loads(row["data"])
        except (KeyError, ValueError):
            continue
        side = row["board"]
        kind = body.get("type")
        if kind == "status":
            states[side] = {key: body.get(key) for key in ("discovery", "window_remaining_ms", "queued",
                                                           "motion_triggers", "ir_paired", "paused", "imu_samples")}
        elif kind == "discovery_start":
            windows[side].append(dict(start=row["utc"], source=body["source"], samples=[], end=None))
        elif kind == "ble_observation" and windows[side]:
            windows[side][-1]["samples"].append([body["peer_short_id"], body["rssi"], body["uptime_ms"]])
        elif kind == "discovery_end" and windows[side]:
            windows[side][-1]["end"] = row["utc"]
            windows[side][-1]["complete"] = body["complete"]
        elif kind == "wire" and body.get("phase") in ("queued","transmit") and not body.get("topic","").endswith("status"):
            noteworthy.append([row["utc"],side,body["phase"],body["topic"],body["payload"]])
        elif kind in ("mqtt_puback", "application_ack", "queue_remove", "dequeued", "sync_complete"):
            noteworthy.append([row["utc"], side, kind, body])
    return {"windows": windows, "states": states, "events": noteworthy}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("serial_jsonl", type=Path)
    p.add_argument("--last", type=int, default=3)
    data = p.parse_args()
    result = summarize(data.serial_jsonl)
    for side in result["windows"]:
        result["windows"][side] = result["windows"][side][-data.last:]
    result["events"] = result["events"][-data.last*4:]
    print(json.dumps(result, ensure_ascii=True))
