from datetime import datetime,timezone
from pathlib import Path
import json,sqlite3,sys
root=Path(__file__).resolve().parent
serial=root/'logs/dual-live-01/serial.jsonl'
start=json.loads((root/'logs/outage_start.json').read_text(encoding='utf-8-sig'))['utc']
dbpath=root.parent/'private/live/live.sqlite3'
def t(v): return datetime.fromisoformat(v.replace('Z','+00:00'))
def collect():
    rows=[]
    for line in serial.read_text(encoding='utf8').splitlines():
        try: r=json.loads(line)
        except ValueError: continue
        if t(r['utc']) < t(start) or r['kind']!='rx':continue
        try: d=json.loads(r['data'])
        except ValueError: continue
        rows.append((r['utc'],r['board'],d))
    targets={}; stages={}; latest={}; statuses={}; failures={}; removed={}
    for at,board,d in rows:
        if d.get('type')=='wire' and d.get('topic','').endswith('/ir'):
            eid=d['payload']['event_id']
            if d.get('phase')=='queued':
                targets[eid]=dict(utc=at,board=board,packet=d['payload'])
            if eid in targets: stages.setdefault(eid,[]).append(dict(utc=at,phase=d.get('phase'),source=d.get('source')))
        if d.get('type')=='status':
            v={k:d.get(k) for k in ('boot_id','queued','uptime_ms','dropped','ir_paired','peer_code16')}
            latest[board]={'utc':at,**v}
            if v['queued']: statuses.setdefault(board,[]).append(latest[board])
        if d.get('type')=='radio' and d.get('detail')=='wifi_failed':
            failures.setdefault(board,[]).append(at)
        if d.get('type')=='queue_removed_after_application_ack':
            removed[d.get('event_id')]=dict(utc=at,board=board,record=d)
    ids=list(targets)
    with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        db=[dict(r) for r in c.execute('SELECT event_id,device,boot,peer_device,received_at,transport,matched FROM ir_events WHERE event_id IN ('+','.join('?' for _ in ids)+')',ids)]
    current=datetime.now(timezone.utc)
    ages=[(current-t(e['utc'])).total_seconds() for e in targets.values()]
    return dict(captured_utc=current.isoformat(),outage_start=start,latest=latest,events=targets,wire=stages,wifi_failed_utc=failures,queue_observations={k:{'first':v[0],'last':v[-1],'count':len(v)} for k,v in statuses.items()},database_rows=db,removed=removed,minimum_event_age_s=min(ages) if ages else None)
if __name__=='__main__':
    out=collect()
    if len(sys.argv)>1: (root/'logs'/sys.argv[1]).write_text(json.dumps(out,indent=2),encoding='utf8')
    print(json.dumps(dict(age=out['minimum_event_age_s'],events=list(out['events']),queued={k:v['queued'] for k,v in out['latest'].items()},paired={k:v['ir_paired'] for k,v in out['latest'].items()},failures={k:len(v) for k,v in out['wifi_failed_utc'].items()},dbrows=len(out['database_rows']),removed=list(out['removed']))))
