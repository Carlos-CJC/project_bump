"""Verify an existing final status against same-board serial evidence. Read only by default."""

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from bump.v1_store import SYNC_SCHEMA, V1Store
from bump.v1_wire import parse_message


def successful_reports(path):
    """Yield completed 0.5.8-dual cycles with an explicit offline publish and its PUBACK."""
    pending = {}
    with path.open(encoding="utf-8-sig") as lines:
        for line in lines:
            entry = json.loads(line)
            board = entry.get("board")
            if entry.get("kind") != "rx" or not isinstance(entry.get("data"), str):
                continue
            try:
                item = json.loads(entry["data"])
            except ValueError:
                continue  # The UART also prints human-readable diagnostics.
            if not isinstance(item, dict):
                continue
            stamp = datetime.fromisoformat(entry["utc"]).timestamp()
            kind = item.get("type")
            if kind == "wire" and item.get("phase") == "transmit" and item.get("source") == "presence":
                pending.pop(board, None)
                body = parse_message(item["topic"], json.dumps(item["payload"]).encode(),
                                     observed_at=stamp)["body"]
                if body["fw"] == "0.5.8-dual" and body["state"] == "offline" and body["queue_depth"] == 0:
                    pending[board] = (body, stamp, None)
            elif board in pending:
                body, sent, ack = pending[board]
                if kind == "sync_stage" and item.get("stage") == "presence_offline":
                    elapsed = item["elapsed_ms"]
                    same_uptime = abs(item["uptime_ms"] - elapsed - body["uptime_s"] * 1000) <= 1005
                    if item["result"] == 1 and same_uptime and 0 <= stamp - sent <= 5:
                        pending[board] = (body, sent, stamp)
                    else:
                        pending.pop(board)
                elif kind == "sync_complete":
                    if (ack is not None and item.get("queued") == 0 and
                            item.get("transport") == "wifi_mqtt_tcp" and 0 <= stamp - ack <= 5):
                        yield body, sent, ack
                    pending.pop(board)
                elif kind == "radio" and item.get("detail") == "wifi_begin":
                    pending.pop(board)


def verify(database, serial_log, apply=False):
    reports = list(successful_reports(serial_log))
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=" + mode, uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        if apply:
            db.execute(SYNC_SCHEMA)
        matched = []
        rows = db.execute("""SELECT s.*,b.short FROM live_status s JOIN bindings b
                          ON b.device=s.device AND b.boot=s.boot
                          WHERE s.state='offline' AND s.queue_depth=0 ORDER BY s.device""").fetchall()
        for row in rows:
            observed = row["observed_at"]
            candidates = [body for body, sent, ack in reports
                          if (body["device_id"],body["boot_id"],body["self_short_id"],
                              body["uptime_s"],body["events_dropped"]) ==
                             (row["device"],row["boot"],row["short"],row["uptime_s"],row["events_dropped"])
                          and sent - 0.1 <= observed <= ack + 0.1]
            if len(candidates) != 1:
                continue
            if apply:
                # Never overwrite a transport outcome captured by the new listener.
                previous = db.execute("SELECT observed_at FROM badge_sync WHERE device=?",
                                      (row["device"],)).fetchone()
                if previous is None or previous["observed_at"] < observed:
                    V1Store._sync_evidence(db, row["device"], observed, observed, "verified_serial", 0)
            matched.append({"device": row["device"], "boot": row["boot"], "observed_at": observed})
        return matched


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--serial-log", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps({"apply": args.apply, "matched": verify(args.database, args.serial_log, args.apply)}))
