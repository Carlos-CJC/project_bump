"""Use verified local serial evidence to restore an independent event clock."""

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from bump import boot_clock
from bump.v1_store import V1Store, _digest
from bump.v1_wire import MAX_PAYLOAD_BYTES, parse_message, event_interval


def utc_stamp(text):
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError("evidence must include its UTC offset")
    return when.timestamp()


def serial_evidence(paths):
    samples, envelopes = [], []
    for path in paths:
        requested, responses = {}, {}
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    board = record["board"]
                    when = utc_stamp(record["utc"])
                    kind, raw = record["kind"], record["data"]
                except (KeyError, TypeError, ValueError):
                    continue
                if kind == "tx":
                    requested.pop(board, None)
                    responses.pop(board, None)
                    if raw == "status":
                        requested[board] = when
                    continue
                if kind != "rx":
                    continue
                # The real explicit command ends with the nearfield STATUS
                # line. Discard streaming-only status and ambiguous responses.
                if isinstance(raw, str) and raw.startswith("STATUS ") and board in requested:
                    trigger = requested.pop(board)
                    possible = responses.pop(board, [])
                    if len(possible) == 1 and 0 <= when - trigger <= 0.30:
                        arrived, body = possible[0]
                        if all(key in body for key in ("device_id", "boot_id", "self_short_id", "uptime_ms")):
                            # TX is logged after the write, not before it.
                            samples.append((body["device_id"], body["boot_id"], body["self_short_id"],
                                            body["uptime_ms"], trigger - 0.051, arrived + 0.002))
                    continue
                try:
                    body = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                if not isinstance(body, dict):
                    continue
                if (body.get("type") == "status" and board in requested and
                        0 <= when - requested[board] <= 0.30):
                    responses.setdefault(board, []).append((when, body))
                topic = body.get("topic")
                if (body.get("type") == "wire" and body.get("phase") == "transmit"
                        and isinstance(topic, str) and topic.endswith("/encounters")):
                    payload = body.get("payload")
                    try:
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
                            envelopes.append((topic, payload))
                    except (TypeError, ValueError):
                        continue
    return samples, envelopes


def copy_readonly(source, target):
    source, target = source.resolve(), target.resolve()
    if source == target or target.exists():
        raise ValueError("choose a fresh destination distinct from the read-only source")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as existing:
        existing.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as clone:
            existing.backup(clone)
            if clone.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("copy failed database integrity check")


def reconcile(path, paths):
    samples, envelopes = serial_evidence(paths)
    store = V1Store(path, {})
    inserted_samples = inserted_events = unsupported = 0
    affected = set()
    with store._transaction(write=True) as db:
        events = {row["event_id"]: row for row in db.execute(
            """SELECT * FROM observations WHERE transport='mqtt' AND time_quality='estimated'""")}
        for device, boot, short, uptime, low, high in samples:
            if boot_clock.add_sample(db, device, boot, short, uptime, low, high, "serial_status"):
                inserted_samples += 1
                affected.add((device, boot))
        for topic, payload in envelopes:
            possible = [events.get(e.get("event_id")) for e in payload["events"] if isinstance(e, dict)]
            selected = [old for old in possible if old is not None]
            if not selected:
                continue
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            if len(encoded) > MAX_PAYLOAD_BYTES:
                unsupported += 1
                continue
            for prior in selected:
                try:
                    body = parse_message(topic, encoded, observed_at=prior["received_at"],
                                         authenticated_device_id=prior["device"])["body"]
                    candidate = next(e for e in body["events"] if e["event_id"] == prior["event_id"])
                    if (_digest([prior["device"],body["boot_id"],candidate]) != prior["event_hash"]
                            or body["boot_id"] != prior["boot"]
                            or abs(event_interval(body,candidate,prior["received_at"])[1]-prior["seen_at"])>0.002):
                        continue
                except (ValueError, StopIteration):
                    unsupported += 1
                    continue
                if boot_clock.add_event(db, prior["event_id"], candidate["occurred_uptime_ms"], body["uptime_ms"]):
                    inserted_events += 1
                    affected.add((prior["device"], prior["boot"]))
        for device, boot in affected:
            boot_clock.refresh(db, device, boot)
        projected = db.execute("SELECT COUNT(*) FROM clock_projection").fetchone()[0]
        return {"serial_samples_paired":len(samples),"new_anchors":inserted_samples,
                "new_event_payloads_verified":inserted_events, "projected_events_total":projected,
                "invalid_wire_samples_skipped":unsupported}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="intended candidate database")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--copy-from", type=Path, help="read-only source for a fresh candidate copy")
    mode.add_argument("--existing-candidate", action="store_true", help="apply to the explicitly designated candidate")
    parser.add_argument("--serial-log", required=True, nargs="+", type=Path)
    args = parser.parse_args()
    database = args.database.resolve()
    if args.copy_from:
        copy_readonly(args.copy_from, database)
    if not database.is_file():
        parser.error("candidate database must already exist")
    print(json.dumps(reconcile(database,args.serial_log),sort_keys=True))


if __name__ == "__main__":
    main()

