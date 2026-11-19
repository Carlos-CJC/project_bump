"""Verify event-driven MQTT clocks from new boot IDs with no earlier Wi-Fi connection."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import run_dual_reliability as live

def statuses(log):
    states = {}
    for row in log_rows(log):
        if row["payload"].get("type") == "status":
            states[row["board"]] = row["payload"]
    return states

def log_rows(log):
    with log.open("rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                break
            row = json.loads(line)
            if row["kind"] != "rx" or row["board"] not in live.BOARDS:
                continue
            try:
                row["payload"] = json.loads(row["data"])
            except ValueError:
                continue
            yield row

def configure_after_starts(session, hotspot, runtime):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        started = {r["board"] for r in log_rows(session/"serial.jsonl")
                   if r["payload"].get("type") == "discovery_start"}
        if len(started) == 2:
            tool = Path(__file__).resolve().parents[2] / "ops/board_session.py"
            subprocess.run([sys.executable, "-B", str(tool), "configure", str(session), "both",
                            "--hotspot", str(hotspot), "--runtime", str(runtime)], check=True)
            return
        time.sleep(.1)
    raise RuntimeError("Both fresh discovery windows must be active before network configuration")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("session", "hotspot", "runtime", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument('--b-leads-by', type=float, default=0.0)
    args=parser.parse_args()
    assert 0 <= args.b_leads_by <= 15
    log=args.session/"serial.jsonl"
    identity=statuses(log)
    assert set(identity)==set(live.BOARDS)
    assert all(s["fw"]=="0.5.8-dual" and not s["network_configured"] and not s["discovery"] and
               not s["queued"] and not s["paused"] for s in identity.values())
    boots=tuple(identity[b]["boot_id"] for b in live.BOARDS)
    db_path=(args.runtime/"live.sqlite3").resolve()
    opts=SimpleNamespace(source="serial_diagnostic", b_leads_by=args.b_leads_by, start_timeout=15,
                         window_timeout=82, ack_timeout=65, max_start_skew=2)
    with closing(sqlite3.connect(db_path.as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        assert not db.execute("select 1 from clock_samples where boot in (?,?)",boots).fetchone()
        assert not db.execute("select 1 from resolved where boot in (?,?)",boots).fetchone()
        past={r[0] for r in db.execute("select event_id from resolved")}
        reader=live.SerialLog(log)
        with args.output.open("x", encoding="utf8") as out, ThreadPoolExecutor(max_workers=1) as workers:
            live.write_record(out,dict(record="fresh_boot", at=live.utc(), boots=boots, network_configured=False,
                                       prior_mqtt_clock_samples=0, prior_events=0))
            config=workers.submit(configure_after_starts,args.session,args.hotspot,args.runtime)
            result=live.run_round(1,reader,db,args.session/"commands",identity,opts,past)
            config.result()
            live.write_record(out,result)
            print("Two physical observations",result["both_observed"],"65-second delivery",result["passed"],flush=True)
            return 0 if result["passed"] else 1

if __name__=="__main__":
    raise SystemExit(main())
