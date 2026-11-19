"""Audit native MQTT clocks and eventual delivery using the raw commands and read-only SQLite."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from contextlib import closing

def audit(session, driver, runtime):
    log=[json.loads(l) for l in (session/"serial.jsonl").read_text(encoding="utf8").splitlines()]
    measurements=[json.loads(l) for l in driver.read_text(encoding="utf8").splitlines()]
    trial=next(r for r in measurements if r.get("record")=="round")
    out=dict(firmware="0.5.8-dual",entering=measurements[0],initial_65s_pass=trial["passed"],
             initial_timeout=trial["timeout"],boards={})
    with closing(sqlite3.connect((runtime/"live.sqlite3").resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        for board,run in trial["directions"].items():
            event=run["queued_observations"][0]; eid=event["event_id"]; ended=run["end"]["at"]
            events=[]
            for row in log:
                if row["board"]!=board or row["kind"]!="rx" or row["utc"]<ended: continue
                try: obj=json.loads(row["data"])
                except ValueError: continue
                if obj.get("type")=="discovery_start": break
                events.append(dict(at=row["utc"],**obj))
            cleared=next(i for i in events if i.get("type")=="queue_removed_after_application_ack"
                         and i["event_id"]==eid and i["queued"]==0)
            prior=events[:events.index(cleared)]
            puback=next(i for i in reversed(prior) if i.get("type")=="mqtt_puback")
            assert puback["detail"]=="received" and any(i.get("type")=="application_ack" and i["detail"]=="matched" for i in prior)
            saved=[dict(i) for i in db.execute("SELECT event_id,boot,transport,time_quality,clock_uncertainty_s FROM clock_resolved WHERE event_id=?",(eid,))]
            assert len(saved)==1 and saved[0]["transport"]=="mqtt" and saved[0]["time_quality"]=="anchored"
            anchors=[dict(i) for i in db.execute("SELECT raw_uptime_ms,utc_low,utc_high,source FROM clock_samples WHERE boot=? ORDER BY utc_high",(event["boot_id"],))]
            assert anchors and all(i["source"]=="mqtt_connack" for i in anchors)
            probes=[i for i in events if i.get("type")=="clock_probe"]
            elapsed=(datetime.fromisoformat(cleared["at"])-datetime.fromisoformat(ended)).total_seconds()
            out["boards"][board]=dict(event=eid,samples=event["samples"],duration_s=event["duration_s"],
                                      seconds_from_own_end_to_clear=elapsed,raw_anchors=anchors,puback=puback,
                                      cleared=cleared,database=saved,clock_probes=probes)
    latest=max(x["cleared"]["at"] for x in out["boards"].values())
    out["external_sync_through_delivery"]=[dict(at=r["utc"],board=r["board"]) for r in log
            if r["kind"]=="tx" and r["data"]=="sync" and r["utc"]<=latest]
    out["any_external_sync_in_capture"]=any(r["kind"]=="tx" and r["data"]=="sync" for r in log)
    return out

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ("session","driver","runtime","evidence"):parser.add_argument(arg,type=Path)
    parser.add_argument("--require-no-sync",action="store_true")
    a=parser.parse_args(); result=audit(a.session,a.driver,a.runtime)
    if a.require_no_sync:assert not result["any_external_sync_in_capture"]
    a.evidence.write_text(json.dumps(result,indent=2)+"\n",encoding="utf8")
    print(result["initial_65s_pass"],result["external_sync_through_delivery"],{b:i["seconds_from_own_end_to_clear"] for b,i in result["boards"].items()})
