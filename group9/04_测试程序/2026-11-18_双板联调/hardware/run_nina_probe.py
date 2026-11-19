"""Run a timed scanner comparison on A against B's unchanged integration image."""
import json,subprocess,sys,time
from datetime import datetime
from pathlib import Path
import original_backup as hw

variant=sys.argv[1]; identity=sys.argv[2]
folder=hw.BASE/'logs'/('nina_probe_'+variant+'_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
ops=hw.BASE.parent/'ops/board_session.py'
def run(*args):
    subprocess.run([sys.executable,'-B',str(ops),args[0],str(folder),*args[1:]],check=True,timeout=6)
def read():
    f=folder/'serial.jsonl'
    if not f.exists():return []
    rows=[]
    for line in f.read_text(encoding='utf8').splitlines():
        try:r=json.loads(line)
        except ValueError:continue
        if r['kind']=='rx':
            try:r['decoded']=json.loads(r['data'])
            except ValueError:pass
        rows.append(r)
    return rows
def displayed(rows,s,typ):
    return [r for r in rows if r['board']==s and r.get('decoded',{}).get('type')==typ]
def ready():
    end=time.monotonic()+12
    while time.monotonic()<end:
        rows=read(); aa=displayed(rows,'A','status');bb=displayed(rows,'B','status')
        if aa and bb:return aa[-1]['decoded'],bb[-1]['decoded']
        time.sleep(.1)
    raise RuntimeError('Both status reports required')
p=subprocess.Popen([sys.executable,'-B',str(ops),'listen',str(folder)])
try:
    aa,bb=ready()
    assert aa['firmware']==identity and aa['role']=='idle'
    assert bb['fw']=='0.5.3-dual' and not bb['paused'] and not bb['discovery'] and bb['queued']==0
    run('send','B','discover'); run('send','A','scan')
    begin=hw.now()
    end=time.monotonic()+83;last_query=0;result=None
    while time.monotonic()<end:
        if time.monotonic()>last_query+4:
            run('send','both','status');last_query=time.monotonic()
        r=read()
        complete=displayed(r,'A','probe_complete')
        losses=[v for v in r if v['board']=='A' and v['kind']=='lost']
        sb=displayed(r,'B','status'); stA=displayed(r,'A','status')
        if complete: result='probe_complete'
        if losses and stA and stA[-1]['utc']>losses[-1]['utc'] and stA[-1]['decoded']['role']=='idle':
            result='watchdog_reboot' if stA[-1]['decoded']['last_watchdog_stage']!='none' else 'reconnected'
        b_idle=sb and sb[-1]['utc']>begin and not sb[-1]['decoded']['discovery']
        b_active=any(x['decoded']['discovery'] for x in sb)
        if result and b_idle and b_active:break
        time.sleep(.28)
    r=read()
    q=displayed(r,'A','probe_progress')+displayed(r,'A','probe_complete')+displayed(r,'A','status')
    b=displayed(r,'B','status')
    best=max((x['decoded'] for x in q),key=lambda d:(d['samples'],d['elapsed_ms']))
    max_reports=max((x['decoded']['reports'] for x in q),default=0)
    a_final=displayed(r,'A','status')[-1]['decoded']
    b_final=b[-1]['decoded']
    actual_loss=[(x['board'],x['utc'],x['kind']) for x in r if x['kind'] in ('lost','reopen')]
    b_adv=[x['utc'] for x in r if x['board']=='B' and x.get('decoded',{}).get('detail')=='ble_advertise_ready']
    res=dict(at_utc=hw.now(),image_identity=identity,begin=begin,outcome=result or 'timeout',A_before=aa,B_before=bb,A_best=best,A_max_reports=max_reports,A_last_status=a_final,B_last_status=b_final,B_advertise_ready_utc=b_adv,usb_events=actual_loss,probe_complete=[x['decoded'] for x in displayed(r,'A','probe_complete')])
    (folder/'result.json').write_text(json.dumps(res,indent=2),encoding='utf8')
    print(folder.name,res['outcome'],a_final['last_watchdog_stage'],'samples',best['samples'],'span',best['span_ms'],'reports',max_reports,'B advert slots',len(b_adv),'USB',actual_loss,flush=True)
finally:
    if p.poll() is None and (folder/'commands').exists():run('stop');p.wait(timeout=7)
