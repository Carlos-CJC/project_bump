"""Compare previous and corrected private timelines on a read-only snapshot."""

from collections import Counter
from contextlib import closing
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

from backfill_clock import copy_readonly, reconcile
from bump.v1_store import V1Store


BEFORE = '''
import json, sqlite3, sys
from pathlib import Path
from bump.v1_store import V1Store
conn=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True)
conn.row_factory=sqlite3.Row
conn.execute("PRAGMA query_only=ON")
result={group[0]["event_id"]: group[0]["match_state"]
        for device, in conn.execute("SELECT DISTINCT device FROM observations")
        for group in V1Store._groups(conn,device)}
print(json.dumps(result))
conn.close()
'''
DATA_TABLES = ("observations", "ir_events", "contact_requests", "live_status", "users", "browser_sessions",
               "bindings", "sightings", "events")


def digest(db):
    return {t: sha256(repr(db.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()).encode()).hexdigest()
            for t in DATA_TABLES}


def decode_line(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def recorded_pairs(project):
    # Used exclusively by the audit after migration, never by the matcher or importer.
    known = {}
    for path in (project / "firmware/verification").glob("live-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record=decode_line(line)
            if record.get("record")!="round" or not record.get("both_observed"):
                continue
            sides=record["directions"]
            choices={b:[e["event_id"] for e in d["queued_observations"]
                         if e["peer_short_id"]==d["expected_peer_short_id"]]
                     for b,d in sides.items()}
            if set(choices)=={"A","B"} and all(len(a)==1 for a in choices.values()):
                a,b=choices["A"][0],choices["B"][0]
                known[a]=b
                known[b]=a
    return known


def episode_row(db, event):
    row=db.execute("""SELECT r.event_id,r.boot,r.seen_at,r.discovery_start_at,
                             o.seen_at AS raw_seen,o.discovery_start_at AS raw_start,
                             r.clock_uncertainty_s,r.time_quality
                      FROM clock_resolved r JOIN observations o USING(event_id)
                      WHERE r.event_id=?""",(event,)).fetchone()
    return dict(row)


def review_snapshot(project, actual):
    private=project/"private"
    with TemporaryDirectory(prefix="clock-verify-",dir=private) as scratch:
        database=Path(scratch)/"readback.sqlite3"
        copy_readonly(actual,database)
        base_cfg=project/"server-mqtt-stages-preview"
        before=json.loads(subprocess.check_output([sys.executable,"-X","utf8","-c",BEFORE,str(database)],
                                                   cwd=base_cfg,encoding="utf-8"))
        with closing(sqlite3.connect(database)) as raw:
            original=digest(raw)
        raw_logs=project/"hardware/logs"
        paths=[raw_logs/f"dual-live-{i:02d}"/"serial.jsonl" for i in range(1,9)]
        paths.append(raw_logs/"final-ir-offline"/"serial.jsonl")
        repairs=reconcile(database,paths)
        store=V1Store(database,{})
        with closing(sqlite3.connect(database)) as raw:
            still=digest(raw)
            assert original==still, "ordinary data table changed"
        with store._transaction() as db:
            reviewed={g[0]["event_id"]:(g[0]["match_state"],g[0]["counterpart_event_id"])
                      for device, in db.execute("SELECT DISTINCT device FROM observations")
                      for g in store._groups(db,device)}
            numbered=json.loads((project/"hardware/logs/ble_056_delivery_verified.json").read_text(encoding="utf-8"))
            last={k:v["events"][0]["event_id"] for k,v in numbered["rounds"][-1]["directions"].items()}
            case={side:{"before":before[event],"after":reviewed[event][0],
                        "linked_to_other_selected_event":reviewed[event][1]==last["B" if side=="A" else "A"],
                        "clock":episode_row(db,event)}
                  for side,event in last.items()}
            known=recorded_pairs(project)
            newly_confirmed={e:p for e,(s,p) in reviewed.items() if s=="confirmed" and before.get(e)!="confirmed"}
            conflicting=[e for e,p in newly_confirmed.items() if e in known and known[e]!=p]
            unannotated=[e for e in newly_confirmed if e not in known]
            return {"backfill":repairs,"source_log_count":len(paths),"ordinary_tables_unchanged":True,
                    "prior_states":dict(Counter(before.values())),
                    "new_states":dict(Counter(s for s,p in reviewed.values())),
                    "round_20":case,
                    "newly_confirmed_directions":len(newly_confirmed),
                    "new_confirmations_with_independent_recorded_pair":len(newly_confirmed)-len(unannotated)-len(conflicting),
                    "conflicting_new_confirmations":conflicting,
                    "newly_confirmed_without_series_annotation":unannotated,
                    "steady_confirmed_directions":sum(s=="confirmed" and before.get(e)=="confirmed"
                                                      for e,(s,_) in reviewed.items())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",type=Path,required=True)
    parser.add_argument("--source-db",type=Path,required=True)
    args=parser.parse_args()
    outcome=review_snapshot(args.project.resolve(),args.source_db.resolve())
    print(json.dumps(outcome,ensure_ascii=False,indent=2))
    if (any(x["after"]!="confirmed" or not x["linked_to_other_selected_event"]
            for x in outcome["round_20"].values()) or outcome["conflicting_new_confirmations"]):
        raise SystemExit(1)


if __name__=="__main__":
    main()

